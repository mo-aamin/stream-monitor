# Stream Monitor

[![Docker Hub](https://img.shields.io/badge/Docker_Hub-moamin1%2Fstream--monitor-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/moamin1/stream-monitor)
[![Platforms](https://img.shields.io/badge/platforms-linux%2Famd64%20%7C%20linux%2Farm64-informational)](https://hub.docker.com/r/moamin1/stream-monitor/tags)

**Self-hosted live-stream recorder for YouTube, Twitch, Kick and Rumble.**

Stream Monitor watches the channels you add and records their live streams
automatically with [yt-dlp](https://github.com/yt-dlp/yt-dlp). It runs as a
single Docker container — web UI, SQLite database, background monitor, and the
whole recording pipeline included. No cloud service, no external database, and
no Python, FFmpeg, Deno or yt-dlp to install on the host.

Recordings are written straight to a folder you control.

---

## Features

- **Set and forget** — add a channel once; every live stream is recorded as it starts.
- **Four platforms** — YouTube, Twitch, Kick and Rumble.
- **One container** — FFmpeg, Deno, yt-dlp and `curl-cffi` are bundled in the image.
- **Your files** — video recordings (MP4 by default) plus a plain SQLite database on your own disk.
- **Web UI** — dashboard, channels, recording history, per-recording logs and settings.
- **Multi-architecture** — runs natively on `linux/amd64` and `linux/arm64`.
- **Simple stack** — one container, one SQLite file, no message broker.

---

## Quick start

You need Docker with the Compose v2 plugin (`docker compose version`), and a
host that stays awake — a Linux server, NAS, or always-on mini PC. Docker
Desktop on a laptop works for testing, but it pauses when the machine sleeps.

### 1. Create the folders

```bash
sudo mkdir -p /opt/stream-monitor/data
sudo mkdir -p /opt/stream-monitor/recordings
```

### 2. Save the Compose file

Save this as `/opt/stream-monitor/docker-compose.yml`:

```yaml
services:
  stream-monitor:
    image: moamin1/stream-monitor:latest
    container_name: stream-monitor
    restart: unless-stopped
    environment:
      - TZ=America/Chicago
    ports:
      - "8080:8080"
    volumes:
      - /opt/stream-monitor/data:/data
      - /opt/stream-monitor/recordings:/recordings
```

### 3. Start it

```bash
cd /opt/stream-monitor
sudo docker compose up -d
```

### 4. Open the web UI

Browse to **`http://<server-ip>:8080`**, open **Channels**, and add a channel
name, `@handle`, or URL. The first live check runs immediately.

> **Port already in use?** Change the left side of the mapping, for example
> `"8000:8080"`, then run `sudo docker compose up -d` again.
>
> **Firewall?** Allow the port if needed, e.g. `sudo ufw allow 8080/tcp`.

---

## Updating the image

New images are published to Docker Hub. To pick up the latest one:

```bash
cd /opt/stream-monitor
sudo docker compose pull
sudo docker compose up -d
```

That is the whole update. `pull` downloads the new image and `up -d` recreates
the container only if the image changed. Your `data` and `recordings` folders
are bind mounts, so they are untouched.

> `docker compose restart` and `docker compose up -d` **without** `pull` will
> keep running the old image. Always pull first.
>
> Recreating the container stops it. If a recording is in progress, allow the
> shutdown grace period to finish muxing — or update when nothing is recording.

Verify what is running:

```bash
sudo docker compose images
curl -fsS http://localhost:8080/healthz
```

The `/healthz` response reports the application version and monitor state. The
version string only changes between releases; the image digest is the reliable
way to confirm a new build.

---

## Where your data lives

| Host path | Container path | Contents |
| --- | --- | --- |
| `/opt/stream-monitor/data` | `/data` | SQLite database, application log, per-recording yt-dlp logs |
| `/opt/stream-monitor/recordings` | `/recordings` | Recorded video files |

**Backups:** stop the container before copying `data` (or use SQLite's online
backup). The `recordings` folder can be copied at any time; an active file may
end in `.part`.

To reset everything, stop the container and remove both folders. The database
is recreated on the next start.

---

## Configuration

Environment variables in the Compose file:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `UTC` | Timezone for UI timestamps and recording filenames. |
| `PUID` / `PGID` | `1000` | Runtime user/group for the mounted folders. |
| `MONITOR_AUTOSTART` | `true` | Start the monitor loop when the container starts. |
| `LOG_LEVEL` | `INFO` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `DATA_DIR` | `/data` | Database and log location inside the container. |
| `RECORDINGS_DIR` | `/recordings` | Recording location inside the container. |

Everything else — yt-dlp options, cookies file, proxy, check interval, filename
template, format selector — is configured in the web UI under **Settings**.

### Permissions

The container drops privileges to `PUID:PGID` (default `1000:1000`). If the app
cannot write to `/opt/stream-monitor`, add these lines to the service's
`environment` list and use your host user's IDs:

```yaml
    environment:
      - TZ=America/Chicago
      - PUID=1000
      - PGID=1000
```

Use `id -u` and `id -g` on the host to find your values, then
`sudo chown -R <PUID>:<PGID> /opt/stream-monitor` if needed.

---

## Platform notes

| Platform | What you can add | Notes |
| --- | --- | --- |
| **YouTube** | `@handle`, `UC…` channel ID, or URL | Deno and yt-dlp's EJS support are bundled. Cookies help if YouTube asks for sign-in or a bot check. |
| **Twitch** | Channel name or URL | Browser impersonation is enabled by default; cookies help with subscriber-only streams. |
| **Kick** | Channel name or URL | Requires browser impersonation, enabled by default and backed by `curl-cffi`. |
| **Rumble** | Channel name, `c/name`, `user/name`, or URL | Live streams are resolved from the channel's `/live` page; a direct live video URL also works. |

Recordings are **not** playable or downloadable through the web UI. Browse the
`recordings` folder on the host, or point it at a NAS mount.

Optional: mount a Netscape-format `cookies.txt` read-only and set its path in
**Settings → Cookies file**.

---

## Security

**Stream Monitor has no built-in authentication.** Every page and API endpoint
is open to anyone who can reach the port. Run it on a trusted LAN, behind a VPN,
or behind an authenticating TLS reverse proxy. Do not expose it directly to the
internet.

The app only accepts channel URLs for the four supported platforms, and file
deletion is confined to the mounted recordings directory.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Container keeps restarting | `sudo docker compose logs --tail=100 stream-monitor` |
| Page does not load | Port mapping, host firewall, and `sudo docker compose ps` |
| `permission denied` in logs | Set `PUID`/`PGID` to the folder owner and `chown` the host folders |
| A channel always errors | Open its recording log; check the tool paths in **Settings → Tools** |
| YouTube bot check | Add a cookies file in Settings |
| Kick/Rumble returns 403 | Try cookies or a proxy in Settings |
| New image did not apply | Run `pull` then `up -d`; `restart` alone does not update |
| Database is locked | Run only one container against a given `data` folder |

Health endpoint: `GET /healthz` on port `8080` inside the container.

---

## Image tags

| Tag | Source | Architecture |
| --- | --- | --- |
| `latest` | `main` branch | `linux/amd64`, `linux/arm64` |
| `dev` | `dev` branch — preview build | `linux/amd64` |
| `sha-<short>` | Every build, immutable | Depends on source branch |
| `1.0`, `1.2.0`, … | Git release tags (`v1.0`, `v1.2.0`) | `linux/amd64`, `linux/arm64` |

Use `latest` for normal deployments. For reproducible installs, pin a version
tag, a `sha-…` tag, or a digest instead of `latest`.

---

Built with FastAPI, SQLite, yt-dlp, FFmpeg and Deno.
