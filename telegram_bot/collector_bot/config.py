"""Configuration management for Phoenix_Bolotni Collector Bot."""
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


class Config:
    def __init__(self, root_dir: Optional[Path] = None):
        self.ROOT_DIR = root_dir or Path(__file__).resolve().parent.parent
        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
        
        # Telegram storage and admin
        raw_archive = os.getenv("ARCHIVE_CHANNEL_ID", "0").strip()
        try:
            self.ARCHIVE_CHANNEL_ID: int = int(raw_archive)
        except ValueError:
            self.ARCHIVE_CHANNEL_ID = 0
            
        raw_admin = os.getenv("ADMIN_CHAT_ID", "0").strip()
        try:
            self.ADMIN_CHAT_ID: int = int(raw_admin)
        except ValueError:
            self.ADMIN_CHAT_ID = 0

        admin_ids_raw = os.getenv("ADMIN_USER_IDS", "").strip()
        self.ADMIN_USER_IDS: List[int] = []
        if admin_ids_raw:
            for item in admin_ids_raw.split(","):
                item = item.strip()
                if item.isdigit() or (item.startswith("-") and item[1:].isdigit()):
                    self.ADMIN_USER_IDS.append(int(item))
        if self.ADMIN_CHAT_ID and self.ADMIN_CHAT_ID not in self.ADMIN_USER_IDS:
            self.ADMIN_USER_IDS.append(self.ADMIN_CHAT_ID)

        # Database
        self.DB_PATH = Path(os.getenv("DB_PATH", str(self.ROOT_DIR / "collector.db")))

        # Artifacts from model repo
        default_wl = self.ROOT_DIR / "species" / "ru_birds_whitelist.json"
        if not default_wl.exists() and (self.ROOT_DIR.parent / "species" / "ru_birds_whitelist.json").exists():
            default_wl = self.ROOT_DIR.parent / "species" / "ru_birds_whitelist.json"
        self.WHITELIST_PATH = Path(os.getenv("WHITELIST_PATH", str(default_wl)))

        default_cfg = self.ROOT_DIR / "model_config.json"
        if not default_cfg.exists() and (self.ROOT_DIR.parent / "model_config.json").exists():
            default_cfg = self.ROOT_DIR.parent / "model_config.json"
        self.MODEL_CONFIG_PATH = Path(os.getenv("MODEL_CONFIG_PATH", str(default_cfg)))


        # Audio conversion defaults (from model_config.json if available)
        self.TARGET_SR = 32000
        self.MONO = True
        self._load_model_config()

        # Batch export threshold
        raw_batch_size = os.getenv("BATCH_SIZE", "10").strip()
        try:
            self.BATCH_SIZE = max(1, int(raw_batch_size))
        except ValueError:
            self.BATCH_SIZE = 10

        # Storage & temp paths
        self.TEMP_DIR = Path(os.getenv("TEMP_DIR", str(self.ROOT_DIR / "tmp")))
        self.TEMP_DIR.mkdir(parents=True, exist_ok=True)

        # WebApp & Audio Proxy
        self.WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8080/webapp").strip()
        self.PROXY_HOST = os.getenv("PROXY_HOST", "0.0.0.0").strip()
        self.PROXY_PORT = int(os.getenv("PROXY_PORT", "8080").strip())
        self.PROXY_SECRET = os.getenv("PROXY_SECRET", "phoenix_bolotni_secret_key_change_in_prod").strip()
        self.PROXY_URL_BASE = os.getenv("PROXY_URL_BASE", f"http://localhost:{self.PROXY_PORT}/api/audio").strip()

        # Consent validity
        self.CONSENT_VALIDITY_DAYS = int(os.getenv("CONSENT_VALIDITY_DAYS", "365").strip())

    def _load_model_config(self) -> None:
        if self.MODEL_CONFIG_PATH.exists():
            try:
                with open(self.MODEL_CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.TARGET_SR = int(data.get("target_sr", self.TARGET_SR))
            except Exception:
                pass


config = Config()
