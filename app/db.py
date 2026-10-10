"""SQLite 存取层。

设计要点：**活动不是日历事件**。羊毛活动有平台、品类、开抢时刻、有效期、
状态等结构化字段，而「今天9点半抢什么」本质是对这些字段做筛选。
所以主存储是这张表，日历只是它的一种呈现。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from . import timeutil as tu

log = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    platform    TEXT,
    category    TEXT,
    grab_at     TEXT,                              -- 'YYYY-MM-DDTHH:MM' 开抢时刻（核心）
    start_at    TEXT,                              -- 活动开始
    end_at      TEXT,                              -- 活动结束 / 有效期止
    url         TEXT,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | done | expired
    source      TEXT NOT NULL DEFAULT 'text',      -- text | image
    confidence  REAL,
    raw_text    TEXT,                              -- 原始自然语言，回溯纠错用
    msg_id      TEXT,
    -- 重复型活动（每天/每周几/每月几号）。NULL = 一次性事件，见 app/repeat.py。
    -- 「每日 09:00:00 起发放」这种券，记成一次是答不出「明天还能领吗」的。
    repeat_kind       TEXT,                        -- daily | weekly | monthly | NULL
    repeat_at         TEXT,                        -- 'HH:MM' 每个周期开抢的那一刻
    repeat_until      TEXT,                        -- 'HH:MM' 当天可领截止，可空
    repeat_weekday    INTEGER,                     -- 0~6（周一=0），weekly 的重复列，取 repeat_days 第一个
    repeat_days       TEXT,                        -- '1,3' weekly 的多个星期几（周一=0）
    repeat_month_days TEXT,                        -- '1,15' monthly 的每月几号
    quota_week        INTEGER,                     -- 一周最多领几次，NULL = 不限
    quota_month       INTEGER,                     -- 一个月最多领几次，NULL = 不限
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_grab_at ON activity(grab_at);
CREATE INDEX IF NOT EXISTS idx_activity_window  ON activity(start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_activity_status  ON activity(status);

-- 完成台账：一条重复活动会在很多天里各完成一次。
-- activity.status 只有一格，装不下「这周三领过了、下周三还能领」；把「哪天完成的」
-- 单独记在这儿，周/月的配额都从它数出来（见 app/repeat.py 的 quota_state）。
CREATE TABLE IF NOT EXISTS activity_done (
    activity_id INTEGER NOT NULL,
    day         TEXT    NOT NULL,                  -- 'YYYY-MM-DD' 完成的那一天
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (activity_id, day)
);

CREATE INDEX IF NOT EXISTS idx_activity_done_day ON activity_done(day);

-- 幂等：同一条入站消息只处理一次
CREATE TABLE IF NOT EXISTS inbound_msg (
    msg_id     TEXT PRIMARY KEY,
    user_id    TEXT,
    kind       TEXT,
    handled_at TEXT NOT NULL
);

-- 待澄清：机器人问了「是不是明天9点？」，等用户回一句确认
CREATE TABLE IF NOT EXISTS pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    question   TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_user ON pending(user_id, expires_at);

-- 运行时可改的设置（主要是 AI 模型）。放在库里而不是 config.json 里，是为了
-- 「存了立刻生效」：改 config.json 要重启容器，而用户正站在页面上等着用。
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
"""

_FIELDS = (
    "title",
    "platform",
    "category",
    "grab_at",
    "start_at",
    "end_at",
    "url",
    "note",
    "status",
    "source",
    "confidence",
    "raw_text",
    "msg_id",
    "repeat_kind",
    "repeat_at",
    "repeat_until",
    "repeat_weekday",
    "repeat_days",
    "repeat_month_days",
    "quota_week",
    "quota_month",
)

#: 后加的列。`CREATE TABLE IF NOT EXISTS` 对**已存在的表什么都不做**，所以老库
#: （用户库里已经有真数据了）必须靠显式 ALTER 补列，不能重建表。
_ADDED_COLUMNS = (
    ("repeat_kind", "TEXT"),
    ("repeat_at", "TEXT"),
    ("repeat_until", "TEXT"),
    ("repeat_weekday", "INTEGER"),
    ("repeat_days", "TEXT"),
    ("repeat_month_days", "TEXT"),
    ("quota_week", "INTEGER"),
    ("quota_month", "INTEGER"),
)

