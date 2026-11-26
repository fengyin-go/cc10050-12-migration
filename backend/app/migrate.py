"""命令行执行幂等迁移：python -m app.migrate [--prepare]."""
from __future__ import annotations

import json
import sys

from app.db import init_db, start_migration


def main(argv: list[str] | None = None) -> int:
    args = set(sys.argv[1:] if argv is None else argv)
    prepare_only = "--prepare" in args
    init_db()
    result = start_migration(activate=not prepare_only)
    print(json.dumps({
        "ok": result.get("ok"),
        "duplicate": result.get("duplicate", False),
        "message": result.get("message"),
        "run_id": result.get("run_id"),
        "phase": result.get("phase"),
        "counts": (result.get("report") or {}).get("counts"),
    }, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
