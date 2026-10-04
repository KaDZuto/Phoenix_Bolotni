import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import numpy as np
import soundfile as sf

from scripts.collect_xenocanto import collect_xenocanto


def test_collect_xenocanto_filter_and_attribution():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        data_dir = tmp_path / "dataset"
        wl_path = tmp_path / "whitelist.json"

        # Создаем тестовый whitelist
        wl_data = {
            "anas_platyrhynchos": {"ru": "Кряква", "habitat": "wetland"}
        }
        with open(wl_path, "w", encoding="utf-8") as f:
            json.dump(wl_data, f)

        mock_recordings = {
            "numRecordings": 1,
            "recordings": [
                {
                    "id": "12345",
                    "file": "https://example.com/audio.wav",
                    "rec": "Test Ornithologist",
                    "lic": "CC BY-NC-SA 4.0",
                    "url": "https://xeno-canto.org/12345",
                    "cnt": "Russia",
                    "loc": "Lake Baikal",
                }
            ],
        }

        # Mock сетевых вызовов
        with patch("scripts.collect_xenocanto.search_recordings", return_value=mock_recordings):
            with patch("scripts.collect_xenocanto.download_and_save_audio") as mock_dl:
                def fake_save(url, out_path):
                    # Записываем валидный фейковый wav
                    data = np.zeros(32000, dtype=np.float32)
                    sf.write(str(out_path), data, 32000)
                    return True

                mock_dl.side_effect = fake_save

                # Попытка запросить вид из whitelist и левый вид
                collect_xenocanto(
                    whitelist_path=str(wl_path),
                    data_dir=str(data_dir),
                    species_filter="anas_platyrhynchos,unknown_alien_bird",
                    max_per_species=1,
                    sleep_sec=0.0,
                )

        sp_dir = data_dir / "anas_platyrhynchos"
        assert sp_dir.exists(), "Target species folder must be created"
        assert (sp_dir / "XC12345.wav").exists(), "Downloaded audio must exist"
        assert (sp_dir / "attribution.json").exists(), "attribution.json must exist"
        assert (sp_dir / "ATTRIBUTION.md").exists(), "ATTRIBUTION.md must exist"

        with open(sp_dir / "attribution.json", "r", encoding="utf-8") as f:
            attr = json.load(f)
            assert len(attr) == 1
            assert attr[0]["author"] == "Test Ornithologist"
            assert attr[0]["license"] == "CC BY-NC-SA 4.0"

        # Левый вид не должен быть создан
        alien_dir = data_dir / "unknown_alien_bird"
        assert not alien_dir.exists(), "Alien species must not be downloaded"
