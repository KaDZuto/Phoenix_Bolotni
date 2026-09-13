import json
from pathlib import Path
import numpy as np
from sklearn.model_selection import train_test_split

def prepare_dataset(data_dir="./dataset", artifacts_dir="./artifacts"):
    data_path = Path(data_dir)
    art_path = Path(artifacts_dir)
    art_path.mkdir(parents=True, exist_ok=True)

    print(f"🔍 Сканирование датасета в: {data_path.resolve()}")
    
    # Поиск всех папок с классами
    classes = sorted([p.name for p in data_path.iterdir() if p.is_dir()])
    if not classes:
        print("❌ Классы не найдены! Убедитесь, что в папке dataset есть подпапки с названиями птиц.")
        return

    label2idx = {c: i for i, c in enumerate(classes)}
    
    all_paths = []
    all_labels = []
    
    for cls in classes:
        cls_dir = data_path / cls
        wavs = sorted(list(cls_dir.rglob("*.wav")))
        print(f" {cls:25s} : {len(wavs)} файлов")
        all_paths.extend([str(p) for p in wavs])
        all_labels.extend([label2idx[cls]] * len(wavs))

    print(f"\n✅ Всего файлов: {len(all_paths)}")
    print(f"✅ Всего классов: {len(classes)}")

    # Split train/val/test (80/10/10)
    train_val_paths, test_paths, train_val_y, test_y = train_test_split(
        all_paths, all_labels, test_size=0.1, random_state=42, stratify=all_labels
    )
    
    train_paths, val_paths, train_y, val_y = train_test_split(
        train_val_paths, train_val_y, test_size=0.1111, random_state=42, stratify=train_val_y
    )

    def save_jsonl(filename, paths, labels):
        with open(art_path / filename, "w", encoding="utf-8") as f:
            for p, l in zip(paths, labels):
                f.write(json.dumps({"path": p, "label": int(l)}) + "\n")

    save_jsonl("train.jsonl", train_paths, train_y)
    save_jsonl("val.jsonl", val_paths, val_y)
    save_jsonl("test.jsonl", test_paths, test_y)

    with open(art_path / "classes.json", "w", encoding="utf-8") as f:
        json.dump(classes, f, ensure_ascii=False, indent=2)
    
    with open(art_path / "label2idx.json", "w", encoding="utf-8") as f:
        json.dump(label2idx, f, ensure_ascii=False, indent=2)

    print(f"\n📂 Метаданные сохранены в {art_path.resolve()}")
    print(f"Train: {len(train_paths)}, Val: {len(val_paths)}, Test: {len(test_paths)}")

if __name__ == "__main__":
    prepare_dataset()
