"""Thin HTTP audio proxy and static server for Telegram Mini App.

Provides:
- GET /api/audio/{sub_id}?sig=...
  Streams audio directly from Telegram Bot API without exposing BOT_TOKEN to the client.
  Protected by HMAC-SHA256 time-limited signatures.
- GET /api/species
  Returns whitelist species in JSON for search and filtering in the WebApp.
- GET /webapp/*
  Serves the static Mini App frontend.
"""
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiohttp import ClientSession, web

from .auth import verify_audio_token
from .config import Config
from .db import Database
from .keyboards import SpeciesCatalog

logger = logging.getLogger(__name__)


def create_proxy_app(bot: Bot, db: Database, catalog: SpeciesCatalog, cfg: Config) -> web.Application:
    app = web.Application()

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "species_count": len(catalog.species_map)})

    async def handle_get_species(request: web.Request) -> web.Response:
        habitat = request.query.get("habitat")
        items = catalog.get_items(habitat=habitat)
        data = [{"slug": s, "ru": r, "habitat": h} for s, r, h in items]
        return web.json_response(data)

    async def handle_stream_audio(request: web.Request) -> web.StreamResponse:
        sub_id_str = request.match_info.get("sub_id", "")
        if not sub_id_str.isdigit():
            return web.Response(status=400, text="Invalid submission ID")

        sub_id = int(sub_id_str)
        token = request.query.get("sig", "")

        # Verify signed HMAC token
        if not verify_audio_token(sub_id, token, cfg.PROXY_SECRET):
            logger.warning(f"Forbidden audio proxy access for submission {sub_id}")
            return web.Response(status=403, text="Forbidden: Invalid or expired signature")

        sub = db.get_submission_by_id(sub_id)
        if not sub:
            return web.Response(status=404, text="Submission not found")

        # In offline/mock mode (empty bot token), return a dummy wave
        if not cfg.BOT_TOKEN:
            dummy_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x00}\x00\x00\x00\xfa\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
            return web.Response(body=dummy_bytes, content_type="audio/wav")

        try:
            file_id = sub["telegram_file_id"]
            file_info = await bot.get_file(file_id)
            tg_file_url = f"https://api.telegram.org/file/bot{cfg.BOT_TOKEN}/{file_info.file_path}"

            # Stream audio bytes from Telegram to client
            async with ClientSession() as session:
                async with session.get(tg_file_url) as resp:
                    if resp.status != 200:
                        return web.Response(status=502, text="Telegram file gateway error")

                    content_type = resp.headers.get("Content-Type", "audio/ogg")
                    stream_resp = web.StreamResponse(
                        status=200,
                        reason="OK",
                        headers={
                            "Content-Type": content_type,
                            "Cache-Control": "private, max-age=600",
                            "Access-Control-Allow-Origin": "*"
                        }
                    )
                    await stream_resp.prepare(request)

                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await stream_resp.write(chunk)

                    await stream_resp.write_eof()
                    return stream_resp

        except Exception as e:
            logger.error(f"Error streaming audio for submission {sub_id}: {e}", exc_info=True)
            return web.Response(status=500, text="Internal proxy streaming error")

    # Add routes
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/species", handle_get_species)
    app.router.add_get("/api/audio/{sub_id}", handle_stream_audio)

    # Static web app route
    webapp_dir = cfg.ROOT_DIR / "collector_webapp"
    if webapp_dir.exists():
        app.router.add_static("/webapp", path=str(webapp_dir), show_index=True)

    return app
