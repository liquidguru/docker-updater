#!/usr/bin/env python3
"""
docker-updater — poll registries for image digest changes, apply updates with approval.
Supports multiple Docker hosts via SSH or TCP.
"""

import datetime
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="paramiko")
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

import apprise
import docker
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

DATA_DIR             = "/app/data"
STATE_FILE           = os.path.join(DATA_DIR, "state.json")
HOSTS_FILE           = os.path.join(DATA_DIR, "hosts.json")
HOSTS_STATE_DIR      = os.path.join(DATA_DIR, "hosts")
CHECK_TIME            = os.environ.get("CHECK_TIME", "03:00")
TIMEZONE              = os.environ.get("TIMEZONE", "Australia/Melbourne")
NOTIFY_URL            = os.environ.get("NOTIFY_URL", "").strip()
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

_state_lock    = threading.Lock()
_check_lock    = threading.Lock()
_logs_lock     = threading.Lock()   # guards _update_logs and _update_running
_check_running = False
_update_logs: dict[str, list[str]] = {}
_update_running: set[str] = set()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _container_key(name: str, host_id: str = "local") -> str:
    """Unique key for _update_logs / _update_running across hosts."""
    return name if host_id == "local" else f"{host_id}:{name}"


# ── Local state ───────────────────────────────────────────────────────────────

def load_state() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"available": {}, "deferred": {}, "history": [], "last_check": None,
            "notify_url": None, "rollbacks": {}, "backup_enabled": False, "backup_hours": 24}


def save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Host config ───────────────────────────────────────────────────────────────

def load_hosts() -> list:
    """Load remote host configs. The local host is always implicit."""
    if os.path.exists(HOSTS_FILE):
        try:
            with open(HOSTS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_hosts(hosts: list) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HOSTS_FILE, "w") as f:
        json.dump(hosts, f, indent=2)


def load_host_state(host_id: str) -> dict:
    os.makedirs(HOSTS_STATE_DIR, exist_ok=True)
    path = os.path.join(HOSTS_STATE_DIR, f"{host_id}.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {"available": {}, "deferred": {}, "history": [], "last_check": None,
            "status": "unknown"}


def save_host_state(host_id: str, state: dict) -> None:
    os.makedirs(HOSTS_STATE_DIR, exist_ok=True)
    path = os.path.join(HOSTS_STATE_DIR, f"{host_id}.json")
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_docker_client(url: str | None = None):
    """Return a DockerClient for a local socket, SSH, or TCP URL."""
    if not url or url.startswith("unix://") or url.startswith("npipe://"):
        return docker.from_env()
    return docker.DockerClient(base_url=url)


# ── Notifications ─────────────────────────────────────────────────────────────

def get_effective_notify_url() -> str:
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
    url = get_effective_notify_url()
    if not url or not url.startswith("ntfy://"):
        return None
    subscribe = url.replace("ntfy://", "")
    return {"subscribe": subscribe, "auto": not bool(NOTIFY_URL)}


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

def _scan_host(client, host_id: str) -> dict:
    """Scan one Docker host for image updates. Returns available dict."""
    available = {}
    for container in client.containers.list():
        name = container.name
        image_name = container.attrs["Config"]["Image"]
        if is_locally_built(container):
            continue
        local_digest = get_local_digest(container)
        remote_digest = get_remote_digest(image_name)
        has_update = bool(local_digest and remote_digest and local_digest != remote_digest)
        flag = "UPDATE" if has_update else ("no digest" if not remote_digest else "ok")
        print(f"[checker:{host_id}] {name}: [{flag}]")
        if remote_digest:
            available[name] = {
                "image": image_name,
                "local_digest": local_digest,
                "remote_digest": remote_digest,
                "has_update": has_update,
                "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
            }
    return available


