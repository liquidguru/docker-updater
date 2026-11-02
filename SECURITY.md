# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Include the version you are running (shown in the dashboard header), what you found, and how to reproduce it.

This is a one-person project. I aim to reply within a few days and to fix confirmed problems in the next release, crediting you in the changelog unless you would rather stay anonymous.

## Supported versions

Only the latest release (the `:latest` image) receives fixes.

## Things worth knowing when you deploy it

- **Docker socket access is root-equivalent.** docker-updater needs `/var/run/docker.sock` to update containers, so anything that can use the dashboard can control every container on that host. Treat access to it accordingly.
- **Login is optional and off by default.** Set both `AUTH_USERNAME` and `AUTH_PASSWORD` before exposing it beyond a network you trust, and put it behind HTTPS. See [Authentication](README.md#authentication).
- **The optional `API_KEY`** lets its holder read `GET /api/status` only, which lists every container name, image and host. Keep it secret and rotate it by changing the variable and recreating the container.
- **Changing the password does not end existing sessions.** To invalidate them, rotate `FLASK_SECRET_KEY` too.
- Remote hosts are reached over SSH or TCP with credentials you provide; keep those hosts' access limited to what the dashboard needs.
