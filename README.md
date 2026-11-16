# Podimo

Download Podimo podcast episodes and serve them as private RSS feeds.

> A valid Podimo subscription is required to access premium content.

This is a rebuild of [luca-patrignani/podimo](https://github.com/luca-patrignani/podimo) and [thijsraymakers/podimo](https://github.com/thijsraymakers/podimo) in the style of [pasjonsfrukt](https://github.com/terjefl/pasjonsfrukt): episodes are downloaded and stored locally rather than proxied on-demand, so your feeds keep working even if Podimo removes a show or changes their CDN.

---

### Docker Compose

The recommended way to run podimo is with Docker Compose.

**`compose.yml`**
```yaml
services:
  podimo:
    image: ghcr.io/terjefl/podimo:latest
    container_name: podimo
    restart: unless-stopped
    ports:
      - "8200:8000"
    environment:
      - TZ=Europe/Oslo
      - PODIMO_MODE=web
    volumes:
      - /path/to/config.yaml:/app/config.yaml:ro
      - /path/to/podcast/files:/app/yield
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s

  podimo-harvest:
    image: ghcr.io/terjefl/podimo:latest
    container_name: podimo-harvest
    restart: unless-stopped
    environment:
      - TZ=Europe/Oslo
      - PODIMO_MODE=harvest
    volumes:
      - /path/to/config.yaml:/app/config.yaml:ro
      - /path/to/crontab:/etc/cron.d/podimo-crontab:ro
      - /path/to/podcast/files:/app/yield
      - /path/on/local/disk/podimo-staging:/app/staging
```

The image runs in one of two modes, selected with `PODIMO_MODE`:

- `web` (default): only the HTTP server. No crontab and no cron daemon.
- `harvest`: only cron with the mounted crontab, no server.

Run them as two containers from the same image, as above, so the bulk writes of a harvest to the share do not share a process, cgroup or I/O queue with the server. Downloads go to `STAGING_DIR` (default `/app/staging`; bind-mount local disk there) and are then copied to `yield_dir` as `<name>.part`, fsynced and renamed to the final name, so clients never see a half-written file on the share. Staging files and `.part` files older than an hour are removed when a harvest starts.

The crontab controls when harvesting runs. It is installed with `crontab` inside the harvest container, so use the user-crontab format (no user column). Avoid starting on the hour, when other jobs (e.g. Plex analysis) often read the same share. Example:

```cron
17 */4 * * *   /usr/local/bin/podimo harvest    >> /var/log/podimo.log 2>&1
```

The image sets `PODIMO_CONFIG_FILE=/app/config.yaml`, so `podimo` finds the config regardless of the working directory cron starts from. Outside Docker, either run from the directory containing `config.yaml`, pass `--config-file`, or set `PODIMO_CONFIG_FILE`.

Podimo serves most of its own episodes as HLS streams (`.m3u8`) rather than MP3 files. These are fetched with `ffmpeg` (bundled in the image) and transcoded to MP3, so `ffmpeg` must be on `PATH` when running outside Docker.

---

### Configuration

Copy [`config.template.yaml`](config.template.yaml) to `config.yaml` and fill in your details.

```yaml
host: "https://your-domain-here"
yield_dir: "yield"

auth:
  email: "your@podimo.com"
  password: "yourpassword"
  region: "no"
  locale: "no-NO"

podcasts:
  min-podcast:
    podcast_id: "uuid-from-podimo-url"
    most_recent_episodes_limit: 100
```

#### `host`

The public base URL of your instance. Used to build links in RSS feeds and on the index page — must be reachable by your podcast app.

#### `yield_dir`

Directory where downloaded MP3 files and generated RSS feeds are stored. Defaults to `"yield"`. A relative path is resolved against the directory containing the config file.

#### `secret`

Adds a `?secret=<value>` query parameter requirement on all endpoints. All clients share the same secret.

```yaml
secret: "my-shared-secret"
```

#### `users`

Per-user secrets for multi-user setups. Each user gets their own private RSS feeds with their secret embedded in episode URLs. MP3 files are not duplicated; the secret is only a URL parameter.

```yaml
users:
  - alias: "alice"
    secret: "alice-secret"
  - alias: "bob"
    secret: "bob-secret"
```

When `users` is configured it takes precedence over `secret`. After adding or changing users, run `podimo sync` to regenerate all feed files.

> **Migrating from `secret` to `users`:** The existing `<feed_name>.xml` files on disk become orphaned — the server now looks for `<feed_name>-<alias>.xml` instead. Run `podimo sync` to generate the new per-user feed files, then remove the old ones manually.

#### `disable_index`

Set to `true` to disable the HTML index page. `GET /` will return 404.

```yaml
disable_index: true
```

#### `auth`

Podimo account credentials and region.

```yaml
auth:
  email: "your@podimo.com"
  password: "yourpassword"
  region: "no"    # no, nl, de, dk, es, latam, en, mx, fi, uk
  locale: "no-NO" # no-NO, nl-NL, de-DE, da-DK, es-ES, en-US, es-MX, fi-FI, en-GB
```

#### `api`

```yaml
api:
  max_concurrent_downloads: 3   # Max simultaneous episode downloads (default: 3)
  token_cache_time: 432000      # Auth token lifetime in seconds (default: 5 days)
  inter_podcast_delay: 10       # Seconds to wait between podcasts during harvest (default: 10)
  harvest_retry_delay: 30       # Seconds before the one retry of a podcast whose harvest failed (default: 30)
  mp3_bitrate: "128k"           # MP3 bitrate used when transcoding HLS streams (default: 128k)
  health_max_harvest_age: 28800 # /status returns 503 if the last harvest is older than this, in seconds (default: 8h)
```

#### `podcasts`

A map of slugs to per-podcast settings. The slug is chosen by you and used in the URL and as the folder name on disk.

```yaml
podcasts:
  my-show:
    podcast_id: "abc123-..."    # Required — Podimo UUID (see below)
    feed_name: "feed"           # Base filename for RSS XML (default: "feed")
    most_recent_episodes_limit: 100  # Only harvest N most recent (default: no limit)
```

**Finding the `podcast_id`:** Open the podcast on [podimo.com](https://podimo.com) — the UUID at the end of the URL is the `podcast_id`. For example:
```
https://podimo.com/shows/abc12345-1234-1234-1234-abcdef123456
                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                         this is the podcast_id
```

`feed_name` only affects the filename on disk — the URL is always `GET /{slug}` regardless.
- Single-secret / no-auth: `yield/{slug}/{feed_name}.xml`
- Multi-user: `yield/{slug}/{feed_name}-{alias}.xml` per user

---

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | HTML index of all configured podcasts |
| `GET /{slug}` | RSS feed for a podcast |
| `GET /{slug}/{episode_id}` | Episode audio file |
| `GET /health` | Unauthenticated liveness check (JSON), always 200 while the process answers |
| `GET /status` | Unauthenticated deep check (JSON), 200 or 503, cached for 60 s |

**With `secret`:** append `?secret=<value>` to all requests.

**With `users`:** append `?secret=<user-secret>` to all requests. The RSS feed contains episode URLs pre-populated with that user's secret, so podcast apps fetch audio correctly without extra configuration.

#### Health check

`GET /health` is the liveness check for the Docker healthcheck and monitoring (InfraDash, Uptime Kuma). It needs no secret, does no file I/O and has no dependencies, so it returns `200` with `{"status": "ok", "version": ..., "uptime_seconds": ...}` as long as the process answers, also when the share with `yield_dir` is slow or missing.

`GET /status` is the deep check, also without a secret. It returns `200` with `{"status": "ok", ...}` when every podcast has its feed file(s) and the last full `podimo harvest` finished within `api.health_max_harvest_age` seconds (default 8 hours, i.e. two missed 4-hourly cron runs) without a persistent failure. Otherwise it returns `503` with the reasons listed in `problems`. A podcast that fails is retried once during the harvest (after `api.harvest_retry_delay` seconds); if it still fails, but did not fail in the previous full harvest, `/status` stays `200` and lists it under `warnings`. A podcast that fails in two full harvests in a row gives `503`. The response also carries `last_harvest`, `last_harvest_age_seconds` and `last_harvest_duration_seconds`, and never includes secrets. The file lookups run in a thread, the result is cached for 60 seconds, and a lookup that takes more than 10 seconds gives `503` instead of hanging.

`podimo serve` passes extra options to uvicorn, including `--workers N`. Each worker reads the same config file (through `PODIMO_CONFIG_FILE`, which `serve` sets), and secrets are redacted from the access log in every worker.

The harvest status is written to `<yield_dir>/.harvest-status.json` at the end of every full harvest (runs limited to specific slugs do not update it).

#### Podcast index page

The index page (`GET /`) lists all configured podcasts as cards with direct links to subscribe in Overcast or Pocket Casts. Feed URLs include the requesting user's secret automatically. Can be disabled with `disable_index: true`.

---

### CLI commands

```sh
podimo harvest [SLUG...]   # Download new episodes from Podimo
podimo sync [SLUG...]      # Regenerate RSS feed files from downloaded episodes
podimo serve               # Start the HTTP server
podimo config              # Print the parsed configuration
```

All commands accept `--config-file` / `-c` to specify an alternative config file (default: `$PODIMO_CONFIG_FILE`, else `config.yaml`).

`podimo harvest` continues with the next podcast if one fails, and exits with status 1 at the end if any podcast failed. All log lines are prefixed with a timestamp.

---

### Development

#### Formatting

```sh
poetry run black podimo
```
