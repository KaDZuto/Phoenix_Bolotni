"""Script to synchronize species whitelist and model configuration from Phoenix_Bolotni model repo.

Does not share git history; copies and validates artifacts:
- species/ru_birds_whitelist.json
- model_config.json
"""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("sync_artifacts")


def sync_artifacts(model_repo_dir: Path, collector_dir: Path) -> bool:
    model_repo_dir = Path(model_repo_dir).resolve()
    collector_dir = Path(collector_dir).resolve()

    if not model_repo_dir.exists():
        logger.error(f"Model repository path does not exist: {model_repo_dir}")
        return False

    src_whitelist = model_repo_dir / "species" / "ru_birds_whitelist.json"
    dst_whitelist = collector_dir / "species" / "ru_birds_whitelist.json"

    src_config = model_repo_dir / "model_config.json"
    dst_config = collector_dir / "model_config.json"

    # 1. Sync & validate whitelist
    if src_whitelist.exists():
        try:
            with open(src_whitelist, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or len(data) == 0:
                raise ValueError("Whitelist JSON must be a non-empty dictionary of species.")
            for slug, val in data.items():
                if not isinstance(val, dict) or "ru" not in val:
                    raise ValueError(f"Invalid entry for slug '{slug}': missing 'ru' name.")
            dst_whitelist.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_whitelist, dst_whitelist)
            logger.info(f"✅ Successfully synced whitelist ({len(data)} species) to {dst_whitelist}")
        except Exception as e:
            logger.error(f"Failed to validate/sync whitelist: {e}")
            return False
    else:
        logger.warning(f"Source whitelist not found at {src_whitelist}")

    # 2. Sync & validate model_config
    if src_config.exists():
        try:
            with open(src_config, "r", encoding="utf-8") as f:
                cfg_data = json.load(f)
            target_sr = cfg_data.get("target_sr", 32000)
            logger.info(f"Detected TARGET_SR: {target_sr} Hz")
            shutil.copy2(src_config, dst_config)
            logger.info(f"✅ Successfully synced model_config.json to {dst_config}")
        except Exception as e:
            logger.error(f"Failed to validate/sync model_config: {e}")
            return False
    else:
        logger.warning(f"Source model_config not found at {src_config}")

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync model artifacts to Phoenix_Bolotni Collector")
    parser.add_argument("--model-repo", type=str, default="/root/Phoenix_Bolotni", help="Path to Phoenix_Bolotni model repo")
    parser.add_argument("--collector-dir", type=str, default=".", help="Path to collector repo")
    args = parser.parse_args()

    success = sync_artifacts(Path(args.model_repo), Path(args.collector_dir))
    sys.exit(0 if success else 1)
