import asyncio
import contextlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Optional

import aiohttp
import cloudscraper
from rfeed import Feed, Image, Item, Guid, Enclosure, iTunes, iTunesItem

from .client import PodimoClient
from .config import Config, User
from .utils import token_key


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
        print("[INFO] Authenticating with Podimo...")
        await client.podimoLogin()
        save_token(key, client.token, config)
        print("[INFO] Authentication successful, token cached")
    yield client


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
        if url and "hls-media" in url and "/main.m3u8" in url:
            url = url.replace("hls-media", "audios").replace("/main.m3u8", ".mp3")
    return url, duration


# --- Download ---

async def _download_file(session: aiohttp.ClientSession, url: str, path: Path):
    interim = path.with_name(path.stem + "_interim.mp3")
    async with session.get(url) as response:
        response.raise_for_status()
        with interim.open("wb") as f:
            async for chunk in response.content.iter_chunked(65536):
                f.write(chunk)
    interim.rename(path)
    print(f"[INFO] Downloaded {path.name}")


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
        print(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return
    print(f"[INFO] Syncing '{slug}' feed...")
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
            with build_podcast_feed_path(config, slug, alias=user.alias).open("w", encoding="utf-8") as f:
                f.write(feed)
        print(
            f"[INFO] '{slug}' feed written for {len(config.users)} user{'s' if len(config.users) != 1 else ''}"
            f" ({len(episodes)} episode{'s' if len(episodes) != 1 else ''})"
        )
    else:
        feed = build_feed(
            config, episodes, slug, title, description, image_url,
            unavailable_ids=unavailable_ids,
        )
        with build_podcast_feed_path(config, slug).open("w", encoding="utf-8") as f:
            f.write(feed)
        print(f"[INFO] '{slug}' feed now serving {len(episodes)} episode{'s' if len(episodes) != 1 else ''}")


# --- Harvest ---

async def harvest_podcast(client: PodimoClient, config: Config, slug: str):
    if slug not in config.podcasts:
        print(f"[FAIL] The slug '{slug}' did not match any podcasts in the config file")
        return

    podcast_config = config.podcasts[slug]
    print(f"[INFO] Fetching episode list for '{slug}'...")
    data = await client.getPodcasts(
        podcast_config.podcast_id,
        limit=podcast_config.most_recent_episodes_limit,
    )
    all_episodes = data["episodes"]

    if not all_episodes:
        print(f"[WARN] No published episodes found for '{slug}'")
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
        print(f"[INFO] Nothing new from '{slug}', all available episodes already harvested{limit_note}")
        await sync_slug_feed(client, config, slug)
        return

    print(
        f"[INFO] Found {len(to_harvest)} new episode{'s' if len(to_harvest) != 1 else ''}"
        f" of '{slug}' ready to harvest{limit_note}"
    )

    podcast_dir = build_podcast_dir(config, slug)
    podcast_dir.mkdir(parents=True, exist_ok=True)

    for f in podcast_dir.glob("*_interim.mp3"):
        print(f"[INFO] Removing stale interim file: {f.name}")
        f.unlink()

    placeholder = Path(__file__).parent.parent / "episode_not_available.mp3"
    sem = asyncio.Semaphore(config.api.max_concurrent_downloads)

    async def download_one(episode: dict, url: str):
        episode_id = episode["id"]
        path = build_podcast_episode_file_path(config, slug, episode_id)
        async with sem:
            try:
                async with aiohttp.ClientSession() as session:
                    await _download_file(session, url, path)
            except Exception as exc:
                print(f"[WARN] Download failed for {episode_id}: {exc}")
                if path.exists():
                    path.unlink()
                if placeholder.exists():
                    shutil.copy2(placeholder, path)
                    path.with_suffix(".unavailable").touch()
                    print(f"[INFO] Placed unavailable-episode placeholder at {path.name}")

    await asyncio.gather(*[download_one(e, url) for e, url in to_harvest])
    await sync_slug_feed(client, config, slug)
