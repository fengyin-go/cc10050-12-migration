"""批次领取、版本绑定、计价依据、结果幂等与续办回执。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app import db

SIDE_NAMES = {
    "legacy": "旧入口（内存兼容层）",
    "old": "旧入口（内存兼容层）",
    "new": "新入口（SQLite）",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def side_name(side: str | None) -> str:
    return SIDE_NAMES.get(str(side or ""), str(side or "未指定入口"))


def normalize_batch(batch: Any) -> str:
    return str(batch or "").strip()


def claim_key_for(batch: str) -> str:
    return f"round:{normalize_batch(batch)}"


def safe_receipt_fragment(batch: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_-]+", "-", batch).strip("-_").upper()
    return fragment[:32] if fragment else db.content_hash({"batch": batch})[:12].upper()


# ---------------------------------------------------------------------------
# Compare task persistence
# ---------------------------------------------------------------------------

def upsert_compare_task(row: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    db.init_db()
    skey = db.row_source_key("price_compare", row)
    batch = normalize_batch(row.get("比价批次") or row.get("batch_key"))
    entry_id = int(row["id"]) if db.parse_decimal(row.get("id")) is not None else None
    ts = now_iso()
    status = str(row.get("status") or "排队中")
    existing = get_compare_task(skey)
    if existing is None and entry_id is not None:
        existing = get_compare_task(entry_id)
    with db.connect() as conn:
        if existing is None:
            tid = entry_id
            if tid is None:
                tid = conn.execute("SELECT COALESCE(MAX(id),0)+1 x FROM compare_task").fetchone()["x"]
            elif conn.execute("SELECT id FROM compare_task WHERE id=?", (tid,)).fetchone():
                tid = conn.execute("SELECT COALESCE(MAX(id),0)+1 x FROM compare_task").fetchone()["x"]
            conn.execute(
                """INSERT INTO compare_task(id,source_key,batch_key,goods_id,spec,status,source_side,
                   source_ref,legacy_id,data_json,migration_run_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, skey, batch, str(row.get("商品标识", "") or ""), str(row.get("归一规格", row.get("spec", "")) or ""),
                 status, str(row.get("source_side", "legacy")), skey, entry_id,
                 db.json_dumps(row), run_id, ts, ts),
            )
        else:
            tid = existing["id"]
            # 已有领取/依据/结果等交接字段不能被旧内存快照覆盖为空。
            merged = {**existing.get("data", {}), **row}
            conn.execute(
                """UPDATE compare_task SET batch_key=?,goods_id=?,spec=?,status=?,data_json=?,
                   migration_run_id=COALESCE(?,migration_run_id),updated_at=? WHERE id=?""",
                (batch or existing.get("batch_key"), str(row.get("商品标识", existing.get("goods_id", "")) or ""),
                 str(row.get("归一规格", row.get("spec", existing.get("spec", ""))) or ""),
                 status, db.json_dumps(merged), run_id, ts, tid),
            )
    db.upsert_snapshot("price_compare", row, batch_key=batch, key=skey)
    basis_keys = {
        "basis", "计价依据", "site_prices", "quotes", "报价", "站点报价", "sample_prices",
        "baseline_version_key", "price_version", "基线版本", "价格版本", "line_code", "baseline_id",
        "baseline_amount", "基线金额", "source", "来源", "source_ref", "evidence", "凭证",
    }
    if any(key in row for key in basis_keys):
        register_basis(tid, row)
    return get_compare_task(tid) or {}


def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    basis = db.json_loads(item.pop("basis_json", None), {})
    result = db.json_loads(item.pop("result_json", None), None)
    data = db.json_loads(item.pop("data_json", "{}"), {})
    blocking_missing = db.json_loads(item.pop("blocking_missing_json", "[]"), [])
    pending_verification = db.json_loads(item.pop("pending_verification_json", "[]"), [])
    item["basis"] = basis if isinstance(basis, dict) else {}
    item["result"] = result if isinstance(result, dict) else None
    item["data"] = data if isinstance(data, dict) else {}
    item["blocking_missing"] = blocking_missing if isinstance(blocking_missing, list) else []
    item["pending_verification"] = pending_verification if isinstance(pending_verification, list) else []
    item["basis_complete"] = bool(item.get("basis_complete"))
    item["confirmed"] = bool(item.get("confirmed"))
    # 合并原始业务字段，保持 /api/price_compare 响应契约。
    merged = dict(item["data"])
    merged.update({
        "id": item["id"],
        "source_key": item["source_key"],
        "比价批次": item.get("batch_key"),
        "商品标识": item.get("goods_id"),
        "归一规格": item.get("spec") or merged.get("归一规格", ""),
        "status": item.get("status"),
        "receipt_no": item.get("receipt_no"),
        "owner_side": item.get("owner_side"),
        "execution_side": item.get("execution_side"),
        "original_owner_side": item.get("original_owner_side"),
        "claim_state": item.get("claim_state"),
        "baseline_version_key": item.get("bound_version_key"),
        "bound_version_key": item.get("bound_version_key"),
        "bound_price": item.get("bound_amount"),
        "bound_currency": item.get("bound_currency"),
        "bound_spec": item.get("bound_spec"),
        "basis": item["basis"],
        "basis_complete": item["basis_complete"],
        "blocking_missing": item["blocking_missing"],
        "pending_verification": item["pending_verification"],
        "result": item["result"],
        "confirmed_result": item["confirmed"],
        "trace": {
            "source_side": item.get("source_side"),
            "source_ref": item.get("source_ref"),
            "legacy_id": item.get("legacy_id"),
            "snapshot_available": db.get_snapshot("price_compare", item["source_key"]) is not None,
        },
    })
    return merged


