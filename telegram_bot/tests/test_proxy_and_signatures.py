"""Tests for Web Proxy, HMAC signature verification, and Mini App endpoints."""
import time
import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from collector_bot.auth import generate_audio_token, verify_audio_token
from collector_bot.config import Config
from collector_bot.db import Database
from collector_bot.keyboards import SpeciesCatalog
from collector_bot.web_proxy import create_proxy_app


def test_hmac_signatures():
    sub_id = 42
    secret = "test_super_secret"

    # Valid token
    token = generate_audio_token(sub_id, secret, ttl_seconds=60)
    assert verify_audio_token(sub_id, token, secret)

    # Tampered sub_id
    assert not verify_audio_token(sub_id + 1, token, secret)

    # Tampered secret
    assert not verify_audio_token(sub_id, token, "wrong_secret")

    # Expired token
    expired_token = generate_audio_token(sub_id, secret, ttl_seconds=-10)
    assert not verify_audio_token(sub_id, expired_token, secret)


from aiohttp.test_utils import TestClient, TestServer


@pytest.mark.asyncio
async def test_proxy_endpoints(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    catalog = SpeciesCatalog(tmp_path / "dummy_whitelist.json")
    catalog.species_map = {
        "anas_platyrhynchos": {"ru": "Кряква", "habitat": "wetland"}
    }
    cfg = Config()
    cfg.PROXY_SECRET = "test_secret_key"
    cfg.BOT_TOKEN = ""  # mock mode

    # Add a mock submission
    sub = db.create_incoming_submission(
        user_id=123,
        username="tester",
        file_id="tg_file_999",
        chat_id=123,
        message_id=1
    )
    sub_id = sub["id"]

    app = create_proxy_app(bot=None, db=db, catalog=catalog, cfg=cfg)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 1. Health check
        resp = await client.get("/health")
        assert resp.status == 200
        json_data = await resp.json()
        assert json_data["status"] == "ok"
        assert json_data["species_count"] == 1

        # 2. Get species list
        resp = await client.get("/api/species")
        assert resp.status == 200
        species = await resp.json()
        assert len(species) == 1
        assert species[0]["slug"] == "anas_platyrhynchos"

        # 3. Audio stream without signature -> 403 Forbidden
        resp = await client.get(f"/api/audio/{sub_id}")
        assert resp.status == 403

        # 4. Audio stream with invalid signature -> 403 Forbidden
        resp = await client.get(f"/api/audio/{sub_id}?sig=invalid_sig")
        assert resp.status == 403

        # 5. Audio stream with valid signature -> 200 OK (in mock mode returns synthetic WAV)
        valid_sig = generate_audio_token(sub_id, cfg.PROXY_SECRET, ttl_seconds=300)
        resp = await client.get(f"/api/audio/{sub_id}?sig={valid_sig}")
        assert resp.status == 200
        body = await resp.read()
        assert len(body) > 0
        assert body.startswith(b"RIFF")

        # 6. Static WebApp route
        resp = await client.get("/webapp/index.html")
        assert resp.status == 200
        text = await resp.text()
        assert "Точная разметка" in text
    finally:
        await client.close()

