"""SQLite 持久化与迁移原语。

这里只依赖 Python 标准库，保证干净环境可直接运行。数据库路径可通过
``PRICE_WATCH_SQLITE_PATH`` / ``PRICE_WATCH_DB`` / ``DATABASE_PATH`` 覆盖；
也兼容 ``DATABASE_URL=sqlite:////abs/path``。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "price_watch.sqlite3"

MIGRATION_KEY = "memory-baseline-compare-v1"
SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> Path:
    raw = (
        os.getenv("PRICE_WATCH_SQLITE_PATH")
        or os.getenv("PRICE_WATCH_DB")
        or os.getenv("DATABASE_PATH")
        or ""
    ).strip()
    if not raw:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if database_url.startswith("sqlite:"):
            rest = database_url[len("sqlite:"):]
            if rest.startswith("////"):
                # sqlite:////absolute/path -> /absolute/path
                raw = rest[3:]
            elif rest.startswith("///"):
                # sqlite:///relative/path 或 sqlite:///./relative
                raw = rest[3:]
            elif rest.startswith("//"):
                raw = rest[2:]
            else:
                raw = rest
    if not raw:
        path = DEFAULT_DB_PATH
    elif raw == ":memory:":
        path = Path(":memory:")
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = BACKEND_DIR / path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = get_db_path()
    # :memory: 仅用于单进程内临时测试；普通部署使用文件库，重启后仍在。
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_state (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              phase TEXT NOT NULL,
              active_entry TEXT NOT NULL,
              current_run_id TEXT,
              updated_at TEXT NOT NULL,
              payload_json TEXT
            );

            CREATE TABLE IF NOT EXISTS migration_run (
              run_id TEXT PRIMARY KEY,
              idempotency_key TEXT UNIQUE NOT NULL,
              phase TEXT NOT NULL,
              activated INTEGER NOT NULL DEFAULT 0,
              source_counts_json TEXT,
              target_counts_json TEXT,
              checksum TEXT,
              report_json TEXT,
              error TEXT,
              started_at TEXT NOT NULL,
              verified_at TEXT,
              activated_at TEXT,
              rolled_back_at TEXT
            );

            CREATE TABLE IF NOT EXISTS legacy_snapshot (
              module TEXT NOT NULL,
              source_key TEXT NOT NULL,
              entry_id INTEGER,
              batch_key TEXT,
              data_json TEXT NOT NULL,
              source_side TEXT NOT NULL DEFAULT 'legacy',
              updated_at TEXT NOT NULL,
              PRIMARY KEY (module, source_key)
            );

            CREATE TABLE IF NOT EXISTS baseline (
              id INTEGER PRIMARY KEY,
              source_key TEXT UNIQUE NOT NULL,
              line_code TEXT,
              goods_id TEXT,
              spec TEXT,
              currency TEXT,
              currency_status TEXT NOT NULL DEFAULT 'pending_check',
              amount_text TEXT,
              amount_decimal TEXT,
              current_version_no INTEGER NOT NULL DEFAULT 1,
              status TEXT,
              source TEXT,
              source_ref TEXT,
              verification_status TEXT NOT NULL DEFAULT 'migrated_unverified',
              confirmed INTEGER NOT NULL DEFAULT 0,
              data_json TEXT NOT NULL,
              migration_run_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS baseline_version (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              baseline_id INTEGER NOT NULL,
              version_no INTEGER NOT NULL,
              version_key TEXT UNIQUE NOT NULL,
              aliases_json TEXT NOT NULL DEFAULT '[]',
              line_code TEXT,
              goods_id TEXT,
              spec TEXT,
              currency TEXT,
              amount_text TEXT NOT NULL,
              amount_decimal TEXT NOT NULL,
              source TEXT,
              source_ref TEXT,
              content_hash TEXT NOT NULL,
              correction_reason TEXT,
              confirmed INTEGER NOT NULL DEFAULT 1,
              migration_run_id TEXT,
              data_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE (baseline_id, version_no)
            );

            CREATE TABLE IF NOT EXISTS compare_task (
              id INTEGER PRIMARY KEY,
              source_key TEXT UNIQUE NOT NULL,
              batch_key TEXT,
              goods_id TEXT,
              spec TEXT,
              status TEXT,
              owner_side TEXT,
              execution_side TEXT,
              claim_state TEXT,
              receipt_no TEXT,
              original_owner_side TEXT,
              bound_baseline_id INTEGER,
              bound_version_key TEXT,
              bound_amount TEXT,
              bound_currency TEXT,
              bound_spec TEXT,
              basis_json TEXT,
              basis_complete INTEGER NOT NULL DEFAULT 0,
              blocking_missing_json TEXT NOT NULL DEFAULT '[]',
              pending_verification_json TEXT NOT NULL DEFAULT '[]',
              result_json TEXT,
              result_key TEXT UNIQUE,
              confirmed INTEGER NOT NULL DEFAULT 0,
              source_side TEXT NOT NULL DEFAULT 'legacy',
              source_ref TEXT,
              legacy_id INTEGER,
              data_json TEXT NOT NULL,
              migration_run_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS claim (
              claim_key TEXT PRIMARY KEY,
              batch_key TEXT NOT NULL,
              scope_key TEXT NOT NULL DEFAULT 'batch',
              owner_side TEXT NOT NULL,
              execution_side TEXT NOT NULL,
              original_owner_side TEXT NOT NULL,
              state TEXT NOT NULL,
              receipt_no TEXT UNIQUE NOT NULL,
              idempotency_key TEXT UNIQUE,
              task_source_key TEXT,
              bound_version_key TEXT,
              price_snapshot_json TEXT,
              result_key TEXT,
              operator TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS compare_result (
              result_key TEXT PRIMARY KEY,
              claim_key TEXT NOT NULL,
              batch_key TEXT NOT NULL,
              task_source_key TEXT NOT NULL,
              entry_id INTEGER,
              owner_side TEXT NOT NULL,
              baseline_version_key TEXT,
              price_text TEXT,
              currency TEXT,
              spec TEXT,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE (claim_key, task_source_key)
            );

            CREATE TABLE IF NOT EXISTS handoff_event (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_key TEXT UNIQUE,
              batch_key TEXT,
              event_type TEXT NOT NULL,
              side TEXT,
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );

            CREATE VIEW IF NOT EXISTS price_baseline AS
            SELECT
              id, source_key, line_code, goods_id AS "商品标识", spec AS "归一规格",
              currency AS "币种", currency_status, amount_text AS "基线价格",
              amount_decimal, current_version_no, status, source AS "来源", source_ref,
              verification_status, confirmed, data_json, migration_run_id, created_at, updated_at
            FROM baseline;

            CREATE VIEW IF NOT EXISTS price_compare AS
            SELECT
              id, source_key, batch_key AS "比价批次", goods_id AS "商品标识", spec AS "归一规格",
              status, owner_side, execution_side, original_owner_side, claim_state, receipt_no,
              bound_baseline_id, bound_version_key, bound_amount AS "绑定价格",
              bound_currency AS "绑定币种", bound_spec AS "绑定规格",
              basis_complete, confirmed, result_key, source_side, source_ref, legacy_id,
              data_json, migration_run_id, created_at, updated_at
            FROM compare_task;

            CREATE INDEX IF NOT EXISTS idx_baseline_goods_spec ON baseline(goods_id, spec);
            CREATE INDEX IF NOT EXISTS idx_baseline_version_lookup ON baseline_version(version_key, line_code);
            CREATE INDEX IF NOT EXISTS idx_compare_batch ON compare_task(batch_key);
            CREATE INDEX IF NOT EXISTS idx_claim_batch ON claim(batch_key);
            CREATE INDEX IF NOT EXISTS idx_event_batch ON handoff_event(batch_key, id);
            """
        )
        row = conn.execute("SELECT id FROM app_state WHERE id=1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO app_state(id, phase, active_entry, updated_at, payload_json) "
                "VALUES(1, 'legacy', 'legacy', ?, '{}')",
                (now_iso(),),
            )


