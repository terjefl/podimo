import logging
import pprint
from typing import Optional, Annotated

import typer
import uvicorn

from . import api
from .api import api as api_app, api_config
from .async_cli import AsyncTyper
from .config import config_from_stream
from .logging_utils import LogRedactSecretFilter
from .main import get_podimo_client, harvest_podcast, sync_slug_feed

cli = AsyncTyper()


@cli.command()
async def harvest(
    podcast_slugs: Annotated[
        Optional[list[str]],
        typer.Argument(metavar="[PODCAST_SLUG]..."),
    ] = None,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = "config.yaml",
):
    """
    Download podcast episodes from Podimo
    """
    config = config_from_stream(config_stream)
    async with get_podimo_client(config) as client:
        import asyncio
        slugs = list(config.podcasts.keys()) if podcast_slugs is None else podcast_slugs
        for i, slug in enumerate(slugs):
            await harvest_podcast(client, config, slug)
            if i < len(slugs) - 1:
                await asyncio.sleep(config.api.inter_podcast_delay)


@cli.command("sync")
async def sync_feeds(
    podcast_slugs: Annotated[
        Optional[list[str]],
        typer.Argument(metavar="[PODCAST_SLUG]..."),
    ] = None,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = "config.yaml",
):
    """
    Regenerate RSS feeds from downloaded episodes
    """
    config = config_from_stream(config_stream)
    async with get_podimo_client(config) as client:
        to_sync = config.podcasts.keys() if podcast_slugs is None else podcast_slugs
        for slug in to_sync:
            await sync_slug_feed(client, config, slug)


@cli.command(
    name="serve",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def serve_api(
    ctx: typer.Context,
    config_stream: Annotated[
        Optional[typer.FileText],
        typer.Option("--config-file", "-c", encoding="utf-8", help="Configuration file"),
    ] = "config.yaml",
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
    ] = "config.yaml",
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
