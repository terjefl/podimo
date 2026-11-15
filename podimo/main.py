import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Optional

import aiohttp
import cloudscraper
from rfeed import Feed, Image, Item, Guid, Enclosure, iTunes, iTunesItem

from .client import PodimoClient
from .config import Config, User
from .utils import async_wrap
from .utils import token_key


def log(msg: str):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --- Token cache (JSON files in yield_dir/.tokens/) ---

def _token_path(config: Config, key: str) -> Path:
    return Path(config.yield_dir) / ".tokens" / f"{key}.json"


def load_cached_token(key: str, config: Config) -> Optional[str]:
    path = _token_path(config, key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("expires", 0) > time():
            return data["token"]
    except Exception:
        pass
    return None


def save_token(key: str, token: str, config: Config):
    path = _token_path(config, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "token": token,
        "expires": time() + config.api.token_cache_time,
    }))


# --- Podimo client context manager ---

@contextlib.asynccontextmanager
async def get_podimo_client(config: Config):
    key = token_key(config.auth.email, config.auth.password)
    client = PodimoClient(
        config.auth.email,
        config.auth.password,
        config.auth.region,
        config.auth.locale,
    )
    client.scraper = cloudscraper.create_scraper()
    client.token = load_cached_token(key, config)
    if not client.token:
        log("[INFO] Authenticating with Podimo...")
        await client.podimoLogin()
        save_token(key, client.token, config)
        log("[INFO] Authentication successful, token cached")
    yield client


# --- Harvest status (read by GET /health) ---

def harvest_status_path(config: Config) -> Path:
    return Path(config.yield_dir) / ".harvest-status.json"


