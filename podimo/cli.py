import fcntl
import hashlib
import logging
import os
import pprint
import tempfile
from pathlib import Path
from typing import Optional, Annotated

import typer
import uvicorn

from . import api
from .api import api as api_app, api_config
from .async_cli import AsyncTyper
from .config import config_from_stream
from .logging_utils import LogRedactSecretFilter
from .main import get_podimo_client, harvest_podcast, log, sync_slug_feed

DEFAULT_CONFIG_FILE = os.environ.get("PODIMO_CONFIG_FILE", "config.yaml")

# Never dump local variables (they include the parsed config with credentials) into tracebacks.
cli = AsyncTyper(pretty_exceptions_show_locals=False)


@cli.command()
async def harvest(
    podcast_slugs: Annotated[
        Optional[list[str]],
        typer.Argument(metavar="[PODCAST_SLUG]..."),
    ] = None,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = DEFAULT_CONFIG_FILE,
):
    """
    Download podcast episodes from Podimo
    """
    import asyncio

    config = config_from_stream(config_stream)

    # Harvests can take hours when there is a backlog; make sure two scheduled
    # runs never overlap (they would download the same episodes and remove each
    # other's interim files). The lock lives on the local filesystem, not in
    # yield_dir, because network shares (CIFS/NFS) handle flock unreliably.
    lock_name = hashlib.sha256(os.path.abspath(config.yield_dir).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"podimo-harvest-{lock_name}.lock"
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError):
        log("[WARN] Another harvest is already running, skipping this run")
        raise typer.Exit(code=0)

    log("[INFO] Harvest started")
    failed = []
    try:
        async with get_podimo_client(config) as client:
            slugs = list(config.podcasts.keys()) if podcast_slugs is None else podcast_slugs
            for i, slug in enumerate(slugs):
                try:
                    await harvest_podcast(client, config, slug)
                except Exception as exc:
                    log(f"[FAIL] Harvest of '{slug}' failed: {exc}")
                    failed.append(slug)
                if i < len(slugs) - 1:
                    await asyncio.sleep(config.api.inter_podcast_delay)
    except Exception as exc:
        log(f"[FAIL] Harvest aborted: {exc}")
        raise typer.Exit(code=1)
    if failed:
        log(f"[WARN] Harvest finished with {len(failed)} failed podcast{'s' if len(failed) != 1 else ''}: {', '.join(failed)}")
        raise typer.Exit(code=1)
    log("[INFO] Harvest finished")


@cli.command("sync")
async def sync_feeds(
    podcast_slugs: Annotated[
        Optional[list[str]],
        typer.Argument(metavar="[PODCAST_SLUG]..."),
    ] = None,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = DEFAULT_CONFIG_FILE,
):
    """
    Regenerate RSS feeds from downloaded episodes
    """
    config = config_from_stream(config_stream)
    async with get_podimo_client(config) as client:
        to_sync = config.podcasts.keys() if podcast_slugs is None else podcast_slugs
        for slug in to_sync:
            try:
                await sync_slug_feed(client, config, slug)
            except Exception as exc:
                log(f"[FAIL] Sync of '{slug}' failed: {exc}")


@cli.command(
    name="serve",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def serve_api(
    ctx: typer.Context,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = DEFAULT_CONFIG_FILE,
):
    """
    Serve RSS feeds and episode audio files

    Wrapper around uvicorn; supports passing additional options to uvicorn.run().
    """
    ctx.args.insert(0, f"{api.__name__}:api")
    config = config_from_stream(config_stream)
    api_app.dependency_overrides[api_config] = lambda: config

    secrets_to_redact = []
    if config.secret is not None:
        secrets_to_redact.append(config.secret)
    if config.users:
        secrets_to_redact.extend(u.secret for u in config.users)
    if secrets_to_redact:
        secret_filter = LogRedactSecretFilter(secrets_to_redact)
        logging.getLogger("uvicorn.access").addFilter(secret_filter)
        logging.getLogger("uvicorn.error").addFilter(secret_filter)

    uvicorn.main.main(args=ctx.args)


@cli.command(name="config")
def print_config(
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = DEFAULT_CONFIG_FILE,
):
    """
    Print the parsed configuration
    """
    pprint.pprint(config_from_stream(config_stream))


@cli.callback()
def callback():
    """
    Download Podimo podcast episodes and serve them as RSS feeds
    """
