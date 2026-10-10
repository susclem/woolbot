"""重复型活动（每天 / 每周几 / 每月几号）的规则与「本期还能领几次」。

**为什么需要它**：用户库里那几条真券，备注原文写着「每日 09:00:00 起发放」
「领券时间：每天 08:30:00-22:00:00」—— 本质是**每天重复**的活动，而 activity 表
的一行只描述一次。模型只能按「下一次」把它记成 10月7日 09:00 一次，于是问
「后天抢什么」永远答不出「这张券明天还能不能领」。

规则存在 activity 表的这些列里：

=================  ========================================================
repeat_kind        NULL / ``daily`` / ``weekly`` / ``monthly``
repeat_at          ``HH:MM``，每个周期里开抢的那一刻
repeat_until       ``HH:MM`` 或 NULL，当天可领的截止时刻（「08:30-22:00」的 22:00）
repeat_weekday     0~6（周一=0），只有 weekly 用；``repeat_days`` 里的第一个
repeat_days        ``"1,3"``，weekly 的多个星期几（周一=0）
repeat_month_days  ``"1,15"``，monthly 的每月几号
quota_week         整数或 NULL，一周最多领几次
quota_month        整数或 NULL，一个月最多领几次
=================  ========================================================

``repeat_weekday`` **显式存下来**，不靠 ``grab_at`` 反推：模型把「每周五」锚错成
周三时，靠反推会一路错下去，而用户根本看不出来。``repeat_days`` 后来才加，
所以它空着的时候仍然回退到 ``repeat_weekday``——老数据不用迁移也能读。

对外这些事：

- :func:`parse_repeat` / :func:`parse_quota` 从中文里认规则（模型给的 ``repeat``
  字段和用户原话走的是**同一个解析器** —— 模型只要说得出「每天 9 点」，剩下的
  交给 Python）
- :func:`occurrence_on` 某一天它几点开抢（不是这天就 None）
- :func:`occurrence_status` 那一天该显示成待抢还是已完成（**配额感知**：
  本周/本月的次数用完了，那个周期里剩下的场次就都算拿不到了）
- :func:`describe` 回执/日历里那句「🔁 每天 09:00 · 周限2次」

日历（``web.py``）和机器人（``pipeline.py``）都调这里，**不能再各算一套** ——
它们曾经各说各话过一次：机器人说 10月16日 有 3 条，日历上那天是空的。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable, NamedTuple, Optional

from . import timeutil as tu

#: 规则种类。写成常量而不是裸字符串，免得各处拼错（拼错会静默退化成一次性活动）。
KIND_DAILY = "daily"
KIND_WEEKLY = "weekly"
KIND_MONTHLY = "monthly"
KINDS = (KIND_DAILY, KIND_WEEKLY, KIND_MONTHLY)

#: 一次展开最多算多少天。日历一个月也就 42 天，这里只是防「有人查十年」把内存撑爆。
MAX_DAYS = 400

#: 配额上限：写「每周 200 次」的多半是看错了行，不如不认。
MAX_QUOTA = 99

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_EXPIRED = "expired"

#: 排期那几列。`db` 的 `_FIELDS`、web 的接口层、pipeline 的录入都用它，
#: 免得哪一处漏了一列 —— 漏一列就是静默退化成一次性活动。
SCHEDULE_FIELDS = (
    "repeat_kind",
    "repeat_at",
    "repeat_until",
    "repeat_weekday",
    "repeat_days",
    "repeat_month_days",
)

#: 配额那两列。与排期分开：页面上「每周限领」是个独立的输入框，用户填了就说了算，
#: 不该被备注原文里的另一种写法盖掉。
QUOTA_FIELDS = ("quota_week", "quota_month")

FIELDS = SCHEDULE_FIELDS + QUOTA_FIELDS

_WEEKDAY_CH = "一二三四五六日"
_WEEKDAY_INDEX = {ch: i for i, ch in enumerate(_WEEKDAY_CH)}
_WEEKDAY_INDEX["天"] = 6

_CN_NUM = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_DAILY_RE = re.compile(r"每天|每日|天天|每晚")

#: 星期几。可以是一串：「每周二、四」「每周一三五」，也可以每一项各自带前缀：
#: 「每周六、周日」「每周六和星期日」。
#: 真数据（#80 建行银联商圈满1000减116元券）写的就是「每周六、周日09:00:00至
#: 23:59:59」，只认到「周六」的话周日那一场永远不显示，用户看见的是
#: 「clawbot 说已记下，日历上却没有」。
#: **不吃阿拉伯数字**：「每周日10点」里的 10 会被当成星期几，宁可只认汉字。
_WEEKDAY_RUN = r"[一二三四五六日天]"
_WEEK_PREFIX = r"(?:周|星期|礼拜)\s*"
_WEEKLY_RE = re.compile(
    r"每(?:个|一)?\s*" + _WEEK_PREFIX + r"("
    + _WEEKDAY_RUN
    + r"(?:\s*[、,，和及与]\s*(?:" + _WEEK_PREFIX + r")?" + _WEEKDAY_RUN
    + r"|\s*(?:" + _WEEK_PREFIX + r")?" + _WEEKDAY_RUN + r")*)"
)

#: 每月几号。**必须带「日/号」**，否则「每月一次」「每月2次」会被认成日期。
#: 「每**自然**月」也要认 —— 真数据里那条工行月月刷写的就是「领券时间为每自然月10号
#: 10:00-20号23:59:59」，中间夹两个字就整条认不出、静默退化成一次性活动（#74）。
_MONTH_NUM = r"(?:\d{1,2}|[一二三四五六七八九十两]{1,3})"
_MONTH_ITEM = _MONTH_NUM + r"\s*[日号]"
_MONTHLY_RE = re.compile(
    r"每(?:个|一)?\s*(?:自然)?\s*月\s*("
    + _MONTH_ITEM
    + r"(?:\s*[、,，和及与]\s*" + _MONTH_ITEM + r")*)"
)

#: 紧跟在「每月N号」后面的这些字说明它讲的是周期**结束**，不是开抢日：
#: 「每自然月30号结束每期消费达标活动」里的 30 号要是被当成规则，每月就平白多一场。
_MONTH_END_CH = "结束截止为止"

#: 排期词往后看多少个字符算「附近」：用来判「这个词后面紧跟时刻吗」
#: （「每周五中午12:00」「每自然月10号10:00-20号…」）。
_NEAR_CHARS = 12

#: 就近找时刻时往后看多远 —— 得够装下整段「08:30:00-22:00:00」，
#: 12 个字会在第二个时刻中间截断，时段就丢了。
_TIME_NEAR_CHARS = 24

#: 跟在数字后面的量词 —— 有它就不是星期几，而是「每周三次」这种频率。
#: 这一条是被真数据逼出来的：用户库里那条备注写着「每人每周三**档**力度乐园门票
#: 消费券」，取第一个「每周三」会把「每周五中午12:00」认成周三。
_MEASURE_CH = "档种类款张次个只笔份期条波轮场台遍"

#: 配额：「每周限领1张」「单用户单月限领1张」「一周最多2次」。
#: 真数据里就是「单周/单月」这种写法，所以前缀除了「每」还认「本/单/一」。
#: 中间那截要塞得下「限**选择领取**1张」这种啰嗦写法（#74 那条工行券的真备注），
#: 所以动词前允许「选择/自行/再/去」，动词本身也允许「领取/领券/抢购」两个字的写法。
_QUOTA_NUM = r"(?:\d{1,2}|[一二三四五六七八九十两]{1,3})"
_QUOTA_UNIT = r"(?:次|张|份|个|件|笔|单|回)"
_QUOTA_HEAD = r"(?:每|本|单|一)\s*(?:个|一)?\s*"
_QUOTA_MID = (
    r"\s*(?:内)?\s*(?:限|最多|至多|只能|可)?\s*"
    r"(?:选择|自行|再|去)?\s*"
    r"(?:(?:领|抢|买|购|参与|中|得)(?:取|到|用|券)?)?\s*"
)
_QUOTA_WEEK_RE = re.compile(
    _QUOTA_HEAD + r"(?:周|星期|礼拜)" + _QUOTA_MID + r"(" + _QUOTA_NUM + r")\s*" + _QUOTA_UNIT
)
_QUOTA_MONTH_RE = re.compile(
    _QUOTA_HEAD + r"月" + _QUOTA_MID + r"(" + _QUOTA_NUM + r")\s*" + _QUOTA_UNIT
)

#: 时刻：``09:30`` / ``9点半`` / ``9点`` / ``08:30:00``（秒忽略）
_HHMM = r"(\d{1,2})\s*[:：点]\s*(\d{1,2}|半)?\s*(?::\d{2})?"
#: 时段：``08:30-22:00`` / ``08:30:00-22:00:00`` / ``9点-11点``
_WINDOW_RE = re.compile(_HHMM + r"\s*[-–—~～至到]\s*" + _HHMM)
_TIME_RE = re.compile(_HHMM)


class Rule(NamedTuple):
    kind: str
    at: Optional[str] = None
    until: Optional[str] = None
    weekday: Optional[int] = None           # 兼容老字段：days 里的第一个
    days: tuple[int, ...] = ()              # weekly：哪几个星期几（周一=0）
    month_days: tuple[int, ...] = ()        # monthly：每月哪几号


class Quota(NamedTuple):
    week: Optional[int] = None
    month: Optional[int] = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _minutes(raw: Optional[str]) -> int:
    if not raw:
        return 0
    if raw == "半":
        return 30
    return int(raw)


def _hm(hour: Optional[str], minute: Optional[str], *, allow_24: bool = False) -> Optional[str]:
    """``("9", "半")`` → ``"09:30"``。认不出来返回 None（**不猜**）。"""
    if not hour:
        return None
    try:
        h, m = int(hour), _minutes(minute)
    except (TypeError, ValueError):
        return None
    if not 0 <= m <= 59:
        return None
    if h == 24 and allow_24:
        # 「到晚上12点」在中文里就是当天 24:00，也就是这一天的最后一刻
        return "23:59"
    if not 0 <= h <= 23:
        return None
    return f"{h:02d}:{m:02d}"


def _cn_number(raw: str) -> Optional[int]:
    """``"三"`` → 3，``"十五"`` → 15。认不出返回 None。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_NUM.get(head, 1) if head else 1
        ones = _CN_NUM.get(tail, 0) if tail else 0
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_NUM.get(s)
    return None


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()[:10]
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _date_set(days: Iterable[Any]) -> set[date]:
    out: set[date] = set()
    for item in days or ():
        d = _as_date(item)
        if d is not None:
            out.add(d)
    return out