def check_for_updates(notify: bool = False) -> None:
    global _check_running
    if not _check_lock.acquire(blocking=False):
        return
    _check_running = True
    print(f"[checker] Starting digest check (notify={notify})...")

    # Collect all updates for notification: list of (host_label, container_name)
    all_updates: list[tuple[str, str]] = []

    try:
        # ── Local host (existing behaviour, completely unchanged) ─────────────
        try:
            local_client = docker.from_env()
            local_available = _scan_host(local_client, "local")
            with _state_lock:
                state = load_state()
                state["available"] = local_available
                state["last_check"] = datetime.datetime.utcnow().isoformat() + "Z"
                save_state(state)
            updates = [n for n, v in local_available.items() if v["has_update"]]
            all_updates.extend([("Local", n) for n in updates])
            print(f"[checker:local] Done — {len(updates)} update(s).")
        except Exception as e:
            print(f"[checker:local] Fatal: {e}")

        # ── Remote hosts ───────────────────────────────────────────────────────
        for host in load_hosts():
            host_id   = host["id"]
            host_name = host["name"]
            try:
                client = get_docker_client(host.get("url"))
                client.ping()
                available = _scan_host(client, host_id)
                hs = load_host_state(host_id)
                hs["available"]  = available
                hs["last_check"] = datetime.datetime.utcnow().isoformat() + "Z"
                hs["status"]     = "online"
                hs.pop("last_error", None)
                save_host_state(host_id, hs)
                updates = [n for n, v in available.items() if v["has_update"]]
                all_updates.extend([(host_name, n) for n in updates])
                print(f"[checker:{host_id}] Done — {len(updates)} update(s).")
            except Exception as e:
                print(f"[checker:{host_id}] Offline: {e}")
                hs = load_host_state(host_id)
                hs["status"]     = "offline"
                hs["last_error"] = str(e)
                save_host_state(host_id, hs)

        count = len(all_updates)
        print(f"[checker] Complete — {count} total update(s).")

        if notify and count > 0:
            # Group by host label for a clean notification body
            by_host: dict[str, list[str]] = {}
            for host_label, cname in all_updates:
                by_host.setdefault(host_label, []).append(cname)

            if len(by_host) == 1:
                body = ", ".join(sorted(next(iter(by_host.values()))))
            else:
                body = "\n".join(
                    f"{hl}: {', '.join(sorted(names))}"
                    for hl, names in by_host.items()
                )
            send_notification(
                title=f"Docker: {count} update{'s' if count != 1 else ''} available",
                body=body,
            )

    except Exception as e:
        print(f"[checker] Fatal error: {e}")
    finally:
        _check_running = False
        _check_lock.release()


# ── Container recreation (Watchtower pattern) ─────────────────────────────────

def _exposed_ports(cfg: dict) -> list:
    """Convert a container's ExposedPorts dict ({"7575/tcp": {}}) into the
    (port, proto) tuples docker-py's create_container expects. Passing the raw
    "PORT/proto" strings makes docker-py re-append the protocol, producing a
    bogus "PORT/proto/proto" exposed port that no longer matches PortBindings,
    which silently breaks host-port publishing on user-defined networks."""
    out = []
    for key in (cfg.get("ExposedPorts") or {}).keys():
        port, _, proto = key.partition("/")
        try:
            port = int(port)
        except ValueError:
            pass
        out.append((port, proto or "tcp"))
    return out