def list_compare_tasks() -> list[dict[str, Any]]:
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM compare_task ORDER BY id").fetchall()
    return [_task_dict(r) for r in rows]


def get_compare_task(identifier: Any) -> dict[str, Any] | None:
    db.init_db()
    with db.connect() as conn:
        row: sqlite3.Row | None = None
        if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
            row = conn.execute("SELECT * FROM compare_task WHERE id=?", (int(identifier),)).fetchone()
        if row is None and identifier is not None:
            row = conn.execute("SELECT * FROM compare_task WHERE source_key=?", (str(identifier),)).fetchone()
        if row is None and identifier is not None:
            row = conn.execute(
                "SELECT * FROM compare_task WHERE batch_key=? ORDER BY id LIMIT 1", (str(identifier),)
            ).fetchone()
    return _task_dict(row) if row else None


def ensure_task_from_identifier(identifier: Any) -> dict[str, Any] | None:
    task = get_compare_task(identifier)
    if task is not None:
        return task
    # 延迟导入避免循环。
    from app.store import store
    row = None
    text = str(identifier)
    if text.isdigit():
        row = store.find("price_compare", int(text))
    if row is None:
        row = next((r for r in store.rows("price_compare") if normalize_batch(r.get("比价批次")) == text), None)
    if row is None:
        snap = db.get_snapshot("price_compare", text)
        if snap:
            row = snap
    if row is None:
        return None
    return upsert_compare_task(row)


# ---------------------------------------------------------------------------
# 计价依据
# ---------------------------------------------------------------------------