def as_int(value, default: int) -> int:
    """int(value), or default for anything that is not a number (hand-edited or corrupt status file)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def write_harvest_status(config: Config, started: datetime, failed: list[str], error: Optional[str] = None):
    """
    Record the outcome of a full harvest for GET /health.

    ``failed_streak`` counts, per failed podcast, how many full harvests in a row it
    has failed: a slug that also failed in the previous run gets that run's streak + 1,
    otherwise 1. /health only reports a podcast as down from a streak of 2.
    """
    previous = read_harvest_status(config) or {}
    previous_failed = previous.get("failed") or []
    previous_streak = previous.get("failed_streak")
    if not isinstance(previous_streak, dict):
        # Status file from before streaks were recorded: its failures count as one run.
        previous_streak = {slug: 1 for slug in previous_failed}
    failed_streak = {
        slug: (as_int(previous_streak.get(slug, 1), default=1) + 1 if slug in previous_failed else 1)
        for slug in failed
    }
    finished = datetime.now(timezone.utc)
    status = {
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "duration_seconds": int((finished - started).total_seconds()),
        "failed": failed,
        "failed_streak": failed_streak,
        "previous_failed": previous_failed,
        "error": error,
    }
    write_text_atomic(harvest_status_path(config), json.dumps(status, indent=2))


def read_harvest_status(config: Config) -> Optional[dict]:
    try:
        return json.loads(harvest_status_path(config).read_text())
    except (FileNotFoundError, ValueError):
        return None


# --- Path helpers ---

def build_podcast_dir(config: Config, slug: str) -> Path:
    return Path(config.yield_dir) / slug


def build_podcast_feed_path(config: Config, slug: str, alias: Optional[str] = None) -> Path:
    base = config.podcasts[slug].feed_name
    filename = f"{base}-{alias}.xml" if alias else f"{base}.xml"
    return build_podcast_dir(config, slug) / filename


def build_podcast_episode_file_path(config: Config, slug: str, episode_id: str) -> Path:
    return build_podcast_dir(config, slug) / f"{episode_id}.mp3"


def get_secret_query_parameter(config: Config) -> str:
    if config.secret is None:
        return ""
    return f"?secret={config.secret}"


def harvested_episode_ids(config: Config, slug: str) -> list[str]:
    podcast_dir = build_podcast_dir(config, slug)
    if not podcast_dir.is_dir():
        return []
    ids = []
    for f in podcast_dir.iterdir():
        if f.is_file() and f.suffix == ".mp3" and not f.stem.endswith("_interim"):
            ids.append(f.stem)
    return ids


# --- Audio URL extraction ---

def extract_audio_url(episode: dict) -> tuple[Optional[str], int]:
    url = None
    duration = 0
    if episode.get("audio"):
        url = episode["audio"].get("url")
        duration = episode["audio"].get("duration", 0) or 0
    if not url and episode.get("streamMedia"):
        url = episode["streamMedia"].get("url")
        duration = episode["streamMedia"].get("duration", 0) or 0
    return url, duration


def is_hls_url(url: str) -> bool:
    return url.split("?", 1)[0].lower().endswith(".m3u8")


# --- Staging and atomic publishing ---
#
# Downloads are written to a local staging directory first and only then copied to
# yield_dir (usually a CIFS/SMB share): copy to <name>.part, fsync, os.replace to the
# final name. Clients never see a half-written file on the share, and the share gets
# short bursts instead of a long, live write stream.

STAGING_ENV = "STAGING_DIR"
DEFAULT_STAGING_DIR = "/app/staging"  # bind-mounted from local disk in Docker
STALE_AGE_S = 3600  # staging/.part files older than this are left over from a crashed run


def staging_dir() -> Path:
    """The local staging directory; falls back to the temp dir outside Docker."""
    for candidate in (os.environ.get(STAGING_ENV), DEFAULT_STAGING_DIR):
        if not candidate:
            continue
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    path = Path(tempfile.gettempdir()) / "podimo-staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def staging_path_for(final: Path) -> Path:
    """Where a download for `final` is written before it is published."""
    return staging_dir() / f"{final.parent.name}__{final.name}"


def _fsync_dir(path: Path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # not supported on every file system (e.g. some CIFS mounts)
    finally:
        os.close(fd)


def publish_file(src: Path, final: Path, move: bool = True):
    """Copy `src` to `final` atomically: <final>.part, fsync, os.replace. Removes `src` when `move`."""
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    try:
        with src.open("rb") as fin, part.open("wb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    _fsync_dir(final.parent)
    if move:
        src.unlink(missing_ok=True)


def write_text_atomic(final: Path, text: str):
    """Write a small text file (feed, status) so readers see either the old or the new version."""
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    try:
        with part.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def remove_stale(directory: Path, pattern: str, max_age_s: int = STALE_AGE_S) -> list[Path]:
    """Delete files matching `pattern` older than `max_age_s` (newer ones may belong to a running harvest)."""
    removed = []
    cutoff = time() - max_age_s
    try:
        candidates = list(directory.glob(pattern))
    except OSError:
        return removed
    for f in candidates:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed.append(f)
        except OSError:
            continue
    return removed


def clean_staging():
    """Remove downloads left in the staging directory by a crashed harvest."""
    for f in remove_stale(staging_dir(), "*"):
        log(f"[INFO] Removed stale staging file: {f.name}")


# --- Download ---

def _download_hls_sync(url: str, interim: Path, bitrate: str):
    """Fetch an HLS stream with ffmpeg and transcode the audio track to MP3."""
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-user_agent", "Mozilla/5.0",
        "-i", url,
        "-vn", "-codec:a", "libmp3lame", "-b:a", bitrate,
        str(interim),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {result.stderr.strip()[-500:]}")
    if not interim.exists() or interim.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no output")


def _download_file_sync(scraper, url: str, path: Path, mp3_bitrate: str = "128k"):
    # download to local staging, then publish to the share in one short, atomic copy
    staged = staging_path_for(path)
    staged.unlink(missing_ok=True)
    try:
        if is_hls_url(url):
            _download_hls_sync(url, staged, mp3_bitrate)
        else:
            with scraper.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with staged.open("wb") as f:
                    for chunk in response.iter_content(65536):
                        f.write(chunk)
        publish_file(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    log(f"[INFO] Downloaded {path.name}")


# --- Feed builder ---

def build_feed(
    config: Config,
    episodes: list[dict],
    slug: str,
    title: str,
    description: str,
    image_url: str,
    unavailable_ids: set[str] = None,
    secret: Optional[str] = None,
) -> str:
    secret_query_param = f"?secret={secret}" if secret is not None else get_secret_query_parameter(config)
    unavailable_ids = unavailable_ids or set()
    items = []
    for e in episodes:
        episode_id = e["id"]
        episode_title = (
            f"[Ikke tilgjengelig] {e['title']}"
            if episode_id in unavailable_ids
            else e['title']
        )
        pub_date_str = e.get("publishDatetime") or e.get("datetime")
        try:
            pub_date = datetime.fromisoformat(pub_date_str)
        except Exception:
            pub_date = datetime.now(timezone.utc)

        _, duration = extract_audio_url(e)
        file_path = build_podcast_episode_file_path(config, slug, episode_id)
        try:
            file_size = file_path.stat().st_size
        except FileNotFoundError:
            file_size = 0

        items.append(
            Item(
                title=episode_title,
                description=e.get("description", ""),
                guid=Guid(episode_id, isPermaLink=False),
                enclosure=Enclosure(
                    url=f"{config.host}/{slug}/{episode_id}{secret_query_param}",
                    type="audio/mpeg",
                    length=file_size,
                ),
                pubDate=pub_date,
                extensions=[
                    iTunesItem(
                        author=e.get("artist") or e.get("podcastName", ""),
                        duration=duration,
                    )
                ],
            )
        )

    feed_link = f"{config.host}/{slug}{secret_query_param}"
    feed = Feed(
        title=title or slug,
        link=feed_link,
        description=description or title or slug,
        language="no",
        image=Image(url=image_url or "", title=title or slug, link=feed_link),
        items=sorted(items, key=lambda i: i.pubDate, reverse=True),
        extensions=[iTunes(block="Yes")],
    )
    return feed.rss()


# --- Sync feeds ---

async def sync_slug_feed(client: PodimoClient, config: Config, slug: str):
    if slug not in config.podcasts:
        log(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return
    log(f"[INFO] Syncing '{slug}' feed...")
    podcast_config = config.podcasts[slug]
    data = await client.getPodcasts(podcast_config.podcast_id)
    all_episodes = data["episodes"]
    podcast_info = data["podcast"]

    on_disk = set(harvested_episode_ids(config, slug))
    episodes = [e for e in all_episodes if e["id"] in on_disk]

    podcast_dir = build_podcast_dir(config, slug)
    unavailable_ids = {f.stem for f in podcast_dir.glob("*.unavailable")}

    build_podcast_dir(config, slug).mkdir(parents=True, exist_ok=True)

    title = podcast_info.get("title") or slug
    description = podcast_info.get("description") or ""
    image_url = (podcast_info.get("images") or {}).get("coverImageUrl") or ""

    if config.users:
        for user in config.users:
            feed = build_feed(
                config, episodes, slug, title, description, image_url,
                unavailable_ids=unavailable_ids,
                secret=user.secret,
            )
            write_text_atomic(build_podcast_feed_path(config, slug, alias=user.alias), feed)
        log(
            f"[INFO] '{slug}' feed written for {len(config.users)} user{'s' if len(config.users) != 1 else ''}"
            f" ({len(episodes)} episode{'s' if len(episodes) != 1 else ''})"
        )
    else:
        feed = build_feed(
            config, episodes, slug, title, description, image_url,
            unavailable_ids=unavailable_ids,
        )
        write_text_atomic(build_podcast_feed_path(config, slug), feed)
        log(f"[INFO] '{slug}' feed now serving {len(episodes)} episode{'s' if len(episodes) != 1 else ''}")


# --- Harvest ---

async def harvest_podcast(client: PodimoClient, config: Config, slug: str):
    if slug not in config.podcasts:
        log(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return

    podcast_config = config.podcasts[slug]
    log(f"[INFO] Fetching episode list for '{slug}'...")
    data = await client.getPodcasts(
        podcast_config.podcast_id,
        limit=podcast_config.most_recent_episodes_limit,
    )
    all_episodes = data["episodes"]

    if not all_episodes:
        log(f"[WARN] No published episodes found for '{slug}'")
        return

    existing_ids = set(harvested_episode_ids(config, slug))
    to_harvest = [
        (e, url)
        for e in all_episodes
        if e["id"] not in existing_ids
        for url, _ in [extract_audio_url(e)]
        if url
    ]

    limit = podcast_config.most_recent_episodes_limit
    limit_note = f" (only looking at {limit} most recent)" if limit is not None else ""

    if not to_harvest:
        log(f"[INFO] Nothing new from '{slug}', all available episodes already harvested{limit_note}")
        await sync_slug_feed(client, config, slug)
        return

    log(
        f"[INFO] Found {len(to_harvest)} new episode{'s' if len(to_harvest) != 1 else ''}"
        f" of '{slug}' ready to harvest{limit_note}"
    )

    podcast_dir = build_podcast_dir(config, slug)
    podcast_dir.mkdir(parents=True, exist_ok=True)

    # left over from before staging existed, or from a publish that was interrupted
    for f in podcast_dir.glob("*_interim.mp3"):
        log(f"[INFO] Removing stale interim file: {f.name}")
        f.unlink()
    for f in remove_stale(podcast_dir, "*.part"):
        log(f"[INFO] Removed stale partial file: {f.name}")

    placeholder = Path(__file__).parent.parent / "episode_not_available.mp3"
    sem = asyncio.Semaphore(config.api.max_concurrent_downloads)

    async def download_one(episode: dict, url: str):
        episode_id = episode["id"]
        path = build_podcast_episode_file_path(config, slug, episode_id)
        async with sem:
            try:
                await async_wrap(_download_file_sync)(client.scraper, url, path, config.api.mp3_bitrate)
            except Exception as exc:
                log(f"[WARN] Download failed for {episode_id}: {exc}")
                if path.exists():
                    path.unlink()
                if placeholder.exists():
                    publish_file(placeholder, path, move=False)
                    path.with_suffix(".unavailable").touch()
                    log(f"[INFO] Placed unavailable-episode placeholder at {path.name}")

    await asyncio.gather(*[download_one(e, url) for e, url in to_harvest])
    await sync_slug_feed(client, config, slug)