def apply_update(container_name: str, host_id: str = "local") -> None:
    key = _container_key(container_name, host_id)
    with _logs_lock:
        _update_logs[key] = []
        _update_running.add(key)
    log = _update_logs[key]

    image_name = "?"  # will be overwritten once container config is read

    def emit(line: str) -> None:
        print(f"[update:{host_id}:{container_name}] {line}")
        log.append(line)

    try:
        # Resolve Docker client
        if host_id == "local":
            client = docker.from_env()
        else:
            hosts = load_hosts()
            host = next((h for h in hosts if h["id"] == host_id), None)
            if not host:
                emit(f"\nERROR: Host '{host_id}' not found.")
                return
            client = get_docker_client(host.get("url"))

        container   = client.containers.get(container_name)
        old_id      = container.id
        attrs       = container.attrs
        cfg         = attrs["Config"]
        hcfg        = attrs["HostConfig"]
        nets        = attrs["NetworkSettings"]["Networks"]
        image_name  = cfg.get("Image", "?")

        emit(f"Container : {container_name}")
        emit(f"Host      : {host_id}")
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

        # Remove any stale _old container left by a previous failed rollback
        old_name = f"{container_name}_old"
        try:
            stale = client.containers.get(old_name)
            emit(f"  Removing stale {old_name} from previous failed update...")
            stale.remove(force=True)
        except docker.errors.NotFound:
            pass

        emit("▶ Renaming old container (kept for rollback)...")
        container.rename(old_name)
        old_container = client.containers.get(old_name)

        # Read backup setting before recreation so we know what to do on success
        with _state_lock:
            _bst = load_state()
        _backup_enabled = _bst.get("backup_enabled", False)
        _backup_hours   = int(_bst.get("backup_hours", 24))

        emit("▶ Recreating container...")

        network_mode = hcfg.get("NetworkMode", "bridge")
        hc = client.api.create_host_config(
            binds=hcfg.get("Binds") or [],
            port_bindings=hcfg.get("PortBindings") or {},
            network_mode=network_mode,
            restart_policy=hcfg.get("RestartPolicy"),
            cap_add=hcfg.get("CapAdd"), cap_drop=hcfg.get("CapDrop"),
            privileged=hcfg.get("Privileged", False),
            security_opt=hcfg.get("SecurityOpt"), devices=hcfg.get("Devices"),
            extra_hosts=hcfg.get("ExtraHosts"), dns=hcfg.get("Dns"),
            dns_search=hcfg.get("DnsSearch"), volumes_from=hcfg.get("VolumesFrom"),
            pid_mode=hcfg.get("PidMode") or "", ipc_mode=hcfg.get("IpcMode") or "",
            tmpfs=hcfg.get("Tmpfs"),
        )

        short_id = old_id[:12]
        full_nets = {
            net_name: {
                "aliases": [a for a in (net_data.get("Aliases") or []) if a != short_id],
                "ipv4_address": (net_data.get("IPAMConfig") or {}).get("IPv4Address") or None,
                "ipv6_address": (net_data.get("IPAMConfig") or {}).get("IPv6Address") or None,
            }
            for net_name, net_data in nets.items()
        }

        simple_net_name = next(iter(full_nets), None)
        simple_nc = None
        if simple_net_name:
            simple_nc = client.api.create_networking_config({
                simple_net_name: client.api.create_endpoint_config(
                    aliases=full_nets[simple_net_name]["aliases"] or None,
                    ipv4_address=full_nets[simple_net_name]["ipv4_address"],
                    ipv6_address=full_nets[simple_net_name]["ipv6_address"],
                )
            })

        # ── Recreation with rollback ──────────────────────────────────────────
        new_c = None
        try:
            new_c = client.api.create_container(
                image=image_name, name=container_name,
                hostname=cfg.get("Hostname", ""), user=cfg.get("User", ""),
                detach=True, environment=cfg.get("Env"), command=cfg.get("Cmd"),
                entrypoint=cfg.get("Entrypoint"), labels=cfg.get("Labels"),
                volumes=list((cfg.get("Volumes") or {}).keys()) or None,
                working_dir=cfg.get("WorkingDir", ""),
                ports=_exposed_ports(cfg) or None,
                host_config=hc, networking_config=simple_nc,
            )

            # The primary network (simple_net_name) is already attached at create
            # time via networking_config, carrying its static IP and aliases, and
            # NetworkMode matches it so published ports bind correctly. Only
            # connect any *additional* networks here — never disconnect/reconnect
            # the primary, as that tears down port publishing on user-defined nets.
            if network_mode != "host" and full_nets:
                for net_name, net_info in full_nets.items():
                    if net_name == simple_net_name:
                        continue
                    client.api.connect_container_to_network(
                        new_c["Id"], net_name,
                        aliases=net_info["aliases"] or None,
                        ipv4_address=net_info["ipv4_address"],
                        ipv6_address=net_info["ipv6_address"],
                    )

            client.api.start(new_c["Id"])

            # Verify the new container actually stayed up
            time.sleep(2)
            started = client.containers.get(container_name)
            if started.status not in ("running", "restarting"):
                raise RuntimeError(
                    f"New container exited immediately (status={started.status}). "
                    "Check logs for startup errors."
                )

            if _backup_enabled:
                _expires = (datetime.datetime.utcnow() + datetime.timedelta(hours=_backup_hours)).isoformat() + "Z"
                emit(f"  Backup '{old_name}' kept for {_backup_hours}h — rollback available.")
            else:
                emit("▶ Removing old container...")
                old_container.remove()
            emit(f"\nSUCCESS: {container_name} updated and running.")

        except Exception as recreate_err:
            emit(f"\nERROR during recreation: {recreate_err}")
            emit("▶ Rolling back to previous container...")
            try:
                # Remove the failed new container if it was created
                if new_c is not None:
                    try:
                        failed = client.containers.get(container_name)
                        failed.stop(timeout=10)
                        failed.remove()
                    except Exception:
                        pass
                # Rename _old back to original name and restart
                old_container.rename(container_name)
                old_container.start()
                emit("  Rollback successful — previous container restored.")
            except Exception as rb_err:
                emit(f"  Rollback failed: {rb_err}")
                emit(f"  Manual intervention required: check container '{old_name}'")
            raise recreate_err
        # ── End rollback block ────────────────────────────────────────────────

        history_entry = {
            "container": container_name, "image": image_name,
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "status": "success", "host_id": host_id,
        }
        rollback_entry = {
            "backed_up_at": datetime.datetime.utcnow().isoformat() + "Z",
            "expires_at": _expires,
        } if _backup_enabled else None
        if host_id == "local":
            with _state_lock:
                state = load_state()
                state["available"].pop(container_name, None)
                state["history"].insert(0, history_entry)
                state["history"] = state["history"][:50]
                if rollback_entry:
                    state.setdefault("rollbacks", {})[container_name] = rollback_entry
                else:
                    state.setdefault("rollbacks", {}).pop(container_name, None)
                save_state(state)
        else:
            hs = load_host_state(host_id)
            hs["available"].pop(container_name, None)
            hs.setdefault("history", []).insert(0, history_entry)
            hs["history"] = hs["history"][:50]
            if rollback_entry:
                hs.setdefault("rollbacks", {})[container_name] = rollback_entry
            else:
                hs.setdefault("rollbacks", {}).pop(container_name, None)
            save_host_state(host_id, hs)

    except Exception as e:
        emit(f"\nERROR: {e}")
        history_entry = {
            "container": container_name, "image": image_name,
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "status": f"error: {e}", "host_id": host_id,
        }
        if host_id == "local":
            with _state_lock:
                state = load_state()
                state["history"].insert(0, history_entry)
                state["history"] = state["history"][:50]
                save_state(state)
        else:
            hs = load_host_state(host_id)
            hs.setdefault("history", []).insert(0, history_entry)
            hs["history"] = hs["history"][:50]
            save_host_state(host_id, hs)
    finally:
        with _logs_lock:
            _update_running.discard(key)


