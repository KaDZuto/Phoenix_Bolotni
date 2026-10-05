"""Keyboards and species catalog for Phoenix_Bolotni Collector Bot."""
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

logger = logging.getLogger(__name__)


class SpeciesCatalog:
    """Manages ru_birds_whitelist.json and supports live reloading without restart."""
    def __init__(self, whitelist_path: Path):
        self.whitelist_path = Path(whitelist_path)
        self.species_map: Dict[str, Dict[str, str]] = {}
        self.reload()

    def reload(self) -> int:
        if not self.whitelist_path.exists():
            logger.warning(f"Whitelist file {self.whitelist_path} does not exist.")
            self.species_map = {}
            return 0
        try:
            with open(self.whitelist_path, "r", encoding="utf-8") as f:
                self.species_map = json.load(f)
            logger.info(f"Loaded {len(self.species_map)} species from {self.whitelist_path}")
            return len(self.species_map)
        except Exception as e:
            logger.error(f"Error loading species whitelist: {e}")
            return len(self.species_map)

    def get_species_name(self, slug: str) -> str:
        if slug == "_noise":
            return "Шум / нет птицы"
        if slug == "_uncertain":
            return "Не уверен(а)"
        data = self.species_map.get(slug)
        if data:
            return data.get("ru", slug)
        return slug

    def get_items(self, habitat: Optional[str] = None) -> List[Tuple[str, str, str]]:
        """Returns list of (slug, ru_name, habitat) tuples, sorted by Russian name."""
        items = []
        for slug, info in self.species_map.items():
            hab = info.get("habitat", "other")
            if habitat and habitat != "all" and hab != habitat:
                continue
            ru = info.get("ru", slug)
            items.append((slug, ru, hab))
        items.sort(key=lambda x: x[1].lower())
        return items


def get_consent_keyboard() -> InlineKeyboardMarkup:
    """Consent keyboard for GDPR / attribution."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Согласен(-на) на использование записей",
                    callback_data="consent_agree"
                )
            ]
        ]
    )


def get_annotation_choice_keyboard(sub_id: int, webapp_url: Optional[str] = None) -> InlineKeyboardMarkup:
    """Offers Fast Annotation or Precise WebApp annotation."""
    buttons = [
        [
            InlineKeyboardButton(
                text="⚡ Быстрая разметка (весь клип)",
                callback_data=f"fast_ann:{sub_id}"
            )
        ]
    ]
    if webapp_url:
        buttons.append([
            InlineKeyboardButton(
                text="🎧 Точная разметка (открыть плеер)",
                web_app=WebAppInfo(url=webapp_url)
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_species_keyboard(
    catalog: SpeciesCatalog,
    sub_id: int,
    page: int = 0,
    page_size: int = 6,
    habitat: str = "all"
) -> InlineKeyboardMarkup:
    """Builds paginated species keyboard with habitat filter and special action buttons."""
    items = catalog.get_items(habitat=habitat)
    total_pages = max(1, math.ceil(len(items) / page_size))
    current_page = max(0, min(page, total_pages - 1))

    start_idx = current_page * page_size
    page_items = items[start_idx : start_idx + page_size]

    rows: List[List[InlineKeyboardButton]] = []

    # Habitat filters row
    habitat_buttons = [
        InlineKeyboardButton(
            text="🌿 Все" if habitat == "all" else "Все",
            callback_data=f"sp_p:{sub_id}:0:all"
        ),
        InlineKeyboardButton(
            text="💧 Болотные" if habitat == "wetland" else "Болотные",
            callback_data=f"sp_p:{sub_id}:0:wetland"
        ),
        InlineKeyboardButton(
            text="🌲 Лесные" if habitat == "forest" else "Лесные",
            callback_data=f"sp_p:{sub_id}:0:forest"
        ),
    ]
    rows.append(habitat_buttons)

    # Species buttons (2 per row)
    spec_row = []
    for slug, ru_name, _ in page_items:
        btn = InlineKeyboardButton(
            text=ru_name[:24],
            callback_data=f"sp_sel:{sub_id}:{slug}"
        )
        spec_row.append(btn)
        if len(spec_row) == 2:
            rows.append(spec_row)
            spec_row = []
    if spec_row:
        rows.append(spec_row)

    # Navigation row
    nav_row = []
    if current_page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"sp_p:{sub_id}:{current_page - 1}:{habitat}"
            )
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"Стр. {current_page + 1}/{total_pages}",
            callback_data=f"sp_noop"
        )
    )
    if current_page < total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data=f"sp_p:{sub_id}:{current_page + 1}:{habitat}"
            )
        )
    rows.append(nav_row)

    # Special buttons: Noise, Uncertain, Other
    special_row = [
        InlineKeyboardButton(text="🔇 Шум", callback_data=f"sp_sel:{sub_id}:_noise"),
        InlineKeyboardButton(text="❓ Не уверен", callback_data=f"sp_sel:{sub_id}:_uncertain"),
        InlineKeyboardButton(text="➕ Другой вид", callback_data=f"sp_other:{sub_id}"),
    ]
    rows.append(special_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_location_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard requesting geolocation with skip option."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геопозицию", request_location=True)],
            [KeyboardButton(text="⏩ Пропустить")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def get_skip_inline_keyboard(action_name: str, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏩ Пропустить", callback_data=f"skip_{action_name}:{sub_id}")]
        ]
    )
