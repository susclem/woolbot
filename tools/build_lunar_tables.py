# -*- coding: utf-8 -*-
"""生成 woolbot/app/_lunar_tables.py（开发时跑一次，运行时不依赖任何第三方库）。

用法：
    $env:PYTHONIOENCODING="utf-8"
    & $py tools\\build_lunar_tables.py            # 写 app/_lunar_tables.py
    & $py tools\\build_lunar_tables.py --stdout   # 只打印，不写文件

生成依赖（只在生成时用，不进 requirements.txt）：
    pip install --target .lunartools lunardate LunarCalendar

- 农历表：lunardate 的 YEAR_INFOS（1900–2099，每年一个整数，低 4 位是闰月，bit 0x10000 起是每月大小月）。
- 节气表：LunarCalendar（PyEphem 按太阳黄经算）每年 24 个节气的“日”，每个日 5 位、24 个 = 120 位 = 30 个十六进制字符。
  按“公历年内”的顺序：小寒、大寒（1 月）、立春、雨水（2 月）……大雪、冬至（12 月）。
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
HERE = TOOLS.parent
TARGET = HERE / "app" / "_lunar_tables.py"

FIRST_YEAR = 1900
LAST_YEAR = 2099
# 农历年 2099 的腊月会伸到 2100 年 2 月，所以节气表多算一年，免得那几天查不到。
TERM_LAST_YEAR = 2100

TERM_NAMES = (
    "小寒 大寒 立春 雨水 惊蛰 春分 清明 谷雨 立夏 小满 芒种 夏至 "
    "小暑 大暑 立秋 处暑 白露 秋分 寒露 霜降 立冬 小雪 大雪 冬至"
).split()


def _load_oracles():
    for cand in (HERE.parent / ".lunartools", Path(r".lunartools")):
        if cand.is_dir():
            sys.path.insert(0, str(cand))
            break
    else:  # pragma: no cover - 只在没装生成依赖时走到
        raise SystemExit("找不到 .lunartools，先 pip install --target 它 lunardate LunarCalendar")
    from lunardate import YEAR_INFOS
    from lunarcalendar.solarterm import solarterms

    return YEAR_INFOS, solarterms


def term_days(solarterms, year: int) -> list[int]:
    """公历 `year` 年里 24 个节气的“日”，按 1 月到 12 月、每月两个的顺序。"""
    order = [22, 23] + list(range(0, 22))  # 小寒/大寒在库里的下标是 22/23
    days = []
    for idx in order:
        d = solarterms[idx].date_func(year)
        if d.year != year:  # pragma: no cover - 说明对齐错了
            raise SystemExit(f"{year} 的第 {idx} 个节气算到 {d} 去了")
        days.append(d.day)
    return days


def pack(days: list[int]) -> str:
    value = 0
    for day in days:
        if not 1 <= day <= 31:
            raise SystemExit(f"日期 {day} 装不进 5 位")
        value = (value << 5) | day
    return "%030x" % value


def render(year_infos, solarterms) -> str:
    out = [
        "# -*- coding: utf-8 -*-",
        '"""农历与二十四节气的数据表——**由 tools/build_lunar_tables.py 生成，不要手改**。',
        "",
        f"覆盖 {FIRST_YEAR}–{LAST_YEAR} 年。LUNAR_INFO 每年一个整数（低 4 位 = 闰月，1–12；"
        "bit 0x10000 起从正月开始，1 = 30 天、0 = 29 天）；TERM_BLOB 每年 30 个十六进制字符，"
        "每 5 位一个节气的日，按公历年内顺序：小寒、大寒、立春……大雪、冬至。",
        '"""',
        "from __future__ import annotations",
        "",
        f"FIRST_YEAR = {FIRST_YEAR}",
        f"LAST_YEAR = {LAST_YEAR}",
        f"TERM_LAST_YEAR = {TERM_LAST_YEAR}",
        "",
        "TERM_NAMES = (",
    ]
    for i in range(0, 24, 6):
        out.append('    "%s "' % " ".join(TERM_NAMES[i : i + 6]))
    out.append(").split()")
    out.append("")
    out.append("LUNAR_INFO: tuple[int, ...] = (")
    line = "    "
    for info in year_infos:
        piece = "0x%05x, " % info
        if len(line) + len(piece) > 96:
            out.append(line.rstrip())
            line = "    "
        line += piece
    if line.strip():
        out.append(line.rstrip())
    out.append(")")
    out.append("")
    out.append("TERM_BLOB = (")
    blob = "".join(pack(term_days(solarterms, y)) for y in range(FIRST_YEAR, TERM_LAST_YEAR + 1))
    for i in range(0, len(blob), 96):
        out.append('    "%s"' % blob[i : i + 96])
    out.append(")")
    out.append("")
    return "\n".join(out)


def main() -> None:
    year_infos, solarterms = _load_oracles()
    if len(year_infos) != LAST_YEAR - FIRST_YEAR + 1:
        raise SystemExit("lunardate 的年份数量对不上：%d" % len(year_infos))
    text = render(year_infos, solarterms)
    if "--stdout" in sys.argv:
        print(text)
        return
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print("已写出 %s（%d 字节）" % (TARGET, len(text.encode("utf-8"))))
    for y in (2025, 2026, 2027):
        days = term_days(solarterms, y)
        sample = " ".join(
            "%s%d/%d" % (TERM_NAMES[i], i // 2 + 1, days[i]) for i in (0, 2, 4, 6, 8, 10, 18, 22)
        )
        print(y, sample)


if __name__ == "__main__":
    main()