# ── Rollback ─────────────────────────────────────────────────────────────────

def apply_rollback(container_name: str, host_id: str = "local") -> None:
    key = _container_key(container_name, host_id)
    with _logs_lock:
        _update_logs[key] = []
        _update_running.add(key)
    log = _update_logs[key]

    def emit(line: str) -> None:
        print(f"[rollback:{host_id}:{container_name}] {line}")
        log.append(line)

    try:
        if host_id == "local":
            client = docker.from_env()
        else:
            hosts = load_hosts()
            host = next((h for h in hosts if h["id"] == host_id), None)
            if not host:
                emit(f"\nERROR: Host '{host_id}' not found.")
                return
            client = get_docker_client(host.get("url"))

        old_name = f"{container_name}_old"
        emit(f"Container : {container_name}")
        emit(f"Host      : {host_id}")
        emit("")
        emit("▶ Stopping current container...")
        try:
            current = client.containers.get(container_name)
            current.stop(timeout=30)
            current.remove()
        except docker.errors.NotFound:
            emit("  Current container not found, continuing...")

        emit("▶ Restoring previous container...")
        old_container = client.containers.get(old_name)
        old_container.rename(container_name)
        old_container.start()
        emit(f"\nSUCCESS: {container_name} rolled back to previous version.")

        history_entry = {
            "container": container_name, "image": "↩ rollback",
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
            "status": "success", "host_id": host_id,
        }
        if host_id == "local":
            with _state_lock:
                state = load_state()
                state.setdefault("rollbacks", {}).pop(container_name, None)
                state["history"].insert(0, history_entry)
                state["history"] = state["history"][:50]
                save_state(state)
        else:
            hs = load_host_state(host_id)
            hs.setdefault("rollbacks", {}).pop(container_name, None)
            hs.setdefault("history", []).insert(0, history_entry)
            hs["history"] = hs["history"][:50]
            save_host_state(host_id, hs)

    except Exception as e:
        emit(f"\nERROR: {e}")
    finally:
        with _logs_lock:
            _update_running.discard(key)