def _as_price_list(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not value:
        return rows
    if isinstance(value, dict):
        for site, price in value.items():
            rows.append({"site": str(site), "price": price})
        return rows
    if not isinstance(value, list):
        return rows
    for item in value:
        if isinstance(item, dict):
            site = item.get("site") or item.get("站点") or item.get("站点名称") or item.get("name") or "未命名站点"
            price = item.get("price", item.get("价格", item.get("quote_price")))
            dec = db.parse_decimal(price)
            if dec is not None:
                rows.append({"site": str(site), "price_text": str(price), "price": dec})
        else:
            dec = db.parse_decimal(item)
            if dec is not None:
                rows.append({"site": f"样本{len(rows)+1}", "price_text": str(item), "price": dec})
    return rows


def _normalize_basis_input(payload: dict[str, Any]) -> dict[str, Any]:
    """兼容中英文/页面表单里常见的计价依据字段名。"""
    raw_basis = payload.get("basis")
    if raw_basis is None:
        raw_basis = payload.get("计价依据")
    incoming = raw_basis if isinstance(raw_basis, dict) else payload
    basis = dict(incoming if isinstance(incoming, dict) else {})
    if isinstance(payload.get("计价依据"), str):
        basis.setdefault("evidence", [payload["计价依据"]])
    aliases = {
        "baseline_version_key": ("baseline_version_key", "version_key", "price_version",
                                  "bound_version_key", "基线版本", "价格版本", "绑定版本"),
        "baseline_id": ("baseline_id", "基线ID", "基线编号"),
        "line_code": ("line_code", "价格行", "行号", "L编号"),
        "goods_id": ("goods_id", "商品标识"),
        "spec": ("spec", "归一规格", "绑定规格", "规格"),
        "currency": ("currency", "币种", "货币", "结算币种"),
        "baseline_amount": ("baseline_amount", "基线金额", "基线价格", "绑定价格", "amount"),
        "source": ("source", "来源", "计价来源", "取数来源"),
        "source_ref": ("source_ref", "来源引用", "source_key"),
        "formula": ("formula", "baseline_formula", "计算公式"),
        "lowest_site": ("lowest_site", "最低价站点", "最低站点"),
        "lowest_price": ("lowest_price", "最低价", "最低站点价格"),
        "site_prices": ("site_prices", "quotes", "报价", "站点报价", "实际报价"),
        "sample_prices": ("sample_prices", "samples", "样本价格", "计算样本"),
        "evidence": ("evidence", "证据", "凭证", "计价凭证"),
    }
    for target, names in aliases.items():
        if basis.get(target) not in (None, ""):
            continue
        for name in names:
            if payload.get(name) not in (None, ""):
                basis[target] = payload.get(name)
                break
            if isinstance(incoming, dict) and incoming.get(name) not in (None, ""):
                basis[target] = incoming.get(name)
                break
    if isinstance(basis.get("evidence"), str):
        basis["evidence"] = [basis["evidence"]]
    return basis


def register_basis(identifier: Any, payload: dict[str, Any]) -> dict[str, Any]:
    task = ensure_task_from_identifier(identifier)
    if task is None:
        return {"ok": False, "message": f"未办比价 {identifier} 不存在", "missing": []}
    incoming = _normalize_basis_input(payload)
    basis = {**task.get("basis", {}), **incoming}
    basis["registered"] = True
    basis["registered_at"] = now_iso()

    version = None
    version_ref = (
        basis.get("baseline_version_key")
        or basis.get("version_key")
        or basis.get("price_version")
        or basis.get("bound_version_key")
    )
    baseline_ref = basis.get("baseline_id") or basis.get("line_code") or basis.get("baseline") or version_ref
    if version_ref:
        version = db.get_baseline_version(version_ref)
        if version is None:
            base = db.get_baseline(version_ref)
            if base:
                version = db.get_baseline_version(base.get("baseline_version_key"))
    if version is None and baseline_ref is not None:
        base = db.get_baseline(baseline_ref)
        if base:
            version = db.get_baseline_version(base.get("baseline_version_key"))
            basis.setdefault("baseline_id", base.get("id"))
            basis.setdefault("line_code", base.get("line_code"))
    if version:
        basis.update({
            "baseline_version_key": version["version_key"],
            "baseline_id": version["baseline_id"],
            "line_code": version.get("line_code"),
            "goods_id": version.get("goods_id"),
            "spec": version.get("spec"),
            "currency": version.get("currency"),
            "baseline_amount": version.get("amount_decimal"),
            "source": basis.get("source") or version.get("source"),
            "source_ref": basis.get("source_ref") or version.get("source_ref"),
            "content_hash": version.get("content_hash"),
        })

    quotes = _as_price_list(basis.get("site_prices") or basis.get("quotes") or basis.get("报价"))
    samples = _as_price_list(basis.get("sample_prices") or basis.get("samples") or basis.get("样本价格"))
    if quotes:
        basis["site_prices"] = [{**q, "price": format(q["price"], "f") if isinstance(q.get("price"), Decimal) else q.get("price")} for q in quotes]
    if samples:
        basis["sample_prices"] = [{**s, "price": format(s["price"], "f") if isinstance(s.get("price"), Decimal) else s.get("price")} for s in samples]

    # 任务自身的商品/规格优先，依据里缺省时补齐。
    basis.setdefault("goods_id", task.get("goods_id"))
    basis.setdefault("spec", task.get("spec"))
    if not basis.get("currency"):
        bound_currency = task.get("bound_currency")
        if bound_currency:
            basis["currency"] = bound_currency

    missing: list[str] = []
    if not basis.get("baseline_version_key"):
        if db.parse_decimal(basis.get("baseline_amount")) is None:
            missing.append("基线金额 baseline_amount")
        if not (basis.get("source") or basis.get("source_ref")):
            missing.append("计价来源 source/source_ref")
        if not (basis.get("evidence") or basis.get("sample_prices") or basis.get("content_hash")):
            missing.append("可核验证据（样本、凭证或版本哈希）")
    if not basis.get("spec"):
        missing.append("绑定规格 spec/归一规格")
    if not basis.get("goods_id"):
        missing.append("商品标识 goods_id")

    pending: list[str] = []
    currency = str(basis.get("currency") or "").strip()
    if not currency or currency == "待核对":
        pending.append("币种（存量缺币种，待核对）")
        basis["currency"] = "待核对"
        basis["currency_status"] = "missing_pending_check"

    complete = not missing
    bound_version = basis.get("baseline_version_key")
    bound_amount = str(basis.get("baseline_amount") or "")
    with db.connect() as conn:
        conn.execute(
            """UPDATE compare_task SET basis_json=?,basis_complete=?,blocking_missing_json=?,
               pending_verification_json=?,bound_version_key=COALESCE(?,bound_version_key),
               bound_amount=COALESCE(?,bound_amount),bound_currency=?,bound_spec=?,updated_at=?
               WHERE id=?""",
            (db.json_dumps(basis), 1 if complete else 0, db.json_dumps(missing), db.json_dumps(pending),
             bound_version, bound_amount if bound_amount else None, basis.get("currency"),
             basis.get("spec"), now_iso(), task["id"]),
        )
    refreshed = get_compare_task(task["id"])
    return {
        "ok": True,
        "message": "计价依据已登记并绑定到任务" if complete else "计价依据已暂存，但仍有缺项；复算前会明确提示",
        "task": refreshed,
        "missing": missing,
        "pending_verification": pending,
    }


def _find_bound_version(task: dict[str, Any], payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any], list[str], list[str]]:
    payload = payload or {}
    raw_data_basis = _normalize_basis_input(task.get("data") if isinstance(task.get("data"), dict) else {})
    incoming_basis = _normalize_basis_input(payload)
    basis = {
        **raw_data_basis,
        **(task.get("basis") if isinstance(task.get("basis"), dict) else {}),
        **incoming_basis,
    }
    basis.setdefault("spec", task.get("spec"))
    basis.setdefault("currency", task.get("bound_currency"))
    basis.setdefault("baseline_version_key", task.get("bound_version_key"))
    version_ref = (
        basis.get("baseline_version_key") or task.get("bound_version_key")
        or basis.get("version_key") or basis.get("price_version")
    )
    version = db.get_baseline_version(version_ref) if version_ref else None
    if version is None:
        base_ref = basis.get("baseline_id") or basis.get("line_code") or basis.get("baseline")
        if base_ref is not None:
            base = db.get_baseline(base_ref)
            if base:
                version = db.get_baseline_version(base.get("baseline_version_key"))
                version_ref = version["version_key"] if version else None
    missing: list[str] = []
    if version is None and db.parse_decimal(basis.get("baseline_amount")) is None:
        missing.append("绑定价格版本或基线金额")
    if not (basis.get("spec") or task.get("spec")):
        missing.append("实际读到并绑定的商品规格")
    if not (basis.get("source") or basis.get("source_ref") or version):
        missing.append("价格/计价依据来源")
    pending = []
    currency = basis.get("currency") or task.get("bound_currency") or (version or {}).get("currency")
    if not currency or currency == "待核对":
        pending.append("币种（存量缺币种，待核对）")
    if version is not None:
        basis.setdefault("baseline_amount", version.get("amount_decimal"))
        basis.setdefault("source", version.get("source"))
        basis.setdefault("spec", version.get("spec"))
        basis.setdefault("currency", version.get("currency"))
        basis["baseline_version_key"] = version["version_key"]
    return version, basis, missing, pending


