#!/usr/bin/env python3
"""
docker-updater — poll registries for image digest changes, apply updates with approval.
"""

import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import threading

import apprise
import docker
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

DATA_DIR = "/app/data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
CHECK_TIME            = os.environ.get("CHECK_TIME", "03:00")
TIMEZONE              = os.environ.get("TIMEZONE", "Australia/Melbourne")
NOTIFY_URL            = os.environ.get("NOTIFY_URL", "").strip()
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

_state_lock    = threading.Lock()
_check_lock    = threading.Lock()
_check_running = False
_update_logs: dict[str, list[str]] = {}
_update_running: set[str] = set()


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"available": {}, "deferred": {}, "history": [], "last_check": None,
            "notify_url": None}


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Notifications ─────────────────────────────────────────────────────────────

def get_effective_notify_url() -> str:
    """Return the effective notify URL.

    If NOTIFY_URL env var is set, use it.
    Otherwise auto-generate a unique private ntfy.sh topic on first run
    and persist it in state.json so it survives container restarts.
    """
    if NOTIFY_URL:
        return NOTIFY_URL
    with _state_lock:
        state = load_state()
        if not state.get("notify_url"):
            topic = f"du-{secrets.token_hex(4)}"
            state["notify_url"] = f"ntfy://ntfy.sh/{topic}"
            save_state(state)
            print(f"[notify] Auto-generated private topic: ntfy.sh/{topic}")
        return state["notify_url"]


def get_notify_info() -> dict | None:
    """Return subscribe info for the UI, or None if notifications are disabled."""
    url = get_effective_notify_url()
    if not url or not url.startswith("ntfy://"):
        return None
    # ntfy://ntfy.sh/topic  →  ntfy.sh/topic
    subscribe = url.replace("ntfy://", "")
    return {
        "subscribe": subscribe,
        "auto": not bool(NOTIFY_URL),  # True = auto-generated, show setup banner
    }


def send_notification(title: str, body: str) -> None:
    url = get_effective_notify_url()
    if not url:
        return
    try:
        a = apprise.Apprise()
        a.add(url)
        a.notify(title=title, body=body)
        print(f"[notify] Sent: {title}")
    except Exception as e:
        print(f"[notify] Failed: {e}")


# ── Registry helpers ──────────────────────────────────────────────────────────

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
])


def parse_image(image_name: str) -> tuple[str, str, str]:
    tag = "latest"
    name = image_name
    last_slash = image_name.rfind("/")
    last_colon = image_name.rfind(":")
    if last_colon > last_slash:
        name, tag = image_name[:last_colon], image_name[last_colon + 1:]
    parts = name.split("/")
    first = parts[0]
    if "." in first or ":" in first or first == "localhost":
        registry = first
        repo = "/".join(parts[1:])
    else:
        registry = "registry-1.docker.io"
        repo = name if "/" in name else f"library/{name}"
    return registry, repo, tag


def _token_from_challenge(www_auth: str) -> str | None:
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)="([^"]*)"', www_auth):
        params[m.group(1)] = m.group(2)
    realm = params.pop("realm", None)
    if not realm:
        return None
    try:
        r = requests.get(realm, params=params, timeout=10)
        data = r.json()
        return data.get("token") or data.get("access_token")
    except Exception:
        return None


def get_remote_digest(image_name: str) -> str | None:
    try:
        registry, repo, tag = parse_image(image_name)
        url = f"https://{registry}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": MANIFEST_ACCEPT}
        r = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
        if r.status_code == 401:
            token = _token_from_challenge(r.headers.get("WWW-Authenticate", ""))
            if token:
                headers["Authorization"] = f"Bearer {token}"
                r = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
        if r.status_code == 405:
            r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            return r.headers.get("Docker-Content-Digest")
    except Exception as e:
        print(f"[checker] registry error for {image_name}: {e}")
    return None


