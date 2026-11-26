"""价格基线库领域服务：版本化基线、迁移核对、生效与重算。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import db
from app.services.base import BaseModuleService
from app.store import store


class PriceBaselineService(BaseModuleService):
    MODULE = "price_baseline"
    ENTITY = "价格基线"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["商品标识", "归一规格", "基线价格", "采样窗口", "更新时间"]
    REQUIRED_FIELDS = ["商品标识", "归一规格", "基线价格"]
    STATUS_ORDER = ["计算中", "已生效", "待重算"]
    ACTION_RULES = {"生效基线": "已生效", "重算基线": "待重算"}
    NEGATIVE_ACTIONS = []

    # ---- 新旧入口数据视图 ----

    def all_rows(self) -> list[dict[str, Any]]:
        if db.is_new_active():
            return db.list_baselines(confirmed_only=False)
        rows = [dict(r) for r in store.rows(self.MODULE)]
        # 旧入口也叠加 SQLite 版本/币种核对信息，但不覆盖内存当前价。
        for row in rows:
            skey = db.row_source_key(self.MODULE, row)
            base = db.get_baseline(skey)
            if base:
                row.setdefault("source_key", skey)
                row["baseline_version_key"] = base.get("baseline_version_key")
                row["price_version"] = base.get("baseline_version_key")
                row["currency_status"] = base.get("currency_status")
                row.setdefault("币种", base.get("币种", "待核对"))
                if base.get("currency_status") == "missing_pending_check":
                    row["币种核对"] = "待核对：存量记录未提供币种"
        return rows

    def find_entry(self, entry_id: int) -> dict[str, Any] | None:
        if db.is_new_active():
            item = db.get_baseline(entry_id)
            if item is not None:
                return item
        row = store.find(self.MODULE, entry_id)
        if row is not None:
            skey = db.row_source_key(self.MODULE, row)
            base = db.get_baseline(skey)
            if base:
                row = {**base, **row}
            return row
        return db.get_baseline(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        # 保留币种、来源、价格行号和计价依据等迁移关键字段。
        entry.update({k: v for k, v in values.items() if k not in {"id", "status"}})
        db.upsert_snapshot(self.MODULE, entry)
        if db.is_new_active():
            db.upsert_baseline(entry, confirmed=True)
            persisted = db.get_baseline(entry["id"])
            if persisted:
                return persisted, []
        return entry, []

    def after_create(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        db.upsert_snapshot(self.MODULE, entry)

    def run_action(self, entry_id: int, action: str):  # type: ignore[override]
        entry = self.find_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在", False
        if action == "生效基线":
            result = self.activate_baseline(entry_id)
            return self.find_entry(entry_id), result["message"], result.get("ok", False), result
        if action == "重算基线":
            result = self.recompute_baseline(entry_id)
            ok = result.get("ok", False)
            return self.find_entry(entry_id), result["message"], ok, result
        return super().run_action(entry_id, action)

    # ---- 业务扩展：基线计算与滚动窗口 ----

    def activate_baseline(self, entry_id: int) -> dict[str, Any]:
        """生效价格基线，要求归一规格与价格齐备；缺币种只挂待核对，不伪造币种。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        spec = entry.get("归一规格", "")
        window = entry.get("采样窗口", "")
        if not spec:
            return {"ok": False, "message": "缺少归一规格，不能生效"}
        if not window:
            entry["采样窗口"] = "7d"
        price = str(entry.get("基线价格", "") or "")
        try:
            float(price)
        except ValueError:
            return {"ok": False, "message": "基线价格必须是数值"}
        entry["status"] = "已生效"
        entry["pending"] = False
        entry["更新时间"] = datetime.now(timezone.utc).isoformat()
        if not entry.get("币种") or entry.get("币种") == "待核对":
            entry["币种"] = "待核对"
            entry["currency_status"] = "missing_pending_check"
            entry["币种核对"] = "待核对：存量记录未提供币种"
        if not db.is_new_active():
            memory = store.find(self.MODULE, entry_id)
            if memory is not None:
                memory.update(entry)
        db.upsert_snapshot(self.MODULE, entry)
        db.upsert_baseline(entry, confirmed=True)
        return {"ok": True, "message": "基线已生效；缺币种已注明待核对" if entry.get("currency_status") else "基线已生效",
                "spec": spec, "baseline_version_key": entry.get("baseline_version_key")}

    def recompute_baseline(self, entry_id: int) -> dict[str, Any]:
        """重算基线：优先按绑定版本/登记依据复算，避免当前价覆盖旧基线。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "基线不存在"}
        base = db.get_baseline(entry.get("source_key") or entry_id) or db.get_baseline(entry_id)
        if base and base.get("baseline_version_key"):
            version = db.get_baseline_version(base["baseline_version_key"])
            calc = __import__("app.handoff", fromlist=["calculate_baseline"]).calculate_baseline(entry, version)
            if calc.get("matches"):
                entry["status"] = "已生效"
                entry["pending"] = False
                entry["更新时间"] = datetime.now(timezone.utc).isoformat()
                return {"ok": True, "message": "基线已按可核验依据复算，金额一致", "recomputation": calc}
            return {"ok": False, "message": "基线复算不一致或缺少样本公式", "recomputation": calc}

        spec = entry.get("归一规格", "")
        candidates = [
            s for s in store.rows("sample_store")
            if spec and spec in str(s.get("原始HTML摘要", "")) and s.get("status") == "已解析"
        ]
        if not candidates:
            entry["status"] = "待重算"
            entry["pending"] = True
            return {
                "ok": False,
                "code": "missing_calculation_basis",
                "missing": ["绑定价格版本", "可核验样本", "计算公式"],
                "original_record": entry,
                "message": "缺少实际计算依据，复算前需先明确提示缺项；没有可用样本，基线待重算，可查看原记录",
            }
        entry["基线价格"] = f"{float(len(candidates)) * 10:.2f}"
        entry["status"] = "已生效"
        entry["更新时间"] = datetime.now(timezone.utc).isoformat()
        entry["pending"] = False
        db.upsert_snapshot(self.MODULE, entry)
        db.upsert_baseline(entry, confirmed=True)
        return {
            "ok": True,
            "message": "基线已按原示例样本规则重算",
            "samples_used": len(candidates),
            "new_price": entry.get("基线价格"),
        }

    def publish_to_compare(self, entry_id: int) -> dict[str, Any]:
        """把生效基线发布给比价计算器排队计算。"""
        entry = self.find_entry(entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"ok": False, "message": "基线不存在或未生效"}
        rows = store.rows("price_compare")
        cid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        row = {
            "id": cid,
            "比价批次": f"baseline-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}",
            "商品标识": entry.get("商品标识", ""),
            "归一规格": entry.get("归一规格", ""),
            "最低价站点": "",
            "价差比例": "0%",
            "计算时间": datetime.now(timezone.utc).isoformat(),
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "bound_version_key": entry.get("baseline_version_key"),
        }
        rows.append(row)
        db.upsert_snapshot("price_compare", row)
        __import__("app.handoff", fromlist=["upsert_compare_task"]).upsert_compare_task(row)
        return {"ok": True, "message": "基线已发布比价", "compare_id": cid}

    def apply_currency(self, entry_id: int, rate: float) -> dict[str, Any]:
        """按汇率把基线价格换算到目标币种。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "基线不存在"}
        if rate <= 0:
            return {"ok": False, "message": "汇率必须为正数"}
        try:
            price = float(entry.get("基线价格", "0") or 0)
        except ValueError:
            return {"ok": False, "message": "基线价格无效"}
        converted = round(price * rate, 2)
        entry["基线价格"] = f"{converted:.2f}"
        entry["更新时间"] = datetime.now(timezone.utc).isoformat()
        memory = store.find(self.MODULE, entry_id)
        if memory is not None:
            memory.update(entry)
        db.upsert_snapshot(self.MODULE, entry)
        db.correct_baseline(entry.get("source_key") or entry_id, converted, reason=f"汇率换算 rate={rate}")
        return {"ok": True, "message": "基线已按汇率换算并登记为新版本", "new_price": entry.get("基线价格")}

    def detect_stale(self, entry_id: int) -> dict[str, Any]:
        """检测基线是否过期需要重算。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "基线不存在"}
        updated = str(entry.get("更新时间", "") or "")
        stale = not updated or updated[:10] < datetime.now(timezone.utc).date().isoformat()
        if stale and entry.get("status") == "已生效":
            entry["status"] = "待重算"
            entry["pending"] = True
        return {"ok": True, "message": "基线新鲜度检测完成", "stale": stale}

    def register_goods_link(self, entry_id: int) -> dict[str, Any]:
        """把基线商品标识登记到商品档案中心形成主数据关联。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "基线不存在"}
        goods_id = entry.get("商品标识", "")
        exists = any(
            g.get("商品标识") == goods_id for g in store.rows("goods_registry")
        )
        if exists:
            return {"ok": True, "message": "商品档案已存在", "goods_id": goods_id}
        rows = store.rows("goods_registry")
        gid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": gid,
            "商品标识": goods_id,
            "商品名称": f"基线商品 {goods_id}",
            "品牌": "未登记",
            "类目": "未分类",
            "登记来源": "price_baseline",
            "status": "待合并",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "商品档案已登记", "registry_id": gid}
