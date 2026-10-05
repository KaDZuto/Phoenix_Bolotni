"""Integrity tests ensuring species slugs match 1:1 with ru_birds_whitelist.json and pipeline specifications."""
import json
import re
from pathlib import Path
import pytest
from collector_bot.config import Config
from collector_bot.keyboards import SpeciesCatalog


def test_whitelist_slug_integrity():
    cfg = Config()
    catalog = SpeciesCatalog(cfg.WHITELIST_PATH)
    assert len(catalog.species_map) > 0, "Whitelist must not be empty"

    slug_pattern = re.compile(r"^[a-z]+_[a-z0-9_]+$")
    valid_habitats = {"wetland", "forest", "urban", "field_steppe", "other"}

    for slug, info in catalog.species_map.items():
        # Check slug naming convention (latin lowercase with underscore: genus_species)
        assert slug_pattern.match(slug), f"Invalid slug format: '{slug}'"

        # Check Russian name exists and is not blank
        ru_name = info.get("ru")
        assert ru_name and len(ru_name.strip()) > 0, f"Missing or empty Russian name for slug '{slug}'"

        # Check habitat tag
        habitat = info.get("habitat", "other")
        assert habitat in valid_habitats, f"Invalid habitat '{habitat}' for slug '{slug}'"


def test_target_sample_rate_matches_model_config():
    cfg = Config()
    assert cfg.TARGET_SR == 32000, f"Target sample rate must be 32000 Hz, got {cfg.TARGET_SR}"
    assert cfg.MONO is True, "Audio must be converted to mono"


def test_species_slug_match_model_classes():
    """Ensure baseline species (like anas_platyrhynchos, ardea_cinerea) are present."""
    cfg = Config()
    catalog = SpeciesCatalog(cfg.WHITELIST_PATH)

    baseline_sample = [
        "anas_platyrhynchos",
        "ardea_cinerea",
        "fringilla_coelebs",
        "parus_major",
        "corvus_frugilegus"
    ]
    for sp in baseline_sample:
        assert sp in catalog.species_map, f"Baseline species '{sp}' missing from whitelist"
