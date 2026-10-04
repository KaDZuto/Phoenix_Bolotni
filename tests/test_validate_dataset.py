import json
import tempfile
from pathlib import Path
from scripts.validate_dataset import validate_dataset, load_whitelist


def test_whitelist_contains_16_base_classes():
    wl_path = Path("species/ru_birds_whitelist.json")
    assert wl_path.exists(), "ru_birds_whitelist.json does not exist"
    wl = load_whitelist(wl_path)

    classes_path = Path("metadata/classes.json")
    with open(classes_path, "r", encoding="utf-8") as f:
        base_classes = json.load(f)

    for cls in base_classes:
        assert cls in wl, f"Base class {cls} must be in ru_birds_whitelist.json"
        assert "ru" in wl[cls], f"Species {cls} must have Russian name"
        assert "habitat" in wl[cls], f"Species {cls} must have habitat tag"


def test_validate_dataset_positive_and_negative():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        data_dir = tmp_path / "dataset"
        data_dir.mkdir()

        # Valid species
        (data_dir / "anas_platyrhynchos").mkdir()
        # Special class
        (data_dir / "_noise").mkdir()
        # Unknown alien species
        (data_dir / "unknown_alien_bird").mkdir()

        report_file = tmp_path / "report.json"
        report = validate_dataset(
            data_dir=str(data_dir),
            whitelist_path="species/ru_birds_whitelist.json",
            report_path=str(report_file),
        )

        assert "anas_platyrhynchos" in report["valid_classes"]
        assert "_noise" in report["special_classes"]
        assert "unknown_alien_bird" in report["invalid_classes"]
        assert report_file.exists()