_db_path: Optional[Path] = None

# 公开别名：网页层要用它来过滤用户能改的字段
FIELDS = _FIELDS


def _migrate(conn) -> list[str]:
    """给老库补新列，返回这次真正补上的列名。"""
    have = {str(r["name"]) for r in conn.execute("PRAGMA table_info(activity)")}
    added: list[str] = []
    for name, decl in _ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE activity ADD COLUMN {name} {decl}")
            added.append(name)
    return added


def init(path: str | Path) -> Path:
    global _db_path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _db_path = p
    with connect() as conn:
        conn.executescript(SCHEMA)
        added = _migrate(conn)
    if added:
        log.info("老库补列：%s", "、".join(added))
    return p


def db_path() -> Path:
    if _db_path is None:
        raise RuntimeError("db.init() 还没调用")
    return _db_path


@contextmanager
def connect():
    """每次调用开一个连接。

    网页线程和机器人线程会并发访问，连接按调用开、用完就关，
    配合 WAL 模式是最省心的做法。
    """
    if _db_path is None:
        raise RuntimeError("db.init() 还没调用")
    conn = sqlite3.connect(str(_db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["grab_dt"] = tu.parse_minute(d.get("grab_at"))
    d["start_dt"] = tu.parse_minute(d.get("start_at"))
    d["end_dt"] = tu.parse_minute(d.get("end_at"))
    return d


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------


def insert_activity(data: dict[str, Any]) -> Optional[int]:
    """插入一条活动。``msg_id`` 重复时返回 None（幂等）。"""
    now_s = tu.to_minute(tu.now())
    payload = {k: data.get(k) for k in _FIELDS}
    payload["title"] = (payload.get("title") or "").strip() or "未命名活动"
    payload["status"] = payload.get("status") or "pending"
    payload["source"] = payload.get("source") or "text"
    if isinstance(payload.get("confidence"), str):
        try:
            payload["confidence"] = float(payload["confidence"])
        except ValueError:
            payload["confidence"] = None

    cols = ", ".join(_FIELDS + ("created_at", "updated_at"))
    marks = ", ".join(["?"] * (len(_FIELDS) + 2))
    values = [payload[k] for k in _FIELDS] + [now_s, now_s]
    try:
        with connect() as conn:
            cur = conn.execute(
                f"INSERT INTO activity ({cols}) VALUES ({marks})", values
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def update_activity(activity_id: int, **changes: Any) -> bool:
    allowed = {k: v for k, v in changes.items() if k in _FIELDS}
    if not allowed:
        return False
    allowed["updated_at"] = tu.to_minute(tu.now())
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE activity SET {sets} WHERE id = ?",
            list(allowed.values()) + [activity_id],
        )
        return cur.rowcount > 0


def set_status(activity_id: int, status: str) -> bool:
    if status not in ("pending", "done", "expired"):
        raise ValueError(f"非法状态: {status}")
    return update_activity(activity_id, status=status)


def delete_activity(activity_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM activity WHERE id = ?", (activity_id,))
        # 台账跟着走：留着就是孤儿行，还会让「本周已领几次」虚高
        conn.execute("DELETE FROM activity_done WHERE activity_id = ?", (int(activity_id),))
        return cur.rowcount > 0


def refresh_expired() -> int:
    """把有效期已过、且仍是 pending 的活动标成 expired。"""
    now_s = tu.to_minute(tu.now())
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE activity
               SET status = 'expired', updated_at = ?
             WHERE status = 'pending'
               AND end_at IS NOT NULL
               AND end_at != ''
               AND end_at < ?
            """,
            (now_s, now_s),
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------


def _select(
    where: str,
    params: Iterable[Any],
    order: str = "ORDER BY grab_at IS NULL, grab_at, start_at",
    limit: Optional[int] = None,
) -> list[dict]:
    sql = f"SELECT * FROM activity WHERE {where} {order}"
    bound: list[Any] = list(params)
    if limit is not None:
        sql += " LIMIT ?"
        bound.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, tuple(bound)).fetchall()
    return [_row_to_dict(r) for r in rows]


def grab_in_window(start: str, end: str) -> list[dict]:
    """开抢时刻落在 [start, end) 内的活动 —— 严格命中集合。"""
    return _select(
        "grab_at IS NOT NULL AND grab_at != '' AND grab_at >= ? AND grab_at < ?",
        (start, end),
    )


def ongoing_at(start: str, end: str) -> list[dict]:
    """在该时段内处于「可领取/可参与」状态的活动（有效期覆盖查询点）。

    `start_at` 缺了就退回 `grab_at`：模型经常只给出「10点开抢」+「到今天23:59」，
    这时候 10:01 问「还能领吗」的答案是「能」—— 要求 `start_at` 非空会让这种
    活动在 10:01 之后彻底隐身（用户看到「没有开抢安排」，也就以为真没得领了）。
    """
    return _select(
        """
        status != 'expired'
        AND COALESCE(NULLIF(start_at, ''), NULLIF(grab_at, '')) IS NOT NULL
        AND COALESCE(NULLIF(start_at, ''), NULLIF(grab_at, '')) <= ?
        AND end_at IS NOT NULL AND end_at != '' AND end_at > ?
        """,
        (start, end),
        order="ORDER BY end_at",
    )


def activities_on_day(d: date) -> list[dict]:
    """某天「有安排」的活动：开抢在那天，或有效期覆盖那天。"""
    day_start, day_end = tu.day_bounds(d)
    return _select(
        """
        (grab_at IS NOT NULL AND grab_at != '' AND grab_at >= ? AND grab_at < ?)
        OR (start_at IS NOT NULL AND start_at != '' AND start_at < ? AND COALESCE(NULLIF(end_at,''), '9999') >= ?)
        """,
        (day_start, day_end, day_end, day_start),
    )


def rows_without_repeat() -> list[dict]:
    """还没判过重复规则的行（不看配额）。

    补老数据的是 `rows_needing_repeat()` —— 它连「有规则但没配额」一起看。
    这个更窄的查询留给「只想知道哪些行还没有规则」的地方。
    """
    return _select("repeat_kind IS NULL OR repeat_kind = ''", ())


def rows_needing_repeat() -> list[dict]:
    """`backfill-repeat` 该看一眼的行：还没有规则的，**或者有规则但没配额**的。

    「每周/每月几次」是后加的，光看 ``repeat_kind`` 是空的会把它们漏掉 ——
    升级上来的库里，一条「每天 09:00」的老券有规则、配额却是 NULL，不补的话
    页面永远显示「还能领」，用户白跑一趟。
    """
    return _select(
        "repeat_kind IS NULL OR repeat_kind = '' "
        "OR (quota_week IS NULL AND quota_month IS NULL)",
        (),
    )


def all_activities() -> list[dict]:
    """全部活动，按 id 排 —— 给 `backfill-repeat --all` 按原文重判用。

    解析器修好之后（例如「每人每周三**档**」被误认成周三），老库里的错规则得能重判，
    否则用户只能自己一条条改。
    """
    return _select("1=1", (), order="ORDER BY id")


def _repeat_rows_between(start: date, end: date) -> list[dict]:
    """区间内**可能**有场次的重复活动（SQL 只做粗筛）。

    重复活动的一行只有一个 ``grab_at``，却要在区间里的每一天出现 —— 光按
    ``grab_at`` 查会一条都查不到（用户报的「问后天，答没有」就是这么来的）。
    SQL 只排除掉「系列还没开始」和「系列已经结束」的，具体哪几天几点交给
    `app/repeat.py` 精确算：**不要在 SQL 里再写一套规则**，两套规则分叉过一次，
    结果就是日历和机器人各说各话。
    """
    from . import repeat as rp  # 放函数里，避免 db/repeat 顶层互相 import

    s, _ = tu.day_bounds(start)
    _, e = tu.day_bounds(end)
    rows = _select(
        """
        repeat_kind IS NOT NULL AND repeat_kind != ''
        AND COALESCE(NULLIF(grab_at,''), NULLIF(start_at,'')) IS NOT NULL
        AND COALESCE(NULLIF(grab_at,''), NULLIF(start_at,'')) < ?
        AND (end_at IS NULL OR end_at = '' OR end_at >= ?)
        """,
        (e, s),
        order="ORDER BY COALESCE(NULLIF(grab_at,''), start_at)",
    )
    return [r for r in rows if rp.occurrences_between(r, start, end)]


def list_range(start: date, end: date, include_expired: bool = True) -> list[dict]:
    """[start, end] 闭区间内所有活动，前端日历用。

    包含**重复活动落在区间内的那些行**（同一行只出现一次，不止一次 —— 展开成
    哪几天是 `app/repeat.py` 的事，日历自己也会调它）。
    """
    s, _ = tu.day_bounds(start)
    _, e = tu.day_bounds(end)
    clauses = [
        # 三种入选方式，缺一不可：
        #   ① 开抢就落在区间内；
        #   ② 有 start_at 且区间落在它的有效期内；
        #   ③ 没有 start_at、但开抢时刻已经过去、有效期还没过 —— 「每天 08:30 起发放」
        #      这类券如果只有 grab_at（模型经常只给这个），少了这一支就会对**所有**
        #      日期隐身：机器人说「没有活动记录」，可它明明还能领。
        #      ③ 必须要求 end_at 真的存在：没写有效期的老券不该被当成「永远有效」，
        #      否则它会出现在自己开抢之后的每一天里（连「这天没有活动」都会改口）。
        "((grab_at IS NOT NULL AND grab_at != '' AND grab_at >= ? AND grab_at < ?)"
        " OR (start_at IS NOT NULL AND start_at != '' AND start_at < ? AND COALESCE(NULLIF(end_at,''),'9999') >= ?)"
        " OR (grab_at IS NOT NULL AND grab_at != '' AND grab_at < ?"
        "     AND end_at IS NOT NULL AND end_at != '' AND end_at >= ?))"
    ]
    params: list[Any] = [s, e, e, s, e, s]
    if not include_expired:
        clauses.append("status != 'expired'")
    rows = _select(" AND ".join(f"({c})" for c in clauses), params)

    repeats = _repeat_rows_between(start, end)
    if not include_expired:
        repeats = [r for r in repeats if r.get("status") != "expired"]
    seen = {r["id"] for r in rows}
    extra = [r for r in repeats if r["id"] not in seen]
    if not extra:
        return rows
    rows = rows + extra
    rows.sort(key=lambda r: (r.get("grab_at") or "", r.get("start_at") or "", r["id"]))
    return rows


def upcoming(limit: int = 20, after: Optional[datetime] = None) -> list[dict]:
    """接下来要抢的活动，按开抢时刻升序。"""
    ref = tu.to_minute(after or tu.now())
    return _select(
        """
        status = 'pending'
        AND ((grab_at IS NOT NULL AND grab_at != '' AND grab_at >= ?)
             OR ((grab_at IS NULL OR grab_at = '')
                 AND end_at IS NOT NULL AND end_at != '' AND end_at >= ?))
        """,
        (ref, ref),
        order="ORDER BY COALESCE(NULLIF(grab_at,''), start_at)",
        limit=limit,
    )


def nearest_grab(target: datetime, count: int = 3) -> tuple[list[dict], list[dict]]:
    """找离 target 最近的开抢时刻：返回 (之前, 之后)。"""
    key = tu.to_minute(target)
    before = _select(
        "grab_at IS NOT NULL AND grab_at != '' AND grab_at < ? AND status != 'expired'",
        (key,),
        order="ORDER BY grab_at DESC",
        limit=count,
    )
    after = _select(
        "grab_at IS NOT NULL AND grab_at != '' AND grab_at > ? AND status != 'expired'",
        (key,),
        order="ORDER BY grab_at ASC",
        limit=count,
    )
    return before, after


def find_duplicate(title: str, grab_at: Optional[str]) -> Optional[dict]:
    """按「标题 + 开抢时刻」判重，避免同一场活动被投喂两次。"""
    t = (title or "").strip()
    if not t:
        return None
    if grab_at:
        rows = _select(
            "title = ? AND grab_at = ?", (t, grab_at), limit=1
        )
    else:
        rows = _select(
            "title = ? AND (grab_at IS NULL OR grab_at = '')", (t,), limit=1
        )
    return rows[0] if rows else None


def get(activity_id: int) -> Optional[dict]:
    rows = _select("id = ?", (activity_id,))
    return rows[0] if rows else None


def search(keyword: str, limit: int = 50) -> list[dict]:
    like = f"%{keyword}%"
    return _select(
        "(title LIKE ? OR platform LIKE ? OR category LIKE ? OR note LIKE ?)",
        (like, like, like, like),
        order="ORDER BY grab_at IS NULL, grab_at",
        limit=limit,
    )


def stats() -> dict[str, int]:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE status = 'pending'"
        ).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE status = 'done'"
        ).fetchone()[0]
        expired = conn.execute(
            "SELECT COUNT(*) FROM activity WHERE status = 'expired'"
        ).fetchone()[0]
    return {"total": total, "pending": pending, "done": done, "expired": expired}


# --------------------------------------------------------------------------
# 完成台账：重复活动的「哪几天领到了」
# --------------------------------------------------------------------------


def _day_key(day: Any) -> str:
    """把 ``date`` / ``datetime`` / ``'2026-10-07'`` / ``'2026-10-07T09:00'`` 统一成日期键。

    认不出返回空串 —— 调用方据此拒绝写入，免得台账里塞进垃圾日期。
    """
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    try:
        return date.fromisoformat(str(day or "").strip()[:10]).isoformat()
    except ValueError:
        return ""


def mark_done(activity_id: int, day: Any) -> bool:
    """记下「这一天领到了」。这天已经记过就返回 False（重复点不该变成两次）。"""
    key = _day_key(day)
    if not key:
        return False
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO activity_done (activity_id, day) VALUES (?, ?)",
            (int(activity_id), key),
        )
        return cur.rowcount > 0


def unmark_done(activity_id: int, day: Any) -> bool:
    """撤销「这一天领到了」。"""
    key = _day_key(day)
    if not key:
        return False
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM activity_done WHERE activity_id = ? AND day = ?",
            (int(activity_id), key),
        )
        return cur.rowcount > 0


def done_days(activity_id: int, d_from: Any = None, d_to: Any = None) -> list[str]:
    """这条活动已完成的日子（升序）。给了区间就只数区间内的。"""
    where = "activity_id = ?"
    params: list[Any] = [int(activity_id)]
    lo, hi = _day_key(d_from), _day_key(d_to)
    if lo:
        where += " AND day >= ?"
        params.append(lo)
    if hi:
        where += " AND day <= ?"
        params.append(hi)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT day FROM activity_done WHERE {where} ORDER BY day", tuple(params)
        ).fetchall()
    return [str(r["day"]) for r in rows]


def done_map(activity_ids: Iterable[int]) -> dict[int, list[str]]:
    """一批活动的完成台账一次查完 —— 日历一个月几十条，一条一查太浪费。"""
    ids = [int(i) for i in activity_ids]
    if not ids:
        return {}
    marks = ", ".join(["?"] * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT activity_id, day FROM activity_done WHERE activity_id IN ({marks}) "
            "ORDER BY activity_id, day",
            tuple(ids),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(int(r["activity_id"]), []).append(str(r["day"]))
    return out


def clear_done(activity_id: int) -> int:
    """清掉一条活动的全部完成记录（用户手改规则时用得上）。"""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM activity_done WHERE activity_id = ?", (int(activity_id),)
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# 消息幂等
# --------------------------------------------------------------------------


def seen_msg(msg_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM inbound_msg WHERE msg_id = ?", (str(msg_id),)
        ).fetchone()
    return row is not None


def mark_msg(msg_id: str, user_id: str = "", kind: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO inbound_msg (msg_id, user_id, kind, handled_at) "
            "VALUES (?, ?, ?, ?)",
            (str(msg_id), user_id, kind, tu.to_minute(tu.now())),
        )


# --------------------------------------------------------------------------
# 待澄清会话
# --------------------------------------------------------------------------


def set_pending(
    user_id: str,
    kind: str,
    payload: dict[str, Any],
    question: str,
    ttl_minutes: int = 30,
) -> int:
    now_dt = tu.now()
    with connect() as conn:
        conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        cur = conn.execute(
            "INSERT INTO pending (user_id, kind, payload, question, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id,
                kind,
                json.dumps(payload, ensure_ascii=False),
                question,
                tu.to_minute(now_dt),
                tu.to_minute(now_dt + timedelta(minutes=ttl_minutes)),
            ),
        )
        return int(cur.lastrowid)


def pop_pending(user_id: str) -> Optional[dict[str, Any]]:
    """取出并删除该用户尚未过期的待澄清项。"""
    now_s = tu.to_minute(tu.now())
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending WHERE user_id = ? AND expires_at > ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, now_s),
        ).fetchone()
        if not row:
            conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
            return None
        conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def clear_pending(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
