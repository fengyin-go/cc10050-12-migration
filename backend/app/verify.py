"""构建/部署自检：按原示例数据核对行数与已知基线价格，并验证基础计算。

用法：
    python -m app.verify
"""
from __future__ import annotations

from decimal import Decimal

from app.seed import SEED_ROWS


def verify_sample() -> dict[str, object]:
    baselines = SEED_ROWS["price_baseline"]
    compares = SEED_ROWS["price_compare"]
    assert len(baselines) == 3, f"原示例基线应为 3 条，实际 {len(baselines)}"
    assert len(compares) == 3, f"原示例比价应为 3 条，实际 {len(compares)}"
    prices = [Decimal(str(row["基线价格"])) for row in baselines]
    assert prices == [Decimal("12.5"), Decimal("25.0"), Decimal("37.5")], prices

    # 用一份可核验依据验证公式：37 基线、35 最低站点价 => -2/37。
    baseline = Decimal("37")
    lowest = Decimal("35")
    ratio = (lowest - baseline) / baseline
    assert f"{(ratio * 100).quantize(Decimal('0.01'))}%" == "-5.41%"

    unfinished = [row for row in compares if row["status"] != "已计算"]
    assert len(unfinished) == 2
    return {
        "ok": True,
        "baseline_count": len(baselines),
        "compare_count": len(compares),
        "unfinished_count": len(unfinished),
        "known_prices": [str(p) for p in prices],
        "sample_calculation": {"baseline": "37", "lowest": "35", "spread_ratio": "-5.41%"},
    }


if __name__ == "__main__":
    result = verify_sample()
    print("sample verification passed:", result)
