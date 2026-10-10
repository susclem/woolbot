# -*- coding: utf-8 -*-
"""农历、二十四节气、中国节日、法定节假日——纯标准库，不联网、不加依赖。

日历页面用 `day_info(日期)` 拿格子右上角/左下角要显示的东西：

    >>> day_info(date(2026, 10, 7))
    {'lunar': '廿七', 'lunar_full': '八月廿七', 'term': '', 'festival': '', 'holiday': '', 'text': '廿七', 'kind': 'lunar'}

数据的来源与边界：
- 农历与节气：`app/_lunar_tables.py`（由 `tools/build_lunar_tables.py` 生成，覆盖 1900–2099）。
- 法定节假日：国务院办公厅历年通知（见 `_HOLIDAY_PLAN` 里的文号），只收 2024–2026——
  以后每年 11 月前后出新通知时，照着同一个格式加一条即可，没收录的年份格子就不显示「休/班」。
"""
from __future__ import annotations

from datetime import date, timedelta

from ._lunar_tables import FIRST_YEAR, LAST_YEAR, LUNAR_INFO, TERM_BLOB, TERM_LAST_YEAR, TERM_NAMES

__all__ = ["day_info", "available", "lunar_date", "solar_term", "festival", "holiday", "FIRST_YEAR", "LAST_YEAR"]

_MONTH_NAMES = ("正月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "冬月", "腊月")
_DIGITS = "一二三四五六七八九十"

# 农历节日：(月, 日) → 名字。除夕单独算（腊月的最后一天）。
_LUNAR_FESTIVALS = {
    (1, 1): "春节",
    (1, 15): "元宵节",
    (2, 2): "龙抬头",
    (5, 5): "端午节",
    (7, 7): "七夕",
    (7, 15): "中元节",
    (8, 15): "中秋节",
    (9, 9): "重阳节",
    (12, 8): "腊八节",
    (12, 23): "小年",
}

# 公历节日：(月, 日) → 名字。
_SOLAR_FESTIVALS = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (3, 12): "植树节",
    (4, 1): "愚人节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (7, 1): "建党节",
    (8, 1): "建军节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (10, 31): "万圣夜",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
}

# 按“某月第几个星期几”算的节日：(月, 第几个, 星期几) → 名字。星期几用 date.weekday()：周一 0、周日 6。
_WEEKDAY_FESTIVALS = {
    (5, 2, 6): "母亲节",
    (6, 3, 6): "父亲节",
    (11, 4, 3): "感恩节",
}

# 法定节假日安排——每个元组是 (名字, (放假第一天, 放假最后一天), (调休上班的日子, …))，"月-日"。
# 出处：2024 国办发明电〔2023〕7号；2025 国办发明电〔2024〕12号；2026 国办发明电〔2025〕7号。
# 注意 2024 年元旦原文是「1月1日放假，与周末连休」，所以只收 1 月 1 日自己。
_HOLIDAY_PLAN = {
    2024: (
        ("元旦", ("01-01", "01-01"), ()),
        ("春节", ("02-10", "02-17"), ("02-04", "02-18")),
        ("清明节", ("04-04", "04-06"), ("04-07",)),
        ("劳动节", ("05-01", "05-05"), ("04-28", "05-11")),
        ("端午节", ("06-10", "06-10"), ()),
        ("中秋节", ("09-15", "09-17"), ("09-14",)),
        ("国庆节", ("10-01", "10-07"), ("09-29", "10-12")),
    ),
    2025: (
        ("元旦", ("01-01", "01-01"), ()),
        ("春节", ("01-28", "02-04"), ("01-26", "02-08")),
        ("清明节", ("04-04", "04-06"), ()),
        ("劳动节", ("05-01", "05-05"), ("04-27",)),
        ("端午节", ("05-31", "06-02"), ()),
        ("国庆节、中秋节", ("10-01", "10-08"), ("09-28", "10-11")),
    ),
    2026: (
        ("元旦", ("01-01", "01-03"), ("01-04",)),
        ("春节", ("02-15", "02-23"), ("02-14", "02-28")),
        ("清明节", ("04-04", "04-06"), ()),
        ("劳动节", ("05-01", "05-05"), ("05-09",)),
        ("端午节", ("06-19", "06-21"), ()),
        ("中秋节", ("09-25", "09-27"), ()),
        ("国庆节", ("10-01", "10-07"), ("09-20", "10-10")),
    ),
}


def _month_day(year: int, text: str) -> date:
    month, day = text.split("-")
    return date(year, int(month), int(day))


def _build_holidays() -> dict[date, tuple[str, str]]:
    """把 _HOLIDAY_PLAN 摊平成 {日期: (节日名, "休"/"班")}。上班的日子不带节日名，只显示「班」。"""
    out: dict[date, tuple[str, str]] = {}
    for year, plan in _HOLIDAY_PLAN.items():
        for name, (start, end), workdays in plan:
            day, last = _month_day(year, start), _month_day(year, end)
            while day <= last:
                out[day] = (name, "休")
                day += timedelta(days=1)
            for text in workdays:
                out[_month_day(year, text)] = ("", "班")
    return out


_HOLIDAYS = _build_holidays()


def _leap_month(year: int) -> int:
    return LUNAR_INFO[year - FIRST_YEAR] & 0xF


def _leap_days(year: int) -> int:
    if not _leap_month(year):
        return 0
    return 30 if LUNAR_INFO[year - FIRST_YEAR] & 0x10000 else 29


