"""Unit tests for keyboards and species catalog."""
import json
import tempfile
from pathlib import Path
from collector_bot.keyboards import SpeciesCatalog, get_species_keyboard


def test_species_catalog_live_reload():
    with tempfile.TemporaryDirectory() as tmp_dir:
        wl_path = Path(tmp_dir) / "test_whitelist.json"
        data = {
            "anas_platyrhynchos": {"ru": "Кряква", "habitat": "wetland"},
            "ardea_cinerea": {"ru": "Серая цапля", "habitat": "wetland"}
        }
        wl_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        catalog = SpeciesCatalog(wl_path)
        assert len(catalog.species_map) == 2
        assert catalog.get_species_name("anas_platyrhynchos") == "Кряква"

        # Dynamically add another species
        data["luscinia_luscinia"] = {"ru": "Обыкновенный соловей", "habitat": "forest"}
        wl_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        new_count = catalog.reload()
        assert new_count == 3
        assert catalog.get_species_name("luscinia_luscinia") == "Обыкновенный соловей"


def test_species_keyboard_generation():
    with tempfile.TemporaryDirectory() as tmp_dir:
        wl_path = Path(tmp_dir) / "test_whitelist.json"
        data = {f"species_{i}": {"ru": f"Птица {i}", "habitat": "wetland" if i % 2 == 0 else "forest"} for i in range(15)}
        wl_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        catalog = SpeciesCatalog(wl_path)
        kb = get_species_keyboard(catalog, sub_id=1, page=0, page_size=6, habitat="all")
        assert len(kb.inline_keyboard) > 0
        # Check special buttons exist in the keyboard
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        button_texts = [b.text for b in all_buttons]
        assert any("Шум" in t for t in button_texts)
        assert any("Не уверен" in t for t in button_texts)
        assert any("Другой вид" in t for t in button_texts)
