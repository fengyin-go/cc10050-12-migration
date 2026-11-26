"""迁移、交接、续办与计价依据专用接口。

固定路径必须先于通用 ``/api/{module}/{entry_id}`` 资源路由注册。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app import db, handoff
from app.store import store

router = APIRouter(prefix="/api", tags=["迁移与续办"])


def _body(payload: dict[str, Any] | None) -> dict[str, Any]:
    return payload if isinstance(payload, dict) else {}


def _conflict_aware(result: dict[str, Any]) -> JSONResponse | dict[str, Any]:
    if result.get("http_status") and int(result["http_status"]) >= 400:
        # 同时保留项目惯用的 200+ok=false 入口；严格 REST 客户端可调用 /strict 路径。
        return result
    return result


# ---------------------------------------------------------------------------
# Migration status / inventory / controls
# ---------------------------------------------------------------------------

@router.get("/migration/status")
def migration_status() -> dict[str, Any]:
    return {"ok": True, **db.migration_inventory()}


@router.get("/migration/inventory")
def migration_inventory() -> dict[str, Any]:
    return db.migration_inventory()


@router.get("/migration/sample-check")
def migration_sample_check() -> dict[str, Any]:
    from app.verify import verify_sample
    return verify_sample()


@router.get("/migration/events")
def migration_events(batch: str | None = None, limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "items": db.list_events(batch, limit=limit)}


@router.post("/migration/start")
def start_migration(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return db.start_migration(
        activate=bool(data.get("activate", True)),
        idempotency_key=data.get("idempotency_key") or db.MIGRATION_KEY,
        operator=data.get("operator"),
    )


@router.post("/migration/prepare")
def prepare_migration(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return db.start_migration(activate=False, idempotency_key=data.get("idempotency_key") or db.MIGRATION_KEY,
                              operator=data.get("operator"))


@router.post("/migration/verify")
def verify_migration() -> dict[str, Any]:
    inventory = db.migration_inventory()
    state = db.get_state()
    complete = state.get("current_run_id") is not None
    return {"ok": complete, "message": "迁移行数、价格、来源与校验值核对完成" if complete else "尚未启动迁移",
            **inventory}


@router.post("/migration/switch")
def switch_new(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return db.switch_to_new(operator=data.get("operator"))


@router.post("/migration/activate")
def activate_new(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return db.switch_to_new(operator=data.get("operator"))


@router.post("/migration/rollback")
def rollback_legacy(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return db.rollback_to_legacy(data.get("reason", "值班员发起回滚"), operator=data.get("operator"))


@router.post("/migration/failback")
def failback(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return db.rollback_to_legacy(data.get("reason", "切换故障，返回旧入口"), operator=data.get("operator"))


@router.post("/migration/simulate-failure")
def simulate_failure() -> dict[str, Any]:
    return db.simulate_cutover_failure()


# ---------------------------------------------------------------------------
# Continuation receipts: generic + old/new entry aliases
# ---------------------------------------------------------------------------

@router.post("/migrate")
def migrate_alias(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return start_migration(payload)


@router.get("/migrate/status")
def migrate_status_alias() -> dict[str, Any]:
    return migration_status()


@router.post("/rollback")
def rollback_alias(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return rollback_legacy(payload)


@router.get("/continuation")
def continuation_by_query(batch: str, entry: str | None = None) -> dict[str, Any]:
    return handoff.get_continuation(batch, entry)


@router.get("/handoff/{batch}")
def handoff_alias(batch: str, entry: str | None = None) -> dict[str, Any]:
    return handoff.get_continuation(batch, entry)


@router.get("/handoff/continuation/{batch}")
def handoff_continuation_alias(batch: str, entry: str | None = None) -> dict[str, Any]:
    return handoff.get_continuation(batch, entry)


@router.post("/handoff/{batch}/claim")
def handoff_claim_alias(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return continuation_claim(batch, payload)


@router.post("/handoff/{batch}/continue")
def handoff_continue_alias(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return continuation_continue(batch, payload)


@router.get("/continuation/{batch}")
def continuation(batch: str, entry: str | None = None) -> dict[str, Any]:
    return handoff.get_continuation(batch, entry)


@router.get("/continuation/{batch}/receipt")
def continuation_receipt(batch: str, entry: str | None = None) -> dict[str, Any]:
    return handoff.get_continuation(batch, entry)


@router.post("/continuation/{batch}/claim")
def continuation_claim(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    side = data.get("entry") or data.get("side") or db.active_entry()
    return handoff.claim_round(batch, str(side), idempotency_key=data.get("idempotency_key"),
                               operator=data.get("operator"), task_identifier=data.get("task_id"),
                               allow_takeover=bool(data.get("allow_takeover")))


@router.post("/continuation/{batch}/continue")
def continuation_continue(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    side = data.get("entry") or data.get("side") or db.active_entry()
    return handoff.execute_continuation(batch, str(side), data)


@router.get("/legacy/continuation/{batch}")
def legacy_continuation(batch: str) -> dict[str, Any]:
    return handoff.get_continuation(batch, "legacy")


@router.post("/legacy/continuation/{batch}/continue")
def legacy_continue(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return handoff.execute_continuation(batch, "legacy", _body(payload))


@router.post("/legacy/continuation/{batch}/claim")
def legacy_claim(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return handoff.claim_round(batch, "legacy", idempotency_key=data.get("idempotency_key"),
                               operator=data.get("operator"),
                               allow_takeover=bool(data.get("allow_takeover", False)))


@router.get("/old/continuation/{batch}")
def old_continuation(batch: str) -> dict[str, Any]:
    return handoff.get_continuation(batch, "legacy")


@router.get("/new/continuation/{batch}")
def new_continuation(batch: str) -> dict[str, Any]:
    return handoff.get_continuation(batch, "new")


@router.post("/new/continuation/{batch}/continue")
def new_continue(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return handoff.execute_continuation(batch, "new", _body(payload))


@router.post("/new/continuation/{batch}/claim")
def new_claim(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    return handoff.claim_round(batch, "new", idempotency_key=data.get("idempotency_key"),
                               operator=data.get("operator"),
                               allow_takeover=bool(data.get("allow_takeover", False)))


# ---------------------------------------------------------------------------
# Claims REST endpoints
# ---------------------------------------------------------------------------

@router.get("/claims/{batch}")
def get_claim(batch: str) -> dict[str, Any]:
    receipt = handoff.get_continuation(batch)
    return {"ok": True, "claim": receipt["receipt"].get("claim"), "receipt": receipt["receipt"]}


@router.post("/claims")
def create_claim_collection(payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    data = _body(payload)
    batch = str(data.get("batch") or data.get("比价批次") or "")
    side = str(data.get("entry") or data.get("side") or db.active_entry())
    result = handoff.claim_round(batch, side, idempotency_key=data.get("idempotency_key"),
                                 operator=data.get("operator"))
    status_code = 409 if result.get("conflict") else 200
    return JSONResponse(status_code=status_code, content=result)


@router.post("/claims/{batch}")
def create_claim_named(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    data = _body(payload)
    side = str(data.get("entry") or data.get("side") or db.active_entry())
    result = handoff.claim_round(batch, side, idempotency_key=data.get("idempotency_key"),
                                 operator=data.get("operator"))
    status_code = 409 if result.get("conflict") else 200
    return JSONResponse(status_code=status_code, content=result)


@router.post("/claims/{batch}/claim")
def claim(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    side = str(data.get("entry") or data.get("side") or db.active_entry())
    result = handoff.claim_round(batch, side, idempotency_key=data.get("idempotency_key"),
                                 operator=data.get("operator"), task_identifier=data.get("task_id"))
    return result


@router.post("/claims/{batch}/claim/strict")
def claim_strict(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> JSONResponse:
    data = _body(payload)
    side = str(data.get("entry") or data.get("side") or db.active_entry())
    result = handoff.claim_round(batch, side, idempotency_key=data.get("idempotency_key"),
                                 operator=data.get("operator"), task_identifier=data.get("task_id"))
    status_code = int(result.pop("http_status", 200)) if result.get("conflict") else 200
    if status_code == 409:
        result["ok"] = False
    return JSONResponse(status_code=status_code, content=result)


# ---------------------------------------------------------------------------
# Module-scoped continuation books and basis/recompute/correction
# ---------------------------------------------------------------------------

@router.get("/price_baseline/continuation")
def baseline_continuation_book() -> dict[str, Any]:
    items = db.list_baselines(confirmed_only=False)
    tasks = handoff.list_compare_tasks()
    for item in items:
        version_keys = {item.get("baseline_version_key")}
        for version in item.get("versions", []):
            version_keys.add(version.get("version_key"))
            for alias in version.get("aliases", []) if isinstance(version, dict) else []:
                version_keys.add(alias)
        item["bound_compare_tasks"] = [
            {
                "id": task.get("id"),
                "batch": task.get("比价批次"),
                "status": task.get("status"),
                "receipt_no": task.get("receipt_no"),
                "bound_version_key": task.get("bound_version_key"),
            }
            for task in tasks
            if task.get("bound_version_key") in version_keys
        ]
    return {
        "ok": True,
        "kind": "price_baseline_continuation_book",
        "active_entry": db.active_entry(),
        "total": len(items),
        "confirmed": sum(1 for x in items if x.get("confirmed")),
        "currency_pending": sum(1 for x in items if x.get("currency_status") == "missing_pending_check"),
        "items": items,
    }


@router.get("/price_compare/continuation")
def compare_continuation_book() -> dict[str, Any]:
    tasks = handoff.list_compare_tasks()
    todos = [x for x in tasks if x.get("status") != "已计算"]
    confirmed = [x for x in tasks if x.get("status") == "已计算"]
    return {
        "ok": True,
        "kind": "price_compare_continuation_book",
        "active_entry": db.active_entry(),
        "total": len(tasks),
        "actionable_total": len(todos),
        "confirmed_total": len(confirmed),
        "actionable": todos,
        "confirmed": confirmed,
        "items": tasks,
    }


@router.get("/price_compare/continuation/{batch}")
def compare_batch_continuation(batch: str, entry: str | None = None) -> dict[str, Any]:
    return handoff.get_continuation(batch, entry)


@router.post("/price_compare/continuation/{batch}/continue")
def compare_batch_continue(batch: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    side = str(data.get("entry") or data.get("side") or db.active_entry())
    return handoff.execute_continuation(batch, side, data)


@router.post("/price_compare/{entry_id}/basis")
def register_compare_basis(entry_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return handoff.register_basis(entry_id, _body(payload))


@router.get("/price_compare/{entry_id}/basis")
def get_compare_basis(entry_id: str) -> dict[str, Any]:
    task = handoff.ensure_task_from_identifier(entry_id)
    if task is None:
        return {"ok": False, "message": f"比价任务 {entry_id} 不存在"}
    return {"ok": True, "basis": task.get("basis", {}), "missing": task.get("blocking_missing", []),
            "pending_verification": task.get("pending_verification", []), "task": task}


@router.post("/price_compare/{entry_id}/recompute-baseline")
def recompute_compare_baseline(entry_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    return handoff.recompute_baseline(entry_id, _body(payload))


@router.get("/price_compare/{entry_id}/trace")
def trace_compare(entry_id: str) -> dict[str, Any]:
    task = handoff.ensure_task_from_identifier(entry_id)
    if task is None:
        return {"ok": False, "message": f"比价任务 {entry_id} 不存在"}
    batch = task.get("比价批次") or entry_id
    return handoff.get_continuation(batch)


@router.post("/price_baseline/{entry_id}/corrections")
def correct_baseline(entry_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    price = data.get("new_price", data.get("基线价格", data.get("price")))
    result = db.correct_baseline(
        entry_id, price, reason=data.get("reason", "当次基线更正"),
        source=data.get("source"), operator=data.get("operator"),
    )
    # 同步旧内存行，保证旧入口列表/原记录能看到当前值；旧结果仍由版本快照保护。
    if result.get("ok") and result.get("baseline"):
        base = result["baseline"]
        row = store.find("price_baseline", int(base["id"])) if str(base.get("id", "")).isdigit() else None
        if row is not None:
            row["基线价格"] = price if isinstance(price, (int, float)) else price
            row["status"] = base.get("status", row.get("status"))
            db.upsert_snapshot("price_baseline", row)
    return result


@router.post("/price_baseline/{entry_id}/recompute")
def recompute_baseline_by_id(entry_id: str, payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    base = db.get_baseline(entry_id)
    if base is None:
        return {"ok": False, "code": "baseline_not_found", "message": f"基线 {entry_id} 不存在"}
    version = db.get_baseline_version(base.get("baseline_version_key"))
    basis = _body(payload).get("basis", _body(payload))
    calc = handoff.calculate_baseline({**basis, "baseline_amount": base.get("amount_decimal")}, version)
    missing = [] if calc.get("matches") else ["可核验样本/公式"]
    return {"ok": calc.get("matches", False), "missing": missing, "recomputation": calc, "baseline": base}


# ---------------------------------------------------------------------------
# Deterministic K/L8 handover drill
# ---------------------------------------------------------------------------

@router.post("/migration/scenarios/k-l8")
def setup_k_l8(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    data = _body(payload)
    reset = bool(data.get("reset", True))
    batch = str(data.get("batch", "K"))
    l8_price = data.get("initial_price", 37)
    corrected_price = data.get("corrected_price", 41)
    if reset:
        with db.connect() as conn:
            for b in (batch,):
                ck = handoff.claim_key_for(b)
                conn.execute("DELETE FROM compare_result WHERE claim_key=?", (ck,))
                conn.execute("DELETE FROM claim WHERE claim_key=?", (ck,))
                conn.execute("DELETE FROM handoff_event WHERE batch_key=?", (b,))
            conn.execute("DELETE FROM compare_task WHERE source_key IN (?,?)",
                         ("memory:price_compare:8001", "memory:price_compare:K-UNFINISHED"))
            conn.execute("DELETE FROM baseline_version WHERE baseline_id IN (7,8)")
            conn.execute("DELETE FROM baseline WHERE id IN (7,8)")
        for module, ids in (("price_baseline", (7, 8)), ("price_compare", (8001,))):
            rows = store.rows(module)
            rows[:] = [r for r in rows if int(r.get("id", -1)) not in ids]

    spec_a = data.get("spec_a", "500g 标准装")
    spec_b = data.get("spec_b", "1kg 礼盒装")
    rows_baseline = [
        {
            "id": 7, "status": "已生效", "pending": False, "abnormal": False,
            "商品标识": data.get("goods_a", "GOODS-L7"), "归一规格": spec_a,
            "基线价格": data.get("l7_price", 26), "采样窗口": "7d", "更新时间": db.now_iso(),
            "来源": "原台账-L7", "line_code": "L7",
        },
        {
            "id": 8, "status": "已生效", "pending": False, "abnormal": False,
            "商品标识": data.get("goods_b", "GOODS-L8"), "归一规格": spec_b,
            "基线价格": l8_price, "采样窗口": "7d", "更新时间": db.now_iso(),
            "来源": "原台账-L8", "line_code": "L8",
        },
    ]
    for row in rows_baseline:
        existing = store.find("price_baseline", row["id"])
        if existing:
            existing.update(row)
        else:
            store.rows("price_baseline").append(row)
        db.upsert_snapshot("price_baseline", row)
        db.upsert_baseline(row, confirmed=True)

    k_row = {
        "id": 8001, "status": "排队中", "pending": True, "abnormal": False,
        "比价批次": batch, "商品标识": data.get("goods_b", "GOODS-L8"), "归一规格": spec_b,
        "最低价站点": "", "价差比例": "", "计算时间": "",
        "source_key": "memory:price_compare:8001",
    }
    existing_k = store.find("price_compare", 8001)
    if existing_k:
        existing_k.update(k_row)
    else:
        store.rows("price_compare").append(k_row)
    task = handoff.upsert_compare_task(k_row)

    accepted = handoff.claim_round(batch, "legacy", task_identifier=task["id"],
                                   operator=data.get("operator", "值班员"))
    migration = db.start_migration(activate=bool(data.get("activate", True)),
                                   idempotency_key=data.get("migration_key", db.MIGRATION_KEY))
    # 迁移后把 K 明确绑定到 L8 的原始 v1/37。
    quotes = data.get("site_prices")
    basis_payload: dict[str, Any] = {
        "baseline_version_key": "L8@v1",
        "source": "原台账-L8",
        "evidence": data.get("evidence", ["L8 原台账价格行", "迁移校验快照"]),
        "formula": "(lowest_site_price-baseline_price)/baseline_price",
    }
    if quotes is not None:
        basis_payload["site_prices"] = quotes
    basis_result = handoff.register_basis(task["id"], basis_payload)
    correction = db.correct_baseline("L8", corrected_price, reason="值班期间核价更正",
                                     source="核价单-L8-41")
    if correction.get("ok"):
        row = store.find("price_baseline", 8)
        if row:
            row["基线价格"] = corrected_price
    continued = None
    if data.get("continue_during_handoff"):
        continued = handoff.execute_continuation(batch, "legacy", {
            "allow_takeover": True,
            **({"site_prices": quotes} if quotes is not None else {}),
        })
    rolled_back = None
    if data.get("auto_rollback"):
        rolled_back = db.rollback_to_legacy("演练：新部署重启后回滚", operator=data.get("operator", "值班员"))
    receipt = handoff.get_continuation(batch, "legacy")["receipt"]
    inventory = db.migration_inventory()
    return {
        "ok": True,
        "message": "K/L8 交接演练已构造：两个规格、一份未完成 K、L8 从 37 更正为 41",
        "baseline_rows": rows_baseline,
        "task": k_row,
        "accepted": accepted,
        "migration": migration,
        "basis": basis_result,
        "correction": correction,
        "continued": continued,
        "rollback": rolled_back,
        "receipt": receipt,
        "inventory": inventory,
    }
