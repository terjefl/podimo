import os
from dataclasses import dataclass, field
from io import TextIOWrapper
from typing import Optional

from dataclass_wizard import YAMLWizard


@dataclass
class Auth:
    email: str
    password: str
    region: str = "no"
    locale: str = "no-NO"


@dataclass
class Podcast:
    podcast_id: str
    feed_name: str = "feed"
    most_recent_episodes_limit: Optional[int] = None


@dataclass
class ApiConfig:
    max_concurrent_downloads: int = 3
    token_cache_time: int = 432000  # 5 days in seconds
    inter_podcast_delay: int = 10  # seconds between podcast fetches
    mp3_bitrate: str = "128k"  # MP3 bitrate when transcoding HLS streams with ffmpeg
    health_max_harvest_age: int = 28800  # /health reports 503 if the last harvest is older than this (8h)


@dataclass
class User:
    alias: str
    secret: str


@dataclass
class Config(YAMLWizard):
    host: str
    auth: Auth
    podcasts: dict[str, Optional[Podcast]]
    yield_dir: str = "yield"
    secret: Optional[str] = None
    disable_index: bool = False
    users: list[User] = field(default_factory=list)
    api: ApiConfig = field(default_factory=ApiConfig)

    def __post_init__(self):
        self.podcasts = {
            k: (v if v is not None else Podcast(podcast_id="")) for k, v in self.podcasts.items()
        }


def config_from_stream(stream: TextIOWrapper) -> Config:
    config = Config.from_yaml(stream)
    # A relative yield_dir is resolved against the config file's directory, not the
    # current working directory, so cron jobs and `podimo -c path/config.yaml` behave
    # the same wherever they are started from.
    config_path = getattr(stream, "name", None)
    if isinstance(config_path, str) and not os.path.isabs(config.yield_dir):
        config.yield_dir = os.path.join(os.path.dirname(os.path.abspath(config_path)), config.yield_dir)
    return config
