# docker-updater

A lightweight self-hosted web UI for managing Docker container updates — a manual-approval alternative to Watchtower.

Instead of automatically pulling and restarting containers the moment a new image is published, docker-updater polls your registries on a schedule, shows you what's available, and lets you decide when (or whether) to update each container. You can also view release changelogs from GitHub before committing to an update.

---

![docker-updater dashboard](screenshot.jpeg)

## Features

- **Registry polling** — compares local image digests against the registry without pulling, using the Docker Registry v2 manifest API (`HEAD` + `Docker-Content-Digest`)
- **Multi-registry support** — Docker Hub, GHCR (`ghcr.io`), LinuxServer (`lscr.io`), and any registry that implements the Bearer token challenge
- **Per-container control** — update individually, defer for 7/14/30/90 days or indefinitely, or un-defer at any time
- **Bulk updates** — select multiple containers and update them all at once
- **Changelog viewer** — fetches the last 5 GitHub Releases for any image that publishes an `org.opencontainers.image.source` label
- **Live update log** — streaming log modal shows pull progress and recreation status in real time; auto-reconnects if you refresh the page mid-update
- **Push notifications** — ntfy, Pushover, Discord, Slack (via Apprise) when scheduled check finds updates; silent on startup and manual checks
- **Scheduled checks** — cron-style daily check at a configurable time and timezone
- **Safe recreation** — recreates containers using the Python Docker SDK (Watchtower pattern), preserving all original config: volumes, ports, environment variables, networks, restart policy, capabilities, etc.
- **Locally-built images skipped** — containers with no `RepoDigests` (built from local Dockerfiles) are automatically ignored
- **Persistent state** — update history, deferred decisions, and last-check timestamps survive container restarts
- **Dark UI** — tabbed dashboard: Updates / Deferred / Up to Date / Unchecked / All

---

## Requirements

- Docker with access to `/var/run/docker.sock`
- Works on Synology DSM, Unraid, Proxmox, or any Linux host running Docker

---

## Quick start

```bash
mkdir docker-updater && cd docker-updater
mkdir -p data

docker run -d \
  --name docker-updater \
  --restart unless-stopped \
  -p 9292:9090 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/data:/app/data \
  -e CHECK_TIME=03:00 \
  -e TIMEZONE=Australia/Melbourne \
  ghcr.io/liquidguru/docker-updater:latest
```

Then open `http://<your-host>:9292` in your browser.

---

## docker-compose.yml

```yaml
services:
  docker-updater:
    image: ghcr.io/liquidguru/docker-updater:latest
    container_name: docker-updater
    restart: unless-stopped
    ports:
      - "9292:9090"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/app/data
    environment:
      - CHECK_TIME=03:00
      - TIMEZONE=Australia/Melbourne
      - NOTIFY_URL=ntfy://ntfy.sh/your-topic   # optional
      - DOCKER_HOST=unix:///var/run/docker.sock
```

Save as `docker-compose.yml`, create a `data/` directory alongside it, then run `docker compose up -d`. No clone required.

> **Port note:** The container listens internally on port 9090. The host binding `9292:9090` avoids clashing with Prometheus, which commonly uses 9090. Change it to whatever suits your setup.

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CHECK_TIME` | `03:00` | Time of day to run the scheduled digest check (HH:MM) |
| `TIMEZONE` | `Australia/Melbourne` | Timezone for the scheduled check — any [tz database name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) |
| `NOTIFY_URL` | *(empty)* | [Apprise URL](https://github.com/caronc/apprise/wiki) for push notifications — e.g. `ntfy://ntfy.sh/my-topic`, `discord://...`, `slack://...` |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker socket path |

Push notifications are **only sent by the scheduled check** when updates are found. Manual "Check Now" and startup scans update the UI silently.

---

## How it works

1. On startup (silently) and at the configured `CHECK_TIME`, docker-updater iterates all running containers
2. For each container it extracts the local image digest from `RepoDigests`
3. It sends a `HEAD` request to the registry for the image's manifest, reading the `Docker-Content-Digest` response header — no image data is transferred
4. If the digests differ, the container is flagged as having an update available
5. When you click **Update**, the app:
   - Pulls the new image (streaming progress to the log modal)
   - Stops and removes the old container
   - Recreates it with identical config using the Docker SDK low-level API (Watchtower pattern)
   - Reconnects all networks via `NetworkConnect` to ensure correct port binding and iptables setup
   - Starts the new container

Container state (update availability, defer decisions, history) is persisted to `data/state.json`.

---

## Changelog viewer

For containers whose image was built with an `org.opencontainers.image.source` label pointing to a GitHub repository, a **What's new?** link appears in the update card. Clicking it fetches the last 5 releases from the GitHub Releases API and renders them inline with basic markdown formatting.

This works out of the box for most images maintained by projects that publish GitHub Releases (Home Assistant, Homarr, Vaultwarden, Calibre-Web, and many LinuxServer images).

---

## Building from source

If you want to hack on it or run the latest uncommitted changes:

```bash
git clone https://github.com/liquidguru/docker-updater.git
cd docker-updater
mkdir -p data
docker compose up -d   # uses the build: . compose file in the repo
```

---

## Replacing Watchtower

If you have Watchtower running, stop it after confirming docker-updater is working:

```bash
docker stop watchtower
docker rm watchtower
```

---

## Caveats

- **docker compose stacks**: Updates recreate individual containers using the Docker SDK. The container's `docker-compose.yml` is not modified — if you later run `docker compose up` it will see the new image and behave correctly, but the compose file's image tag won't be changed.
- **Named volumes**: Preserved automatically — volume mounts are read from the container's `HostConfig.Binds` and reattached on recreation.
- **Locally-built images**: Any container whose image has no `RepoDigests` is skipped (these can't be compared against a registry).
- **Private registries**: Currently supports anonymous and Bearer-token registries. Basic auth (username/password) registries are not yet supported.
- **Breaking changes in new versions**: docker-updater preserves the environment variables your container was running with, but cannot detect when a new image version introduces new required environment variables. If an update fails with an application-level error after recreation, check the image's release notes for new required env vars.
- **host network mode**: Containers using `--network host` are recreated correctly; the network reconnect step is skipped for these.

---

## License

MIT
