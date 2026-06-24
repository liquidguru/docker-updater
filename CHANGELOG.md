# Changelog

All notable changes to docker-updater are documented here.

## [1.10.4] — 2026-06-25

### Fixed
- **Data loss on update for containers with named volumes** — recreation only restored `HostConfig.Binds`, but Docker Compose stores named volumes in `HostConfig.Mounts` (where `Binds` is null). Updating a compose-managed container therefore recreated it with **no volume attached**, silently discarding its data — e.g. a Grafana update wiping every dashboard. Recreation now also rebuilds `HostConfig.Mounts` (named volumes, `--mount` entries), so volume data survives updates. Bind mounts (`docker run -v`) were unaffected. **If you run docker-updater on compose stacks, upgrade to this version before your next update.**

## [1.10.3] — 2026-06-22

### Fixed
- **Self-update only worked once, then stopped being detected** — detection compared the container hostname against the container ID. That matched on a fresh container, but the recreate step baked the *old* container's auto-generated hostname into the new one, so after the first successful self-update the hostname no longer matched the new container's ID and all later self-updates silently fell back to a normal (failed) update. Two-part fix: (1) detection now reads the real container ID from `/proc/self/mountinfo` (and also matches `Config.Hostname`), and (2) recreate no longer carries over an auto-generated 12-hex hostname, letting Docker assign a fresh matching one (user-set hostnames are still preserved). Works under both `docker run` and docker-compose (#10)

## [1.10.2] — 2026-06-21

### Fixed
- **Persistent false "Update Available" after v1.10.1** — multi-arch tags can publish a new index digest even when the current platform image is unchanged. docker-updater now compares the remote platform manifest's config digest with the local Docker image ID before flagging an update, so index-only churn no longer keeps images like `postgres:16-alpine` or `nextcloud:stable-apache` stuck as updateable.

## [1.10.1] — 2026-06-21

### Added
- **Version display** — the running version now appears next to the title in the header, linked to the changelog

### Fixed
- **Persistent false "Update Available"** — multi-arch images could be flagged as having an update forever, even immediately after pulling. The registry returns the *manifest list* digest while Docker sometimes stores the *platform-specific* digest locally; these never match by design. docker-updater now fetches the manifest list when the digests differ and treats the image as up to date if the local digest is one of the list's platform entries (closes #10)

### Added
- **Compose stack chip** — container cards now show a small **⬡ stackname** chip when the container was started by Docker Compose, read from the `com.docker.compose.project` label. No configuration needed; standalone containers show nothing extra (closes #9)

## [1.9.0] — 2026-06-12

### Added
- **Changelog source override** — for containers whose image lacks an `org.opencontainers.image.source` label, a *Set changelog source…* link now appears on update/deferred cards. Paste a GitHub repo URL; the *What's new?* button replaces it immediately and persists across restarts (closes #8)
- **GHCR auto-detection** — images hosted on `ghcr.io` (e.g. `ghcr.io/owner/repo`) automatically show *What's new?* with no configuration; the GitHub repo is derived directly from the image name
- **Pre-filled dialog for Docker Hub images** — when setting a changelog source manually, the input is pre-filled with a best-guess GitHub URL derived from the image name (`owner/image` → `github.com/owner/image`; official images like `redis` → `github.com/redis/redis`), with a hint to verify before saving

## [1.8.0] — 2026-06-11

### Added
- **Self-update** — docker-updater can now update its own container from the dashboard without manual intervention (closes #7). After pulling the new image, it spawns a short-lived helper container (from the new image) that handles the stop/recreate after the main process exits
- **Rollback for self-update** — the helper renames the old container to `_old` (same as any other update), verifies the new container started, and automatically rolls back to the previous version if it exits immediately
- History entry and optional rollback entry written to `state.json` when the data volume is mounted in the helper

## [1.7.0] — 2026-06-10

### Added
- **Safe backup restart policy** — backup containers (`{name}_old`) have their restart policy set to `no` immediately after creation; a host reboot no longer starts both the live container and its backup simultaneously. The original policy is saved in `state.json` and restored on rollback
- **`_old` containers hidden** — backup containers created by docker-updater are excluded from the update check list and all dashboard tabs

## [1.6.0] — 2026-06-09

### Added
- **Container log viewer** — a *Logs* button on every container card (and Backups tab card) opens the last 200 lines of `docker logs` in a modal, with a running/stopped status pill and a Refresh button
- **Persistent update logs** — the full log from every update and rollback is saved to `data/logs/{name}.log` and accessible via the *Log* button on each history row, even after a container restart
- **Docker client timeout raised to 300 s** — fixes false failures on large LSIO images that run `apt-get` on first boot (e.g. calibre-web), which previously timed out at 60–90 s and triggered a spurious auto-rollback

### Fixed
- Hosts tab form inputs (name, URL) clearing while typing during the poll interval (#6)

## [1.5.0] — 2026-06-08

### Added
- **Multi-arch image** — published for `linux/arm64` in addition to `linux/amd64`; runs on Raspberry Pi and other ARM boards (#4)

### Fixed
- SSH remote hosts failing with a "known_hosts" error on first connection (#5) — the first *Test Connection* click now automatically fetches and saves the remote host key (Trust On First Use via `ssh-keyscan`), stored in `data/known_hosts` and verified on all subsequent connections

## [1.4.0] — 2026-06-04 → 06

### Added
- **Backup retention** — opt-in setting (*Keep backup after successful update*, Settings tab) retains the previous container for a configurable window (default 24 h) so you can roll back even after a clean update
- **Backups tab** — lists active rollback points with *Rollback*, *Delete backup*, and *Logs* buttons; shows time remaining until expiry
- **Crash-safe startup recovery** — on boot, docker-updater reconciles any leftover `{name}_old` containers from interrupted operations: restores the previous version if the primary is down, or cleans up the backup if the new container is running
- Mobile tab bar scrollable on narrow screens

### Fixed
- Static IP addresses now preserved on container recreation (previously dropped)

## [1.3.0] — 2026-06-02 → 03

### Added
- **Multi-host support** — manage containers across multiple Docker hosts (SSH or TCP) from one dashboard; each host shows a connection health indicator and containers appear together with a host chip
- **Smart history icons** — recent updates show ✅ (success), ⚠️ (errored but container running), or ❌ (errored and stopped), each with a running/stopped dot; hover the icon to see the error message
- **Rollback safety net** — before recreating a container, the old one is renamed to `{name}_old`; if the new container fails to start it is removed and the previous version is automatically restored

## [1.2.0] — 2026-05-30

### Added
- **Published to GHCR** — image available at `ghcr.io/liquidguru/docker-updater:latest` via GitHub Actions; no clone required
- **GitHub webhook endpoint** — `POST /webhook/github` receives issue, PR, star, push, and release events and forwards them as push notifications
- **Auto-generated ntfy topic** — if `NOTIFY_URL` is not set, a unique private topic is generated on first run and shown in the dashboard with a Copy button
- Notifications only fire on the scheduled daily check, not on startup or manual *Check Now* runs

## [1.1.0] — 2026-05-23 → 24

### Added
- **Push notifications** — ntfy support via Apprise; configurable with any Apprise-compatible URL
- **Scheduled digest check** — daily cron at a configurable time and timezone (`CHECK_TIME`, `TIMEZONE`)
- **Changelog viewer** — *What's new?* button fetches the last 5 GitHub releases for any image with an `org.opencontainers.image.source` label
- Custom Docker whale favicon
- Log modal opens instantly on click and auto-reconnects if the page is refreshed during an update

### Fixed
- Port binding for containers on compose user-defined networks
- Container networking rewritten to match the Watchtower pattern — preserves ports, static IPs, aliases, and iptables setup on recreation

## [1.0.0] — 2026-05-22

### Added
- Initial release
- Registry digest polling via Docker Registry v2 manifest API — no unnecessary pulls
- Per-container update, defer (7/14/30/90 days or indefinitely), or skip
- Bulk update with checkbox selection
- Live streaming update log modal
- Persistent state (`data/state.json`) — history, deferred decisions, and last-check timestamps survive restarts
- Locally-built images (no `RepoDigests`) automatically skipped
- Multi-registry support: Docker Hub, GHCR, LinuxServer (`lscr.io`), and any Bearer-token registry
