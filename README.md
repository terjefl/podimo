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
    volumes:
      - /path/to/config.yaml:/app/config.yaml:ro
      - /path/to/crontab:/etc/cron.d/podimo-crontab:ro
      - /path/to/podcast/files:/app/yield
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/openapi.json || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 15s
```

The crontab controls when harvesting runs. Example:

```cron
0 4 * * * root podimo harvest >> /var/log/podimo.log 2>&1
```

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

Directory where downloaded MP3 files and generated RSS feeds are stored. Defaults to `"yield"`.

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

**With `secret`:** append `?secret=<value>` to all requests.

**With `users`:** append `?secret=<user-secret>` to all requests. The RSS feed contains episode URLs pre-populated with that user's secret, so podcast apps fetch audio correctly without extra configuration.

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

All commands accept `--config-file` / `-c` to specify an alternative config file.

---

### Development

#### Formatting

```sh
poetry run black podimo
```
