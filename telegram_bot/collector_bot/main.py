"""Unified entrypoint for Phoenix_Bolotni Collector Bot and Web Proxy."""
import asyncio
import logging
import sys
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .bot import setup_bot_router
from .config import config
from .db import Database
from .keyboards import SpeciesCatalog
from .web_proxy import create_proxy_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("collector_main")


async def main() -> None:
    logger.info("Initializing Phoenix_Bolotni Collector...")

    db = Database(config.DB_PATH)
    catalog = SpeciesCatalog(config.WHITELIST_PATH)
    logger.info(f"Loaded {len(catalog.species_map)} whitelist species.")

    if not config.BOT_TOKEN:
        logger.warning("BOT_TOKEN is not set in environment! Bot polling will not start. Running proxy server only.")
        app = create_proxy_app(bot=None, db=db, catalog=catalog, cfg=config)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=config.PROXY_HOST, port=config.PROXY_PORT)
        await site.start()
        logger.info(f"Audio Proxy running on http://{config.PROXY_HOST}:{config.PROXY_PORT}")
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner.cleanup()
        return

    bot = Bot(token=config.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    router = setup_bot_router(bot=bot, db=db, catalog=catalog, cfg=config)
    dp.include_router(router)

    # Start audio proxy & webapp server
    app = create_proxy_app(bot=bot, db=db, catalog=catalog, cfg=config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.PROXY_HOST, port=config.PROXY_PORT)
    await site.start()
    logger.info(f"Audio Proxy & WebApp running on http://{config.PROXY_HOST}:{config.PROXY_PORT}")

    try:
        logger.info("Starting Telegram Bot long polling...")
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down...")
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