def get_local_digest(container) -> str | None:
    try:
        digests = container.image.attrs.get("RepoDigests", [])
        if digests:
            return digests[0].split("@", 1)[-1]
    except Exception:
        pass
    return None


def _has_changelog(container) -> bool:
    try:
        labels = (container.image.attrs.get("Config") or {}).get("Labels") or {}
        return _github_repo_from_labels(labels) is not None
    except Exception:
        return False


def is_locally_built(container) -> bool:
    try:
        return not container.image.attrs.get("RepoDigests")
    except Exception:
        return True


# ── Update checking ───────────────────────────────────────────────────────────

def check_for_updates(notify: bool = False) -> None:
    global _check_running
    if not _check_lock.acquire(blocking=False):
        return
    _check_running = True
    print(f"[checker] Starting digest check (notify={notify})...")
    try:
        client = docker.from_env()
        available: dict = {}
        for container in client.containers.list():
            name = container.name
            image_name = container.attrs["Config"]["Image"]
            if is_locally_built(container):
                continue
            local_digest = get_local_digest(container)
            remote_digest = get_remote_digest(image_name)
            has_update = bool(local_digest and remote_digest and local_digest != remote_digest)
            flag = "UPDATE" if has_update else ("no digest" if not remote_digest else "ok")
            print(f"[checker] {name}: [{flag}]")
            if remote_digest:
                available[name] = {
                    "image": image_name,
                    "local_digest": local_digest,
                    "remote_digest": remote_digest,
                    "has_update": has_update,
                    "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
                }
        with _state_lock:
            state = load_state()
            state["available"] = available
            state["last_check"] = datetime.datetime.utcnow().isoformat() + "Z"
            save_state(state)

        updates = [n for n, v in available.items() if v["has_update"]]
        count = len(updates)
        print(f"[checker] Done — {count} update(s) available.")

        if notify and count > 0:
            send_notification(
                title=f"Docker: {count} update{'s' if count > 1 else ''} available",
                body=", ".join(sorted(updates)),
            )

    except Exception as e:
        print(f"[checker] Fatal error: {e}")
    finally:
        _check_running = False
        _check_lock.release()


# ── Container recreation via Docker SDK (Watchtower pattern) ──────────────────

