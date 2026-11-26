#!/usr/bin/env python3
"""K/L8 交接演练与迁移不变量自检。

该脚本使用临时 SQLite 文件，不污染开发库；它验证：
1. 两个规格 + 一份未完成 K 的迁移前后行数、价格、来源；
2. 重复迁移不复制数据，新旧入口不能领取两份结果；
3. L8 的 37→41 更正交接为新版本，但 K 的旧结果仍绑定 37；
4. 回滚后旧入口仍可追溯原受理回执和原记录；
5. 旧已计算结果缺计价依据时，复算被缺项提示拦截。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_DB = Path(tempfile.gettempdir()) / "price-watch-handoff-check.sqlite3"
if _DB.exists():
    _DB.unlink()
os.environ["PRICE_WATCH_SQLITE_PATH"] = str(_DB)

from app import db, handoff  # noqa: E402
from app.routers.migration import setup_k_l8  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"PASS: {message}")


def main() -> int:
    scenario = setup_k_l8({
        "reset": True,
        "activate": True,
        "continue_during_handoff": True,
        "site_prices": [
            {"site": "A站", "price": 35},
            {"site": "B站", "price": 39},
        ],
    })
    check(scenario["ok"], "构造两个规格、一份未完成 K 和 L8 37→41 更正")

    inventory = db.migration_inventory()["counts"]
    check(inventory["source_price_baseline"] == 5, "迁移前内存基线为原示例 3 条 + 演练 2 条")
    check(inventory["target_baseline"] == 5, "迁移后 SQLite 基线行数一致（5 条）")
    check(inventory["claims"] == 1 and inventory["results"] == 1, "只有一份 K 受理和一份有效结果")

    receipt = handoff.get_continuation("K", "legacy")["receipt"]
    check(receipt["kind"] == "confirmed_result", "K 已有确认结果，页面应与可操作待办区分")
    check(receipt["result"]["price"] == "37", "K 的结果价格固定为受理时绑定的 37")
    check(receipt["result"]["spread_ratio"] == "-5.41%", "按绑定基线和站点报价得到 -5.41%")
    check(receipt["correction"]["current_price"] == "41", "L8 更正 41 已交接为当前新版本")
    check(receipt["correction"]["bound_price"] == "37", "更正没有让 K 的旧结果悄悄换价")
    check(receipt["trace"]["original_record_available"], "旧结果可追到原记录/快照")

    duplicate_migration = db.start_migration(activate=True)
    check(duplicate_migration.get("duplicate") is True, "重复启动迁移返回同一受理，不复制数据")
    conflict = handoff.claim_round("K", "new")
    check(bool(conflict.get("conflict")), "新入口不能把同一轮 K 再领取为第二份有效结果")

    recompute = handoff.recompute_baseline("8001")
    check(recompute["ok"] and recompute["recomputation"]["stored_amount"] == "37",
          "有可核验版本依据的 K 可按 L8@v1/37 复算")

    missing = handoff.recompute_baseline("2")
    check((not missing["ok"]) and missing["code"] == "missing_calculation_basis",
          "缺实际计算依据的旧结果在复算前被明确拦截")
    check(len(missing.get("missing", [])) >= 1 and missing.get("original_record"),
          "返回缺项清单和原记录供查看")

    rolled = db.rollback_to_legacy("自检回滚")
    check(rolled["ok"], "可回滚到旧入口")
    after = handoff.get_continuation("K", "legacy")["receipt"]
    check(after["kind"] == "confirmed_result" and after["result"]["price"] == "37",
          "回滚后旧入口仍能查询 K 的实际领取状态和价格")
    check(after["claim"]["receipt_no"] == "RCP-K-0001", "回滚后仍使用唯一原回执 RCP-K-0001")

    print("\nK 接管解释：")
    print(after["trace"]["explanation"])
    print(f"SQLite: {_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