# ── Changelog ─────────────────────────────────────────────────────────────────

def _github_repo_from_labels(labels: dict) -> str | None:
    for key in ("org.opencontainers.image.source", "org.opencontainers.image.url"):
        url = labels.get(key, "")
        m = re.match(r"https?://github\.com/([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
        if m:
            return m.group(1)
    return None


def fetch_changelog(container_name: str, host_id: str = "local") -> dict:
    if host_id == "local":
        client = docker.from_env()
    else:
        hosts = load_hosts()
        host = next((h for h in hosts if h["id"] == host_id), None)
        if not host:
            return {"error": f"Host '{host_id}' not found.", "releases": []}
        client = get_docker_client(host.get("url"))

    container = client.containers.get(container_name)
    labels    = (container.image.attrs.get("Config") or {}).get("Labels") or {}
    image_name = container.attrs["Config"]["Image"]
    repo       = _github_repo_from_labels(labels)
    source_url = labels.get("org.opencontainers.image.source", "")

    if not repo:
        return {"repo": None, "source_url": source_url or None, "releases": [],
                "error": "No GitHub source URL found in image labels."}
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
        sig      = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            GITHUB_WEBHOOK_SECRET.encode(), request.data, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return jsonify({"error": "Invalid signature"}), 401

    event   = request.headers.get("X-GitHub-Event", "")
    payload = request.get_json(silent=True) or {}
    repo    = payload.get("repository", {}).get("full_name", "unknown")
    title   = body = None

    if event == "issues":
        action = payload.get("action", "")
        issue  = payload.get("issue", {})
        if action == "opened":
            title = f"🐛 New issue — {repo}"
            body  = f"#{issue.get('number')}: {issue.get('title')}\nOpened by {payload.get('sender',{}).get('login','someone')}\n{issue.get('html_url','')}"
        elif action == "closed":
            title = f"✅ Issue closed — {repo}"
            body  = f"#{issue.get('number')}: {issue.get('title')}"
    elif event == "pull_request":
        action = payload.get("action", "")
        pr     = payload.get("pull_request", {})
        if action == "opened":
            title = f"🔀 New PR — {repo}"
            body  = f"#{pr.get('number')}: {pr.get('title')}\nOpened by {payload.get('sender',{}).get('login','someone')}\n{pr.get('html_url','')}"
        elif action == "closed" and pr.get("merged"):
            title = f"✅ PR merged — {repo}"
            body  = f"#{pr.get('number')}: {pr.get('title')}"
    elif event == "watch":
        if payload.get("action") == "started":
            title = f"⭐ New star — {repo}"
            body  = f"{payload.get('sender',{}).get('login','someone')} starred ({payload.get('repository',{}).get('stargazers_count','?')} total)"
    elif event == "push":
        branch = payload.get("ref", "").replace("refs/heads/", "")
        if branch in ("main", "master"):
            commits = payload.get("commits", [])
            title   = f"📦 Push to {branch} — {repo}"
            body    = f"{payload.get('pusher',{}).get('name','someone')} pushed {len(commits)} commit{'s' if len(commits)!=1 else ''}"
            if commits:
                body += f"\n↳ {commits[-1].get('message','').splitlines()[0]}"
    elif event == "release":
        if payload.get("action") == "published":
            rel   = payload.get("release", {})
            title = f"🚀 New release — {repo}"
            body  = f"{rel.get('tag_name','')}: {rel.get('name','')}\n{rel.get('html_url','')}"
    elif event == "issue_comment":
        if payload.get("action") == "created":
            issue   = payload.get("issue", {})
            comment = payload.get("comment", {})
            title   = f"💬 Comment — {repo}"
            body    = f"#{issue.get('number')}: {issue.get('title')}\n{payload.get('sender',{}).get('login','someone')}: {comment.get('body','')[:120]}"

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
    today = datetime.date.today().isoformat()

    # ── Local state ────────────────────────────────────────────────────────────
    with _state_lock:
        state = load_state()

    expired = [n for n, d in state.get("deferred", {}).items()
               if d.get("until", "") <= today]
    if expired:
        with _state_lock:
            s = load_state()
            for n in expired:
                s["deferred"].pop(n, None)
            save_state(s)
        with _state_lock:
            state = load_state()

    containers = []
    try:
        client = docker.from_env()
        for container in client.containers.list():
            name       = container.name
            image_name = container.attrs["Config"]["Image"]
            if is_locally_built(container):
                continue
            info  = state["available"].get(name, {})
            defer = state["deferred"].get(name)
            is_deferred = bool(defer and defer.get("until", "") > today)
            if info:
                status = "update" if info.get("has_update") and not is_deferred else (
                         "deferred" if is_deferred else "ok")
            else:
                status = "unknown"
            key = name  # local uses plain name
            with _logs_lock:
                is_updating = key in _update_running
                has_logs    = key in _update_logs
            now_iso = datetime.datetime.utcnow().isoformat() + "Z"
            rb = state.get("rollbacks", {}).get(name)
            has_rollback = bool(rb and rb.get("expires_at", "") > now_iso)
            if has_rollback:
                try:
                    client.containers.get(f"{name}_old")
                except docker.errors.NotFound:
                    has_rollback = False
                except Exception:
                    pass
            containers.append({
                "name": name, "image": image_name, "status": status,
                "defer_until": defer.get("until") if is_deferred else None,
                "checked_at": info.get("checked_at"),
                "updating": is_updating,
                "has_logs": has_logs,
                "has_changelog": _has_changelog(container),
                "has_rollback": has_rollback,
                "rollback_expires": rb.get("expires_at") if has_rollback else None,
                "host_id": "local", "host_name": "Local",
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # ── Remote hosts (from cached state — no live Docker calls) ───────────────
    remote_hosts_info = []
    all_history = list(state.get("history", []))

    for host in load_hosts():
        host_id   = host["id"]
        host_name = host["name"]
        hs        = load_host_state(host_id)
        host_status = hs.get("status", "unknown")

        remote_hosts_info.append({
            "id": host_id, "name": host_name,
            "url": host.get("url", ""),
            "status": host_status,
            "last_check": hs.get("last_check"),
            "last_error": hs.get("last_error"),
        })

        # Expire deferred for this host
        h_deferred = hs.get("deferred", {})
        h_expired  = [n for n, d in h_deferred.items() if d.get("until","") <= today]
        if h_expired:
            for n in h_expired:
                h_deferred.pop(n, None)
            hs["deferred"] = h_deferred
            save_host_state(host_id, hs)

        for cname, cinfo in hs.get("available", {}).items():
            defer = h_deferred.get(cname)
            is_deferred_c = bool(defer and defer.get("until", "") > today)
            has_update = cinfo.get("has_update", False)
            if has_update and not is_deferred_c:
                cstatus = "update"
            elif is_deferred_c:
                cstatus = "deferred"
            else:
                cstatus = "ok"
            key = _container_key(cname, host_id)
            with _logs_lock:
                is_updating = key in _update_running
                has_logs    = key in _update_logs
            containers.append({
                "name": cname, "image": cinfo.get("image", ""),
                "status": cstatus,
                "defer_until": defer.get("until") if is_deferred_c else None,
                "checked_at": cinfo.get("checked_at"),
                "updating": is_updating,
                "has_logs": has_logs,
                "has_changelog": False,
                "host_id": host_id, "host_name": host_name,
            })

        all_history.extend(hs.get("history", []))

    # Clean up rollback backups: purge entries that are expired (remove the
    # _old container) or orphaned (the _old container no longer exists, e.g.
    # an interrupted rollback). Keeps the Backups tab in sync with reality.
    now_iso = datetime.datetime.utcnow().isoformat() + "Z"
    with _state_lock:
        _cleanup_state = load_state()
    _cleanup_client = None
    try:
        _cleanup_client = docker.from_env()
    except Exception as _ce:
        print(f"[cleanup] Docker unavailable: {_ce}")
    _stale_rbs = []
    if _cleanup_client is not None:
        for _rb_name, _rb in _cleanup_state.get("rollbacks", {}).items():
            _expired = _rb.get("expires_at", "") <= now_iso
            _old_c = None
            try:
                _old_c = _cleanup_client.containers.get(f"{_rb_name}_old")
                _old_exists = True
            except docker.errors.NotFound:
                _old_exists = False
            except Exception:
                _old_exists = True  # transient error: do not purge
            if not _old_exists:
                print(f"[cleanup] Purging stale rollback (no {_rb_name}_old): {_rb_name}")
                _stale_rbs.append(_rb_name)
            elif _expired:
                try:
                    _old_c.remove()
                    print(f"[cleanup] Removed expired backup: {_rb_name}_old")
                    _stale_rbs.append(_rb_name)
                except Exception as _re:
                    print(f"[cleanup] Error removing {_rb_name}_old: {_re}")
    if _stale_rbs:
        with _state_lock:
            _cs = load_state()
            for _rb_name in _stale_rbs:
                _cs.get("rollbacks", {}).pop(_rb_name, None)
            save_state(_cs)

    ORDER = {"update": 0, "deferred": 1, "unknown": 2, "ok": 3}
    containers.sort(key=lambda c: (ORDER.get(c["status"], 9), c["host_name"], c["name"]))
    all_history.sort(key=lambda h: h.get("updated_at", ""), reverse=True)

    hosts_full = [{"id": "local", "name": "Local",
                   "url": "unix:///var/run/docker.sock",
                   "status": "online", "last_check": state.get("last_check")}
                  ] + remote_hosts_info

    return jsonify({
        "containers":   containers,
        "last_check":   state.get("last_check"),
        "check_running": _check_running,
        "history":      all_history[:20],
        "next_check":   _next_check_time(),
        "notify":       get_notify_info(),
        "hosts":        hosts_full,
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    threading.Thread(target=check_for_updates, args=(False,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/update/<name>", methods=["POST"])
def api_update(name):
    host_id = request.args.get("host", "local")
    key = _container_key(name, host_id)
    with _logs_lock:
        if key in _update_running:
            return jsonify({"error": "Already updating"}), 409
    threading.Thread(target=apply_update, args=(name, host_id), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/rollback/<name>", methods=["POST"])
def api_rollback(name):
    host_id = request.args.get("host", "local")
    key = _container_key(name, host_id)
    with _logs_lock:
        if key in _update_running:
            return jsonify({"error": "Operation already in progress"}), 409
    threading.Thread(target=apply_rollback, args=(name, host_id), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/backup/<name>", methods=["DELETE"])
def api_backup_delete(name):
    """Manually delete a kept backup (the <name>_old container) and clear its
    rollback entry, letting users reclaim disk space before retention expiry."""
    host_id = request.args.get("host", "local")
    key = _container_key(name, host_id)
    with _logs_lock:
        if key in _update_running:
            return jsonify({"error": "Operation in progress — try again shortly."}), 409
    old_name = f"{name}_old"
    try:
        if host_id == "local":
            client = docker.from_env()
        else:
            host = next((h for h in load_hosts() if h["id"] == host_id), None)
            if not host:
                return jsonify({"error": f"Host '{host_id}' not found."}), 404
            client = get_docker_client(host.get("url"))
    except Exception as e:
        return jsonify({"error": f"Docker unavailable: {e}"}), 500
    try:
        client.containers.get(old_name).remove(force=True)
        print(f"[backup] Deleted backup container {old_name}")
    except docker.errors.NotFound:
        pass
    except Exception as e:
        return jsonify({"error": f"Could not remove {old_name}: {e}"}), 500
    if host_id == "local":
        with _state_lock:
            state = load_state()
            state.setdefault("rollbacks", {}).pop(name, None)
            save_state(state)
    else:
        hs = load_host_state(host_id)
        hs.setdefault("rollbacks", {}).pop(name, None)
        save_host_state(host_id, hs)
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    with _state_lock:
        state = load_state()
    return jsonify({
        "backup_enabled": state.get("backup_enabled", False),
        "backup_hours":   state.get("backup_hours", 24),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json() or {}
    with _state_lock:
        state = load_state()
        if "backup_enabled" in data:
            state["backup_enabled"] = bool(data["backup_enabled"])
        if "backup_hours" in data:
            state["backup_hours"] = int(data["backup_hours"])
        save_state(state)
    return jsonify({"ok": True})


@app.route("/api/defer/<name>", methods=["POST"])
def api_defer(name):
    host_id = request.args.get("host", "local")
    data    = request.get_json() or {}
    days    = int(data.get("days", 7))
    until   = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    if host_id == "local":
        with _state_lock:
            state = load_state()
            state["deferred"][name] = {"until": until}
            save_state(state)
    else:
        hs = load_host_state(host_id)
        hs.setdefault("deferred", {})[name] = {"until": until}
        save_host_state(host_id, hs)
    return jsonify({"ok": True, "until": until})


@app.route("/api/undefer/<name>", methods=["POST"])
def api_undefer(name):
    host_id = request.args.get("host", "local")
    if host_id == "local":
        with _state_lock:
            state = load_state()
            state["deferred"].pop(name, None)
            save_state(state)
    else:
        hs = load_host_state(host_id)
        hs.get("deferred", {}).pop(name, None)
        save_host_state(host_id, hs)
    return jsonify({"ok": True})


@app.route("/api/logs/<name>")
def api_logs(name):
    host_id = request.args.get("host", "local")
    key     = _container_key(name, host_id)
    with _logs_lock:
        logs    = list(_update_logs.get(key) or _update_logs.get(name, []))
        running = key in _update_running or name in _update_running
    return jsonify({"logs": logs, "running": running})


@app.route("/api/changelog/<name>")
def api_changelog(name):
    host_id = request.args.get("host", "local")
    try:
        return jsonify(fetch_changelog(name, host_id))
    except Exception as e:
        return jsonify({"error": str(e), "releases": []}), 500


# ── Host management routes ────────────────────────────────────────────────────

@app.route("/api/hosts", methods=["GET"])
def api_hosts_list():
    hosts = load_hosts()
    result = []
    for h in hosts:
        hs = load_host_state(h["id"])
        result.append({**h, "status": hs.get("status", "unknown"),
                       "last_check": hs.get("last_check"),
                       "last_error": hs.get("last_error")})
    return jsonify({"hosts": result})


@app.route("/api/hosts", methods=["POST"])
def api_hosts_add():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    url  = data.get("url", "").strip()
    if not name or not url:
        return jsonify({"error": "name and url are required"}), 400
    hosts  = load_hosts()
    host_id = re.sub(r"[^a-z0-9_-]", "-", name.lower())[:32]
    # ensure unique id
    existing_ids = {h["id"] for h in hosts}
    base_id, i = host_id, 1
    while host_id in existing_ids:
        host_id = f"{base_id}-{i}"; i += 1
    hosts.append({"id": host_id, "name": name, "url": url})
    save_hosts(hosts)
    return jsonify({"ok": True, "id": host_id})


@app.route("/api/hosts/<host_id>", methods=["DELETE"])
def api_hosts_delete(host_id):
    hosts = [h for h in load_hosts() if h["id"] != host_id]
    save_hosts(hosts)
    # clean up state file
    path = os.path.join(HOSTS_STATE_DIR, f"{host_id}.json")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"ok": True})


@app.route("/api/hosts/<host_id>/test", methods=["POST"])
def api_hosts_test(host_id):
    hosts = load_hosts()
    host  = next((h for h in hosts if h["id"] == host_id), None)
    if not host:
        return jsonify({"error": "Host not found"}), 404
    try:
        client = get_docker_client(host.get("url"))
        info   = client.info()
        return jsonify({"ok": True, "version": info.get("ServerVersion", "?"),
                        "containers": info.get("Containers", "?")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ── Scheduler ─────────────────────────────────────────────────────────────────

_scheduler: BackgroundScheduler | None = None


def _next_check_time() -> str | None:
    if _scheduler is None:
        return None
    try:
        job = _scheduler.get_jobs()[0]
        nf  = job.next_run_time
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
        timezone=TIMEZONE, kwargs={"notify": True},
    )
    _scheduler.start()
    print(f"[scheduler] Daily check scheduled at {CHECK_TIME} {TIMEZONE}")

    notify_info = get_notify_info()
    if notify_info:
        prefix = "Auto-generated" if notify_info["auto"] else "Configured"
        print(f"[notify] {prefix} topic: {notify_info['subscribe']}")

    if GITHUB_WEBHOOK_SECRET:
        print(f"[github] Webhook endpoint active at /webhook/github")

    remote_hosts = load_hosts()
    if remote_hosts:
        print(f"[hosts] {len(remote_hosts)} remote host(s) configured: "
              f"{', '.join(h['name'] for h in remote_hosts)}")

    threading.Thread(target=check_for_updates, args=(False,), daemon=True).start()
    app.run(host="0.0.0.0", port=9090, debug=False, threaded=True)
