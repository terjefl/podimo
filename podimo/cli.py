import fcntl
import hashlib
import os
import pprint
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Annotated

import typer
import uvicorn

from . import api
from .api import api as api_app, api_config
from .async_cli import AsyncTyper
from .config import config_from_stream
from .main import clean_staging, get_podimo_client, harvest_podcast, log, sync_slug_feed, write_harvest_status

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
    clean_staging()
    started = datetime.now(timezone.utc)
    failed = []
    try:
        async with get_podimo_client(config) as client:
            slugs = list(config.podcasts.keys()) if podcast_slugs is None else podcast_slugs
            for i, slug in enumerate(slugs):
                try:
                    await harvest_podcast(client, config, slug)
                except Exception as exc:
                    # One retry absorbs transient network errors (dropped connection,
                    # DNS blip). harvest_podcast is idempotent, so nothing is fetched twice.
                    delay = config.api.harvest_retry_delay
                    log(f"[WARN] Retrying '{slug}' in {delay}s after: {exc}")
                    await asyncio.sleep(delay)
                    try:
                        await harvest_podcast(client, config, slug)
                    except Exception as exc:
                        log(f"[FAIL] Harvest of '{slug}' failed: {exc}")
                        failed.append(slug)
                if i < len(slugs) - 1:
                    await asyncio.sleep(config.api.inter_podcast_delay)
    except Exception as exc:
        log(f"[FAIL] Harvest aborted: {exc}")
        if podcast_slugs is None:
            write_harvest_status(config, started, failed, error=str(exc))
        raise typer.Exit(code=1)
    # Only a full run (all podcasts) counts as "the last harvest" for /health.
    if podcast_slugs is None:
        write_harvest_status(config, started, failed)
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
    # Workers started with --workers N are new processes without the override above;
    # they read the same file through PODIMO_CONFIG_FILE (see api.api_config).
    config_path = getattr(config_stream, "name", None)
    if isinstance(config_path, str) and os.path.isfile(config_path):
        os.environ[api.CONFIG_ENV] = os.path.abspath(config_path)
    elif workers_requested(ctx.args) > 1:
        # stdin or a path that is not a regular file: the workers could not load it and
        # every request would fail with 500
        raise typer.BadParameter(
            "--workers > 1 needs the configuration in a file (not stdin)", param_hint="--config-file"
        )
    # also installed on startup in every worker (api._startup)
    api.install_secret_filter(config)

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


def workers_requested(args: list[str]) -> int:
    """The value of uvicorn's --workers in the extra args (1 when not given or not a number)."""
    for i, a in enumerate(args):
        value = None
        if a == "--workers" and i + 1 < len(args):
            value = args[i + 1]
        elif a.startswith("--workers="):
            value = a.split("=", 1)[1]
        if value is not None:
            try:
                return int(value)
            except ValueError:
                return 1
    return 1