def apply_update(container_name: str) -> None:
    _update_logs[container_name] = []
    _update_running.add(container_name)
    log = _update_logs[container_name]

    def emit(line: str) -> None:
        print(f"[update:{container_name}] {line}")
        log.append(line)

    try:
        client = docker.from_env()
        container = client.containers.get(container_name)
        old_id    = container.id
        attrs     = container.attrs
        cfg       = attrs["Config"]
        hcfg      = attrs["HostConfig"]
        nets      = attrs["NetworkSettings"]["Networks"]
        image_name = cfg["Image"]

        emit(f"Container : {container_name}")
        emit(f"Image     : {image_name}")
        emit("")
        emit("▶ Pulling latest image...")

        for chunk in client.api.pull(image_name, stream=True, decode=True):
            status = chunk.get("status", "")
            detail = chunk.get("progress", "") or chunk.get("error", "")
            if status in ("Pulling from", "Status: Image is up to date for",
                          "Status: Downloaded newer image for") or "Pull complete" in status:
                emit(f"  {status} {detail}".rstrip())
            if "error" in chunk:
                emit(f"  ERROR: {chunk['error']}")

        emit("\n▶ Stopping old container...")
        container.stop(timeout=30)
        emit("▶ Removing old container...")
        container.remove()
        emit("▶ Recreating container...")

        network_mode = hcfg.get("NetworkMode", "bridge")

        hc = client.api.create_host_config(
            binds=hcfg.get("Binds") or [],
            port_bindings=hcfg.get("PortBindings") or {},
            network_mode=network_mode,
            restart_policy=hcfg.get("RestartPolicy"),
            cap_add=hcfg.get("CapAdd"),
            cap_drop=hcfg.get("CapDrop"),
            privileged=hcfg.get("Privileged", False),
            security_opt=hcfg.get("SecurityOpt"),
            devices=hcfg.get("Devices"),
            extra_hosts=hcfg.get("ExtraHosts"),
            dns=hcfg.get("Dns"),
            dns_search=hcfg.get("DnsSearch"),
            volumes_from=hcfg.get("VolumesFrom"),
            pid_mode=hcfg.get("PidMode") or "",
            ipc_mode=hcfg.get("IpcMode") or "",
            tmpfs=hcfg.get("Tmpfs"),
        )

        short_id = old_id[:12]
        full_nets = {
            net_name: {
                "aliases": [a for a in (net_data.get("Aliases") or []) if a != short_id]
            }
            for net_name, net_data in nets.items()
        }

        simple_net_name = next(iter(full_nets), None)
        if simple_net_name:
            simple_nc = client.api.create_networking_config({
                simple_net_name: client.api.create_endpoint_config(
                    aliases=full_nets[simple_net_name]["aliases"] or None
                )
            })
        else:
            simple_nc = None

        new_c = client.api.create_container(
            image=image_name, name=container_name,
            hostname=cfg.get("Hostname", ""), user=cfg.get("User", ""),
            detach=True, environment=cfg.get("Env"), command=cfg.get("Cmd"),
            entrypoint=cfg.get("Entrypoint"), labels=cfg.get("Labels"),
            volumes=list((cfg.get("Volumes") or {}).keys()) or None,
            working_dir=cfg.get("WorkingDir", ""),
            ports=list((cfg.get("ExposedPorts") or {}).keys()) or None,
            host_config=hc, networking_config=simple_nc,
        )

        if network_mode != "host" and full_nets:
            if simple_net_name:
                try:
                    client.api.disconnect_container_from_network(
                        new_c["Id"], simple_net_name, force=True
                    )
                except Exception:
                    pass
            for net_name, net_info in full_nets.items():
                aliases = net_info["aliases"] or None
                client.api.connect_container_to_network(
                    new_c["Id"], net_name, aliases=aliases
                )

        client.api.start(new_c["Id"])
        emit(f"\nSUCCESS: {container_name} updated and running.")

        with _state_lock:
            state = load_state()
            state["available"].pop(container_name, None)
            state["history"].insert(0, {
                "container": container_name, "image": image_name,
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "status": "success",
            })
            state["history"] = state["history"][:50]
            save_state(state)

    except Exception as e:
        emit(f"\nERROR: {e}")
        with _state_lock:
            state = load_state()
            state["history"].insert(0, {
                "container": container_name, "image": "?",
                "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
                "status": f"error: {e}",
            })
            state["history"] = state["history"][:50]
            save_state(state)
    finally:
        _update_running.discard(container_name)


# ── Changelog ─────────────────────────────────────────────────────────────────

def _github_repo_from_labels(labels: dict) -> str | None:
    for key in ("org.opencontainers.image.source", "org.opencontainers.image.url"):
        url = labels.get(key, "")
        m = re.match(r"https?://github\.com/([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
        if m:
            return m.group(1)
    return None


def fetch_changelog(container_name: str) -> dict:
    client = docker.from_env()
    container = client.containers.get(container_name)
    labels = (container.image.attrs.get("Config") or {}).get("Labels") or {}
    image_name = container.attrs["Config"]["Image"]

    repo = _github_repo_from_labels(labels)
    source_url = labels.get("org.opencontainers.image.source", "")

    if not repo:
        return {
            "repo": None,
            "source_url": source_url or None,
            "releases": [],
            "error": "No GitHub source URL found in image labels.",
        }

    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/releases",
            params={"per_page": 5},
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10,
        )
        if r.status_code == 404:
            return {"repo": repo, "source_url": source_url, "releases": [],
                    "error": "No releases found on GitHub for this repo."}
        if r.status_code != 200:
            return {"repo": repo, "source_url": source_url, "releases": [],
                    "error": f"GitHub API error {r.status_code}."}

        releases = []
        for rel in r.json()[:5]:
            releases.append({
                "name": rel.get("name") or rel.get("tag_name", ""),
                "tag":  rel.get("tag_name", ""),
                "date": rel.get("published_at", ""),
                "body": rel.get("body") or "",
                "url":  rel.get("html_url", ""),
                "prerelease": rel.get("prerelease", False),
            })
        return {"repo": repo, "source_url": source_url, "releases": releases}

    except Exception as e:
        return {"repo": repo, "source_url": source_url, "releases": [], "error": str(e)}