def _month_days(year: int, month: int) -> int:
    return 30 if LUNAR_INFO[year - FIRST_YEAR] & (0x10000 >> month) else 29


def _year_days(year: int) -> int:
    total = 0
    for month in range(1, 13):
        total += _month_days(year, month)
        if month == _leap_month(year):
            total += _leap_days(year)
    return total


def _build_offsets() -> list[int]:
    """每年正月初一距离 1900-01-31 的天数，用来做二分查找。"""
    offsets = [0]
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        offsets.append(offsets[-1] + _year_days(year))
    return offsets


_OFFSETS = _build_offsets()
_EPOCH = date(FIRST_YEAR, 1, 31)  # 1900 年正月初一
_LAST_DAY = _EPOCH + timedelta(days=_OFFSETS[-1] - 1)  # 2099 年腊月最后一天
_TERM_YEARS: dict[int, int] = {}  # 每年的节气位图，第一次用到时解析


def available(day: date) -> bool:
    """这一天能不能算（农历表覆盖 1900-01-31 – 2099 年腊月末）。"""
    return _EPOCH <= day <= _LAST_DAY


def lunar_date(day: date) -> tuple[int, int, int, bool]:
    """公历 → (农历年, 月, 日, 是否闰月)。超出覆盖范围抛 ValueError。"""
    if not available(day):
        raise ValueError(f"{day} 超出农历表范围（{_EPOCH} – {_LAST_DAY}）")
    offset = (day - _EPOCH).days
    low, high = 0, len(_OFFSETS) - 1
    while low + 1 < high:  # 找最后一个 offsets[i] <= offset 的 i，那一年就是农历年
        mid = (low + high) // 2
        if _OFFSETS[mid] <= offset:
            low = mid
        else:
            high = mid
    year = FIRST_YEAR + low
    offset -= _OFFSETS[low]

    leap = _leap_month(year)
    month, is_leap = 1, False
    while month <= 12:
        days = _month_days(year, month)
        if offset < days:
            break
        offset -= days
        if month == leap:
            days = _leap_days(year)
            if offset < days:
                is_leap = True
                break
            offset -= days
        month += 1
    return year, month, offset + 1, is_leap


def _day_name(day: int) -> str:
    if day == 10:
        return "初十"
    if day < 10:
        return "初" + _DIGITS[day - 1]
    if day == 20:
        return "二十"
    if day == 30:
        return "三十"
    if day < 20:
        return "十" + _DIGITS[day - 11]
    return "廿" + _DIGITS[day - 21]


def _lunar_texts(day: date) -> tuple[str, str, int, int]:
    """(格子里显示的农历文字, 完整的农历文字, 农历月, 农历日)。

    初一显示月份（「八月」「闰四月」），其余显示日期（「廿七」）——这是老黄历的习惯。
    """
    _, month, day_of_month, is_leap = lunar_date(day)
    prefix = "闰" if is_leap else ""
    full = f"{prefix}{_MONTH_NAMES[month - 1]}{_day_name(day_of_month)}"
    text = f"{prefix}{_MONTH_NAMES[month - 1]}" if day_of_month == 1 else _day_name(day_of_month)
    return text, full, month, day_of_month


def solar_term(day: date) -> str:
    """这一天是不是节气，是就返回名字，不是返回空串。"""
    if not FIRST_YEAR <= day.year <= TERM_LAST_YEAR:
        return ""
    packed = _TERM_YEARS.get(day.year)
    if packed is None:
        start = (day.year - FIRST_YEAR) * 30
        packed = _TERM_YEARS[day.year] = int(TERM_BLOB[start : start + 30], 16)
    # 一年 24 个节气，每个 5 位，从高位到低位依次是 小寒、大寒、立春……
    for index in ((day.month - 1) * 2, (day.month - 1) * 2 + 1):
        if (packed >> (5 * (len(TERM_NAMES) - 1 - index))) & 0x1F == day.day:
            return TERM_NAMES[index]
    return ""


def festival(day: date) -> str:
    """这一天的节日：农历节日优先，然后公历节日、按星期几算的节日、法定节假日的名字。"""
    if available(day):
        lyear, month, day_of_month, is_leap = lunar_date(day)
        if not is_leap:
            if month == 12 and day_of_month == _month_days(lyear, 12):
                return "除夕"
            name = _LUNAR_FESTIVALS.get((month, day_of_month))
            if name:
                return name
    name = _SOLAR_FESTIVALS.get((day.month, day.day))
    if name:
        return name
    for (month, nth, weekday), name in _WEEKDAY_FESTIVALS.items():
        if day.month == month and day.weekday() == weekday and (day.day - 1) // 7 + 1 == nth:
            return name
    return _HOLIDAYS.get(day, ("", ""))[0]


def holiday(day: date) -> str:
    """法定节假日：放假「休」、调休上班「班」，都不是返回空串。"""
    return _HOLIDAYS.get(day, ("", ""))[1]


def day_info(day: date) -> dict[str, str]:
    """日历格子要用的全部信息（见模块开头的例子）。超出农历表范围时农历相关是空串。"""
    info = {
        "lunar": "",
        "lunar_full": "",
        "term": solar_term(day),
        "festival": festival(day),
        "holiday": holiday(day),
        "text": "",
        "kind": "",
    }
    if available(day):
        info["lunar"], info["lunar_full"], _, _ = _lunar_texts(day)
    for kind in ("term", "festival", "lunar"):  # 格子里只放一条：节气 > 节日 > 农历日
        if info[kind]:
            info["text"], info["kind"] = info[kind], kind
            break
    return info