def reset_database_file() -> None:
    """删除文件库；主要供隔离测试和演练重置使用。"""
    path = get_db_path()
    if str(path) == ":memory:":
        init_db()
        return
    with contextmanager(lambda: iter(()))():  # pragma: no cover - no-op readability
        pass
    if path.exists():
        path.unlink()
    init_db()


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def json_loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return {} if default is None else default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def source_key(module: str, entry_id: Any) -> str:
    return f"memory:{module}:{int(entry_id)}"


def row_source_key(module: str, row: dict[str, Any]) -> str:
    explicit = row.get("source_key") or row.get("_source_key")
    if explicit:
        return str(explicit)
    return source_key(module, row.get("id", 0))


def parse_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def amount_pair(value: Any) -> tuple[str, str] | None:
    dec = parse_decimal(value)
    if dec is None:
        return None
    # 规范化用于校验/哈希；展示值仍优先保留原 JSON 类型。
    normalized = format(dec.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in ("", "-0"):
        normalized = "0"
    display = str(value).strip() if isinstance(value, str) and str(value).strip() else normalized
    return display, normalized


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def add_event(
    event_type: str,
    *,
    batch_key: str | None = None,
    side: str | None = None,
    payload: dict[str, Any] | None = None,
    event_key: str | None = None,
) -> None:
    init_db()
    with connect() as conn:
        if event_key:
            exists = conn.execute(
                "SELECT id FROM handoff_event WHERE event_key=?", (event_key,)
            ).fetchone()
            if exists:
                return
        conn.execute(
            "INSERT INTO handoff_event(event_key,batch_key,event_type,side,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (event_key, batch_key, event_type, side, json_dumps(payload or {}), now_iso()),
        )


def list_events(batch_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM handoff_event"
    args: tuple[Any, ...] = ()
    if batch_key:
        q += " WHERE batch_key=?"
        args = (batch_key,)
    q += " ORDER BY id DESC LIMIT ?"
    args = (*args, limit)
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_event_dict(r) for r in rows]


def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json_loads(item.pop("payload_json", "{}"))
    return item


# ---------------------------------------------------------------------------
# 运行状态
# ---------------------------------------------------------------------------

def get_state() -> dict[str, Any]:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM app_state WHERE id=1").fetchone()
    state = dict(row) if row else {}
    state["payload"] = json_loads(state.pop("payload_json", "{}"))
    state.setdefault("phase", "legacy")
    state.setdefault("active_entry", "legacy")
    return state


def active_entry() -> str:
    return str(get_state().get("active_entry") or "legacy")


def is_new_active() -> bool:
    return active_entry() == "new"


def update_state(
    *,
    phase: str | None = None,
    active_entry: str | None = None,
    current_run_id: str | None = None,
    payload_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    init_db()
    current = get_state()
    payload = current.get("payload") if isinstance(current.get("payload"), dict) else {}
    if payload_patch:
        payload.update(payload_patch)
    with connect() as conn:
        conn.execute(
            "UPDATE app_state SET phase=?, active_entry=?, current_run_id=?, updated_at=?, payload_json=? "
            "WHERE id=1",
            (
                phase or current.get("phase", "legacy"),
                active_entry or current.get("active_entry", "legacy"),
                current_run_id if current_run_id is not None else current.get("current_run_id"),
                now_iso(),
                json_dumps(payload),
            ),
        )
    return get_state()


# ---------------------------------------------------------------------------
# 旧记录快照
# ---------------------------------------------------------------------------

def upsert_snapshot(
    module: str,
    row: dict[str, Any],
    *,
    source_side: str = "legacy",
    batch_key: str | None = None,
    key: str | None = None,
) -> str:
    init_db()
    skey = key or row_source_key(module, row)
    entry_id = int(row["id"]) if parse_decimal(row.get("id")) is not None else None
    batch = batch_key or row.get("比价批次") or row.get("batch_key")
    with connect() as conn:
        conn.execute(
            "INSERT INTO legacy_snapshot(module,source_key,entry_id,batch_key,data_json,source_side,updated_at) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(module,source_key) DO UPDATE SET "
            "entry_id=excluded.entry_id,batch_key=excluded.batch_key,data_json=excluded.data_json,"
            "source_side=excluded.source_side,updated_at=excluded.updated_at",
            (module, skey, entry_id, str(batch) if batch is not None else None,
             json_dumps(row), source_side, now_iso()),
        )
    return skey


def get_snapshot(module: str, skey: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT data_json FROM legacy_snapshot WHERE module=? AND source_key=?",
            (module, skey),
        ).fetchone()
    if not row:
        return None
    data = json_loads(row["data_json"], {})
    return data if isinstance(data, dict) else None


def list_snapshots(module: str | None = None) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM legacy_snapshot"
    args: tuple[Any, ...] = ()
    if module:
        q += " WHERE module=?"
        args = (module,)
    q += " ORDER BY module, entry_id"
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
    result = []
    for r in rows:
        item = dict(r)
        data = json_loads(item.pop("data_json", "{}"))
        item["data"] = data
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# 基线迁移 / 版本
# ---------------------------------------------------------------------------

def detect_currency(row: dict[str, Any]) -> tuple[str, str]:
    for key in ("币种", "货币", "结算币种", "currency", "currency_code"):
        value = row.get(key)
        if value not in (None, "") and str(value).strip():
            text = str(value).strip()
            return text, "preserved"
    return "待核对", "missing_pending_check"


def detect_line_code(row: dict[str, Any], fallback_id: Any = None) -> str | None:
    for key in ("line_code", "价格行", "行号", "基线编号", "code"):
        value = row.get(key)
        if value not in (None, "") and str(value).strip():
            return str(value).strip()
    goods = str(row.get("商品标识", "") or "").strip()
    if re.fullmatch(r"L\d+[A-Za-z0-9_-]*", goods):
        return goods
    return f"L{fallback_id}" if fallback_id is not None else None


def baseline_canonical(row: dict[str, Any]) -> dict[str, Any]:
    currency, currency_status = detect_currency(row)
    amount = row.get("基线价格", row.get("baseline_amount", row.get("amount")))
    pair = amount_pair(amount)
    return {
        "source_key": row_source_key("price_baseline", row),
        "id": int(row["id"]) if parse_decimal(row.get("id")) is not None else None,
        "goods_id": str(row.get("商品标识", row.get("goods_id", "")) or ""),
        "spec": str(row.get("归一规格", row.get("spec", "")) or ""),
        "currency": currency,
        "currency_status": currency_status,
        "amount_display": pair[0] if pair else str(amount or ""),
        "amount_decimal": pair[1] if pair else "",
        "status": str(row.get("status", "") or ""),
        "source": str(row.get("来源", row.get("source", "旧内存价格基线记录")) or "旧内存价格基线记录"),
        "source_ref": str(row.get("source_ref", row_source_key("price_baseline", row)) or ""),
        "line_code": detect_line_code(row, row.get("id")),
    }


def upsert_baseline(
    row: dict[str, Any],
    *,
    run_id: str | None = None,
    confirmed: bool = True,
) -> dict[str, Any]:
    init_db()
    canon = baseline_canonical(row)
    skey = canon["source_key"]
    ts = now_iso()
    data = dict(row)
    data.setdefault("source_key", skey)
    data["币种"] = canon["currency"]
    data["currency_status"] = canon["currency_status"]
    if canon["currency_status"] == "missing_pending_check":
        data["币种核对"] = "待核对：存量记录未提供币种"
    with connect() as conn:
        existing = conn.execute(
            "SELECT id,current_version_no,amount_decimal,currency FROM baseline WHERE source_key=?", (skey,)
        ).fetchone()
        if existing is None:
            bid = canon["id"]
            if bid is None:
                bid = (conn.execute("SELECT COALESCE(MAX(id),0)+1 AS x FROM baseline").fetchone()["x"])
            # 避免显式旧 ID 与新库自增冲突。
            conflict = conn.execute("SELECT id FROM baseline WHERE id=?", (bid,)).fetchone()
            if conflict is not None:
                bid = conn.execute("SELECT COALESCE(MAX(id),0)+1 AS x FROM baseline").fetchone()["x"]
            version_no = 1
            conn.execute(
                """INSERT INTO baseline(id,source_key,line_code,goods_id,spec,currency,currency_status,
                   amount_text,amount_decimal,current_version_no,status,source,source_ref,
                   verification_status,confirmed,data_json,migration_run_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bid, skey, canon["line_code"], canon["goods_id"], canon["spec"], canon["currency"],
                 canon["currency_status"], canon["amount_display"], canon["amount_decimal"], version_no,
                 canon["status"], canon["source"], canon["source_ref"],
                 "migrated_verified" if confirmed else "pending_verification", 1 if confirmed else 0,
                 json_dumps(data), run_id, ts, ts),
            )
        else:
            bid = existing["id"]
            version_no = int(existing["current_version_no"])
            value_changed = (
                (canon["amount_decimal"] or "") != str(existing["amount_decimal"] or "")
                or canon["currency"] != str(existing["currency"] or "")
            )
            if value_changed and canon["amount_decimal"]:
                # 重复迁移发现源值被更正时追加新版本，绝不原地覆盖旧版本价格。
                version_no += 1
            conn.execute(
                """UPDATE baseline SET line_code=?,goods_id=?,spec=?,currency=?,currency_status=?,
                   amount_text=?,amount_decimal=?,current_version_no=?,status=?,source=?,source_ref=?,
                   confirmed=?,verification_status=?,data_json=?,migration_run_id=COALESCE(?,migration_run_id),
                   updated_at=? WHERE id=?""",
                (canon["line_code"], canon["goods_id"], canon["spec"], canon["currency"],
                 canon["currency_status"], canon["amount_display"], canon["amount_decimal"], version_no,
                 canon["status"], canon["source"], canon["source_ref"], 1 if confirmed else 0,
                 "migrated_verified" if confirmed else "pending_verification", json_dumps(data),
                 run_id, ts, bid),
            )
        version_key = f"{canon['line_code'] or f'BV-{bid}'}@v{version_no}"
        aliases = [version_key, f"BV-{bid}-v{version_no}", f"baseline:{skey}:v{version_no}"]
        payload_hash = content_hash({
            "baseline_id": bid,
            "version_no": version_no,
            "goods_id": canon["goods_id"],
            "spec": canon["spec"],
            "currency": canon["currency"],
            "amount_decimal": canon["amount_decimal"],
            "source_ref": canon["source_ref"],
        })
        v_exists = conn.execute(
            "SELECT id FROM baseline_version WHERE baseline_id=? AND version_no=?", (bid, version_no)
        ).fetchone()
        if v_exists is None:
            conn.execute(
                """INSERT INTO baseline_version(baseline_id,version_no,version_key,aliases_json,line_code,
                   goods_id,spec,currency,amount_text,amount_decimal,source,source_ref,content_hash,
                   confirmed,migration_run_id,data_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (bid, version_no, version_key, json_dumps(aliases), canon["line_code"], canon["goods_id"],
                 canon["spec"], canon["currency"], canon["amount_display"], canon["amount_decimal"],
                 canon["source"], canon["source_ref"], payload_hash, 1 if confirmed else 0, run_id,
                 json_dumps(canon), ts),
            )
    return get_baseline(bid) or {}


def _baseline_row_to_dict(row: sqlite3.Row, *, include_versions: bool = False) -> dict[str, Any]:
    item = dict(row)
    data = json_loads(item.pop("data_json", "{}"))
    if not isinstance(data, dict):
        data = {}
    item.pop("migration_run_id", None)
    current_key = None
    with connect() as conn:
        v = conn.execute(
            "SELECT version_key FROM baseline_version WHERE baseline_id=? AND version_no=?",
            (item["id"], item["current_version_no"]),
        ).fetchone()
        if v:
            current_key = v["version_key"]
        versions = []
        if include_versions:
            vr = conn.execute(
                "SELECT version_no,version_key,amount_text,amount_decimal,currency,created_at,correction_reason,"
                "content_hash,source,source_ref FROM baseline_version WHERE baseline_id=? ORDER BY version_no",
                (item["id"],),
            ).fetchall()
            versions = [dict(x) for x in vr]
    # 以原始行字段为底，附加持久化/版本信息，避免改变既有表格契约。
    merged = dict(data)
    merged.update({
        "id": item["id"],
        "amount_text": item.get("amount_text"),
        "amount_decimal": item.get("amount_decimal"),
        "基线金额核对值": item.get("amount_decimal"),
        "status": item.get("status") or merged.get("status"),
        "source_key": item["source_key"],
        "line_code": item.get("line_code"),
        "商品标识": item.get("goods_id"),
        "归一规格": item.get("spec"),
        "币种": item.get("currency"),
        "currency": item.get("currency"),
        "currency_status": item.get("currency_status"),
        "来源": item.get("source"),
        "source": item.get("source"),
        "source_ref": item.get("source_ref"),
        "baseline_version": item.get("current_version_no"),
        "current_version_no": item.get("current_version_no"),
        "baseline_version_key": current_key,
        "price_version": current_key,
        "confirmed": bool(item.get("confirmed")),
        "verification_status": item.get("verification_status"),
        "币种核对": (
            "待核对：存量记录未提供币种"
            if item.get("currency_status") == "missing_pending_check"
            else merged.get("币种核对", "币种已保留，仍可人工复核")
        ),
    })
    if item.get("currency_status") == "missing_pending_check":
        merged["pending_verification"] = sorted(set([*merged.get("pending_verification", []), "币种"]))
    if include_versions:
        merged["versions"] = versions
    return merged


def list_baselines(*, confirmed_only: bool = False) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM baseline"
    if confirmed_only:
        q += " WHERE confirmed=1"
    q += " ORDER BY id"
    with connect() as conn:
        rows = conn.execute(q).fetchall()
    return [_baseline_row_to_dict(r, include_versions=True) for r in rows]


def get_baseline(identifier: Any) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row: sqlite3.Row | None = None
        if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
            row = conn.execute("SELECT * FROM baseline WHERE id=?", (int(identifier),)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM baseline WHERE source_key=?", (str(identifier),)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM baseline WHERE line_code=?", (str(identifier),)).fetchone()
    return _baseline_row_to_dict(row, include_versions=True) if row else None


def get_baseline_version(identifier: Any) -> dict[str, Any] | None:
    init_db()
    text = str(identifier or "").strip()
    if not text:
        return None
    with connect() as conn:
        row = conn.execute("SELECT * FROM baseline_version WHERE version_key=?", (text,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM baseline_version WHERE aliases_json LIKE ? ORDER BY id DESC LIMIT 1",
                (f'%"{text}"%',),
            ).fetchone()
        if row is None and "@v" in text:
            line, no_text = text.rsplit("@v", 1)
            if no_text.isdigit():
                row = conn.execute(
                    "SELECT v.* FROM baseline_version v JOIN baseline b ON b.id=v.baseline_id "
                    "WHERE b.line_code=? AND v.version_no=?",
                    (line, int(no_text)),
                ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["aliases"] = json_loads(item.pop("aliases_json", "[]"))
    item["data"] = json_loads(item.pop("data_json", "{}"))
    item["confirmed"] = bool(item.get("confirmed"))
    return item


def correct_baseline(
    identifier: Any,
    new_price: Any,
    *,
    reason: str = "当次价格更正",
    source: str | None = None,
    operator: str | None = None,
) -> dict[str, Any]:
    init_db()
    pair = amount_pair(new_price)
    if pair is None:
        return {"ok": False, "message": "更正价格必须是数值", "baseline": None}
    base = get_baseline(identifier)
    if base is None:
        return {"ok": False, "message": f"价格基线 {identifier} 不存在", "baseline": None}
    bid = int(base["id"])
    old_version_key = base.get("baseline_version_key")
    old_price = base.get("amount_decimal") or base.get("基线价格")
    ts = now_iso()
    with connect() as conn:
        current = conn.execute(
            "SELECT current_version_no FROM baseline WHERE id=?", (bid,)
        ).fetchone()["current_version_no"]
        next_no = int(current) + 1
        line = base.get("line_code") or f"L{bid}"
        version_key = f"{line}@v{next_no}"
        aliases = [version_key, f"BV-{bid}-v{next_no}", f"baseline:{base['source_key']}:v{next_no}"]
        canon = {
            "baseline_id": bid,
            "version_no": next_no,
            "goods_id": base.get("商品标识"),
            "spec": base.get("归一规格"),
            "currency": base.get("币种"),
            "amount_decimal": pair[1],
            "source_ref": source or base.get("source_ref"),
        }
        h = content_hash(canon)
        conn.execute(
            """INSERT INTO baseline_version(baseline_id,version_no,version_key,aliases_json,line_code,
               goods_id,spec,currency,amount_text,amount_decimal,source,source_ref,content_hash,
               correction_reason,confirmed,migration_run_id,data_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (bid, next_no, version_key, json_dumps(aliases), line, base.get("商品标识"),
             base.get("归一规格"), base.get("币种"), pair[0], pair[1], source or base.get("来源"),
             source or base.get("source_ref"), h, reason, None,
             json_dumps({**canon, "reason": reason, "operator": operator}), ts),
        )
        data = json_loads(base.get("data_json"), {}) if isinstance(base.get("data_json"), str) else {}
        # get_baseline 返回合并字典；直接以它为原始数据载体即可。
        data = {k: v for k, v in base.items() if k != "versions"}
        data["基线价格"] = new_price if isinstance(new_price, (int, float)) else pair[0]
        data["current_version_no"] = next_no
        data["baseline_version_key"] = version_key
        data["price_version"] = version_key
        data["last_correction"] = {"from": old_price, "to": new_price, "reason": reason, "at": ts}
        conn.execute(
            """UPDATE baseline SET amount_text=?,amount_decimal=?,current_version_no=?,source=COALESCE(?,source),
               data_json=?,updated_at=? WHERE id=?""",
            (pair[0], pair[1], next_no, source, json_dumps(data), ts, bid),
        )
    add_event(
        "baseline_corrected",
        batch_key=None,
        side=active_entry(),
        payload={
            "baseline_id": bid,
            "line_code": line,
            "from_version": old_version_key,
            "to_version": version_key,
            "old_price": old_price,
            "new_price": new_price,
            "reason": reason,
            "handed_over": True,
        },
        event_key=f"correction:{bid}:v{next_no}",
    )
    updated = get_baseline(bid)
    return {
        "ok": True,
        "message": f"{line} 已从 {old_price} 更正为 {pair[0]}；更正登记为 {version_key}，旧版本保留",
        "baseline": updated,
        "old_version_key": old_version_key,
        "new_version_key": version_key,
        "handed_over": True,
    }


# ---------------------------------------------------------------------------
# 迁移编排
# ---------------------------------------------------------------------------

def _source_rows(module: str) -> list[dict[str, Any]]:
    # 延迟导入，避免 db <-> store 初始化循环。
    from app.store import store
    return [dict(r) for r in store.rows(module)]


def migration_inventory() -> dict[str, Any]:
    init_db()
    baseline_source = _source_rows("price_baseline")
    compare_source = _source_rows("price_compare")
    unfinished_source = [r for r in compare_source if str(r.get("status")) != "已计算"]
    with connect() as conn:
        baseline_target = conn.execute("SELECT COUNT(*) c FROM baseline").fetchone()["c"]
        baseline_versions = conn.execute("SELECT COUNT(*) c FROM baseline_version").fetchone()["c"]
        compare_target = conn.execute("SELECT COUNT(*) c FROM compare_task").fetchone()["c"]
        claims = conn.execute("SELECT COUNT(*) c FROM claim").fetchone()["c"]
        results = conn.execute("SELECT COUNT(*) c FROM compare_result").fetchone()["c"]
        snapshots = conn.execute("SELECT COUNT(*) c FROM legacy_snapshot").fetchone()["c"]
    baseline_items = []
    for row in baseline_source:
        canon = baseline_canonical(row)
        target = get_baseline(canon["source_key"])
        baseline_items.append({
            "source_key": canon["source_key"],
            "id": canon["id"],
            "goods_id": canon["goods_id"],
            "spec": canon["spec"],
            "currency": canon["currency"],
            "currency_status": canon["currency_status"],
            "source_price": row.get("基线价格"),
            "target_price": target.get("基线价格") if target else None,
            "target_version": target.get("baseline_version_key") if target else None,
            "source": canon["source"],
            "source_ref": canon["source_ref"],
            "migrated": target is not None,
            "price_matches": bool(target and parse_decimal(target.get("amount_decimal")) == parse_decimal(canon["amount_decimal"])),
        })
    return {
        "ok": True,
        "state": get_state(),
        "counts": {
            "source_price_baseline": len(baseline_source),
            "target_baseline": baseline_target,
            "target_baseline_versions": baseline_versions,
            "source_price_compare_total": len(compare_source),
            "source_unfinished_compare": len(unfinished_source),
            "target_compare_task": compare_target,
            "claims": claims,
            "results": results,
            "legacy_snapshots": snapshots,
        },
        "baseline_items": baseline_items,
        "unfinished_source": unfinished_source,
    }


def start_migration(
    *,
    activate: bool = True,
    idempotency_key: str = MIGRATION_KEY,
    operator: str | None = None,
) -> dict[str, Any]:
    init_db()
    ts = now_iso()
    with connect() as conn:
        existing_run = conn.execute(
            "SELECT * FROM migration_run WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
    if existing_run and existing_run["phase"] in {"verified", "activated", "rolled_back"}:
        if activate and active_entry() != "new":
            switch_to_new(run_id=existing_run["run_id"], operator=operator)
        report = migration_inventory()
        return {
            "ok": True,
            "duplicate": True,
            "idempotent_replay": True,
            "message": "迁移已受理并完成；重复启动未复制数据或第二份受理",
            "run_id": existing_run["run_id"],
            "phase": existing_run["phase"],
            "activated": bool(existing_run["activated"]),
            "report": report,
            "state": get_state(),
        }

    from app.store import store  # local import

    run_id = existing_run["run_id"] if existing_run else f"MIG-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    source_baselines = [dict(r) for r in store.rows("price_baseline")]
    source_compares = [dict(r) for r in store.rows("price_compare")]
    unfinished = [r for r in source_compares if str(r.get("status")) != "已计算"]
    source_counts = {
        "price_baseline": len(source_baselines),
        "price_compare_total": len(source_compares),
        "unfinished_compare": len(unfinished),
    }

    with connect() as conn:
        conn.execute(
            """INSERT INTO migration_run(run_id,idempotency_key,phase,activated,source_counts_json,started_at)
               VALUES(?,?, 'writing',0,?,?)
               ON CONFLICT(idempotency_key) DO UPDATE SET phase='writing', error=NULL""",
            (run_id, idempotency_key, json_dumps(source_counts), ts),
        )

    try:
        # 先快照全部旧比价记录，保证已计算旧结果也能追到原记录。
        for row in source_compares:
            upsert_snapshot("price_compare", row, source_side="legacy")
        for row in source_baselines:
            upsert_snapshot("price_baseline", row, source_side="legacy")

        baseline_keys: list[str] = []
        checksum_payload: list[dict[str, Any]] = []
        for row in source_baselines:
            canon = baseline_canonical(row)
            upsert_baseline(row, run_id=run_id, confirmed=False)
            baseline_keys.append(canon["source_key"])
            checksum_payload.append({k: canon[k] for k in (
                "source_key", "goods_id", "spec", "currency", "amount_decimal", "status")})

        for row in unfinished:
            upsert_compare_task(row, run_id=run_id)

        # 同事务内核验行数与内容；任何不一致都抛出并回滚，外部看不到半份已确认数据。
        with connect() as conn:
            target_baseline = conn.execute("SELECT COUNT(*) c FROM baseline").fetchone()["c"]
            target_tasks = conn.execute("SELECT COUNT(*) c FROM compare_task").fetchone()["c"]
            expected_tasks = len(unfinished)
            if target_baseline < len(source_baselines):
                raise RuntimeError("价格基线迁移行数不足")
            # compare_task 可能包含演练/新库任务，因此只校验本批 source_key 均存在。
            for row in unfinished:
                skey = row_source_key("price_compare", row)
                got = conn.execute("SELECT id FROM compare_task WHERE source_key=?", (skey,)).fetchone()
                if got is None:
                    raise RuntimeError(f"未办比价 {skey} 未完整写入")
            for item in checksum_payload:
                got = conn.execute(
                    "SELECT goods_id,spec,currency,amount_decimal,status FROM baseline WHERE source_key=?",
                    (item["source_key"],),
                ).fetchone()
                if got is None or dict(got) != {
                    "goods_id": item["goods_id"], "spec": item["spec"], "currency": item["currency"],
                    "amount_decimal": item["amount_decimal"], "status": item["status"],
                }:
                    raise RuntimeError(f"基线 {item['source_key']} 核对不一致")
            checksum = content_hash({"baselines": sorted(checksum_payload, key=lambda x: x["source_key"])})
            conn.execute("UPDATE baseline SET confirmed=1,verification_status='migrated_verified' WHERE migration_run_id=?", (run_id,))
            conn.execute(
                "UPDATE migration_run SET phase='verified',target_counts_json=?,checksum=?,verified_at=? WHERE run_id=?",
                (json_dumps({
                    "price_baseline": target_baseline,
                    "unfinished_compare": expected_tasks,
                    "compare_task_total": target_tasks,
                }), checksum, now_iso(), run_id),
            )

        report = migration_inventory()
        report["checksum"] = checksum
        report["missing_currency_count"] = sum(
            1 for x in report["baseline_items"] if x["currency_status"] == "missing_pending_check"
        )
        with connect() as conn:
            conn.execute(
                "UPDATE migration_run SET report_json=? WHERE run_id=?", (json_dumps(report), run_id)
            )

        if activate:
            switch_to_new(run_id=run_id, operator=operator)
        else:
            update_state(phase="verified", active_entry="legacy", current_run_id=run_id)
        add_event("migration_verified", payload={"run_id": run_id, "activate": activate, "counts": source_counts})
        return {
            "ok": True,
            "duplicate": False,
            "message": "迁移已原子提交并核对完成" + ("，新入口已接管" if activate else "，当前仍保留旧入口"),
            "run_id": run_id,
            "phase": "activated" if activate else "verified",
            "activated": activate,
            "report": report,
            "state": get_state(),
        }
    except Exception as exc:  # noqa: BLE001 - 需要把失败回执给值班员
        with connect() as conn:
            conn.execute(
                "UPDATE migration_run SET phase='failed',error=? WHERE run_id=?", (str(exc), run_id)
            )
        update_state(phase="migration_failed", active_entry="legacy")
        add_event("migration_failed", payload={"run_id": run_id, "error": str(exc)})
        return {"ok": False, "message": f"迁移核对失败，已回滚，未交付半份数据：{exc}", "run_id": run_id}


def switch_to_new(*, run_id: str | None = None, operator: str | None = None) -> dict[str, Any]:
    state = get_state()
    run_id = run_id or state.get("current_run_id")
    if not run_id:
        result = start_migration(activate=True, operator=operator)
        return result
    with connect() as conn:
        run = conn.execute("SELECT * FROM migration_run WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["phase"] not in {"verified", "activated", "rolled_back"}:
            return {"ok": False, "message": "迁移尚未核对完整，不能切换到新入口"}
        conn.execute(
            "UPDATE migration_run SET phase='activated',activated=1,activated_at=COALESCE(activated_at,?) "
            "WHERE run_id=?", (now_iso(), run_id)
        )
        # 切换只把“尚未有受理方”的任务交给新入口；已有唯一 claim 的执行侧保持不变，
        # 避免切换动作本身把同一轮申请复制/改派成第二份受理。
        conn.execute(
            "UPDATE compare_task SET execution_side='new' "
            "WHERE owner_side IS NULL AND COALESCE(confirmed,0)=0"
        )
    update_state(phase="new", active_entry="new", current_run_id=run_id,
                 payload_patch={"last_switch_at": now_iso(), "operator": operator})
    add_event("entry_switched_new", side="new", payload={"run_id": run_id, "operator": operator})
    return {"ok": True, "message": "新入口已从已确认的 SQLite 基线数据接管", "state": get_state()}


def rollback_to_legacy(reason: str = "切换故障，返回旧入口", *, operator: str | None = None) -> dict[str, Any]:
    state = get_state()
    run_id = state.get("current_run_id")
    # 把新库中产生/更新的任务反向快照到旧内存可恢复的位置。
    tasks = list_compare_tasks()
    for task in tasks:
        data = task.get("data") if isinstance(task.get("data"), dict) else {
            k: v for k, v in task.items() if k not in {"data", "versions"}
        }
        upsert_snapshot("price_compare", data, source_side=task.get("source_side", "new"))
    baselines = list_baselines()
    for item in baselines:
        upsert_snapshot("price_baseline", item, source_side="new")
    with connect() as conn:
        if run_id:
            conn.execute(
                "UPDATE migration_run SET phase='rolled_back',rolled_back_at=? WHERE run_id=?",
                (now_iso(), run_id),
            )
        # 回滚后，所有未完成申请可由旧入口继续；原始受理方字段不变。
        conn.execute(
            "UPDATE compare_task SET execution_side='legacy' WHERE COALESCE(confirmed,0)=0"
        )
        conn.execute(
            "UPDATE claim SET execution_side='legacy',updated_at=? WHERE state!='completed'",
            (now_iso(),),
        )
    update_state(phase="rolled_back", active_entry="legacy", current_run_id=run_id,
                 payload_patch={"last_rollback_at": now_iso(), "rollback_reason": reason, "operator": operator})
    add_event("entry_rolled_back_legacy", side="legacy",
              payload={"reason": reason, "run_id": run_id, "operator": operator})
    # 立即水合当前进程的旧内存仓库，使回滚后的旧入口能继续原申请。
    hydrate_legacy_store()
    return {"ok": True, "message": "已回滚到旧入口；SQLite 基线、版本和受理回执均保留，可继续原申请",
            "state": get_state()}


def simulate_cutover_failure() -> dict[str, Any]:
    update_state(phase="switch_failed", active_entry="legacy",
                 payload_patch={"simulated_failure": "new deployment health check failed"})
    add_event("cutover_failure_simulated", side="legacy",
              payload={"error": "new deployment health check failed"})
    rolled = rollback_to_legacy("模拟切换故障：健康检查失败")
    return {"ok": True, "message": "已模拟切换故障并返回旧入口", "rollback": rolled, "state": get_state()}


def hydrate_legacy_store() -> dict[str, int]:
    """把交接快照恢复进当前旧内存仓库；按 source_key 幂等，不复制行。"""
    from app.store import store
    restored = {"price_baseline": 0, "price_compare": 0}
    for snap in list_snapshots():
        module = snap["module"]
        if module not in {"price_baseline", "price_compare"}:
            continue
        data = dict(snap["data"])
        skey = snap["source_key"]
        data["source_key"] = skey
        rows = store.rows(module)
        existing = None
        if data.get("id") is not None:
            existing = next((r for r in rows if int(r.get("id", -1)) == int(data["id"])), None)
        if existing is None:
            existing = next((r for r in rows if row_source_key(module, r) == skey or r.get("source_key") == skey), None)
        if existing is None:
            rows.append(data)
        else:
            # 保留同一对象引用，同时用最新快照覆盖字段。
            existing.update(data)
        restored[module] = restored.get(module, 0) + 1
    return restored


# 避免循环导入：compare task 函数在 handoff 段补充定义。
def upsert_compare_task(row: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:  # pragma: no cover
    from app.handoff import upsert_compare_task as _impl
    return _impl(row, run_id=run_id)


def list_compare_tasks() -> list[dict[str, Any]]:  # pragma: no cover
    from app.handoff import list_compare_tasks as _impl
    return _impl()


init_db()
