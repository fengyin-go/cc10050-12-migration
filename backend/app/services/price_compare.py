"""比价计算器领域服务：领取互斥、版本绑定、计价依据、复算与续办。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import db, handoff
from app.services.base import BaseModuleService
from app.store import store


class PriceCompareService(BaseModuleService):
    MODULE = "price_compare"
    ENTITY = "比价结果"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["比价批次", "商品标识", "最低价站点", "价差比例", "计算时间"]
    REQUIRED_FIELDS = ["比价批次", "商品标识"]
    STATUS_ORDER = ["排队中", "已计算", "计算失败"]
    ACTION_RULES = {"开始计算": "已计算", "重试计算": "计算失败"}
    NEGATIVE_ACTIONS = []

    # ---- 合并新旧数据视图 ----

    def all_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        db_tasks = handoff.list_compare_tasks()
        if db.is_new_active():
            for task in db_tasks:
                skey = str(task.get("source_key") or "")
                if skey:
                    seen.add(skey)
                rows.append(task)
            for row in store.rows(self.MODULE):
                skey = db.row_source_key(self.MODULE, row)
                if skey not in seen:
                    rows.append(dict(row))
                    seen.add(skey)
        else:
            task_by_source = {str(t.get("source_key")): t for t in db_tasks if t.get("source_key")}
            for row in store.rows(self.MODULE):
                skey = db.row_source_key(self.MODULE, row)
                merged = dict(row)
                task = task_by_source.get(skey)
                if task:
                    # 交接字段以 SQLite 为准，但保留内存当前业务字段。
                    merged.update({k: v for k, v in task.items() if k not in {"data"}})
                merged["source_key"] = skey
                seen.add(skey)
                rows.append(merged)
            for task in db_tasks:
                skey = str(task.get("source_key") or "")
                if skey and skey not in seen and task.get("source_side") == "new":
                    rows.append(task)
                    seen.add(skey)
        rows.sort(key=lambda r: int(r.get("id", 0) or 0))
        return rows

    def find_entry(self, entry_id: int) -> dict[str, Any] | None:
        try:
            eid = int(entry_id)
        except (TypeError, ValueError):
            task = handoff.get_compare_task(entry_id)
            return task or next((r for r in self.all_rows() if str(r.get("比价批次")) == str(entry_id)), None)
        for row in self.all_rows():
            if int(row.get("id", -1) or -1) == eid:
                return row
        return handoff.get_compare_task(eid)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        # 保留列表五字段之外但对版本绑定重要的规格、币种、来源和计价依据。
        entry.update({k: v for k, v in values.items() if k not in {"id", "status"}})
        entry.setdefault("归一规格", values.get("spec", ""))
        task = handoff.upsert_compare_task(entry)
        if db.is_new_active():
            return task, []
        return entry, []

    def after_create(self, entry: dict[str, Any], values: dict[str, Any]) -> None:
        handoff.upsert_compare_task(entry)

    def run_action(self, entry_id: int, action: str):  # type: ignore[override]
        entry = self.find_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在", False, {}
        if action == "开始计算":
            result = self.run_compare(entry_id)
            updated = self.find_entry(entry_id)
            return updated, result.get("message", "比价计算完成"), bool(result.get("ok")), result
        if action == "重试计算":
            retry = self.retry_failed(entry_id)
            if not retry.get("ok"):
                return self.find_entry(entry_id), retry["message"], False, retry
            result = self.run_compare(entry_id)
            updated = self.find_entry(entry_id)
            # 缺依据时 run_compare 会给出明确缺项；重试上下文已隔离。
            extra = {**result, "quarantine_id": retry.get("quarantine_id")}
            return updated, result.get("message", retry["message"]), bool(result.get("ok")), extra
        outcome = super().run_action(entry_id, action)
        if len(outcome) == 2:
            e, msg = outcome
            return e, msg, e is not None, {}
        return outcome

    # ---- 业务扩展：跨站点比价与价差计算 ----

    def run_compare(self, entry_id: int) -> dict[str, Any]:
        """执行比价：通过唯一领取台账和绑定价格版本计算。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        batch = str(entry.get("比价批次") or "").strip()
        side = db.active_entry()
        return handoff.execute_continuation(batch, side, {"task_id": entry.get("id")})

    def retry_failed(self, entry_id: int) -> dict[str, Any]:
        """重试失败批次，先把失败上下文写入隔离区。"""
        entry = store.find(self.MODULE, int(entry_id)) if str(entry_id).isdigit() else None
        if entry is None:
            entry = self.find_entry(entry_id)
        if entry is None or entry.get("status") != "计算失败":
            return {"ok": False, "message": "批次不存在或非失败状态"}
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"compare-{entry_id}",
            "失败阶段": "计算",
            "失败原因": f"批次 {entry.get('比价批次', '')} 缺基线或计价依据",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "1",
            "status": "重试中",
            "pending": True,
            "abnormal": False,
        })
        entry["status"] = "排队中"
        entry["pending"] = True
        entry["abnormal"] = False
        db.upsert_snapshot(self.MODULE, entry)
        handoff.upsert_compare_task(entry)
        return {"ok": True, "message": "失败上下文已隔离，批次重新排队；若缺计价依据，复算前仍会明确提示", "quarantine_id": fid}

    def recompute_baseline(self, entry_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return handoff.recompute_baseline(entry_id, payload)

    def register_basis(self, entry_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return handoff.register_basis(entry_id, payload)

    def retry_failed_original(self, entry_id: int) -> dict[str, Any]:
        """保留原题材中的隔离重试语义（兼容旧调用）。"""
        return self.retry_failed(entry_id)

    def emit_change_history(self, entry_id: int, old_price: float, new_price: float) -> dict[str, Any]:
        """把价差结果写入变价历史流水。"""
        entry = self.find_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": "批次不存在"}
        if old_price <= 0:
            return {"ok": False, "message": "旧价格无效"}
        ratio = (new_price - old_price) / old_price
        rows = store.rows("change_history")
        hid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": hid,
            "商品标识": entry.get("商品标识", ""),
            "旧价格": f"{old_price:.2f}",
            "新价格": f"{new_price:.2f}",
            "变化幅度": f"{ratio:.2%}",
            "发现时间": datetime.now(timezone.utc).isoformat(),
            "status": "待核实",
            "pending": True,
            "abnormal": ratio < 0,
        })
        return {"ok": True, "message": "变价记录已写入", "history_id": hid, "ratio": f"{ratio:.2%}"}

    def notify_alert(self, entry_id: int) -> dict[str, Any]:
        """检查生效告警规则并触发变价通知。"""
        entry = self.find_entry(entry_id)
        if entry is None or entry.get("status") != "已计算":
            return {"ok": True, "message": "无生效告警规则", "triggered": 0} if entry is not None else {"ok": False, "message": "批次不存在或未完成计算"}
        goods_id = entry.get("商品标识", "")
        rules = [
            r for r in store.rows("price_alert")
            if r.get("status") == "已生效" and r.get("监控商品") == goods_id
        ]
        if not rules:
            return {"ok": True, "message": "无生效告警规则", "triggered": 0}
        return {
            "ok": True,
            "message": "已触发变价告警",
            "triggered": len(rules),
            "channels": [r.get("通知渠道", "") for r in rules],
        }

    def push_to_export(self, entry_id: int) -> dict[str, Any]:
        """把已计算批次推送到比价导出通道。"""
        entry = self.find_entry(entry_id)
        if entry is None or entry.get("status") != "已计算":
            return {"ok": False, "message": "批次不存在或未完成计算"}
        rows = store.rows("price_export")
        eid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": eid,
            "任务名称": f"导出批次 {entry.get('比价批次', '')}",
            "商品范围": entry.get("商品标识", ""),
            "导出格式": "CSV",
            "时间范围": str(entry.get("计算时间", ""))[:10],
            "生成状态": "排队中",
            "status": "排队中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "导出任务已创建", "export_id": eid}

    def batch_status(self, batch: str) -> dict[str, Any]:
        """汇总同批次所有比价记录的状态。"""
        rows = [r for r in self.all_rows() if r.get("比价批次") == batch]
        if not rows:
            receipt = handoff.get_continuation(batch).get("receipt", {})
            if receipt.get("claim"):
                return {"ok": True, "total": 0, "computed": 0, "pending": 0, "receipt": receipt,
                        "message": "批次在交接台账中存在"}
            return {"ok": False, "message": f"批次 {batch} 不存在"}
        computed = sum(1 for r in rows if r.get("status") == "已计算")
        return {
            "ok": True,
            "message": "批次状态汇总完成",
            "total": len(rows),
            "computed": computed,
            "pending": len(rows) - computed,
        }