def _int_list(value: Any, low: int, high: int) -> tuple[int, ...]:
    """把库里 ``"1,3"`` 这样的字符串读成有序去重的整数元组。"""
    if value in (None, ""):
        return ()
    out: list[int] = []
    for piece in str(value).replace("，", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            n = int(piece)
        except (TypeError, ValueError):
            continue
        if low <= n <= high and n not in out:
            out.append(n)
    return tuple(sorted(out))


def _weekday_of(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 6 else None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _parse_week_run(run: str) -> tuple[int, ...]:
    """``"二、四"`` / ``"一三五"`` → ``(1, 3)`` / ``(0, 2, 4)``。"""
    days: list[int] = []
    for ch in run:
        idx = _WEEKDAY_INDEX.get(ch)
        if idx is None or idx in days:
            continue
        days.append(idx)
    return tuple(sorted(days))


def _weekly_days(text: str) -> tuple[tuple[int, ...], Optional[int]]:
    """「每周X」里的 X（可能好几个），认不出返回 ``(空元组, None)``。

    第二项是选中那个匹配在原串里的**结束位置**，给 `_time_near` 就近找时刻用。

    不能只取第一个匹配：真备注里「每人每周三档力度…」和「每周五中午12:00开始发放」
    同时出现，取第一个就把周五说成了周三。两条判据：

    - 后面紧跟量词（「每周三**档**」「每周一**次**」）的不是星期几，是频率；
    - 后面紧跟时刻的最像真的（「每周五中午**12:00**」），优先选它。
    """
    best: Optional[tuple[int, ...]] = None
    best_end: Optional[int] = None
    for m in _WEEKLY_RE.finditer(text):
        after = text[m.end():m.end() + 1]
        if after and after in _MEASURE_CH:
            continue
        days = _parse_week_run(m.group(1))
        if not days:
            continue
        near = text[m.end():m.end() + _NEAR_CHARS]
        if _WINDOW_RE.search(near) or _TIME_RE.search(near):
            return days, m.end()
        if best is None:
            best, best_end = days, m.end()
    return (best or ()), (best_end if best else None)


def _month_days(text: str) -> tuple[tuple[int, ...], Optional[int]]:
    """「每月1日、15日」里的号数；认不出返回 ``(空元组, None)``。

    第二项与 `_weekly_days` 同义（选中匹配的结束位置）。判据也一致：

    - 后面紧跟时刻/时段的优先（「每自然月**10号10:00**-20号23:59:59」说的是 10 号）；
    - 后面紧跟「结束/截止」的不是开抢日，直接跳过（「每自然月30号**结束**每期活动」）。
    """
    best: Optional[tuple[int, ...]] = None
    best_end: Optional[int] = None
    for m in _MONTHLY_RE.finditer(text):
        after = text[m.end():m.end() + 1]
        if after and after in _MONTH_END_CH:
            continue
        days: list[int] = []
        for item in re.finditer(r"(" + _MONTH_NUM + r")\s*[日号]", m.group(1)):
            n = _cn_number(item.group(1))
            if n is None or not 1 <= n <= 31 or n in days:
                continue
            days.append(n)
        if not days:
            continue
        days.sort()
        near = text[m.end():m.end() + _NEAR_CHARS]
        if _WINDOW_RE.search(near) or _TIME_RE.search(near):
            return tuple(days), m.end()
        if best is None:
            best, best_end = tuple(days), m.end()
    return (best or ()), (best_end if best else None)


def _time_near(text: str, end: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """找这条规则的（开始, 截止）时刻：先看排期词**附近**，再退回全文第一处。

    「每自然月10号10:00-20号23:59:59」的时刻属于 10 号，可截图 OCR 的正文开头
    还挂着手机状态栏的「09:34」—— 只看全文第一处，规则就被锚到 09:34 上去。
    时段（``08:30-22:00``）比单个时刻更具体，所以两处都先找时段再找时刻；
    就近那一段也找不到时退回全文，这是老行为，免得反而把时刻丢了。
    """
    near = text[end:end + _TIME_NEAR_CHARS] if end is not None else ""
    for pattern in (_WINDOW_RE, _TIME_RE):
        for chunk in (near, text):
            m = pattern.search(chunk or "")
            if not m:
                continue
            if pattern is _WINDOW_RE:
                return (
                    _hm(m.group(1), m.group(2)),
                    _hm(m.group(3), m.group(4), allow_24=True),
                )
            return _hm(m.group(1), m.group(2)), None
    return None, None


def parse_repeat(text: Optional[str]) -> Optional[Rule]:
    """从一句中文里认重复规则。认不出返回 None（**宁可认不出，不要猜错**）。

    能认的写法：``每天 9 点`` / ``每日 09:00`` / ``领券时间：每天 08:30-22:00`` /
    ``每周五 12:00`` / ``每周二、四 10:00`` / ``每月1日、15日 10:00`` /
    ``每自然月10号10:00-20号23:59:59``（跨天的可领窗口只取**开始那天 + 开始时刻**）。
    """
    s = (text or "").strip()
    if not s:
        return None

    days, end = _weekly_days(s)
    mdays: tuple[int, ...] = ()
    if days:
        kind: Optional[str] = KIND_WEEKLY
    else:
        mdays, end = _month_days(s)
        if mdays:
            kind = KIND_MONTHLY
        else:
            daily = _DAILY_RE.search(s)
            if daily:
                kind = KIND_DAILY
                end = daily.end()
            else:
                return None

    at, until = _time_near(s, end)
    weekday = days[0] if days else None
    return Rule(kind, at, until, weekday, days, mdays)


def parse_quota(text: Optional[str]) -> Quota:
    """从一句中文里认「每周/每月最多领几次」。认不出返回两个 None。

    真数据里的写法是「单用户单**周**限领1张」「单用户单**月**限领1张」，
    所以前缀除了「每」还认「本 / 单 / 一」。数量词后必须跟「次/张/份…」——
    只认「每周三次」，不认「每周三档」（那是力度档位，不是次数）。
    """
    s = (text or "").strip()
    if not s:
        return Quota()
    return Quota(_quota_of(_QUOTA_WEEK_RE, s), _quota_of(_QUOTA_MONTH_RE, s))


def _quota_of(pattern: re.Pattern, text: str) -> Optional[int]:
    m = pattern.search(text)
    if not m:
        return None
    n = _cn_number(m.group(1))
    if n is None or not 1 <= n <= MAX_QUOTA:
        return None
    return n


def _hm_from(value: Any) -> Optional[str]:
    """把库里的 ``"09:00"`` 读成一个合法时刻。"""
    s = str(value or "").strip()
    if not s:
        return None
    return _hm(s.split(":")[0], s.split(":")[1] if ":" in s else None)


def _anchor_of(row: dict[str, Any]) -> Optional[datetime]:
    """模型给的「下一次开抢」，没有就用活动开始时间。

    给 `rule_of` 补「没写星期几/几号」时**猜**用 —— 猜「下一个同类型的日子」
    比猜「活动开始那天」准。
    """
    return tu.parse_minute(row.get("grab_at")) or tu.parse_minute(row.get("start_at"))


def _series_start(row: dict[str, Any]) -> Optional[datetime]:
    """重复系列从哪一天算起：``start_at`` 与 ``grab_at`` 里**早的那个**。

    `occurrence_on` 用它当门槛（这一天之前的场次不算数），所以**不能用
    ``grab_at`` 单独当门槛**：它只是「下一次开抢」，指的往往是**下一个**符合
    条件的日子。真数据里（#80 建行银联商圈满1000减116元券，每周六、周日）
    模型把 ``grab_at`` 填成了周日 10-11 —— 门槛一立，今天（周六 10-10）那一场
    被一句「系列还没开始」吃掉，用户看到的就是「clawbot 说已记下，日历上却
    没有」。两块时间都在时取早的：``start_at`` 才是活动真正开始的那天。
    """
    a = tu.parse_minute(row.get("start_at"))
    b = tu.parse_minute(row.get("grab_at"))
    if a and b:
        return min(a, b)
    return a or b


def rule_of(row: dict[str, Any]) -> Optional[Rule]:
    """把一行读成规则；不是重复型（或读不出时刻）就返回 None。"""
    kind = str(row.get("repeat_kind") or "").strip().lower()
    if kind not in KINDS:
        return None
    at = _hm_from(row.get("repeat_at"))
    if not at:
        return None
    until = _hm_from(row.get("repeat_until"))

    days = _int_list(row.get("repeat_days"), 0, 6)
    weekday = _weekday_of(row.get("repeat_weekday"))
    if not days and weekday is not None:
        days = (weekday,)                              # 老库只有单数那一列
    month_days = _int_list(row.get("repeat_month_days"), 1, 31)

    if kind == KIND_WEEKLY and not days:
        # 每周型却没有星期几 —— 算不出下一次，宁可当一次性
        anchor = _anchor_of(row)
        if anchor is None:
            return None
        days = (anchor.weekday(),)
    if kind == KIND_MONTHLY and not month_days:
        anchor = _anchor_of(row)
        if anchor is None:
            return None
        month_days = (anchor.day,)

    first = days[0] if days else None
    return Rule(kind, at, until, first, days if kind == KIND_WEEKLY else (), month_days)


def quota_of(row: dict[str, Any]) -> Quota:
    """把一行读成配额；没写就是 None（不限）。"""
    return Quota(_quota_value(row.get("quota_week")), _quota_value(row.get("quota_month")))


def _quota_value(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= MAX_QUOTA else None


# ---------------------------------------------------------------------------
# 算某一天
# ---------------------------------------------------------------------------


def occurrence_on(row: dict[str, Any], day: date) -> Optional[datetime]:
    """这一天它几点开抢？不是这一天（或系列还没开始 / 已经结束）就 None。

    **只管重复型**：一次性活动返回 None，调用方自己按 ``grab_at``/``start_at`` 判。
    """
    rule = rule_of(row)
    if rule is None:
        return None
    base = _series_start(row)
    if base is None:
        return None
    if day < base.date():
        return None                                   # 系列还没开始
    end = tu.parse_minute(row.get("end_at"))
    if end and day > end.date():
        return None                                   # 系列已经结束
    if rule.kind == KIND_WEEKLY and day.weekday() not in rule.days:
        return None
    if rule.kind == KIND_MONTHLY and day.day not in rule.month_days:
        return None
    hour, minute = (int(x) for x in str(rule.at).split(":"))
    when = datetime(day.year, day.month, day.day, hour, minute)
    if end and when > end:
        return None                                   # 最后一次已经晚于结束时刻
    return when


def occurrences_between(
    row: dict[str, Any], d_from: date, d_to: date
) -> list[dict[str, str]]:
    """区间里它所有场次，给前端日历用：``[{"day": "2026-10-08", "at": "09:00"}, …]``。"""
    out: list[dict[str, str]] = []
    if rule_of(row) is None or d_to < d_from:
        return out
    day = d_from
    while day <= d_to and len(out) < MAX_DAYS:
        when = occurrence_on(row, day)
        if when is not None:
            out.append({"day": day.isoformat(), "at": when.strftime("%H:%M")})
        day += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# 配额：这个周期还能领几次
# ---------------------------------------------------------------------------


def period_of(day: date, unit: str) -> tuple[date, date]:
    """``day`` 所在的周期首尾（闭区间）。``unit`` 是 ``"week"`` 或 ``"month"``。

    周按 ISO 惯例从**周一**算起，跟 ``repeat_weekday``（周一=0）对齐 ——
    否则「每周一、日各一次」会被算进两个不同的周。
    """
    if unit == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    start = day.replace(day=1)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    return start, nxt - timedelta(days=1)


def quota_state(row: dict[str, Any], day: date, done_days: Iterable[Any] = ()) -> dict[str, dict]:
    """``day`` 所在那一周 / 那一月的配额情况，给前端显示「已领 1/2」。

    ``done_days`` 是这条活动已经标记完成的那些天（``"YYYY-MM-DD"`` 或 ``date``）。
    """
    quota = quota_of(row)
    days = _date_set(done_days)
    out: dict[str, dict] = {}
    for unit, limit in (("week", quota.week), ("month", quota.month)):
        d_from, d_to = period_of(day, unit)
        done = sum(1 for d in days if d_from <= d <= d_to)
        out[unit] = {
            "limit": limit,
            "done": done,
            "left": None if limit is None else max(0, limit - done),
            "reached": limit is not None and done >= limit,
            "from": d_from.isoformat(),
            "to": d_to.isoformat(),
        }
    return out


def occurrence_status(row: dict[str, Any], day: date, done_days: Iterable[Any] = ()) -> str:
    """这一天的这一场在日历里该显示成什么。

    用户 2026-10-11 定的规矩：**配额感知**。一周的额度用满之后，那一周里剩下的
    场次就都算「已完成」—— 再显示「待抢」是在骗人（点进去也领不到），下周自动
    恢复待抢。月额度同理。
    """
    if rule_of(row) is None:
        return str(row.get("status") or STATUS_PENDING)
    days = _date_set(done_days)
    if day in days:
        return STATUS_DONE
    state = quota_state(row, day, days)
    if state["week"]["reached"] or state["month"]["reached"]:
        return STATUS_DONE
    # 行级的 ``done`` 对重复活动**没有意义**：一格 status 装不下几十场，用户以前
    # 误点过一次「标记完成」，整条就全成了已完成再也回不来。所以这里只认
    # ``expired``（系列整个过期了，哪一天都不该再说「待抢」），其余一律待抢；
    # 「这一天领到了」由 ``done_days`` 台账负责。
    status = str(row.get("status") or STATUS_PENDING)
    return STATUS_EXPIRED if status == STATUS_EXPIRED else STATUS_PENDING


# ---------------------------------------------------------------------------
# 说人话
# ---------------------------------------------------------------------------


def _describe_rule(rule: Rule) -> str:
    if rule.kind == KIND_WEEKLY:
        head = "每周" + "、".join(_WEEKDAY_CH[d] for d in rule.days) if rule.days else "每周"
    elif rule.kind == KIND_MONTHLY:
        head = "每月" + "、".join(f"{d}日" for d in rule.month_days) if rule.month_days else "每月"
    else:
        head = "每天"
    text = f"🔁 {head} {rule.at}"
    if rule.until:
        text += f"–{rule.until}"
    return text


def rule_text(row: dict[str, Any]) -> str:
    """只有排期、不带配额的那句 —— 编辑框里要回填的就是它。"""
    rule = rule_of(row)
    return _describe_rule(rule) if rule else ""


def describe_quota(row: dict[str, Any]) -> str:
    """「周限2次 · 月限8次」；没配额返回空串。"""
    quota = quota_of(row)
    parts = []
    if quota.week:
        parts.append(f"周限{quota.week}次")
    if quota.month:
        parts.append(f"月限{quota.month}次")
    return " · ".join(parts)


def describe(row: dict[str, Any]) -> str:
    """回执/行里那句「🔁 每天 09:00 · 周限1次」；不是重复型返回空串。"""
    rule = rule_of(row)
    if rule is None:
        return ""
    text = _describe_rule(rule)
    quota = describe_quota(row)
    if quota:
        text += f" · {quota}"
    return text


def fields_from(
    raw: Optional[str],
    note: Optional[str],
    text: Optional[str],
    anchor: Optional[datetime],
    ocr: Optional[str] = None,
) -> dict[str, Any]:
    """给录入用：把（模型的 repeat 字段 / 备注 / 用户原话 / 截图 OCR 原文）变成这几列的值。

    排期认不出来就返回全 None（**一次性活动**，行为与加这个功能之前完全一致）；
    配额按列各自回退（备注里写了「单周限领1张」而原话里写了「每月2次」，两边都算数）。

    认出来了但没给时刻，就用 ``anchor``（``grab_at``）的时刻 —— 模型已经把
    「下一次」锚在 09:00 上了，那正是这个规则每天的时刻。

    ``ocr`` 是**最后一路兜底**：截图那条路 ``text`` 是空的，模型偶尔既不肯把
    「领券时间为每自然月10号10:00-20号23:59:59」写进 ``repeat``、也不写进备注，
    但它抄的 OCR 原文里有（#74 那条工行券就是这么丢掉规则的）。配额**不吃**这一路：
    排期认错了在日历上看得见，配额认错了是静默少领，宁可等 `backfill-repeat` 或人填。
    """
    rule = (
        parse_repeat(raw)
        or parse_repeat(note)
        or parse_repeat(text)
        or parse_repeat(ocr)
    )

    quota = parse_quota(raw)
    for source in (note, text):
        other = parse_quota(source)
        quota = Quota(quota.week or other.week, quota.month or other.month)

    blank = {key: None for key in FIELDS}
    if rule is None:
        if not quota.week and not quota.month:
            return blank
        # 只认出了配额（比如备注写着「每月限领1次」而排期是模型另外给的）——
        # 排期留空，别把用户的一句话当成规则。
        return {**blank, "quota_week": quota.week, "quota_month": quota.month}

    at = rule.at or (anchor.strftime("%H:%M") if anchor else None)
    if not at:
        return blank

    days = rule.days
    month_days = rule.month_days
    if rule.kind == KIND_WEEKLY and not days:
        if anchor is None:
            return blank
        days = (anchor.weekday(),)
    if rule.kind == KIND_MONTHLY and not month_days:
        if anchor is None:
            return blank
        month_days = (anchor.day,)

    weekly = rule.kind == KIND_WEEKLY and bool(days)
    monthly = rule.kind == KIND_MONTHLY and bool(month_days)
    return {
        "repeat_kind": rule.kind,
        "repeat_at": at,
        "repeat_until": rule.until,
        "repeat_weekday": days[0] if weekly else None,
        "repeat_days": ",".join(str(d) for d in days) if weekly else None,
        "repeat_month_days": ",".join(str(d) for d in month_days) if monthly else None,
        "quota_week": quota.week,
        "quota_month": quota.month,
    }