def calculate_baseline(basis: dict[str, Any], version: dict[str, Any] | None) -> dict[str, Any]:
    samples = _as_price_list(basis.get("sample_prices") or basis.get("samples"))
    formula = str(basis.get("baseline_formula") or basis.get("formula") or "average").strip().lower()
    values = [s["price"] for s in samples if isinstance(s.get("price"), Decimal)]
    recomputed: Decimal | None = None
    method = formula
    if values:
        if formula in {"avg", "average", "mean", "算术平均"}:
            recomputed = sum(values, Decimal("0")) / Decimal(len(values))
            method = "arithmetic_mean"
        elif formula in {"min", "minimum", "最低价"}:
            recomputed = min(values)
            method = "minimum"
        elif formula in {"max", "maximum", "最高价"}:
            recomputed = max(values)
            method = "maximum"
        elif formula in {"median", "中位数"}:
            ordered = sorted(values)
            n = len(ordered)
            recomputed = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
            method = "median"
        else:
            method = f"unsupported:{formula}"
    stored = db.parse_decimal((version or {}).get("amount_decimal") or basis.get("baseline_amount"))
    if recomputed is None and version is not None:
        # 不可变版本 + 哈希校验通过时，可按原计价依据回放，而不是让当前价覆盖旧价。
        expected_hash = db.content_hash({
            "baseline_id": version["baseline_id"],
            "version_no": version["version_no"],
            "goods_id": version.get("goods_id"),
            "spec": version.get("spec"),
            "currency": version.get("currency"),
            "amount_decimal": version.get("amount_decimal"),
            "source_ref": version.get("source_ref"),
        })
        hash_ok = expected_hash == version.get("content_hash")
        return {
            "method": "immutable_version_replay",
            "sample_count": 0,
            "samples_used": [],
            "recomputed_amount": format(stored, "f") if stored is not None else None,
            "stored_amount": format(stored, "f") if stored is not None else None,
            "matches": hash_ok,
            "content_hash_verified": hash_ok,
            "note": "未提供可重算样本；已回放并校验绑定的不可变价格版本",
        }
    matches = recomputed is not None and stored is not None and recomputed == stored
    return {
        "method": method,
        "sample_count": len(values),
        "samples_used": [format(v, "f") for v in values],
        "recomputed_amount": format(recomputed, "f") if recomputed is not None else None,
        "stored_amount": format(stored, "f") if stored is not None else None,
        "matches": matches,
        "content_hash_verified": bool(version),
    }