# ── GitHub webhook ────────────────────────────────────────────────────────────

@app.route("/webhook/github", methods=["POST"])
def webhook_github():
    if GITHUB_WEBHOOK_SECRET:
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            GITHUB_WEBHOOK_SECRET.encode(), request.data, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            print("[github] Rejected webhook — bad signature")
            return jsonify({"error": "Invalid signature"}), 401

    event   = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(silent=True) or {}
    repo    = payload.get("repository", {}).get("full_name", "unknown")

    title = body = None

    if event == "issues":
        action = payload.get("action", "")
        issue  = payload.get("issue", {})
        if action == "opened":
            sender = payload.get("sender", {}).get("login", "someone")
            title  = f"🐛 New issue — {repo}"
            body   = f"#{issue.get('number')}: {issue.get('title')}\nOpened by {sender}\n{issue.get('html_url', '')}"
        elif action == "closed":
            title = f"✅ Issue closed — {repo}"
            body  = f"#{issue.get('number')}: {issue.get('title')}"

    elif event == "pull_request":
        action = payload.get("action", "")
        pr     = payload.get("pull_request", {})
        if action == "opened":
            sender = payload.get("sender", {}).get("login", "someone")
            title  = f"🔀 New PR — {repo}"
            body   = f"#{pr.get('number')}: {pr.get('title')}\nOpened by {sender}\n{pr.get('html_url', '')}"
        elif action == "closed" and pr.get("merged"):
            title = f"✅ PR merged — {repo}"
            body  = f"#{pr.get('number')}: {pr.get('title')}"

    elif event == "watch":
        if payload.get("action") == "started":
            sender = payload.get("sender", {}).get("login", "someone")
            stars  = payload.get("repository", {}).get("stargazers_count", "?")
            title  = f"⭐ New star — {repo}"
            body   = f"{sender} starred the repo ({stars} total)"

    elif event == "push":
        branch = payload.get("ref", "").replace("refs/heads/", "")
        if branch in ("main", "master"):
            commits = payload.get("commits", [])
            pusher  = payload.get("pusher", {}).get("name", "someone")
            title   = f"📦 Push to {branch} — {repo}"
            body    = f"{pusher} pushed {len(commits)} commit{'s' if len(commits) != 1 else ''}"
            if commits:
                body += f"\n↳ {commits[-1].get('message', '').splitlines()[0]}"

    elif event == "release":
        if payload.get("action") == "published":
            rel   = payload.get("release", {})
            title = f"🚀 New release — {repo}"
            body  = f"{rel.get('tag_name', '')}: {rel.get('name', '')}\n{rel.get('html_url', '')}"

    elif event == "issue_comment":
        if payload.get("action") == "created":
            issue   = payload.get("issue", {})
            comment = payload.get("comment", {})
            sender  = payload.get("sender", {}).get("login", "someone")
            title   = f"💬 Comment — {repo}"
            body    = f"#{issue.get('number')}: {issue.get('title')}\n{sender}: {comment.get('body', '')[:120]}"

    if title and body:
        print(f"[github] {event} → {title}")
        send_notification(title, body)

    return jsonify({"ok": True})


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    with _state_lock:
        state = load_state()

    today = datetime.date.today().isoformat()
    expired = [n for n, d in state.get("deferred", {}).items()
               if d.get("until", "") <= today]
    if expired:
        with _state_lock:
            s = load_state()
            for n in expired:
                s["deferred"].pop(n, None)
            save_state(s)
        state = load_state()

    containers = []
    try:
        client = docker.from_env()
        for container in client.containers.list():
            name = container.name
            image_name = container.attrs["Config"]["Image"]
            if is_locally_built(container):
                continue
            info = state["available"].get(name, {})
            defer = state["deferred"].get(name)
            is_deferred = bool(defer and defer.get("until", "") > today)

            if info:
                if info.get("has_update") and not is_deferred:
                    status = "update"
                elif is_deferred:
                    status = "deferred"
                else:
                    status = "ok"
            else:
                status = "unknown"

            containers.append({
                "name": name, "image": image_name, "status": status,
                "defer_until": defer.get("until") if is_deferred else None,
                "checked_at": info.get("checked_at"),
                "updating": name in _update_running,
                "has_logs": name in _update_logs,
                "has_changelog": _has_changelog(container),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    ORDER = {"update": 0, "deferred": 1, "unknown": 2, "ok": 3}
    containers.sort(key=lambda c: (ORDER.get(c["status"], 9), c["name"]))

    return jsonify({
        "containers": containers,
        "last_check": state.get("last_check"),
        "check_running": _check_running,
        "history": state.get("history", [])[:20],
        "next_check": _next_check_time(),
        "notify": get_notify_info(),
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    threading.Thread(target=check_for_updates, args=(False,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update/<name>", methods=["POST"])
def api_update(name):
    if name in _update_running:
        return jsonify({"error": "Already updating"}), 409
    threading.Thread(target=apply_update, args=(name,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/defer/<name>", methods=["POST"])
def api_defer(name):
    data = request.get_json() or {}
    days = int(data.get("days", 7))
    until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    with _state_lock:
        state = load_state()
        state["deferred"][name] = {"until": until}
        save_state(state)
    return jsonify({"ok": True, "until": until})


@app.route("/api/undefer/<name>", methods=["POST"])
def api_undefer(name):
    with _state_lock:
        state = load_state()
        state["deferred"].pop(name, None)
        save_state(state)
    return jsonify({"ok": True})


@app.route("/api/logs/<name>")
def api_logs(name):
    return jsonify({
        "logs": _update_logs.get(name, []),
        "running": name in _update_running,
    })


@app.route("/api/changelog/<name>")
def api_changelog(name):
    try:
        return jsonify(fetch_changelog(name))
    except Exception as e:
        return jsonify({"error": str(e), "releases": []}), 500


# ── Scheduler helpers ─────────────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None


def _next_check_time() -> str | None:
    if _scheduler is None:
        return None
    try:
        job = _scheduler.get_jobs()[0]
        nf = job.next_run_time
        return nf.isoformat() if nf else None
    except Exception:
        return None


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    check_hour, check_minute = map(int, CHECK_TIME.split(":"))

    _scheduler = BackgroundScheduler(daemon=True, timezone=TIMEZONE)
    _scheduler.add_job(
        check_for_updates, "cron",
        hour=check_hour, minute=check_minute,
        timezone=TIMEZONE,
        kwargs={"notify": True},
    )
    _scheduler.start()
    print(f"[scheduler] Daily check scheduled at {CHECK_TIME} {TIMEZONE}")

    # Ensure notify URL is initialised and log it
    notify_info = get_notify_info()
    if notify_info:
        prefix = "Auto-generated" if notify_info["auto"] else "Configured"
        print(f"[notify] {prefix} topic: {notify_info['subscribe']}")

    if GITHUB_WEBHOOK_SECRET:
        print(f"[github] Webhook endpoint active at /webhook/github")

    threading.Thread(target=check_for_updates, args=(False,), daemon=True).start()
    app.run(host="0.0.0.0", port=9090, debug=False, threaded=True)
