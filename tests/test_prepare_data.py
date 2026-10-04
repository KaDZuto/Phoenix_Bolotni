import json
import tempfile
from pathlib import Path
import numpy as np
import soundfile as sf

from prepare_data import prepare_dataset, extract_recording_id


def test_extract_recording_id():
    p1 = Path("XC12345_part01.wav")
    assert extract_recording_id(p1) == "XC12345"

    p2 = Path("bird_sound-clip2.wav")
    assert extract_recording_id(p2) == "bird_sound"

    p3 = Path("solo_recording.wav")
    assert extract_recording_id(p3) == "solo_recording"


def test_prepare_dataset_synthetic():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        data_dir = tmp_path / "dataset"
        art_dir = tmp_path / "artifacts"
        data_dir.mkdir()

        # Создаем 3 класса, в каждом по несколько нарезанных кусков от общих записей
        classes = ["anas_platyrhynchos", "ardea_cinerea", "_noise"]
        sr = 32000

        for cls in classes:
            cls_dir = data_dir / cls
            cls_dir.mkdir()
            # 4 исходные записи, каждая с 2 частями (_part01, _part02)
            for rec_idx in range(1, 5):
                rec_name = f"rec_{cls}_{rec_idx}"
                for part_idx in range(1, 3):
                    fn = cls_dir / f"{rec_name}_part{part_idx:02d}.wav"
                    dummy_audio = np.sin(np.linspace(0, 100, sr * 2)).astype(np.float32)
                    sf.write(str(fn), dummy_audio, sr)

        res = prepare_dataset(
            data_dir=str(data_dir),
            artifacts_dir=str(art_dir),
            skip_validation=True,
        )

        assert (art_dir / "train.jsonl").exists()
        assert (art_dir / "val.jsonl").exists()
        assert (art_dir / "test.jsonl").exists()
        assert (art_dir / "classes.json").exists()
        assert (art_dir / "label2idx.json").exists()

        def load_jsonl(name):
            with open(art_dir / name, "r", encoding="utf-8") as f:
                return [json.loads(line) for line in f]

        train_items = load_jsonl("train.jsonl")
        val_items = load_jsonl("val.jsonl")
        test_items = load_jsonl("test.jsonl")

        train_recs = set(x["recording_id"] for x in train_items)
        val_recs = set(x["recording_id"] for x in val_items)
        test_recs = set(x["recording_id"] for x in test_items)

        # Главный критерий: ни один recording_id не встречается более чем в одном сплите
        assert len(train_recs & val_recs) == 0, f"Leakage train/val: {train_recs & val_recs}"
        assert len(train_recs & test_recs) == 0, f"Leakage train/test: {train_recs & test_recs}"
        assert len(val_recs & test_recs) == 0, f"Leakage val/test: {val_recs & test_recs}"

        # Все классы присутствуют
        with open(art_dir / "classes.json", "r", encoding="utf-8") as f:
            saved_classes = json.load(f)
        assert set(saved_classes) == set(classes)