def recompute_baseline(identifier: Any, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    task = ensure_task_from_identifier(identifier)
    if task is None:
        return {"ok": False, "code": "task_not_found", "message": f"比价任务 {identifier} 不存在"}
    version, basis, missing, pending = _find_bound_version(task, payload)
    original_record = task.get("data") or task
    if missing:
        return {
            "ok": False,
            "code": "missing_calculation_basis",
            "message": "缺少实际计算依据，复算前需先补齐：" + "、".join(missing) + "。可先查看原记录，不能用当前基线价替代旧结果。",
            "missing": missing,
            "pending_verification": pending,
            "original_record": original_record,
            "can_view_original": True,
            "task": task,
        }
    calc = calculate_baseline(basis, version)
    current = None
    if version:
        base = db.get_baseline(version["baseline_id"])
        current = {
            "current_version_key": base.get("baseline_version_key") if base else None,
            "current_amount": base.get("amount_decimal") if base else None,
        }
    old_result_price = (task.get("result") or {}).get("price") or task.get("bound_amount")
    return {
        "ok": bool(calc.get("matches")),
        "code": "baseline_recomputed" if calc.get("matches") else "baseline_mismatch",
        "message": (
            f"已按 {basis.get('baseline_version_key') or '登记依据'} 复算，基线 {calc.get('stored_amount')} 一致"
            if calc.get("matches") else
            f"复算结果 {calc.get('recomputed_amount')} 与绑定基线 {calc.get('stored_amount')} 不一致"
        ),
        "missing": [],
        "pending_verification": pending,
        "recomputation": calc,
        "current_baseline": current,
        "price_changed_since_bound": bool(
            current and current.get("current_amount") not in (None, calc.get("stored_amount"))
        ),
        "old_result_price": old_result_price,
        "old_result_protected": True,
        "original_record": original_record,
        "task": task,
    }


# ---------------------------------------------------------------------------
# Claim / receipt
# ---------------------------------------------------------------------------

def _find_auto_baseline(task: dict[str, Any]) -> dict[str, Any] | None:
    goods = str(task.get("goods_id") or "").strip()
    spec = str(task.get("spec") or "").strip()
    candidates = db.list_baselines(confirmed_only=True)
    if goods:
        exact_goods = [b for b in candidates if str(b.get("商品标识")) == goods]
        if exact_goods:
            candidates = exact_goods
    if spec:
        exact_spec = [b for b in candidates if str(b.get("归一规格")) == spec]
        if len(exact_spec) == 1:
            base = exact_spec[0]
            return db.get_baseline_version(base.get("baseline_version_key"))
        if len(exact_spec) > 1:
            return None
    if len(candidates) == 1:
        base = candidates[0]
        return db.get_baseline_version(base.get("baseline_version_key"))
    return None


def _claim_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["price_snapshot"] = db.json_loads(item.pop("price_snapshot_json", "{}"))
    return item


def get_claim(batch: str) -> dict[str, Any] | None:
    db.init_db()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM claim WHERE claim_key=?", (claim_key_for(batch),)).fetchone()
    return _claim_dict(row) if row else None


def claim_round(
    batch: Any,
    side: str,
    *,
    task_identifier: Any = None,
    idempotency_key: str | None = None,
    operator: str | None = None,
    allow_takeover: bool = False,
) -> dict[str, Any]:
    db.init_db()
    batch = normalize_batch(batch)
    if not batch:
        return {"ok": False, "code": "batch_required", "message": "必须提供比价批次/轮次"}
    side = "legacy" if side in {"legacy", "old", "旧入口"} else "new" if side in {"new", "新入口"} else side
    ckey = claim_key_for(batch)
    existing = get_claim(batch)
    if existing is not None:
        same_idempotency = idempotency_key and existing.get("idempotency_key") == idempotency_key
        same_side = existing.get("owner_side") == side or existing.get("execution_side") == side
        if same_idempotency:
            return {"ok": True, "duplicate": True, "idempotent_replay": True, "claim": existing,
                    "receipt": build_receipt(batch, requested_side=side)["receipt"],
                    "message": "相同幂等键重放：返回原受理回执，未复制第二份受理"}
        if same_side or allow_takeover:
            if allow_takeover and existing.get("execution_side") != side:
                with db.connect() as conn:
                    conn.execute("UPDATE claim SET execution_side=?,updated_at=? WHERE claim_key=?", (side, now_iso(), ckey))
                    conn.execute("UPDATE compare_task SET execution_side=? WHERE receipt_no=?", (side, existing["receipt_no"]))
                db.add_event("claim_takeover", batch_key=batch, side=side,
                             payload={"from": existing.get("execution_side"), "to": side},
                             event_key=f"takeover:{ckey}:{side}:{existing.get('updated_at')}")
                existing = get_claim(batch) or existing
            return {"ok": True, "duplicate": True, "claim": existing,
                    "receipt": build_receipt(batch, requested_side=side)["receipt"],
                    "message": "该轮比价已有唯一受理回执；本次按同一申请继续，未新增结果"}
        receipt = build_receipt(batch, requested_side=side)["receipt"]
        return {
            "ok": False,
            "duplicate": True,
            "conflict": True,
            "http_status": 409,
            "code": "already_claimed_by_other_entry",
            "message": f"批次 {batch} 已由{side_name(existing.get('owner_side'))}受理（回执 {existing['receipt_no']}）；"
                       "另一入口不能再领取为第二份有效结果，只能凭原回执继续或查看已确认结果",
            "claim": existing,
            "receipt": receipt,
        }

    task = None
    if task_identifier is not None:
        task = ensure_task_from_identifier(task_identifier)
    if task is None:
        task = ensure_task_from_identifier(batch)
    version = None
    if task:
        version, basis, _, _ = _find_bound_version(task, {})
        if version is None:
            version = _find_auto_baseline(task)
        if version is not None and not task.get("bound_version_key"):
            register_basis(task["id"], {
                "baseline_version_key": version["version_key"],
                "source": version.get("source"),
            })
            task = get_compare_task(task["id"]) or task
    receipt_no = f"RCP-{safe_receipt_fragment(batch)}-0001"
    ts = now_iso()
    snapshot = {
        "batch": batch,
        "claimed_side": side,
        "baseline_version_key": (version or {}).get("version_key") or (task or {}).get("bound_version_key"),
        "goods_id": (version or {}).get("goods_id") or (task or {}).get("goods_id"),
        "spec": (version or {}).get("spec") or (task or {}).get("spec"),
        "currency": (version or {}).get("currency") or (task or {}).get("bound_currency") or "待核对",
        "price": (version or {}).get("amount_decimal") or (task or {}).get("bound_amount"),
        "captured_at": ts,
    }
    try:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO claim(claim_key,batch_key,scope_key,owner_side,execution_side,original_owner_side,
                   state,receipt_no,idempotency_key,task_source_key,bound_version_key,price_snapshot_json,
                   operator,created_at,updated_at)
                   VALUES(?,?,'batch',?,?,?,'accepted',?,?,?,?,?,?,?,?)""",
                (ckey, batch, side, side, side, receipt_no, idempotency_key,
                 task.get("source_key") if task else None, snapshot["baseline_version_key"],
                 db.json_dumps(snapshot), operator, ts, ts),
            )
            if task:
                conn.execute(
                    """UPDATE compare_task SET owner_side=?,execution_side=?,original_owner_side=?,
                       claim_state='accepted',receipt_no=?,updated_at=? WHERE id=?""",
                    (side, side, side, receipt_no, ts, task["id"]),
                )
    except sqlite3.IntegrityError:
        # 并发或重复请求：唯一键已经保证只有一份受理。
        raced = get_claim(batch)
        return {"ok": False, "duplicate": True, "conflict": True, "http_status": 409,
                "code": "claim_already_exists", "claim": raced,
                "message": "该轮比价已被领取；唯一约束阻止了第二份受理"}
    db.add_event("claim_accepted", batch_key=batch, side=side,
                 payload={"receipt_no": receipt_no, "snapshot": snapshot, "operator": operator},
                 event_key=f"claim:{ckey}")
    claim = get_claim(batch)
    return {"ok": True, "duplicate": False, "claim": claim,
            "receipt": build_receipt(batch, requested_side=side)["receipt"],
            "message": f"批次 {batch} 已由{side_name(side)}唯一受理，回执 {receipt_no}"}


def _result_for_claim(claim: dict[str, Any]) -> dict[str, Any] | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM compare_result WHERE claim_key=? ORDER BY created_at LIMIT 1",
                           (claim["claim_key"],)).fetchone()
    if not row:
        return None
    item = dict(row)
    payload = db.json_loads(item.pop("payload_json", "{}"))
    item["payload"] = payload
    if isinstance(payload, dict):
        # 把 price / spread_ratio 等计算回执放到顶层，方便调用方直接断言。
        item.update(payload)
    return item


def build_receipt(batch: Any, requested_side: str | None = None) -> dict[str, Any]:
    batch = normalize_batch(batch)
    claim = get_claim(batch)
    task = ensure_task_from_identifier(batch)
    result = _result_for_claim(claim) if claim else None
    if result is None and task and task.get("result"):
        result = task["result"]
    original_record = None
    if claim and claim.get("task_source_key"):
        original_record = db.get_snapshot("price_compare", claim["task_source_key"])
    if original_record is None and task:
        original_record = task.get("data") or db.get_snapshot("price_compare", task.get("source_key", ""))
    if original_record is None:
        from app.store import store
        row = next((r for r in store.rows("price_compare") if normalize_batch(r.get("比价批次")) == batch), None)
        original_record = row

    is_confirmed = bool(result or (task and task.get("status") == "已计算") or (claim and claim.get("state") == "completed"))
    kind = "confirmed_result" if is_confirmed else "actionable_todo"
    current_version = None
    correction = None
    bound_key = (result or {}).get("baseline_version_key") or (claim or {}).get("bound_version_key") or (task or {}).get("bound_version_key")
    bound_version = db.get_baseline_version(bound_key) if bound_key else None
    if bound_version:
        base = db.get_baseline(bound_version["baseline_id"])
        if base:
            current_version = {
                "current_version_key": base.get("baseline_version_key"),
                "current_amount": base.get("amount_decimal"),
                "currency": base.get("币种"),
            }
            if base.get("baseline_version_key") != bound_key:
                correction = {
                    "correction_handed_over": True,
                    "bound_version_key": bound_key,
                    "bound_price": bound_version.get("amount_decimal"),
                    "current_version_key": base.get("baseline_version_key"),
                    "current_price": base.get("amount_decimal"),
                    "policy": "旧结果继续绑定原版本原价；如需按新价计算，必须显式改绑新版本",
                }
    migration_comparison = None
    if bound_version and task:
        bound_amount = db.parse_decimal(bound_version.get("amount_decimal"))
        current_amount = db.parse_decimal(current_version.get("current_amount")) if current_version else bound_amount
        delta = None if bound_amount is None or current_amount is None else current_amount - bound_amount
        migration_comparison = {
            "can_compare_after_rollback": True,
            "old_task_id": task.get("id"),
            "old_batch": batch,
            "old_task_source_key": task.get("source_key"),
            "old_record_source": task.get("source_ref") or task.get("trace", {}).get("source_ref"),
            "bound_goods_id": bound_version.get("goods_id"),
            "bound_spec": bound_version.get("spec"),
            "bound_version_key": bound_key,
            "bound_price": bound_version.get("amount_decimal"),
            "migrated_current_version": (current_version or {}).get("current_version_key", bound_key),
            "migrated_current_price": (current_version or {}).get("current_amount", bound_version.get("amount_decimal")),
            "currency": bound_version.get("currency") or "待核对",
            "currency_pending_check": (bound_version.get("currency") in (None, "", "待核对")),
            "price_delta": format(delta, "f") if delta is not None else None,
            "price_changed": bool(delta not in (None, Decimal("0"))),
            "note": "回滚只切换受理入口，不删除 SQLite 迁移基线；可继续用旧任务绑定版本与迁移后当前版本比对",
        }
    if claim:
        explanation = (
            f"批次 {batch} 的唯一受理回执是 {claim['receipt_no']}，原始受理方为{side_name(claim.get('original_owner_side') or claim.get('owner_side'))}；"
            f"当前执行侧为{side_name(claim.get('execution_side'))}。"
        )
        if result:
            explanation += " 已有确认结果，任何入口重复领取都不会产生第二份有效结果。"
        else:
            explanation += " 尚无可交付结果，可凭同一回执继续待办。"
    elif is_confirmed:
        explanation = f"批次 {batch} 在旧内存记录中已是已计算结果，但没有迁移后的领取回执；可查看原记录追溯。"
    else:
        explanation = f"批次 {batch} 尚未被任何入口领取；旧入口或新入口首次领取后，另一侧只能重放同一回执。"

    active = db.active_entry()
    can_operate = kind == "actionable_todo"
    this_entry_can_continue = can_operate and (
        requested_side is None
        or claim is None
        or claim.get("execution_side") == requested_side
        or active == requested_side
    )
    receipt = {
        "ok": True,
        "http_status": 200,
        "receipt_type": kind,
        "kind": kind,
        "batch": batch,
        "requested_entry": requested_side,
        "requested_entry_name": side_name(requested_side) if requested_side else None,
        "active_entry": active,
        "active_entry_name": side_name(active),
        "actionable": can_operate,
        "can_operate": can_operate,
        "this_entry_can_continue": this_entry_can_continue,
        "duplicate_claim_blocked": claim is not None,
        "confirmed": is_confirmed,
        "claim": claim,
        "result": result,
        "task": task,
        "bound_version": bound_version,
        "current_baseline": current_version,
        "correction": correction,
        "migration_comparison": migration_comparison,
        "trace": {
            "original_owner_side": (claim or {}).get("original_owner_side") or (task or {}).get("original_owner_side"),
            "execution_side": (claim or {}).get("execution_side") or (task or {}).get("execution_side"),
            "source_side": (task or {}).get("source_side", "legacy"),
            "source_ref": (task or {}).get("source_ref"),
            "legacy_id": (task or {}).get("legacy_id"),
            "original_record_available": original_record is not None,
            "original_record_endpoint": f"/api/price_compare/{(task or {}).get('id')}" if task else None,
            "explanation": explanation,
        },
        "explanation": explanation,
        "allowed_actions": (
            ["view_original", "register_basis", "recompute_baseline"]
            if kind == "confirmed_result"
            else ["claim", "continue", "register_basis"] + (["retry"] if task and task.get("status") == "计算失败" else [])
        ),
        "events": db.list_events(batch, limit=20),
        "original_record": original_record,
        "generated_at": now_iso(),
    }
    return {"ok": True, "receipt": receipt}


def get_continuation(batch: Any, requested_side: str | None = None) -> dict[str, Any]:
    return build_receipt(batch, requested_side)


# ---------------------------------------------------------------------------
# Execute / complete
# ---------------------------------------------------------------------------

def _update_legacy_row_after_result(task: dict[str, Any], result_payload: dict[str, Any]) -> None:
    from app.store import store
    row = None
    legacy_id = task.get("legacy_id") or task.get("id")
    if legacy_id is not None:
        row = store.find("price_compare", int(legacy_id))
    if row is None:
        skey = task.get("source_key")
        row = next((r for r in store.rows("price_compare") if db.row_source_key("price_compare", r) == skey), None)
    if row is None:
        row = {"id": task.get("id")}
        store.rows("price_compare").append(row)
    row.update({
        "比价批次": task.get("batch_key"),
        "商品标识": task.get("goods_id"),
        "归一规格": task.get("spec"),
        "最低价站点": result_payload.get("lowest_site", row.get("最低价站点", "")),
        "价差比例": result_payload.get("spread_ratio", row.get("价差比例", "0.0%")),
        "计算时间": result_payload.get("calculated_at", now_iso()),
        "status": "已计算",
        "pending": False,
        "abnormal": False,
        "source_key": task.get("source_key"),
        "receipt_no": result_payload.get("receipt_no"),
        "baseline_version_key": result_payload.get("baseline_version_key"),
        "bound_price": result_payload.get("price"),
        "bound_currency": result_payload.get("currency"),
    })
    db.upsert_snapshot("price_compare", row, batch_key=task.get("batch_key"), key=task.get("source_key"))


def execute_continuation(batch: Any, requested_side: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    batch = normalize_batch(batch)
    requested_side = "legacy" if requested_side in {"legacy", "old"} else "new" if requested_side == "new" else requested_side
    receipt_wrap = build_receipt(batch, requested_side)
    receipt = receipt_wrap["receipt"]
    if receipt["kind"] == "confirmed_result":
        return {"ok": True, "duplicate": True, "already_confirmed": True, "receipt": receipt,
                "message": "该轮比价已有确认结果；续办接口只回传原结果和回执，不会重新计价或产生第二份结果"}

    claim = receipt.get("claim")
    if claim is None:
        claimed = claim_round(batch, requested_side, task_identifier=payload.get("task_id"),
                              idempotency_key=payload.get("idempotency_key"), operator=payload.get("operator"))
        if not claimed.get("ok"):
            return claimed
        claim = claimed["claim"]
    elif claim.get("execution_side") != requested_side:
        active = db.active_entry()
        if active == requested_side or payload.get("allow_takeover"):
            claimed = claim_round(batch, requested_side, allow_takeover=True,
                                  idempotency_key=payload.get("idempotency_key"), operator=payload.get("operator"))
            if not claimed.get("ok"):
                return claimed
            claim = claimed["claim"]
        else:
            return {"ok": False, "conflict": True, "http_status": 409,
                    "code": "entry_not_owner_after_rollback",
                    "message": f"当前执行侧是{side_name(claim.get('execution_side'))}；未发生对应接管，不能由{side_name(requested_side)}重复领取",
                    "receipt": receipt}

    task = ensure_task_from_identifier(payload.get("task_id") or claim.get("task_source_key") or batch)
    if task is None:
        task = ensure_task_from_identifier(batch)
    if task is None:
        return {"ok": False, "code": "task_not_found", "message": f"找不到批次 {batch} 对应的未办任务", "receipt": receipt}
    basis_keys = {
        "basis", "site_prices", "quotes", "sample_prices", "samples", "baseline_version_key",
        "version_key", "price_version", "baseline_id", "line_code", "baseline_amount",
        "currency", "spec", "source", "source_ref", "evidence", "formula", "lowest_price",
    }
    if any(key in payload for key in basis_keys):
        reg = register_basis(task["id"], payload)
        task = reg.get("task") or get_compare_task(task["id"])
        if reg.get("missing"):
            # 依据已暂存但缺关键项：保持待办并明确提示。
            return {"ok": False, "code": "missing_calculation_basis", "message": reg["message"],
                    "missing": reg.get("missing", []), "pending_verification": reg.get("pending_verification", []),
                    "receipt": build_receipt(batch, requested_side)["receipt"]}

    version, basis, missing, pending = _find_bound_version(task, payload)
    if missing:
        with db.connect() as conn:
            conn.execute("UPDATE compare_task SET status='计算失败',updated_at=? WHERE id=?", (now_iso(), task["id"]))
        from app.store import store
        row = store.find("price_compare", int(task["id"]))
        if row:
            row["status"] = "计算失败"
            row["abnormal"] = True
            db.upsert_snapshot("price_compare", row, batch_key=batch)
        return {
            "ok": False,
            "code": "missing_calculation_basis",
            "message": "缺少实际计算依据，不能复算/交付：" + "、".join(missing) + "。请查看原记录并补齐后再继续。",
            "missing": missing,
            "pending_verification": pending,
            "original_record": task.get("data"),
            "receipt": build_receipt(batch, requested_side)["receipt"],
        }

    baseline_price = db.parse_decimal((version or {}).get("amount_decimal") or basis.get("baseline_amount"))
    quotes = _as_price_list(basis.get("site_prices") or basis.get("quotes"))
    lowest_price = db.parse_decimal(basis.get("lowest_price"))
    lowest_site = basis.get("lowest_site") or ""
    if quotes:
        lowest_quote = min(quotes, key=lambda q: q["price"])
        lowest_price = lowest_quote["price"]
        lowest_site = lowest_site or lowest_quote["site"]
    if lowest_price is None:
        # 没有跨站点报价时不能编造最低价/价差；申请仍为可操作待办。
        return {
            "ok": True,
            "code": "awaiting_site_quotes",
            "kind": "actionable_todo",
            "message": "计价基线已绑定，但缺少各站点实际报价/最低价，尚不能交付比价结果；这仍是可操作待办",
            "pending_verification": pending + ["站点实际报价 site_prices/lowest_price"],
            "receipt": build_receipt(batch, requested_side)["receipt"],
        }
    if baseline_price is None or baseline_price == 0:
        return {"ok": False, "code": "invalid_baseline_price",
                "message": "基线价格无效或为 0，不能计算价差比例", "receipt": build_receipt(batch, requested_side)["receipt"]}
    ratio = (lowest_price - baseline_price) / baseline_price
    ratio_percent = (ratio * Decimal("100")).quantize(Decimal("0.01"))
    spread_ratio = f"{ratio_percent}%"
    ts = now_iso()
    result_key = f"result:{claim['claim_key']}:{task['source_key']}"
    result_payload = {
        "result_key": result_key,
        "batch": batch,
        "task_id": task["id"],
        "receipt_no": claim["receipt_no"],
        "owner_side": claim.get("owner_side"),
        "execution_side": requested_side,
        "baseline_version_key": basis.get("baseline_version_key"),
        "price": format(baseline_price, "f"),
        "currency": basis.get("currency") or "待核对",
        "spec": basis.get("spec") or task.get("spec"),
        "lowest_site": lowest_site,
        "lowest_price": format(lowest_price, "f"),
        "spread_ratio": spread_ratio,
        "ratio": format(ratio, "f"),
        "pending_verification": pending,
        "calculated_at": ts,
        "formula": "(lowest_site_price - baseline_price) / baseline_price",
    }
    try:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO compare_result(result_key,claim_key,batch_key,task_source_key,entry_id,owner_side,
                   baseline_version_key,price_text,currency,spec,payload_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result_key, claim["claim_key"], batch, task["source_key"], task["id"], requested_side,
                 result_payload["baseline_version_key"], result_payload["price"], result_payload["currency"],
                 result_payload["spec"], db.json_dumps(result_payload), ts),
            )
            conn.execute(
                """UPDATE compare_task SET status='已计算',confirmed=1,claim_state='confirmed',result_key=?,
                   result_json=?,bound_version_key=?,bound_amount=?,bound_currency=?,bound_spec=?,updated_at=?
                   WHERE id=?""",
                (result_key, db.json_dumps(result_payload), result_payload["baseline_version_key"],
                 result_payload["price"], result_payload["currency"], result_payload["spec"], ts, task["id"]),
            )
            conn.execute(
                "UPDATE claim SET state='completed',result_key=?,updated_at=? WHERE claim_key=?",
                (result_key, ts, claim["claim_key"]),
            )
    except sqlite3.IntegrityError:
        existing_receipt = build_receipt(batch, requested_side)["receipt"]
        return {"ok": True, "duplicate": True, "already_confirmed": True, "receipt": existing_receipt,
                "message": "唯一结果约束已存在；重复继续返回原结果，未换价"}
    _update_legacy_row_after_result(task, result_payload)
    db.add_event("compare_confirmed", batch_key=batch, side=requested_side,
                 payload=result_payload, event_key=result_key)
    return {"ok": True, "duplicate": False, "already_confirmed": True, "receipt": build_receipt(batch, requested_side)["receipt"],
            "result": result_payload, "message": f"批次 {batch} 已按绑定版本 {result_payload['baseline_version_key']} 计算并确认，旧价 {result_payload['price']} 被固定"}
