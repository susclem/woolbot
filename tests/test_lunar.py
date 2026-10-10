"""农历 / 二十四节气 / 节日 / 法定节假日的测试。

为什么要自己写一张表（`app/_lunar_tables.py`）而不用库：容器的 `requirements.txt`
只有 requests / pycryptodome / tzdata / segno，`Dockerfile` 是 `python:3.12-slim` 且
不装系统包 —— 加一个农历依赖就得连带它的编译环境和体积，而这个功能只用到「某一天的
农历是几月初几、是不是节气」这两件事，一张 1900–2099 的紧凑表就够，纯标准库。

表是怎么来的、怎么校验的，见 `tools/build_lunar_tables.py` 的说明：它用
`lunardate` 和 `LunarCalendar`（PyEphem）当基准逐日生成，开发时另有一个脚本把
1900-01-31 到 2100-02-08 的 73058 天全部对比过（农历 0 处不一致、节气 0 处不一致）。

这里钉住的是**公示日期**：法定节假日安排来自中国政府网的通知原文，写死在
`app/lunar.py::_HOLIDAY_PLAN` 里。一年发一次通知，以后照格式加一段，别改成猜。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

from app import lunar  # noqa: E402


def day(text: str) -> date:
    return date(*map(int, text.split("-")))


class LunarDateTest(unittest.TestCase):
    """农历日期本身 —— 用春节这些「不可能记错」的日子当锚点。"""

    def test_the_spring_festival_days(self):
        # 正月初一。2026 年春节 2 月 17 日、2025 年春节 1 月 29 日（通知里春节假期
        # 从除夕 1 月 28 日开始，所以文件里也能看到 1/28）。
        for text, want in (
            ("2026-02-17", "正月初一"),
            ("2025-01-29", "正月初一"),
            ("2026-02-15", "腊月廿八"),
            ("2025-01-28", "腊月廿九"),  # 除夕（2025 腊月是小月，只有廿九）
        ):
            with self.subTest(text):
                self.assertEqual(lunar.day_info(day(text))["lunar_full"], want)

    def test_the_other_holiday_days_the_notices_name(self):
        # 端午 五月初五、中秋 八月十五 —— 通知里写明了农历日期，正好当交叉验证。
        self.assertEqual(lunar.day_info(day("2026-06-19"))["lunar_full"], "五月初五")
        self.assertEqual(lunar.day_info(day("2026-09-25"))["lunar_full"], "八月十五")

    def test_a_leap_month_is_spelled_out(self):
        # 2025 年是闰六月：2025-07-25 是闰六月初一，往后八月才是正常月份。
        info = lunar.day_info(day("2025-07-25"))
        self.assertEqual(info["lunar_full"], "闰六月初一")
        self.assertEqual(info["lunar"], "闰六月")
        self.assertEqual(lunar.day_info(day("2025-08-23"))["lunar_full"], "七月初一")

    def test_the_first_day_of_a_month_shows_the_month(self):
        # 格子里地方小：初一显示「八月」而不是「初一」，否则一屏都是「初一」。
        info = lunar.day_info(day("2026-10-10"))
        self.assertEqual(info["lunar_full"], "九月初一")
        self.assertEqual(info["text"], "九月")
        self.assertEqual(info["kind"], "lunar")
        self.assertEqual(lunar.day_info(day("2026-10-07"))["lunar_full"], "八月廿七")

    def test_the_table_covers_a_century_and_says_so(self):
        self.assertTrue(lunar.available(day("1900-01-31")))
        self.assertTrue(lunar.available(day("2100-02-08")))
        self.assertFalse(lunar.available(day("1899-12-31")))
        self.assertFalse(lunar.available(day("2100-02-09")))
        # 表外面不是抛异常，而是「什么都没有」—— 页面上顶多少一行小字。
        self.assertEqual(lunar.day_info(day("1899-12-31"))["text"], "")
        self.assertEqual(lunar.day_info(day("2100-02-09"))["text"], "")


class SolarTermTest(unittest.TestCase):
    """二十四节气：日期每年都在挪，所以拿几个公示过的日子钉住。"""

    def test_the_terms_around_a_year(self):
        for text, want in (
            ("2026-02-04", "立春"),
            ("2026-03-20", "春分"),
            ("2026-04-05", "清明"),
            ("2026-06-21", "夏至"),
            ("2026-08-07", "立秋"),
            ("2026-09-23", "秋分"),
            ("2026-10-08", "寒露"),
            ("2026-11-22", "小雪"),
            ("2026-12-22", "冬至"),
        ):
            with self.subTest(text):
                self.assertEqual(lunar.solar_term(day(text)), want)

    def test_january_terms_belong_to_the_year_they_fall_in(self):
        # 小寒/大寒是公历 1 月，容易被「农历年」的边界带跑（表按公历年切）。
        self.assertEqual(lunar.solar_term(day("2026-01-05")), "小寒")
        self.assertEqual(lunar.solar_term(day("2026-01-20")), "大寒")
        self.assertEqual(lunar.solar_term(day("2026-10-07")), "")

    def test_every_year_has_exactly_the_twenty_four_terms(self):
        # 表的完整性：一年不多不少 24 个，且名字都对得上。
        for year in (1900, 1950, 2026, 2099, 2100):
            with self.subTest(year):
                names = []
                d = date(year, 1, 1)
                while d.year == year:
                    term = lunar.solar_term(d)
                    if term:
                        names.append(term)
                    d = date.fromordinal(d.toordinal() + 1)
                self.assertEqual(len(names), 24)
                self.assertEqual(len(set(names)), 24)


class FestivalTest(unittest.TestCase):
    """传统节日 + 公历节日 + 按星期算的节日。"""

    def test_traditional_festivals(self):
        for text, want in (
            ("2026-02-17", "春节"),
            ("2026-06-19", "端午节"),
            ("2026-09-25", "中秋节"),
            ("2026-03-20", "龙抬头"),      # 二月初二
            ("2026-08-19", "七夕"),        # 七月初七
        ):
            with self.subTest(text):
                self.assertEqual(lunar.festival(day(text)), want)

    def test_new_years_eve_is_the_last_day_of_the_lunar_year(self):
        # 除夕没有固定日期，是腊月的最后一天（腊月有时廿九、有时三十）。
        self.assertEqual(lunar.festival(day("2025-01-28")), "除夕")   # 腊月廿九
        self.assertEqual(lunar.festival(day("2026-02-16")), "除夕")   # 腊月三十
        self.assertEqual(lunar.festival(day("2100-02-08")), "除夕")   # 表的最后一天

    def test_solar_festivals(self):
        for text, want in (
            ("2026-01-01", "元旦"),
            ("2026-05-01", "劳动节"),
            ("2026-10-01", "国庆节"),
            ("2026-02-14", "情人节"),
            ("2026-08-01", "建军节"),
        ):
            with self.subTest(text):
                self.assertEqual(lunar.festival(day(text)), want)

    def test_festivals_that_follow_the_weekday(self):
        # 母亲节＝五月第二个周日、父亲节＝六月第三个周日、感恩节＝十一月第四个周四。
        self.assertEqual(lunar.festival(day("2026-05-10")), "母亲节")
        self.assertEqual(lunar.festival(day("2026-06-21")), "父亲节")
        # 2026-11-26 是十一月的第四个周四
        self.assertEqual(lunar.festival(day("2026-11-26")), "感恩节")

    def test_the_calendar_itself_is_not_a_festival(self):
        self.assertEqual(lunar.festival(day("2026-10-07")), "国庆节")
        self.assertEqual(lunar.festival(day("2026-10-08")), "")


class HolidayTest(unittest.TestCase):
    """法定节假日：休/班。数据来源见 `app/lunar.py::_HOLIDAY_PLAN` 的注释。"""

    def test_the_2026_spring_festival_run(self):
        # 国办发明电〔2025〕7号：2/15–2/23 放假调休，2/14、2/28 上班。
        self.assertEqual(lunar.holiday(day("2026-02-14")), "班")
        self.assertEqual(lunar.holiday(day("2026-02-15")), "休")
        self.assertEqual(lunar.holiday(day("2026-02-23")), "休")
        self.assertEqual(lunar.holiday(day("2026-02-24")), "")
        self.assertEqual(lunar.holiday(day("2026-02-28")), "班")

    def test_the_2026_national_day_run_and_its_make_up_days(self):
        # 10/1–10/7 放假，9/20、10/10 上班。
        self.assertEqual(lunar.holiday(day("2026-09-20")), "班")
        self.assertEqual(lunar.holiday(day("2026-10-01")), "休")
        self.assertEqual(lunar.holiday(day("2026-10-07")), "休")
        self.assertEqual(lunar.holiday(day("2026-10-08")), "")
        self.assertEqual(lunar.holiday(day("2026-10-10")), "班")

    def test_the_2026_new_year_and_may_day(self):
        self.assertEqual(lunar.holiday(day("2026-01-01")), "休")
        self.assertEqual(lunar.holiday(day("2026-01-03")), "休")
        self.assertEqual(lunar.holiday(day("2026-01-04")), "班")
        self.assertEqual(lunar.holiday(day("2026-05-05")), "休")
        self.assertEqual(lunar.holiday(day("2026-05-09")), "班")

    def test_the_2025_rules_differ_from_2026(self):
        # 2025 的通知（国办发明电〔2024〕12号）：春节 1/28–2/4，上班 1/26、2/8。
        self.assertEqual(lunar.holiday(day("2025-01-26")), "班")
        self.assertEqual(lunar.holiday(day("2025-01-28")), "休")
        self.assertEqual(lunar.holiday(day("2025-02-04")), "休")
        self.assertEqual(lunar.holiday(day("2025-02-08")), "班")
        # 元旦只放一天，不调休。
        self.assertEqual(lunar.holiday(day("2025-01-01")), "休")
        self.assertEqual(lunar.holiday(day("2025-01-02")), "")

    def test_a_make_up_workday_has_no_festival_name(self):
        # 调休上班的日子只显示「班」——写「春节」会被读成放假。
        info = lunar.day_info(day("2026-02-14"))
        self.assertEqual(info["holiday"], "班")
        self.assertEqual(info["festival"], "情人节")  # 公历节日照常
        self.assertEqual(lunar.day_info(day("2026-09-20"))["festival"], "")

    def test_a_year_we_have_no_notice_for_is_just_empty(self):
        # 2027 年的安排在写这段时还没发布：不知道就说不知道，不要猜。
        self.assertEqual(lunar.holiday(day("2027-10-01")), "")


class DayInfoTest(unittest.TestCase):
    """格子里那一行小字：只有一条，优先级 节气 > 节日 > 农历日。"""

    def test_the_order_of_importance(self):
        # 2026-09-25 既是中秋又是农历八月十五 → 显示节日。
        info = lunar.day_info(day("2026-09-25"))
        self.assertEqual(info["text"], "中秋节")
        self.assertEqual(info["kind"], "festival")
        # 2026-04-05 既是清明（节气）又是清明节 → 显示节气。
        info = lunar.day_info(day("2026-04-05"))
        self.assertEqual(info["text"], "清明")
        self.assertEqual(info["kind"], "term")
        # 什么都没有的日子显示农历日。
        info = lunar.day_info(day("2026-10-12"))
        self.assertEqual(info["kind"], "lunar")
        self.assertEqual(info["text"], info["lunar"])

    def test_every_field_is_a_plain_string(self):
        # 接口直接把它塞进 JSON，不能漏出 date/datetime 对象。
        info = lunar.day_info(day("2026-10-07"))
        self.assertEqual(
            sorted(info),
            ["festival", "holiday", "kind", "lunar", "lunar_full", "term", "text"],
        )
        for key, value in info.items():
            self.assertIsInstance(value, str, key)

    def test_a_whole_year_is_computable_without_blowing_up(self):
        # 接口一次能拉一整年，这里至少把「不抛异常、字段齐全」扫一遍。
        d = date(2026, 1, 1)
        count = 0
        while d.year == 2026:
            info = lunar.day_info(d)
            self.assertTrue(info["lunar_full"], d)
            self.assertIn(info["kind"], ("term", "festival", "lunar", "holiday", ""))
            d = date.fromordinal(d.toordinal() + 1)
            count += 1
        self.assertEqual(count, 365)


if __name__ == "__main__":
    unittest.main(verbosity=2)
