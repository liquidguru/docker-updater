#!/usr/bin/env python3
"""
docker-updater — poll registries for image digest changes, apply updates with approval.
Supports multiple Docker hosts via SSH or TCP.
"""

import base64
import datetime
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="paramiko")
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time

import apprise
import docker
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

APP_VERSION          = "1.11.0"
DATA_DIR             = "/app/data"
STATE_FILE           = os.path.join(DATA_DIR, "state.json")
HOSTS_FILE           = os.path.join(DATA_DIR, "hosts.json")
HOSTS_STATE_DIR      = os.path.join(DATA_DIR, "hosts")
LOG_DIR              = os.path.join(DATA_DIR, "logs")
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
_OWN_CONTAINER_ID: str | None = None  # set at startup via _detect_own_container()
_OWN_HOSTNAME: str | None = None      # gethostname(), fallback self-update match

# Opt-in compose-stack restart (issue #12). Debounced per stack so a bulk update
# of several members triggers only one round of sibling restarts.
STACK_RESTART_QUIET = 8               # seconds of quiet after the last stack update
_stack_lock     = threading.Lock()
_stack_timers: dict[str, threading.Timer] = {}
_stack_updated: dict[str, set] = {}   # stack key -> names just updated (excluded)


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
            "notify_url": None, "rollbacks": {}, "backup_enabled": False,
            "backup_hours": 24, "restart_stack": False}


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


def get_docker_client(url: str | None = None, timeout: int = 300):
    """Return a DockerClient for a local socket, SSH, or TCP URL.

    timeout defaults to 300 s (5 min) — large LSIO images can take that long
    to pull, and some first-boot init scripts run apt-get before the server
    starts.  Pass a shorter value for quick status-check calls if needed.
    """
    if not url or url.startswith("unix://") or url.startswith("npipe://"):
        return docker.from_env(timeout=timeout)
    if url.startswith("ssh://"):
        # use_ssh_client=True delegates to the system SSH binary instead of
        # paramiko, so it respects ~/.ssh/config (including our persistent
        # UserKnownHostsFile in the data volume).
        return docker.DockerClient(base_url=url, use_ssh_client=True, timeout=timeout)
    return docker.DockerClient(base_url=url, timeout=timeout)


def _setup_ssh_config() -> None:
    """Write ~/.ssh/config at startup so the system SSH binary uses
    /app/data/known_hosts as its UserKnownHostsFile. This persists accepted
    host keys across container restarts via the data volume."""
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    known_hosts = os.path.join(DATA_DIR, "known_hosts")
    config_path = os.path.join(ssh_dir, "config")
    with open(config_path, "w") as f:
        f.write(
            f"Host *\n"
            f"    UserKnownHostsFile {known_hosts}\n"
            f"    StrictHostKeyChecking yes\n"
        )
    if not os.path.exists(known_hosts):
        open(known_hosts, "w").close()
        os.chmod(known_hosts, 0o600)
    print(f"[ssh] Persistent known_hosts: {known_hosts}")


