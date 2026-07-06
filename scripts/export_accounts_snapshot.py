from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.config import DATA_DIR
from services.storage.factory import create_storage_backend


def main() -> int:
    parser = argparse.ArgumentParser(description="Export accounts from configured storage backend as JSON list.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    storage = create_storage_backend(DATA_DIR)
    accounts = storage.load_accounts()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"exported_accounts={len(accounts)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
