# Changelog

All notable changes to docker-updater are documented here.

## [1.14.0] — 2026-07-21

### Added
- **Bilingual documentation** — added a complete Simplified Chinese README with reciprocal language links and synchronized setup, authentication, language, and deployment guidance
- **Chinese / English UI** — localized dashboard, settings, cards, dialogs, login, and empty states. Auto follows the browser; Settings → Language can force English or 中文. The effective language also controls scheduled and GitHub push-notification wording
- **Custom dialogs** — replaced browser `alert`/`confirm` with theme-aware, keyboard-accessible project modals
- **Optional login** — set `AUTH_USERNAME` and `AUTH_PASSWORD` to require sign-in with a 7-day session; open access remains the default when both are unset

### Fixed
- **Single-source translations** — Flask now injects `static/i18n_messages.json` into both UI templates, removing the duplicated client catalog and its manual synchronization requirement
- **Authentication setup guidance** — document `.env` substitution versus container environment variables, required container recreation, safe verification commands, and Synology deployment steps
- **Complete Docker image assets** — copy `static/` into the image so favicon and i18n files are available in image-only deployments

## [1.13.0] — 2026-07-15

### Added
- **Theme picker** — a new **Appearance** section in Settings with six themes: GitHub Dark (default), Midnight, Nord, Dracula, Carbon, and **Light**. Applies instantly, and is saved to `state.json` so it persists across restarts and image updates. Rendered server-side, so the page never flashes the previous theme on load. Status colours keep their meaning in every theme — green is always good, red is always a problem — and are re-tuned per theme for contrast (Light uses darker greens/reds so they stay legible on a pale background)
- **Configurable check schedule in Settings** — the check frequency is no longer locked to a daily check set only via an environment variable. A new **Check Schedule** section in Settings offers presets (every 6 hours, every 12 hours, daily, weekly, monthly) with a time picker, plus day-of-week for weekly and day-of-month for monthly — no cron knowledge required. Changes apply immediately to the running scheduler; no container restart needed, and the next scheduled run is shown so it's clear what's in effect (closes #14)
- **Custom cron expressions** — for finer control, the schedule can be set to a standard 5-field cron expression (e.g. `0 3 * * 0` for weekly on Sunday, `0 */6 * * *` for every 6 hours), from the same Settings section or via `CHECK_TIME`
- **`CHECK_TIME` also accepts a cron expression** — previously `HH:MM` only. Plain `HH:MM` still works unchanged, so existing setups need no config change

### Changed
- **Schedule now persists in `state.json`** — a schedule chosen in Settings is stored on the data volume and survives container restarts, recreation, and docker-updater updating itself to a new image. `CHECK_TIME` becomes the *initial default* for a fresh install: once a schedule is saved from the UI, that takes precedence. The startup log now reports the active schedule, where it came from (`settings` or `env`), and the next run time, so there's no ambiguity if the two ever disagree

### Fixed
- An unparsable schedule now falls back to the 03:00 daily default instead of preventing startup; an invalid cron submitted from Settings is rejected with a clear error and leaves the existing schedule untouched

### Internal
- The UI's translucent tints (status badges, banners, chips) were hardcoded `rgba()` values baked to the dark palette's exact RGB. They now derive from the theme's own colours via `color-mix()`, so a theme only needs to define 11 variables. This also fixed three colours that would have been unreadable in Light mode — the update log and changelog body text were a fixed pale grey, and the Compose stack chip a fixed bright purple

## [1.12.3] — 2026-07-14

### Fixed
- **Reclaimable images list not full-width** — the list sat inside a flex row (`.backup-options`) alongside the "Show reclaimable images" button, and flex items don't stretch to fill their container by default — so the list stayed narrower than the rest of the Settings panel, leaving dead space on the right. Made this card's container stack vertically and stretch its children to full width (#13, thanks @monkeyotg)

## [1.12.2] — 2026-07-13

### Fixed
- **Reclaimable images list layout** — the repo name, size, and age columns were crammed together with no fixed alignment, and a long repo name could force horizontal overflow that threw off where the list's scrollbar landed. Rebuilt as a proper grid with fixed-width columns for the ID/size/age and a truncating (ellipsis + hover tooltip) column for the repo name, with `overflow-x: hidden` on the scroll container so the vertical scrollbar stays cleanly on the right edge (#13, thanks @monkeyotg)

## [1.12.1] — 2026-07-12

### Added
- **Repository name shown for reclaimable images** — the "Show reclaimable images" list now displays each image's repo (e.g. `ghcr.io/home-assistant/home-assistant`) instead of just a bare hash. Docker drops the human-readable `:tag` once an image is superseded, but the `RepoDigests` entry recorded at pull time usually survives, so the repo name can still be shown (same technique Portainer uses). Falls back to "unknown image" if no digest reference remains (closes #13 follow-up, thanks @monkeyotg)

## [1.12.0] — 2026-07-11

### Added
- **Remove the old image after an update** — new opt-in Settings toggle (default off). After a successful update, the superseded image is removed to reclaim disk space. Backup-aware by design: while "Keep backup after successful update" is on, the old image is left alone (the `_old` container still references it) and is only removed when that backup is later deleted or expires — the image cleanup hooks into both the manual "Delete backup" action and the automatic expiry sweep, so retention users still get the space back eventually, just later
- **"Show reclaimable images" list** (Settings) — lists dangling (untagged) images with size and age, so you can see exactly what's being removed instead of a black-box prune. Select individual images or "Select all", then "Delete selected". Runs as a background job with live progress (`Working — X/Y…`), so a large "select all" batch of many multi-GB images can't be cut short by a reverse-proxy timeout. Removes images one at a time by ID rather than a blanket `docker image prune`, which as a side benefit avoids colliding with the NAS's own background image cleanup (no more "a prune operation is already running")
- **Named skip reasons** — if an image can't be removed because it's still referenced, the result names the actual blocking container (e.g. *"still used by stopped container 'dispatcharr-redis'"*) instead of a generic error, so it's obvious what to clean up first
- Deliberately **images only** — no volume pruning, and no removal of images still in use by any container or a kept backup (closes #13)

## [1.11.0] — 2026-06-26

### Added
- **Restart the rest of the Compose stack after an update** — new opt-in Settings toggle (default off). When a container started by Docker Compose is updated, its other stack members (same `com.docker.compose.project`) are **restarted** so they pick up the recreated container's new IP/DNS — handy for services that cache a connection to a sibling and would otherwise need a manual `docker compose restart`. Siblings are restarted, not recreated (no image change). Bulk updates within one stack are debounced so the untouched members restart only once, after the updates settle; the updated containers, `_old` backups, docker-updater itself, and any member still mid-update are excluded (closes #12)

## [1.10.4] — 2026-06-25

### Fixed
- **Data loss on update for containers with named volumes** — recreation only restored `HostConfig.Binds`, but Docker Compose stores named volumes in `HostConfig.Mounts` (where `Binds` is null). Updating a compose-managed container therefore recreated it with **no volume attached**, silently discarding its data — e.g. a Grafana update wiping every dashboard. Recreation now also rebuilds `HostConfig.Mounts` (named volumes, `--mount` entries), so volume data survives updates. Bind mounts (`docker run -v`) were unaffected. **If you run docker-updater on compose stacks, upgrade to this version before your next update.** (closes #11)

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