def _ssh_keyscan_and_accept(url: str) -> bool:
    """Run ssh-keyscan for the host in a ssh:// URL and append its key to
    the persistent known_hosts file. Returns True on success.
    This implements Trust On First Use (TOFU) — host keys are verified on
    every subsequent connection via StrictHostKeyChecking=yes."""
    m = re.match(r"ssh://(?:[^@]+@)?([^:/]+)(?::(\d+))?", url)
    if not m:
        return False
    host, port = m.group(1), m.group(2) or "22"
    known_hosts = os.path.join(DATA_DIR, "known_hosts")
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-H", "-p", port, host],
            capture_output=True, text=True, timeout=10,
        )
        lines = [
            ln for ln in result.stdout.splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        if not lines:
            return False
        with open(known_hosts, "a") as f:
            f.write("\n".join(lines) + "\n")
        print(f"[ssh] Accepted host key for {host}:{port}")
        return True
    except Exception as exc:
        print(f"[ssh] ssh-keyscan failed for {host}:{port}: {exc}")
        return False


def _detect_own_container() -> None:
    """Record our own container ID so apply_update can detect self-updates.

    The hostname normally equals the short container ID, but it stops matching
    once a container has been recreated carrying a stale hostname (e.g. after a
    previous self-update). Read the real container ID from /proc/self/mountinfo
    (present regardless of hostname) and keep the hostname as a fallback match
    against Config.Hostname.
    """
    global _OWN_CONTAINER_ID, _OWN_HOSTNAME
    try:
        _OWN_HOSTNAME = socket.gethostname()
    except Exception:
        _OWN_HOSTNAME = None
    try:
        for _p in ("/proc/self/mountinfo", "/proc/self/cgroup"):
            try:
                with open(_p) as f:
                    data = f.read()
            except Exception:
                continue
            m = re.search(r"containers/([0-9a-f]{64})", data) or \
                re.search(r"\b([0-9a-f]{64})\b", data)
            if m:
                _OWN_CONTAINER_ID = m.group(1)
                break
    except Exception:
        pass
    if not _OWN_CONTAINER_ID:
        _OWN_CONTAINER_ID = _OWN_HOSTNAME  # fall back to old behaviour
    print(f"[self-update] Own container id: {_OWN_CONTAINER_ID} "
          f"(hostname: {_OWN_HOSTNAME})")


def _recreate_hostname(cfg: dict) -> str:
    """Hostname to set when recreating a container.

    Docker auto-assigns the short container ID (12 hex chars) as the hostname.
    Carrying that into the recreated container leaves it permanently mismatched
    from its own ID — which silently broke self-update detection after the first
    successful self-update. Drop an auto-generated hostname so Docker assigns a
    fresh matching one; preserve any hostname the user actually set.
    """
    h = (cfg.get("Hostname") or "")
    return "" if re.fullmatch(r"[0-9a-f]{12}", h) else h


def _mounts_from_hcfg(hcfg: dict):
    """Rebuild HostConfig.Mounts (named volumes, --mount entries) for recreation.

    docker-updater historically restored only HostConfig.Binds. Compose-managed
    named volumes live in HostConfig.Mounts (Binds is null), so updating such a
    container recreated it with NO volume attached and silently dropped its data
    (e.g. a Grafana update wiping all dashboards). We now also carry over .Mounts.
    Targets already present in Binds are skipped so docker-run -v + --mount combos
    don't produce a duplicate-mount error.
    """
    bind_targets = set()
    for b in (hcfg.get("Binds") or []):
        parts = b.split(":")
        if len(parts) >= 2:
            bind_targets.add(parts[1])
    out = []
    for m in (hcfg.get("Mounts") or []):
        target = m.get("Target")
        if not target or target in bind_targets:
            continue
        try:
            kwargs = {
                "target": target,
                "source": m.get("Source"),
                "type": m.get("Type", "volume"),
                "read_only": m.get("ReadOnly", False),
            }
            prop = (m.get("BindOptions") or {}).get("Propagation")
            if prop:
                kwargs["propagation"] = prop
            out.append(docker.types.Mount(**kwargs))
        except Exception as e:
            print(f"[recreate] could not rebuild mount {target}: {e}")
    return out or None


def _is_own_container(container) -> bool:
    """True if this container is docker-updater itself (never restart it)."""
    try:
        cid = container.id or ""
        if _OWN_CONTAINER_ID and (cid == _OWN_CONTAINER_ID
                                  or cid.startswith(_OWN_CONTAINER_ID)):
            return True
        h = (container.attrs.get("Config") or {}).get("Hostname")
        return bool(_OWN_HOSTNAME and h == _OWN_HOSTNAME)
    except Exception:
        return False


def _schedule_stack_restart(client, host_id: str, project: str,
                            trigger_name: str, emit) -> None:
    """After a compose-stack member is updated, restart the OTHER members so they
    pick up the recreated container's new IP/DNS (issue #12). Debounced per stack:
    each update (re)starts a short timer, so a bulk update of several members only
    restarts the untouched siblings once, after the updates settle."""
    key = f"{host_id}:{project}"
    with _stack_lock:
        _stack_updated.setdefault(key, set()).add(trigger_name)
        old = _stack_timers.get(key)
        if old:
            old.cancel()
        timer = threading.Timer(STACK_RESTART_QUIET, _do_stack_restart,
                                args=(client, host_id, project, key))
        timer.daemon = True
        _stack_timers[key] = timer
        timer.start()
    emit(f"↻ Stack '{project}': other members will be restarted in "
         f"{STACK_RESTART_QUIET}s so they pick up the new container.")


def _do_stack_restart(client, host_id: str, project: str, key: str) -> None:
    with _stack_lock:
        _stack_timers.pop(key, None)
        updated = _stack_updated.pop(key, set())
    try:
        targets = []
        for c in client.containers.list():
            if c.name.endswith("_old") or c.name in updated:
                continue
            if (c.labels or {}).get("com.docker.compose.project") != project:
                continue
            if _container_key(c.name, host_id) in _update_running:
                continue  # a member that's mid-update will start fresh anyway
            if host_id == "local" and _is_own_container(c):
                continue  # never restart docker-updater itself
            targets.append(c)
        if not targets:
            print(f"[stack-restart:{host_id}] {project}: no other members to restart")
            return
        print(f"[stack-restart:{host_id}] {project}: restarting "
              f"{', '.join(t.name for t in targets)}")
        for c in targets:
            try:
                c.restart(timeout=30)
            except Exception as e:
                print(f"[stack-restart:{host_id}] {c.name} restart failed: {e}")
    except Exception as e:
        print(f"[stack-restart:{host_id}] {project}: enumerate failed: {e}")


def _helper_write_state(name: str, history_entry: dict, rollback_entry: dict | None) -> None:
    """Write history + rollback entry to state.json from the self-update helper.
    Only meaningful when /app/data is mounted in the helper container."""
    if not os.path.exists(DATA_DIR):
        print("[self-update-helper] /app/data not mounted — skipping state.json update")
        return
    try:
        state = load_state()
        state["available"].pop(name, None)
        state.setdefault("history", []).insert(0, history_entry)
        state["history"] = state["history"][:50]
        if rollback_entry:
            state.setdefault("rollbacks", {})[name] = rollback_entry
        else:
            state.setdefault("rollbacks", {}).pop(name, None)
        save_state(state)
        print("[self-update-helper] state.json updated.")
    except Exception as e:
        print(f"[self-update-helper] Failed to update state.json: {e}")


def _run_self_update_helper() -> None:
    """Called when DOCKER_UPDATER_SELF_UPDATE_SPEC_B64 is set in the environment.

    Runs inside a temporary helper container spawned by apply_update. Waits for
    the old container to finish, renames it to {name}_old (rollback point),
    recreates it from the new image, verifies it started, and auto-rolls back
    if the new container exits immediately.
    """
    spec_b64 = os.environ.get("DOCKER_UPDATER_SELF_UPDATE_SPEC_B64", "")
    if not spec_b64:
        print("[self-update-helper] ERROR: spec env var not set")
        return

    try:
        spec = json.loads(base64.b64decode(spec_b64))
    except Exception as e:
        print(f"[self-update-helper] Failed to decode spec: {e}")
        return

    name           = spec["name"]
    image_name     = spec["image"]
    cfg            = spec["cfg"]
    hcfg           = spec["hcfg"]
    full_nets      = spec["full_nets"]
    backup_enabled = spec.get("backup_enabled", False)
    backup_hours   = int(spec.get("backup_hours", 24))
    orig_policy    = hcfg.get("RestartPolicy", {"Name": "unless-stopped"})
    old_name       = f"{name}_old"

    print(f"[self-update-helper] Waiting 10s for {name} to exit...")
    time.sleep(10)

    client = get_docker_client()

    # ── Stop the old container ────────────────────────────────────────────────
    old_container = None
    for attempt in range(3):
        try:
            old_container = client.containers.get(name)
            print(f"[self-update-helper] Stopping {name} (attempt {attempt + 1})...")
            old_container.stop(timeout=30)
            print(f"[self-update-helper] Stopped.")
            break
        except docker.errors.NotFound:
            print(f"[self-update-helper] {name} already gone.")
            break
        except Exception as e:
            print(f"[self-update-helper] Stop failed: {e}")
            if attempt < 2:
                time.sleep(5)

    # ── Rename old → _old (rollback point, restart=no) ───────────────────────
    try:
        stale = client.containers.get(old_name)
        stale.remove(force=True)
        print(f"[self-update-helper] Removed stale {old_name}.")
    except docker.errors.NotFound:
        pass

    if old_container is not None:
        try:
            old_container.rename(old_name)
            client.api.update_container(old_container.id, restart_policy={"Name": "no"})
            print(f"[self-update-helper] Renamed to {old_name} with restart=no.")
        except Exception as e:
            print(f"[self-update-helper] Rename failed: {e} — rollback unavailable.")
            old_container = None

    # ── Recreate with new image ───────────────────────────────────────────────
    print(f"[self-update-helper] Recreating {name} with {image_name}...")
    new_c = None
    try:
        network_mode = hcfg.get("NetworkMode", "bridge")
        hc = client.api.create_host_config(
            binds=hcfg.get("Binds") or [],
            mounts=_mounts_from_hcfg(hcfg),
            port_bindings=hcfg.get("PortBindings") or {},
            network_mode=network_mode,
            restart_policy=orig_policy,
            cap_add=hcfg.get("CapAdd"), cap_drop=hcfg.get("CapDrop"),
            privileged=hcfg.get("Privileged", False),
            security_opt=hcfg.get("SecurityOpt"), devices=hcfg.get("Devices"),
            extra_hosts=hcfg.get("ExtraHosts"), dns=hcfg.get("Dns"),
            dns_search=hcfg.get("DnsSearch"), volumes_from=hcfg.get("VolumesFrom"),
            pid_mode=hcfg.get("PidMode") or "", ipc_mode=hcfg.get("IpcMode") or "",
            tmpfs=hcfg.get("Tmpfs"),
        )

        simple_net_name = next(iter(full_nets), None)
        simple_nc = None
        if simple_net_name:
            simple_nc = client.api.create_networking_config({
                simple_net_name: client.api.create_endpoint_config(
                    aliases=full_nets[simple_net_name].get("aliases") or None,
                    ipv4_address=full_nets[simple_net_name].get("ipv4_address"),
                    ipv6_address=full_nets[simple_net_name].get("ipv6_address"),
                )
            })

        new_c = client.api.create_container(
            image=image_name, name=name,
            hostname=_recreate_hostname(cfg), user=cfg.get("User", ""),
            detach=True, environment=cfg.get("Env"), command=cfg.get("Cmd"),
            entrypoint=cfg.get("Entrypoint"), labels=cfg.get("Labels"),
            volumes=list((cfg.get("Volumes") or {}).keys()) or None,
            working_dir=cfg.get("WorkingDir", ""),
            ports=_exposed_ports(cfg) or None,
            host_config=hc, networking_config=simple_nc,
        )

        if network_mode != "host" and len(full_nets) > 1:
            for net_name, net_info in full_nets.items():
                if net_name == simple_net_name:
                    continue
                client.api.connect_container_to_network(
                    new_c["Id"], net_name,
                    aliases=net_info.get("aliases") or None,
                    ipv4_address=net_info.get("ipv4_address"),
                    ipv6_address=net_info.get("ipv6_address"),
                )

        client.api.start(new_c["Id"])
        time.sleep(2)
        started = client.containers.get(name)
        if started.status not in ("running", "restarting"):
            raise RuntimeError(
                f"New container exited immediately (status={started.status})"
            )

        # ── Success ───────────────────────────────────────────────────────────
        print(f"[self-update-helper] {name} is running.")
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        if backup_enabled:
            expires_iso = (
                datetime.datetime.utcnow() + datetime.timedelta(hours=backup_hours)
            ).isoformat() + "Z"
            rollback_entry: dict | None = {
                "backed_up_at": now_iso, "expires_at": expires_iso,
                "restart_policy": orig_policy,
            }
            print(f"[self-update-helper] Backup kept for {backup_hours}h.")
        else:
            rollback_entry = None
            if old_container is not None:
                try:
                    old_container.remove()
                    print(f"[self-update-helper] Removed {old_name}.")
                except Exception as e:
                    print(f"[self-update-helper] Could not remove {old_name}: {e}")

        history_entry = {
            "container": name, "image": image_name,
            "updated_at": now_iso, "status": "success", "host_id": "local",
        }
        _helper_write_state(name, history_entry, rollback_entry)

    except Exception as recreate_err:
        # ── Failure: roll back to _old ────────────────────────────────────────
        print(f"[self-update-helper] Recreation failed: {recreate_err}")
        print(f"[self-update-helper] Rolling back to previous container...")
        try:
            if new_c is not None:
                try:
                    failed = client.containers.get(name)
                    failed.stop(timeout=10)
                    failed.remove()
                except Exception:
                    pass
            if old_container is not None:
                old_container.rename(name)
                try:
                    client.api.update_container(old_container.id, restart_policy=orig_policy)
                except Exception:
                    pass
                old_container.start()
                print(f"[self-update-helper] Rollback successful — previous container restored.")
            else:
                print(f"[self-update-helper] No old container — manual intervention required.")
        except Exception as rb_err:
            print(f"[self-update-helper] Rollback also failed: {rb_err}")

        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        history_entry = {
            "container": name, "image": image_name,
            "updated_at": now_iso,
            "status": f"error: {recreate_err}", "host_id": "local",
        }
        _helper_write_state(name, history_entry, None)


def _log_path(name: str, host_id: str = "local") -> str:
    """Return the filesystem path for a container's persisted update log."""
    prefix = f"{host_id}__{name}" if host_id != "local" else name
    safe   = re.sub(r"[^a-zA-Z0-9._-]", "_", prefix)
    return os.path.join(LOG_DIR, f"{safe}.log")


def _persist_log(key: str, name: str, host_id: str) -> None:
    """Write the in-memory update log for *key* to disk (best-effort)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with _logs_lock:
            lines = list(_update_logs.get(key, []))
        with open(_log_path(name, host_id), "w") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass


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


def _digest_from_ref(ref: str | None) -> str | None:
    if not ref:
        return None
    return ref.split("@", 1)[-1]


def _docker_repo_candidates(registry: str, repo: str) -> set[str]:
    candidates = {repo, f"{registry}/{repo}"}
    if registry == "registry-1.docker.io":
        candidates.add(f"docker.io/{repo}")
        if repo.startswith("library/"):
            short = repo.split("/", 1)[1]
            candidates.update({
                short,
                f"docker.io/{short}",
                f"registry-1.docker.io/{short}",
            })
    return candidates


def _manifest_matches_platform(manifest: dict, platform: dict | None) -> bool:
    if not platform:
        return False
    candidate = manifest.get("platform") or {}
    if candidate.get("os") == "unknown" or candidate.get("architecture") == "unknown":
        return False
    for key in ("os", "architecture"):
        wanted = platform.get(key)
        if wanted and candidate.get(key) != wanted:
            return False
    wanted_variant = platform.get("variant")
    if wanted_variant and candidate.get("variant") != wanted_variant:
        return False
    return True


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


def get_remote_digest(
    image_name: str,
    local_digest: str | None = None,
    local_image_id: str | None = None,
    local_platform: dict | None = None,
) -> str | None:
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
            remote = r.headers.get("Docker-Content-Digest")
            ct = r.headers.get("Content-Type", "")
            if (remote and local_digest and remote != local_digest
                    and ("manifest.list" in ct or "image.index" in ct)):
                try:
                    body = requests.get(url, headers=headers, timeout=10, allow_redirects=True).json()
                    manifests = body.get("manifests", [])
                    platform_digests = {m.get("digest") for m in manifests}
                    if local_digest in platform_digests:
                        return local_digest
                    if local_image_id and local_platform:
                        platform_manifest = next(
                            (m for m in manifests if _manifest_matches_platform(m, local_platform)),
                            None,
                        )
                        platform_digest = (platform_manifest or {}).get("digest")
                        if platform_digest:
                            manifest_url = f"https://{registry}/v2/{repo}/manifests/{platform_digest}"
                            manifest = requests.get(
                                manifest_url, headers=headers, timeout=10, allow_redirects=True,
                            ).json()
                            remote_image_id = (manifest.get("config") or {}).get("digest")
                            if remote_image_id == local_image_id:
                                return local_digest
                except Exception:
                    pass
            return remote
    except Exception as e:
        print(f"[checker] registry error for {image_name}: {e}")
    return None


def get_local_digest(container, image_name: str | None = None) -> str | None:
    try:
        digests = container.image.attrs.get("RepoDigests", [])
        if digests:
            if image_name:
                registry, repo, _tag = parse_image(image_name)
                candidates = _docker_repo_candidates(registry, repo)
                for digest_ref in digests:
                    repo_ref = digest_ref.split("@", 1)[0]
                    if repo_ref in candidates:
                        return _digest_from_ref(digest_ref)
            return _digest_from_ref(digests[0])
    except Exception:
        pass
    return None


def get_local_image_id(container) -> str | None:
    try:
        return _digest_from_ref(container.image.attrs.get("Id"))
    except Exception:
        return None


def get_local_platform(container) -> dict | None:
    try:
        attrs = container.image.attrs
        platform = {
            "os": attrs.get("Os"),
            "architecture": attrs.get("Architecture"),
            "variant": attrs.get("Variant"),
        }
        return platform if platform["os"] or platform["architecture"] else None
    except Exception:
        return None


def _has_changelog(container) -> bool:
    try:
        labels = (container.image.attrs.get("Config") or {}).get("Labels") or {}
        if _github_repo_from_labels(labels) is not None:
            return True
        image = container.attrs["Config"]["Image"]
        return _github_repo_from_image(image) is not None
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
        if name.endswith("_old"):
            continue  # skip backup containers created by docker-updater
        image_name = container.attrs["Config"]["Image"]
        if is_locally_built(container):
            continue
        local_digest = get_local_digest(container, image_name)
        remote_digest = get_remote_digest(
            image_name, local_digest, get_local_image_id(container), get_local_platform(container),
        )
        has_update = bool(local_digest and remote_digest and local_digest != remote_digest)
        flag = "UPDATE" if has_update else ("no digest" if not remote_digest else "ok")
        print(f"[checker:{host_id}] {name}: [{flag}]")
        if remote_digest:
            labels = container.labels or {}
            available[name] = {
                "image": image_name,
                "local_digest": local_digest,
                "remote_digest": remote_digest,
                "has_update": has_update,
                "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
                "compose_project": labels.get("com.docker.compose.project"),
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
        # Resolve Docker client (generous timeout — pulls can be slow)
        if host_id == "local":
            client = get_docker_client()
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

        # ── Self-update detection ─────────────────────────────────────────────
        # If we're updating our own container, we can't do the stop/rename/
        # recreate ourselves — that would kill the process mid-flight. Instead
        # spawn a helper container using the just-pulled new image; it waits
        # for us to exit, then renames us to _old (rollback point), recreates
        # us with the new image, and rolls back automatically if it fails.
        _is_self = host_id == "local" and (
            (_OWN_CONTAINER_ID and (old_id == _OWN_CONTAINER_ID
                                    or old_id.startswith(_OWN_CONTAINER_ID)))
            or (_OWN_HOSTNAME and cfg.get("Hostname") == _OWN_HOSTNAME)
        )
        if _is_self:
            emit("\n♻ Self-update detected — docker-updater is updating itself.")
            emit("  Preparing handoff to helper container...")
            _su_short = old_id[:12]
            _su_nets = {
                n: {
                    "aliases": [a for a in (d.get("Aliases") or []) if a != _su_short],
                    "ipv4_address": (d.get("IPAMConfig") or {}).get("IPv4Address") or None,
                    "ipv6_address": (d.get("IPAMConfig") or {}).get("IPv6Address") or None,
                }
                for n, d in nets.items()
            }
            # Read backup settings and include in spec so the helper knows
            # whether to keep or remove the _old container after a successful update.
            with _state_lock:
                _su_bst = load_state()
            _su_backup_enabled = _su_bst.get("backup_enabled", False)
            _su_backup_hours   = int(_su_bst.get("backup_hours", 24))

            # Find the host-side path for /app/data so the helper can mount it
            # and write the rollback entry + history to state.json.
            _su_data_host = None
            for _bind in (hcfg.get("Binds") or []):
                _parts = _bind.split(":")
                if len(_parts) >= 2 and _parts[1] == "/app/data":
                    _su_data_host = _parts[0]
                    break

            _su_spec = json.dumps({
                "name": container_name, "image": image_name,
                "cfg": cfg, "hcfg": hcfg, "full_nets": _su_nets,
                "backup_enabled": _su_backup_enabled,
                "backup_hours": _su_backup_hours,
            })
            _su_spec_b64 = base64.b64encode(_su_spec.encode()).decode()
            _su_volumes = {
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            }
            if _su_data_host:
                _su_volumes[_su_data_host] = {"bind": "/app/data", "mode": "rw"}
                emit("  Data volume found — rollback + history will be recorded.")
            else:
                emit("  Data volume path not detected — rollback won't appear in UI")
                emit("  (startup recovery self-heals if the new container fails).")
            try:
                client.containers.run(
                    image_name,
                    environment={"DOCKER_UPDATER_SELF_UPDATE_SPEC_B64": _su_spec_b64},
                    volumes=_su_volumes,
                    detach=True,
                    remove=True,
                )
                emit("  Helper spawned — docker-updater will restart in ~10 seconds.")
                emit("\nSUCCESS: Self-update handed off. The page will be unreachable")
                emit("  briefly while the new container starts. Refresh after 30s.")
            except Exception as su_err:
                emit(f"\nERROR spawning self-update helper: {su_err}")
                emit("  Pull succeeded — to finish the update, manually recreate this")
                emit(f"  container using image: {image_name}")
            return
        # ─────────────────────────────────────────────────────────────────────

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
        # Disable auto-start on the backup so a host reboot doesn't start both
        # the current container and its _old backup simultaneously.
        try:
            client.api.update_container(old_container.id, restart_policy={"Name": "no"})
        except Exception:
            pass

        # Read backup setting before recreation so we know what to do on success
        with _state_lock:
            _bst = load_state()
        _backup_enabled = _bst.get("backup_enabled", False)
        _backup_hours   = int(_bst.get("backup_hours", 24))

        emit("▶ Recreating container...")

        network_mode = hcfg.get("NetworkMode", "bridge")
        hc = client.api.create_host_config(
            binds=hcfg.get("Binds") or [],
            mounts=_mounts_from_hcfg(hcfg),
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
                hostname=_recreate_hostname(cfg), user=cfg.get("User", ""),
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
                # Rename _old back to original name, restore restart policy, restart
                old_container.rename(container_name)
                try:
                    client.api.update_container(
                        old_container.id,
                        restart_policy=hcfg.get("RestartPolicy", {"Name": "unless-stopped"}),
                    )
                except Exception:
                    pass
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
            "restart_policy": hcfg.get("RestartPolicy", {"Name": "unless-stopped"}),
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

        # ── Restart sibling compose-stack members (opt-in, issue #12) ─────────
        try:
            with _state_lock:
                _rs_enabled = load_state().get("restart_stack", False)
            _proj = (cfg.get("Labels") or {}).get("com.docker.compose.project")
            if _rs_enabled and _proj:
                _schedule_stack_restart(client, host_id, _proj, container_name, emit)
        except Exception as _rs_err:
            print(f"[stack-restart] schedule error: {_rs_err}")

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
        _persist_log(key, container_name, host_id)
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
            client = get_docker_client()
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
        # Restore the original restart policy (was set to "no" when backup was made).
        try:
            if host_id == "local":
                _rb_state = load_state()
            else:
                _rb_state = load_host_state(host_id)
            _orig_policy = (_rb_state.get("rollbacks", {})
                            .get(container_name, {})
                            .get("restart_policy", {"Name": "unless-stopped"}))
            client.api.update_container(old_container.id, restart_policy=_orig_policy)
        except Exception:
            pass
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
        _persist_log(key, container_name, host_id)
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


def _github_repo_from_url(url: str) -> str | None:
    """Parse a GitHub URL (repo page, releases page, etc.) into owner/repo."""
    m = re.match(r"https?://github\.com/([^/]+/[^/\s]+?)(?:\.git|/releases|/tags|/)?$", url.strip())
    return m.group(1) if m else None


def _github_repo_from_image(image: str) -> str | None:
    """Parse a GHCR image name into a GitHub owner/repo string.
    Only applied to ghcr.io images — those map 1:1 to a GitHub repo."""
    name = re.split(r"[:@]", image)[0]
    parts = name.split("/")
    if len(parts) >= 3 and parts[0] == "ghcr.io":
        return f"{parts[1]}/{parts[2]}"
    return None


def fetch_changelog(container_name: str, host_id: str = "local") -> dict:
    if host_id == "local":
        client = docker.from_env()
        _cl_state = load_state()
    else:
        hosts = load_hosts()
        host = next((h for h in hosts if h["id"] == host_id), None)
        if not host:
            return {"error": f"Host '{host_id}' not found.", "releases": []}
        client = get_docker_client(host.get("url"))
        _cl_state = load_host_state(host_id)

    container = client.containers.get(container_name)
    labels    = (container.image.attrs.get("Config") or {}).get("Labels") or {}
    repo       = _github_repo_from_labels(labels)
    source_url = labels.get("org.opencontainers.image.source", "")

    # Fall back to manual override if no OCI label
    if not repo:
        override = _cl_state.get("changelog_urls", {}).get(container_name, "")
        if override:
            repo = _github_repo_from_url(override)
            source_url = override

    # Auto-detect GHCR images: ghcr.io/owner/repo always maps to github.com/owner/repo
    if not repo:
        image_name = container.attrs["Config"]["Image"]
        repo = _github_repo_from_image(image_name)
        if repo:
            source_url = f"https://github.com/{repo}"

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
    return render_template("index.html", version=APP_VERSION)


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
            if name.endswith("_old"):
                continue  # skip backup containers created by docker-updater
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
            _cl_override = state.get("changelog_urls", {}).get(name)
            _compose = (container.labels or {}).get("com.docker.compose.project")
            containers.append({
                "name": name, "image": image_name, "status": status,
                "defer_until": defer.get("until") if is_deferred else None,
                "checked_at": info.get("checked_at"),
                "updating": is_updating,
                "has_logs": has_logs,
                "has_changelog": _has_changelog(container) or bool(_cl_override),
                "changelog_url": _cl_override,
                "has_rollback": has_rollback,
                "rollback_expires": rb.get("expires_at") if has_rollback else None,
                "compose_project": _compose,
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
            _h_cl_override = hs.get("changelog_urls", {}).get(cname)
            containers.append({
                "name": cname, "image": cinfo.get("image", ""),
                "status": cstatus,
                "defer_until": defer.get("until") if is_deferred_c else None,
                "checked_at": cinfo.get("checked_at"),
                "updating": is_updating,
                "has_logs": has_logs,
                "has_changelog": bool(_h_cl_override),
                "changelog_url": _h_cl_override,
                "compose_project": cinfo.get("compose_project"),
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
        "restart_stack":  state.get("restart_stack", False),
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
        if "restart_stack" in data:
            state["restart_stack"] = bool(data["restart_stack"])
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


# ── Container & update log routes ────────────────────────────────────────────

@app.route("/api/update-log/<name>")
def api_update_log(name):
    """Return the persisted update/rollback log for a container (survives restarts)."""
    host_id = request.args.get("host_id", "local")
    path    = _log_path(name, host_id)
    if not os.path.exists(path):
        return jsonify({"found": False, "logs": []}), 200
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
        return jsonify({"found": True, "logs": lines})
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 500




@app.route("/api/container/<name>/logs")
def api_container_logs(name):
    """Return the last N lines of a container's docker logs plus its status."""
    host_id = request.args.get("host_id", "local")
    tail    = min(int(request.args.get("tail", 200)), 2000)
    try:
        if host_id == "local":
            client = docker.from_env()
        else:
            hosts = load_hosts()
            host  = next((h for h in hosts if h["id"] == host_id), None)
            if not host:
                return jsonify({"error": f"Host '{host_id}' not found"}), 404
            client = get_docker_client(host.get("url"))
        container  = client.containers.get(name)
        logs       = container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
        state      = container.attrs.get("State", {})
        return jsonify({
            "logs":      logs,
            "status":    container.status,
            "exit_code": state.get("ExitCode"),
        })
    except docker.errors.NotFound:
        return jsonify({"error": f"Container '{name}' not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/container/<name>/changelog-url", methods=["POST"])
def api_set_changelog_url(name):
    """Save or clear a manual changelog URL override for a container."""
    data    = request.get_json(silent=True) or {}
    url     = (data.get("url") or "").strip()
    host_id = data.get("host_id", "local")
    if url and not re.match(r"https?://github\.com/[^/]+/[^/\s]+", url):
        return jsonify({"error": "URL must be a GitHub repository URL"}), 400
    if host_id == "local":
        with _state_lock:
            state = load_state()
            if url:
                state.setdefault("changelog_urls", {})[name] = url
            else:
                state.setdefault("changelog_urls", {}).pop(name, None)
            save_state(state)
    else:
        hs = load_host_state(host_id)
        if url:
            hs.setdefault("changelog_urls", {})[name] = url
        else:
            hs.setdefault("changelog_urls", {}).pop(name, None)
        save_host_state(host_id, hs)
    return jsonify({"ok": True})


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
    url = host.get("url", "")
    try:
        client = get_docker_client(url)
        info   = client.info()
        return jsonify({"ok": True, "version": info.get("ServerVersion", "?"),
                        "containers": info.get("Containers", "?")})
    except Exception as e:
        err_str = str(e)
        # Auto-accept SSH host key on first connection (Trust On First Use).
        # If the connection fails because the host isn't in known_hosts, run
        # ssh-keyscan to fetch and save the key, then retry once.
        if url.startswith("ssh://") and "known_hosts" in err_str:
            try:
                if _ssh_keyscan_and_accept(url):
                    client = get_docker_client(url)
                    info   = client.info()
                    return jsonify({
                        "ok": True,
                        "version":      info.get("ServerVersion", "?"),
                        "containers":   info.get("Containers", "?"),
                        "accepted_key": True,
                    })
            except Exception as e2:
                err_str = str(e2)
        return jsonify({"ok": False, "error": err_str}), 200


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

def recover_interrupted_operations() -> None:
    """On startup, reconcile any '<name>_old' containers left behind by an update
    or rollback that was interrupted (e.g. docker-updater itself was restarted
    mid-operation). Local host only. Goal: never leave a managed service down or
    half-updated. The '_old' naming convention is the signal an op was in flight."""
    try:
        client = docker.from_env()
    except Exception as e:
        print(f"[recover] Docker unavailable, skipping recovery: {e}")
        return
    try:
        olds = [c for c in client.containers.list(all=True) if c.name.endswith("_old")]
    except Exception as e:
        print(f"[recover] Could not list containers: {e}")
        return
    if not olds:
        return

    now = datetime.datetime.utcnow()
    now_iso = now.isoformat() + "Z"
    with _state_lock:
        state = load_state()
    backup_enabled = state.get("backup_enabled", False)
    backup_hours   = int(state.get("backup_hours", 24))
    rollbacks      = state.setdefault("rollbacks", {})
    history        = state.setdefault("history", [])
    changed = False

    for old in olds:
        base = old.name[:-4]  # strip "_old"
        try:
            try:
                primary = client.containers.get(base)
            except docker.errors.NotFound:
                primary = None
            rb = rollbacks.get(base)
            rb_valid = bool(rb and rb.get("expires_at", "") > now_iso)

            if primary is not None and primary.status == "running":
                # New version is up and healthy.
                if rb_valid:
                    continue  # normal retained backup -- leave alone
                if backup_enabled:
                    rollbacks[base] = {
                        "backed_up_at": now_iso,
                        "expires_at": (now + datetime.timedelta(hours=backup_hours)).isoformat() + "Z",
                    }
                    changed = True
                    print(f"[recover] Adopted orphan backup '{old.name}' (primary running, no valid state entry)")
                else:
                    old.remove(force=True)
                    rollbacks.pop(base, None)
                    changed = True
                    print(f"[recover] Removed orphan backup '{old.name}' (retention off)")
                continue

            # Primary missing or not running: restore the known-good previous version.
            if primary is not None:
                print(f"[recover] Removing half-created/stopped '{base}' (status={primary.status})")
                try:
                    primary.remove(force=True)
                except Exception as e:
                    print(f"[recover] Could not remove '{base}': {e}; skipping")
                    continue
            old.rename(base)
            _restored = client.containers.get(base)
            try:
                _orig = rollbacks.get(base, {}).get("restart_policy", {"Name": "unless-stopped"})
                client.api.update_container(_restored.id, restart_policy=_orig)
            except Exception:
                pass
            _restored.start()
            rollbacks.pop(base, None)
            history.insert(0, {
                "container": base, "image": "↩ recovered",
                "updated_at": now_iso, "status": "success", "host_id": "local",
            })
            changed = True
            print(f"[recover] Restored '{base}' from an interrupted operation")
        except Exception as e:
            print(f"[recover] Failed to recover '{old.name}': {e}")

    if changed:
        state["history"] = history[:50]
        with _state_lock:
            save_state(state)
    print(f"[recover] Scan complete -- examined {len(olds)} backup container(s).")


if __name__ == "__main__":
    # Self-update helper mode — runs inside a temporary helper container that
    # was spawned by apply_update when docker-updater updated itself.
    # Must be checked before any other startup code.
    if os.environ.get("DOCKER_UPDATER_SELF_UPDATE_SPEC_B64"):
        _run_self_update_helper()
        sys.exit(0)

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

    _setup_ssh_config()
    _detect_own_container()

    print("[recover] Checking for interrupted operations...")
    recover_interrupted_operations()

    threading.Thread(target=check_for_updates, args=(False,), daemon=True).start()
    app.run(host="0.0.0.0", port=9090, debug=False, threaded=True)
