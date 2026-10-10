"""时间工具。

核心约定：**内部一律使用配置时区（默认 Asia/Shanghai）的朴素本地时间**，
不带 tzinfo。理由是用户只关心「北京时间几点抢」，引入时区换算只会制造 bug。

对外存储格式统一为分钟精度：``YYYY-MM-DDTHH:MM``。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Shanghai"

_TZ: ZoneInfo = ZoneInfo(DEFAULT_TZ)

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

MINUTE_FMT = "%Y-%m-%dT%H:%M"


def configure(tz_name: Optional[str] = None) -> None:
    """设置时区。名字无效时退回固定 +08:00，绝不因为配置写错就崩掉。"""
    global _TZ
    name = (tz_name or DEFAULT_TZ).strip()
    try:
        _TZ = ZoneInfo(name)
    except Exception:
        from datetime import timezone

        _TZ = timezone(timedelta(hours=8))  # type: ignore[assignment]


def tz_name() -> str:
    return getattr(_TZ, "key", str(_TZ))


def now() -> datetime:
    """当前时区的朴素本地时间（秒精度，便于直接格式化）。"""
    return datetime.now(_TZ).replace(tzinfo=None, microsecond=0)


def from_timestamp(ts: float) -> datetime:
    """把 Unix 时间戳换算成本配置时区的时间。

    容器里系统时区通常是 UTC，日志时间戳会全变成 UTC —— 排「这条消息几点收到的」
    的时候就得心算。日志统一走这个函数，写出来就是用户看的时间。
    """
    return datetime.fromtimestamp(ts, tz=_TZ).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()


def weekday_cn(d: date) -> str:
    return WEEKDAY_CN[d.weekday()]


def to_minute(dt: datetime) -> str:
    return dt.strftime(MINUTE_FMT)


def parse_minute(value: Optional[str]) -> Optional[datetime]:
    """解析各种常见写法，失败返回 None（调用方负责反问，不要瞎猜）。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, second=0, microsecond=0)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("/", "-").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.replace(second=0, microsecond=0)
    return None


def human(dt: Optional[datetime], with_weekday: bool = True, with_year: bool = False) -> str:
    """给人看的时间描述，例如「10月7日 09:00 周三」。"""
    if dt is None:
        return "未定"
    base = f"{dt.month}月{dt.day}日 {dt.hour:02d}:{dt.minute:02d}"
    if with_year:
        base = f"{dt.year}年{base}"
    if with_weekday:
        base += " " + weekday_cn(dt.date())
    return base


def human_day(d: Optional[date], with_weekday: bool = True, with_year: bool = False) -> str:
    """只要日子、不要时刻，例如「10月7日 周三」。

    给「按天」的标题用 —— 标题上出现 `00:00` 只会让人以为自己看错了。
    """
    if d is None:
        return "未定"
    base = f"{d.month}月{d.day}日"
    if with_year:
        base = f"{d.year}年{base}"
    if with_weekday:
        base += " " + weekday_cn(d)
    return base


def human_relative(dt: Optional[datetime], ref: Optional[datetime] = None) -> str:
    """「今天 09:00」「明天 09:00」「3天后 09:00」这种说法。"""
    if dt is None:
        return "未定"
    ref = ref or now()
    delta = (dt.date() - ref.date()).days
    clock = f"{dt.hour:02d}:{dt.minute:02d}"
    if delta == 0:
        return f"今天 {clock}"
    if delta == 1:
        return f"明天 {clock}"
    if delta == 2:
        return f"后天 {clock}"
    if delta == -1:
        return f"昨天 {clock}"
    if 3 <= delta <= 7:
        return f"{delta}天后（{weekday_cn(dt.date())}）{clock}"
    return human(dt)


def anchor_block(ref: Optional[datetime] = None) -> str:
    """注入 prompt 的时间锚点。

    这是「能识别系统时间」的落地点：模型自己不知道今天是几号，
    所有相对时间（「明天9点」「周二上午9点半」）都必须靠这个锚点换算。
    """
    ref = ref or now()
    tomorrow = ref + timedelta(days=1)
    return (
        f"当前时间：{ref.strftime('%Y-%m-%d %H:%M')}（{weekday_cn(ref.date())}）\n"
        f"所属时区：{tz_name()}\n"
        f"今天是 {ref.year}年{ref.month}月{ref.day}日，{weekday_cn(ref.date())}\n"
        f"明天是 {tomorrow.month}月{tomorrow.day}日，{weekday_cn(tomorrow.date())}\n"
        f"所有时间一律按 {tz_name()} 本地时间理解并输出。"
    )


def day_bounds(d: date) -> tuple[str, str]:
    """返回某天 [00:00, 次日00:00) 的分钟字符串。"""
    start = datetime(d.year, d.month, d.day, 0, 0)
    return to_minute(start), to_minute(start + timedelta(days=1))


def minute_window(d: date, hour: int, minute: int, span_minutes: int = 1) -> tuple[str, str]:
    """某一分钟起的半开窗口 [start, end)。默认就是那一分钟本身。"""
    start = datetime(d.year, d.month, d.day, hour, minute)
    end = start + timedelta(minutes=max(1, span_minutes))
    return to_minute(start), to_minute(end)
