"""核心逻辑自测：不需要 API key，用一个假的 LLM 替换模型层。

跑法：
    python -m tests.test_core
或
    python tests/test_core.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, pipeline as pl, settings as st, timeutil as tu  # noqa: E402
from app.llm import LLM  # noqa: E402
from app.pipeline import Bot  # noqa: E402


class FakeLLM(LLM):
    """按预设脚本返回，完全不发网络请求。"""

    def __init__(self, script: dict):
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def understand(self, text: str) -> dict:  # type: ignore[override]
        self.calls.append(("understand", text))
        return self.script.get(text, {"intent": "other", "reply": "没听懂"})

    def confirm(self, question: str, payload: dict, user_text: str) -> dict:  # type: ignore[override]
        self.calls.append(("confirm", user_text))
        return self.script.get("__confirm__:" + user_text, {"confirmed": False})

    def understand_image(self, image_bytes, mime="image/jpeg") -> dict:  # type: ignore[override]
        self.calls.append(("image", ""))
        return self.script.get("__image__", {"intent": "ingest"})

    def chat(self, text: str) -> str:  # type: ignore[override]
        return "chat:" + text


CFG = {
    "timezone": "Asia/Shanghai",
    "db_path": "",  # 运行时替换
    "query": {"mode": "minute", "show_ongoing": True},
    "web": {"host": "127.0.0.1", "port": 8080},
}


def iso(days: int, hhmm: str) -> str:
    d = tu.today() + timedelta(days=days)
    return f"{d.isoformat()}T{hhmm}"


class CoreTest(unittest.TestCase):
    def setUp(self) -> None:
        # 用仓库内的临时目录，避免写到系统 Temp（沙箱会拦）。
        # 注意：不要用 tempfile.mkdtemp —— 它建的目录在本机沙箱下不可写
        # （连 os.open 都 Permission denied），手工 mkdir 才正常。
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"case-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")
        self.cfg = dict(CFG)
        self.cfg["db_path"] = self.db_file
        # 设置是按库缓存的：换了库不丢缓存，上一个用例写的 chat_enabled 会飘过来。
        st.reset_cache()

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def bot(self, script: dict) -> Bot:
        return Bot(self.cfg, FakeLLM(script))

    # -- 录入 ---------------------------------------------------------

    def test_ingest_single(self):
        script = {
            "明天上午9点半京东超市满99减50领券，限会员": {
                "intent": "ingest",
                "title": "京东超市满99减50领券",
                "platform": "京东",
                "grab_at": iso(1, "09:30"),
                "end_at": iso(1, "23:59"),
                "note": "限会员",
                "needs_clarify": False,
            }
        }
        reply = self.bot(script).handle_text("明天上午9点半京东超市满99减50领券，限会员")
        self.assertIn("已记下 1 条", reply)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=3))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grab_at"], iso(1, "09:30"))
        self.assertEqual(rows[0]["platform"], "京东")

    def test_ingest_multiple(self):
        text = "京东9点抢券，淘宝10点秒杀"
        script = {
            text: {
                "intent": "ingest",
                "activities": [
                    {"title": "京东抢券", "grab_at": iso(0, "09:00")},
                    {"title": "淘宝秒杀", "grab_at": iso(0, "10:00")},
                ],
                "needs_clarify": False,
            }
        }
        reply = self.bot(script).handle_text(text)
        self.assertIn("已记下 2 条", reply)
        self.assertEqual(len(db.list_range(tu.today(), tu.today())), 2)

    def test_ingest_duplicate_skipped(self):
        script = {
            "京东9点抢券": {
                "intent": "ingest",
                "title": "京东抢券",
                "grab_at": iso(0, "09:00"),
                "needs_clarify": False,
            }
        }
        b = self.bot(script)
        b.handle_text("京东9点抢券")
        b.handle_text("京东9点抢券", msg_id="m2")
        self.assertEqual(len(db.list_range(tu.today(), tu.today())), 1)

    def test_msg_idempotent(self):
        script = {
            "京东9点抢券": {
                "intent": "ingest",
                "title": "京东抢券",
                "grab_at": iso(0, "09:00"),
                "needs_clarify": False,
            }
        }
        b = self.bot(script)
        b.handle_text("京东9点抢券", msg_id="same")
        second = b.handle_text("京东9点抢券", msg_id="same")
        self.assertEqual(second, "")
        self.assertEqual(len(db.list_range(tu.today(), tu.today())), 1)

    def test_clarify_then_confirm(self):
        text = "京东有个活动"
        script = {
            text: {
                "intent": "ingest",
                "title": "京东活动",
                "grab_at": None,
                "needs_clarify": True,
                "clarify_question": "几点开抢？",
            },
            "__confirm__:早上9点": {
                "confirmed": True,
                "activities": [
                    {"title": "京东活动", "grab_at": iso(0, "09:00")}
                ],
            },
        }
        b = self.bot(script)
        r1 = b.handle_text(text, user_id="u1")
        self.assertIn("几点开抢", r1)
        self.assertEqual(len(db.list_range(tu.today(), tu.today())), 0)  # 还没入库
        r2 = b.handle_text("早上9点", user_id="u1")
        self.assertIn("已记下 1 条", r2)
        self.assertEqual(len(db.list_range(tu.today(), tu.today())), 1)

    def test_past_time_triggers_clarify(self):
        """模型把「下周三」算成「本周三」是经典错误，必须被拦下。"""
        script = {
            "周三上午9点秒杀": {
                "intent": "ingest",
                "title": "秒杀",
                "grab_at": "2020-01-01T09:00",
                "needs_clarify": False,
            }
        }
        b = self.bot(script)
        r = b.handle_text("周三上午9点秒杀", user_id="u1")
        self.assertIn("已经过去了", r)
        self.assertEqual(len(db.list_range(tu.today() - timedelta(days=30), tu.today())), 0)

    # -- 查询 ---------------------------------------------------------

    def _seed(self):
        db.insert_activity({"title": "京东整点券", "platform": "京东", "grab_at": iso(0, "09:00")})
        db.insert_activity({"title": "淘宝半点秒杀", "platform": "淘宝", "grab_at": iso(0, "09:30")})
        db.insert_activity({"title": "美团全天领券", "platform": "美团",
                            "start_at": iso(0, "00:00"), "end_at": iso(0, "23:59"),
                            "grab_at": None})

    def test_query_exact_minute_hit(self):
        self._seed()
        script = {"9点半抢什么": {"intent": "query", "date": tu.today().isoformat(), "time": "09:30"}}
        r = self.bot(script).handle_text("9点半抢什么")
        self.assertIn("淘宝半点秒杀", r)
        self.assertNotIn("京东整点券", r.split("📌")[0])  # 9:00 的不该出现在 9:30 命中区

    def test_a_minute_miss_does_not_drag_in_the_earlier_openings(self):
        """用户 2026-10-07 的原话：问 11 点，就别把 9 点、10 点那些抖出来。

        「抢券」抢的是第一分钟，已经开抢过去的信息对他没用；只能领不能抢的
        （`_seed` 里那张「美团全天领券」）同样不带出来。
        """
        self._seed()
        script = {"9点45抢什么": {"intent": "query", "date": tu.today().isoformat(), "time": "09:45"}}
        r = self.bot(script).handle_text("9点45抢什么")
        self.assertIn("没有开抢安排", r)
        self.assertNotIn("京东整点券", r)      # 09:00 已经开抢过了
        self.assertNotIn("淘宝半点秒杀", r)    # 09:30 也是
        self.assertNotIn("美团全天领券", r)    # 能领，但不是这一刻开抢
        self.assertNotIn("还能领的", r)

    def test_a_repeating_coupon_is_found_by_its_minute(self):
        """「每天 08:30」在库里只有一行、日期还停在录入那天。

        精确问「明天 8:30 抢什么」必须命中 —— 否则同一句「明天抢什么」列着 08:30，
        精确问那一刻却说「没有」，自相矛盾（真机上就是这么自相矛盾的）。
        """
        db.insert_activity({"title": "每日签到券", "platform": "京东",
                            "grab_at": iso(0, "08:30"), "end_at": iso(60, "23:59"),
                            "repeat_kind": "daily", "repeat_at": "08:30"})
        r = self.bot({}).handle_text("明天8:30抢什么")
        self.assertIn(f"{pl.bold_time('08:30')} 开抢", r)   # 表头带（加粗的）时刻，每行只写标题
        self.assertIn("每日签到券", r)
        self.assertNotIn("没有开抢安排", r)
        # 换个不存在的时刻问，仍然要说没有
        r2 = self.bot({}).handle_text("后天9点半抢什么")
        self.assertIn("没有开抢安排", r2)

    def test_a_miss_on_an_empty_day_just_says_so(self):
        """那一天没有就问不出东西来 —— 就到这里。

        用户 2026-10-07 的原话：「我问今天的活动，为什么要把后面的给带出来，
        没有就结束就行了啊」。以前这里会补一节「🔎 供参照，接下来最近的」，把后面
        几天的活动抖出来，看着像答非所问。往前、往后都不带。
        """
        db.insert_activity({"title": "昨天的券", "platform": "京东", "grab_at": iso(-1, "10:00")})
        db.insert_activity({"title": "明天的券", "platform": "京东", "grab_at": iso(1, "10:00")})
        script = {"9点45抢什么": {"intent": "query", "date": tu.today().isoformat(), "time": "09:45"}}
        r = self.bot(script).handle_text("9点45抢什么")
        self.assertIn("没有开抢安排", r)
        self.assertNotIn("明天的券", r)
        self.assertNotIn("昨天的券", r)
        self.assertNotIn("供参照", r)

    def test_a_minute_miss_does_not_drag_in_the_next_few_days(self):
        """每天 08:30 的券明天开抢，也不该出现在「今天 15:00 没有」的回复里。"""
        db.insert_activity({"title": "每日港城券", "platform": "中国银行",
                            "grab_at": iso(-1, "08:30"), "end_at": iso(60, "23:59"),
                            "repeat_kind": "daily", "repeat_at": "08:30"})
        # 把「现在」钉死在今天 11:07，否则这条断言随跑测试的钟点变
        fake_now = datetime.combine(tu.today(), time(11, 7))
        with mock.patch.object(pl.tu, "now", return_value=fake_now):
            r = self.bot({}).handle_text("今天15:00抢什么")
        self.assertIn("没有开抢安排", r)
        self.assertNotIn("每日港城券", r)
        self.assertNotIn("供参照", r)

    def test_the_later_section_is_capped(self):
        """真机上这一节能一次列出 8 条长标题 —— 微信里回一屏就是失败。"""
        for i in range(7):
            db.insert_activity({"title": f"待抢券{i}", "platform": "京东",
                                "grab_at": iso(1, f"{10 + i:02d}:00")})
        r = self.bot({}).handle_text("明天09:45抢什么")
        self.assertIn("还没开始的 7 条", r)
        section = r.split("📌")[1]
        self.assertEqual(section.count("待抢券"), 5)    # 默认封顶 5 条
        self.assertIn("还有 2 条，去日历页面看。", section)

    def test_a_minute_query_adds_what_is_still_ahead_that_day(self):
        """用户允许拖带的只有这一种：**还没开始**的那些。"""
        db.insert_activity({"title": "上午券", "grab_at": iso(1, "09:00")})
        db.insert_activity({"title": "午后券", "grab_at": iso(1, "14:00")})
        db.insert_activity({"title": "晚间券", "grab_at": iso(1, "20:00")})
        r = self.bot({}).handle_text("明天9:45抢什么")
        self.assertIn("没有开抢安排", r)
        self.assertIn("还没开始的 2 条", r)
        # 用户 2026-10-07 定的格式：时刻单独一行、加粗，标题摆在它下面
        self.assertIn(f"   {pl.bold_time('14:00')}\n   午后券", r)
        self.assertIn(f"   {pl.bold_time('20:00')}\n   晚间券", r)
        self.assertNotIn("上午券", r)          # 09:00 早于他问的那一刻

    def test_an_empty_day_query_just_says_so(self):
        """「这周没有活动记录」就是句号，后面不再接别的日子的活动。"""
        db.insert_activity({"title": "后天的券", "platform": "京东", "grab_at": iso(2, "12:00")})
        tomorrow = tu.today() + timedelta(days=1)
        script = {"明天有什么": {"intent": "query", "date": tomorrow.isoformat()}}
        r = self.bot(script).handle_text("明天有什么")
        self.assertIn("没有活动记录", r)
        self.assertNotIn("后天的券", r)
        self.assertNotIn("接下来要抢的", r)

    def test_the_day_query_still_shows_what_you_can_still_claim(self):
        """按分钟的问法不再带「还能领的」，但「今天有什么活动」这条老路照旧 ——
        那是看全天的清单，把还在有效期内的券藏起来反而会让人以为没得领。
        """
        db.insert_activity({"title": "长期可领券", "platform": "京东",
                            "grab_at": iso(-2, "08:00"), "start_at": iso(-2, "08:00"),
                            "end_at": iso(10, "23:59")})
        script = {"今天有什么活动": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天有什么活动")
        self.assertIn("还能领的", r)
        self.assertIn("长期可领券", r)

    def test_an_already_started_coupon_is_not_dragged_in_as_still_ahead(self):
        """模型常只给「9点半开抢」+「到今天23:59」，没有 start_at。

        以前它会被列进「还能领的」；按分钟的回复里不许再出现 —— 9:45 问「抢什么」
        时它已经开抢过了，正是用户说的「今天不能抢的」那一类。
        """
        db.insert_activity({"title": "只有开抢时刻的券", "platform": "京东",
                            "grab_at": iso(0, "09:30"), "end_at": iso(0, "23:59")})
        script = {"9点45抢什么": {"intent": "query", "date": tu.today().isoformat(), "time": "09:45"}}
        r = self.bot(script).handle_text("9点45抢什么")
        self.assertIn("没有开抢安排", r)
        self.assertNotIn("只有开抢时刻的券", r)

    def test_the_minute_miss_heading_has_no_midnight(self):
        """「📭 10月6日 00:00 周二 9:45 没有开抢安排」里的 00:00 只会让人愣一下。"""
        self._seed()
        script = {"9点45抢什么": {"intent": "query", "date": tu.today().isoformat(), "time": "09:45"}}
        head = self.bot(script).handle_text("9点45抢什么").split("\n")[0]
        self.assertIn("没有开抢安排", head)
        self.assertNotIn("00:00", head)

    def test_query_day_no_time(self):
        self._seed()
        script = {"今天有什么活动": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天有什么活动")
        self.assertIn("京东整点券", r)
        self.assertIn("淘宝半点秒杀", r)

    def test_query_weekday_phrase(self):
        self._seed()
        tomorrow = tu.today() + timedelta(days=1)
        script = {"明天抢什么": {"intent": "query", "date": tomorrow.isoformat()}}
        r = self.bot(script).handle_text("明天抢什么")
        self.assertIn("没有活动记录", r)
        # 今天那三条都在 `_seed` 里，但用户要的是**明天** —— 不拿它们凑数
        self.assertNotIn("京东整点券", r)
        self.assertNotIn("接下来要抢的", r)

    def test_query_keyword(self):
        self._seed()
        script = {"今天京东有什么": {"intent": "query", "date": tu.today().isoformat(), "keyword": "京东"}}
        r = self.bot(script).handle_text("今天京东有什么")
        self.assertIn("京东整点券", r)
        self.assertNotIn("淘宝半点秒杀", r)

    # -- 「开抢的」和「还能领的」必须分开 --------------------------------

    def _long_running(self, title: str = "港城欢乐购中行券", grab_days: int = 1):
        """用户库里真有两张这样的券：开抢时刻在明天，但有效期跨到年底。

        它们对**每一天**都「还在有效期内」，所以曾经被列进「今天有什么活动」，
        行首却写着明天的日期。
        """
        db.insert_activity({
            "title": title, "platform": "中国银行",
            "grab_at": iso(grab_days, "08:30"),
            "start_at": iso(-100, "08:30"),
            "end_at": iso(86, "23:59"),
        })

    def _range_bot(self):
        """问「这周有哪些活动」的机器人（今天 ~ 今天+6）。"""
        script = {"这周有哪些活动": {
            "intent": "query",
            "date_from": tu.today().isoformat(),
            "date_to": (tu.today() + timedelta(days=6)).isoformat(),
        }}
        return self.bot(script)

    def test_today_does_not_borrow_tomorrows_grab(self):
        self._long_running()
        script = {"今天抢什么": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天抢什么")
        head = r.split("📌")[0]
        self.assertIn("没有开抢安排", head)
        self.assertNotIn("港城欢乐购中行券", head)

    def test_the_claimable_coupon_is_still_told_about(self):
        """不能因为「不是今天开抢」就把用户能领的券藏起来。"""
        self._long_running()
        script = {"今天抢什么": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天抢什么")
        self.assertIn("还能领的", r)
        self.assertIn("港城欢乐购中行券", r)
        self.assertIn("截止", r)  # 「还能领的」重点是截止时间，不是开抢时间

    def test_a_coupon_that_has_not_opened_yet_is_not_called_claimable(self):
        """开抢在两天后、截止也在两天后的券，今天谈不上「还能领」。

        `end >= 今天` 曾经把它冒充成「已开始」—— 日历上摆的是同一套判据，所以
        那一天的字面板也会多摆一张卡。
        """
        row = {"title": "后天才开抢", "grab_at": iso(2, "10:00"), "end_at": iso(2, "22:00")}
        self.assertFalse(pl.claimable_on(row, tu.today()))

    def test_a_coupon_whose_grab_has_passed_is_still_in_this_day(self):
        """开抢时刻在过去、没有 start_at 的长效券，必须被这一天的查询捞出来。

        `db.list_range` 原来只认 `start_at`，模型只给 grab_at 时这张券对**每一天**
        都隐身：机器人答「没有活动记录」，可它明明还能领。
        """
        db.insert_activity({"title": "已开抢的长效券", "grab_at": iso(-2, "10:00"),
                            "end_at": iso(20, "23:59")})
        db.insert_activity({"title": "后天才开抢", "grab_at": iso(2, "10:00"),
                            "end_at": iso(2, "22:00")})
        titles = [r["title"] for r in db.list_range(tu.today(), tu.today())]
        self.assertIn("已开抢的长效券", titles)
        self.assertNotIn("后天才开抢", titles)

    def test_a_running_coupon_without_a_start_is_still_claimable(self):
        """上一条的端到端版本：没有 `start_at`、但已经开过抢的长效券，今天仍算能领。"""
        db.insert_activity({"title": "已经在领的券", "grab_at": iso(-2, "10:00"),
                            "end_at": iso(20, "23:59")})
        script = {"今天抢什么": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天抢什么")
        self.assertIn("还能领的", r)
        self.assertIn("已经在领的券", r)

    def test_today_lists_what_really_grabs_today(self):
        db.insert_activity({"title": "今天的券", "grab_at": iso(0, "10:00")})
        db.insert_activity({"title": "明天的券", "grab_at": iso(1, "10:00")})
        script = {"今天抢什么": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天抢什么")
        head = r.split("📌")[0]
        self.assertIn("今天的券", head)
        self.assertNotIn("明天的券", r)

    def test_the_day_heading_has_no_midnight(self):
        """标题上出现 `00:00` 只会让人以为自己看错了。"""
        self._seed()
        script = {"今天有什么活动": {"intent": "query", "date": tu.today().isoformat()}}
        r = self.bot(script).handle_text("今天有什么活动")
        d = tu.today()
        self.assertIn(f"{d.month}月{d.day}日", r)
        self.assertNotIn(f"{d.month}月{d.day}日 00:00", r)

    def test_a_range_does_not_repeat_a_long_running_coupon(self):
        """问「这周有哪些活动」：一条跨全周的长效券只该出现一次，不是七次。"""
        self._long_running("长效券")
        r = self._range_bot().handle_text("这周有哪些活动")
        self.assertEqual(r.count("长效券"), 1, r)

    def test_a_range_mentions_a_coupon_that_opens_outside_it(self):
        """整周都在有效期内、但这周不开抢的券，末尾单独说一句，别塞进每一天。"""
        self._long_running("长效券", grab_days=10)  # 开抢在区间之外
        r = self._range_bot().handle_text("这周有哪些活动")
        self.assertEqual(r.count("长效券"), 1, r)
        self.assertIn("整个区间内都在有效期内", r)

    def test_show_ongoing_off_hides_the_claimable_section(self):
        self._long_running("长效券")
        cfg = dict(CFG)
        cfg["db_path"] = self.db_file
        cfg["query"] = dict(CFG["query"], show_ongoing=False)
        script = {"今天抢什么": {"intent": "query", "date": tu.today().isoformat()}}
        r = Bot(cfg, FakeLLM(script)).handle_text("今天抢什么")
        self.assertNotIn("长效券", r)

    def test_ingest_stale_today_still_records(self):
        """今天早些时候的活动照记，**一个字都不多提**。

        以前回执里会跟一句「⏰ 提醒一句：… 的开抢时间已经过了。」，用户 2026-10-10 把
        它划掉了：「就算时间已经过了也没关系，不用提醒，就正常录入就行了」。所以这条
        既盯「记进去了」，也反向盯「别提那句」—— 那种话爬回来就是回退。
        仍然不许拦下来问「是不是下周」（那是 `wrong_day` 那一类的事）。
        """
        from datetime import datetime as _dt

        today = tu.today()
        early = _dt(today.year, today.month, today.day, 0, 5)
        if early >= tu.now():  # 深夜跑测试时跳过
            self.skipTest("当前时间早于 00:05，构造不出「今天已过去」")
        script = {
            "今天0点5分秒杀": {
                "intent": "ingest",
                "title": "凌晨秒杀",
                "grab_at": tu.to_minute(early),
                "needs_clarify": False,
            }
        }
        r = self.bot(script).handle_text("今天0点5分秒杀", user_id="u1")
        self.assertIn("已记下 1 条", r)
        self.assertNotIn("已经过了", r)
        self.assertNotIn("提醒一句", r)
        self.assertNotIn("是不是下周", r)
        self.assertEqual(len(db.list_range(tu.today(), tu.today())), 1)

    def test_query_time_without_date_defaults_today(self):
        self._seed()
        script = {"9点抢什么": {"intent": "query", "time": "09:00"}}
        r = self.bot(script).handle_text("9点抢什么")
        self.assertIn("京东整点券", r)

    # -- 指令 ---------------------------------------------------------

    def test_help(self):
        r = self.bot({}).handle_text("/help")
        self.assertEqual(r, pl.HELP_TEXT)
        # 2026-10-11 用户把正文开头那行自报名号的标题删了，直接进「1. 核心操作」
        self.assertNotIn("🧰", pl.HELP_TEXT)
        self.assertNotIn("羊毛机器人", pl.HELP_TEXT)
        # 那对星号是他本人的写法（「你理解错了，我要的就是*号加粗」）：微信收纯文本、
        # markdown 不生效，星号会原样显示 —— 别当成噪声抹掉，`app/sayings.py` 早就这么写。
        self.assertTrue(pl.HELP_TEXT.startswith("**1. 核心操作**"), pl.HELP_TEXT[:40])
        for head in ("**1. 核心操作**", "**2. 一言设置**", "**3. 快捷指令**"):
            self.assertIn(head, pl.HELP_TEXT)
        self.assertTrue(pl.HELP_TEXT.rstrip().endswith(
            "**标准句式或快捷指令 = 不调用AI；截图、大白话、非标句式 = 调用AI。**"
        ), pl.HELP_TEXT[-60:])

    def test_upcoming(self):
        self._seed()
        r = self.bot({}).handle_text("/即将")
        self.assertIn("接下来要抢的", r)

    def test_the_upcoming_command_sees_a_repeating_coupon(self):
        """`/即将` 跟参照节一样，以前只看库里那串 grab_at，重复型整个漏掉。

        真机上 `/即将` 里躺着「后天 12:00」「3天后 09:00」，却没有明天早上
        08:30 那三条港城券。
        """
        db.insert_activity({"title": "每日港城券", "platform": "中国银行",
                            "grab_at": iso(-1, "08:30"), "end_at": iso(60, "23:59"),
                            "repeat_kind": "daily", "repeat_at": "08:30"})
        db.insert_activity({"title": "后天的券", "platform": "云闪付", "grab_at": iso(2, "12:00")})
        fake_now = datetime.combine(tu.today(), time(11, 7))
        with mock.patch.object(pl.tu, "now", return_value=fake_now):
            r = self.bot({}).handle_text("/即将")
        self.assertIn("每日港城券", r)
        self.assertIn(f"明天 {pl.bold_time('08:30')}", r)
        self.assertIn("后天的券", r)

    def test_stats(self):
        self._seed()
        r = self.bot({}).handle_text("/统计")
        self.assertIn("共 3 条", r)

    def test_empty_message(self):
        r = self.bot({}).handle_text("   ")
        self.assertEqual(r, pl.HELP_TEXT)

    def test_llm_error_does_not_raise(self):
        class BoomLLM(FakeLLM):
            def understand(self, text):
                raise RuntimeError("network down")

        b = Bot(self.cfg, BoomLLM({}))
        # 用一句**不会**走固定句式快路的话，否则模型根本不会被调用，这条就白测了
        r = b.handle_text("帮我看看今天抢什么")
        self.assertIn("出错了", r)


class WebStatusTest(unittest.TestCase):
    """机器人状态与登录二维码那两个接口。

    机器人跑在 NAS 上没有屏幕，扫码只能靠日历页面把二维码递出来，
    所以这两条接口坏了就等于登不上，值得单独看着。
    """

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"web-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")

        from app import web as webmod

        self.webmod = webmod
        self.httpd = webmod.create_server(
            {"db_path": self.db_file, "web": {"host": "127.0.0.1", "port": 0}}
        )
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.webmod.set_status_provider(None)
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def get(self, path: str):
        with urllib.request.urlopen(self.url + path, timeout=10) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    def test_status_without_provider(self):
        self.webmod.set_status_provider(None)
        code, body, _ = self.get("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["data"], {"bot": "off"})

    def test_status_hides_path_and_flags_qr(self):
        qr = self.tmp / "login_qr.png"
        qr.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.webmod.set_status_provider(
            lambda: {"bot": "on", "logged_in": False, "qr_path": str(qr), "qr_token": 7}
        )
        data = json.loads(self.get("/api/status")[1])["data"]
        self.assertTrue(data["qr_ready"])
        self.assertEqual(data["qr_token"], 7)
        self.assertNotIn("qr_path", data, "文件路径不该发给浏览器")

    def test_provider_exception_does_not_break_status(self):
        def boom():
            raise RuntimeError("机器人对象还没准备好")

        self.webmod.set_status_provider(boom)
        code, body, _ = self.get("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["data"]["bot"], "error")

    def test_qr_image_is_served(self):
        qr = self.tmp / "login_qr.png"
        qr.write_bytes(b"\x89PNG\r\n\x1a\nfake-png")
        self.webmod.set_status_provider(lambda: {"bot": "on", "qr_path": str(qr)})
        code, body, ctype = self.get("/api/qr.png")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"\x89PNG\r\n\x1a\nfake-png")
        self.assertIn("image/png", ctype)

    def test_qr_missing_is_404_not_500(self):
        self.webmod.set_status_provider(
            lambda: {"bot": "on", "qr_path": str(self.tmp / "nope.png")}
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/qr.png")
        self.assertEqual(ctx.exception.code, 404)

    def test_qr_without_provider_is_404(self):
        self.webmod.set_status_provider(None)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/qr.png")
        self.assertEqual(ctx.exception.code, 404)

    def test_status_carries_the_polling_heartbeat(self):
        """「有 token」不等于「收得到消息」，页面必须能看出后者。

        token 失效后有 1 小时冷却，这段时间页面如果还写着「已连接」，
        用户会一直等 —— 这是最容易让人白等的一种错。
        """
        self.webmod.set_status_provider(lambda: {
            "bot": "on",
            "logged_in": True,
            "polling": False,
            "seconds_since_poll": 3600,
            "last_error": "登录已失效，正在等待重新登录",
        })
        data = json.loads(self.get("/api/status")[1])["data"]
        self.assertTrue(data["logged_in"])
        self.assertFalse(data["polling"])
        self.assertEqual(data["seconds_since_poll"], 3600)
        self.assertIn("失效", data["last_error"])

    def test_frontend_declares_the_login_panel(self):
        """前端三件套都要有扫码面板的接线，缺一处就是白屏。

        2026-10-11：这套控件从顶栏／页面上搬进了「设置 → 功能」页，所以这里加量
        「退出微信」那颗按钮和它的接口 —— 只换个容器不算，接线（`/api/wechat/logout`）
        跟着缺了，按钮就是点不动的装饰。
        """
        web_dir = Path(__file__).resolve().parent.parent / "web"
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        js = (web_dir / "app.js").read_text(encoding="utf-8")
        css = (web_dir / "style.css").read_text(encoding="utf-8")
        for el in ("loginPanel", "qrImg", "botPill", "btnRefreshQr", "btnWxLogout"):
            self.assertIn(f'id="{el}"', html, f"index.html 里缺 {el}")
        self.assertIn("/api/status", js)
        self.assertIn("/api/qr.png", js)
        self.assertIn("/api/wechat/logout", js)
        self.assertIn(".login-panel", css)
        # display:flex 会盖掉 UA 的 [hidden]{display:none}，必须显式关掉
        self.assertIn(".login-panel[hidden]", css)
        # 连接异常要有自己的一档样式，不能复用「已连接」的绿色
        self.assertIn("pill.warn", css)
        self.assertIn("连接异常", js)
        # 「退出微信」只清本机会话，撤销不了微信那侧的授权 —— 这句必须在确认框里
        self.assertIn("解除连接", js)


class WebAuthTest(unittest.TestCase):
    """设了 web.token 之后，哪些接口还需要能匿名访问。

    `/api/health` 必须免鉴权：Dockerfile 的 HEALTHCHECK 打的就是它，而且不带
    token —— 一旦它也开始 401，容器会永远是 unhealthy（在飞牛 NAS 上真踩到过）。
    """

    TOKEN = "s3cret"

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"auth-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")

        from app import web as webmod

        self.httpd = webmod.create_server(
            {
                "db_path": self.db_file,
                "web": {"host": "127.0.0.1", "port": 0, "token": self.TOKEN},
            }
        )
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.url + path, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_health_needs_no_token(self):
        code, body = self.get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_other_endpoints_still_need_the_token(self):
        self.assertEqual(self.get("/api/stats")[0], 401)
        self.assertEqual(self.get("/api/stats?token=" + self.TOKEN)[0], 200)

    def test_healthcheck_command_in_dockerfile_hits_health(self):
        """Dockerfile 里的探针路径要和接口对得上，否则永远 unhealthy。"""
        dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
            encoding="utf-8"
        )
        self.assertIn("/api/health", dockerfile)
        self.assertNotIn("token=", dockerfile)


class FallbackDayTest(unittest.TestCase):
    """日期兜底解析。

    背景：模型是「翻译器」，但它算日期会算错，而算错是**静默**的 ——
    用户问「周三抢什么」，模型若漏了 date，老代码会默默退回今天，
    用户拿到一份今天清单还以为那是周三的。这些用例锁住兜底逻辑。
    """

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"fb-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        db.init(str(self.tmp / "t.db"))
        tu.configure("Asia/Shanghai")

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # 固定一个已知的周三当参照，避免「今天正好是周几」影响断言
    WED = date(2026, 10, 7)   # 周三
    MON = date(2026, 10, 5)   # 那一周的周一

    def test_explicit_words(self):
        from app.pipeline import _fallback_day as fb

        self.assertEqual(fb("今天抢什么", self.WED), self.WED)
        self.assertEqual(fb("明天抢什么", self.WED), self.WED + timedelta(days=1))
        self.assertEqual(fb("后天有什么", self.WED), self.WED + timedelta(days=2))
        self.assertEqual(fb("大后天呢", self.WED), self.WED + timedelta(days=3))
        self.assertEqual(fb("昨天有什么", self.WED), self.WED - timedelta(days=1))
        self.assertEqual(fb("前天抢什么", self.WED), self.WED - timedelta(days=2))
        self.assertEqual(fb("今日活动", self.WED), self.WED)

    def test_weekday_without_prefix_means_the_upcoming_one(self):
        from app.pipeline import _fallback_day as fb

        # 周三问「周二」→ 下一个周二，而不是已经过去的那一个
        self.assertEqual(fb("周二抢什么", self.WED), self.WED + timedelta(days=6))
        # 周三问「周三」→ 就是今天
        self.assertEqual(fb("周三抢什么", self.WED), self.WED)
        # 周三问「周五」→ 本周末
        self.assertEqual(fb("周五有什么", self.WED), self.WED + timedelta(days=2))

    def test_weekday_with_prefix(self):
        from app.pipeline import _fallback_day as fb

        self.assertEqual(fb("下周三抢什么", self.WED), self.WED + timedelta(days=7))
        self.assertEqual(fb("上周三有什么", self.WED), self.WED - timedelta(days=7))
        # 「本周三」就是本周那个周三，哪怕已经过去
        self.assertEqual(fb("本周一有什么", self.WED), self.MON)
        self.assertEqual(fb("这周三呢", self.WED), self.WED)

    def test_weekday_spellings(self):
        from app.pipeline import _fallback_day as fb

        self.assertEqual(fb("星期二抢什么", self.WED), self.WED + timedelta(days=6))
        self.assertEqual(fb("礼拜天有什么", self.WED), self.WED + timedelta(days=4))
        self.assertEqual(fb("星期日呢", self.WED), self.WED + timedelta(days=4))

    def test_no_day_word_returns_none(self):
        from app.pipeline import _fallback_day as fb

        self.assertIsNone(fb("抢什么", self.WED))
        self.assertIsNone(fb("", self.WED))
        self.assertIsNone(fb("有什么活动", self.WED))

    def test_this_week_phrase_is_not_a_single_day(self):
        """「这周有哪些活动」是区间，不能被当成某一天。"""
        from app.pipeline import _fallback_day as fb

        self.assertIsNone(fb("这周有哪些活动", self.WED))
        self.assertIsNone(fb("下周有什么", self.WED))
        self.assertIsNone(fb("最近有什么", self.WED))

    def test_explicit_day_ignores_weekday_words(self):
        """只有今天/明天这类无歧义词才配去否决模型，周几不配。"""
        from app.pipeline import _explicit_day as ed

        self.assertEqual(ed("明天抢什么", self.WED), self.WED + timedelta(days=1))
        self.assertIsNone(ed("周三抢什么", self.WED))
        self.assertIsNone(ed("有什么活动", self.WED))

    # -- 接到流水线上 -------------------------------------------------

    CFG = {
        "timezone": "Asia/Shanghai",
        "query": {"show_ongoing": True},
        "web": {"host": "127.0.0.1", "port": 8080},
    }

    def _bot(self, script: dict) -> Bot:
        return Bot(dict(self.CFG), FakeLLM(script))

    def test_model_omits_date_and_python_recovers_it(self):
        """模型漏了日期，靠原话里的「后天」把正确的活动捞出来。"""
        target = tu.today() + timedelta(days=2)
        db.insert_activity({"title": "后天整点券", "grab_at": iso(2, "09:00")})
        db.insert_activity({"title": "今天整点券", "grab_at": iso(0, "09:00")})

        # 模型只说了 intent，date/date_from/date_to 全空
        script = {"后天抢什么": {"intent": "query"}}
        r = self._bot(script).handle_text("后天抢什么")
        self.assertIn("后天整点券", r)
        self.assertNotIn("今天整点券", r)
        self.assertIn(f"{target.month}月{target.day}日", r)

    def test_model_wrong_day_is_overridden_by_the_words(self):
        """模型把「明天」解成了今天 —— 以原话为准，不能让用户拿到错日子。"""
        db.insert_activity({"title": "明天整点券", "grab_at": iso(1, "09:00")})
        db.insert_activity({"title": "今天整点券", "grab_at": iso(0, "09:00")})

        script = {"明天抢什么": {"intent": "query", "date": tu.today().isoformat()}}
        r = self._bot(script).handle_text("明天抢什么")
        self.assertIn("明天整点券", r)
        self.assertNotIn("今天整点券", r)

    def test_an_explicit_range_is_never_clobbered(self):
        """「从明天到这周五」是区间，不能被「明天」压成单独一天。"""
        db.insert_activity({"title": "周五远处的券", "grab_at": iso(4, "10:00")})
        d1 = (tu.today() + timedelta(days=1)).isoformat()
        d2 = (tu.today() + timedelta(days=6)).isoformat()
        script = {
            "从明天到这周五有哪些活动": {
                "intent": "query",
                "date_from": d1,
                "date_to": d2,
            }
        }
        r = self._bot(script).handle_text("从明天到这周五有哪些活动")
        self.assertIn("周五远处的券", r)


class IngestDateTest(unittest.TestCase):
    """录入侧的两道修正：把算错的日子拨正、把倒挂的有效期推顺。

    这两处都是**静默错答**的入口，比查询侧更隐蔽：
      · 模型把「明天上午9点半」记成今天 09:30，只要现在还没到 09:30，
        `_classify_past` 就拦不住（它只拦「落在今天之前」），活动会静默躺到
        错日子上，到点也不提醒；
      · end_at 早于 grab_at 的活动一入库就被 `refresh_expired()` 判成「已过期」，
        日历上直接灰掉，用户以为白记了。
    """

    WED = date(2026, 10, 7)   # 周三，固定参照日

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"ing-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")
        self.cfg = dict(CFG)
        self.cfg["db_path"] = self.db_file

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def bot(self, script: dict) -> Bot:
        return Bot(self.cfg, FakeLLM(script))

    # -- _sole_day：只有毫无歧义才配推翻模型 --------------------------

    def test_sole_day_needs_one_unambiguous_day(self):
        from app.pipeline import _sole_day as sole

        self.assertEqual(sole("明天上午9点半京东", self.WED), self.WED + timedelta(days=1))
        self.assertEqual(sole("下周三亚马逊", self.WED), self.WED + timedelta(days=7))
        self.assertEqual(sole("周一秒杀", self.WED), self.MON_AFTER(self.WED))
        self.assertIsNone(sole("亚马逊百亿补贴", self.WED))
        self.assertIsNone(sole("", self.WED))

    def test_two_days_in_one_sentence_is_left_to_the_model(self):
        """「明天开抢，有效期到后天」里有两个日子，Python 猜不出哪个词管哪个字段。"""
        from app.pipeline import _sole_day as sole

        self.assertIsNone(sole("明天9点开抢，有效期到后天晚上12点", self.WED))
        self.assertIsNone(sole("今天领券明天用", self.WED))

    @staticmethod
    def MON_AFTER(ref: date) -> date:
        # 周三说「周一」= 下一个周一
        return ref + timedelta(days=(0 - ref.weekday()) % 7)

    # -- _align_day：拨正日子 ----------------------------------------

    def test_model_wrong_day_is_pulled_back_to_the_words(self):
        from app.pipeline import _align_day, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-07T09:30", "end_at": "2026-10-07T23:59", "title": "京东"},
            "text",
        )
        moved = _align_day(acts, "明天上午9点半京东", self.WED)
        self.assertEqual(len(moved), 1)
        self.assertEqual(acts[0]["grab_at"], "2026-10-08T09:30")
        # end_at 原本跟 grab_at 同一天，跟着一起搬
        self.assertEqual(acts[0]["end_at"], "2026-10-08T23:59")

    def test_end_on_another_day_is_not_dragged_along(self):
        from app.pipeline import _align_day, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-07T09:30", "end_at": "2026-10-20T23:59", "title": "京东"},
            "text",
        )
        _align_day(acts, "明天上午9点半京东", self.WED)
        self.assertEqual(acts[0]["grab_at"], "2026-10-08T09:30")
        self.assertEqual(acts[0]["end_at"], "2026-10-20T23:59")

    def test_a_past_date_is_left_for_the_clarify_flow(self):
        """模型记成过去 → 交给 `_classify_past` 去问，不许 Python 悄悄挪到下周。"""
        from app.pipeline import _align_day, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-06T09:30", "title": "京东"}, "text"
        )
        self.assertEqual(_align_day(acts, "明天上午9点半京东", self.WED), [])
        self.assertEqual(acts[0]["grab_at"], "2026-10-06T09:30")

    def test_ambiguous_text_never_moves_anything(self):
        from app.pipeline import _align_day, _collect_activities

        acts = _collect_activities({"grab_at": "2026-10-06T09:30", "title": "京东"}, "text")
        self.assertEqual(_align_day(acts, "明天开抢，后天结束", self.WED), [])
        self.assertEqual(acts[0]["grab_at"], "2026-10-06T09:30")

    # -- _repair_end：推顺倒挂的有效期 --------------------------------

    def test_end_before_grab_is_pushed_forward(self):
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-08T09:00", "end_at": "2026-10-07T23:59", "title": "京东"},
            "text",
        )
        self.assertEqual(len(_repair_end(acts)), 1)
        self.assertEqual(acts[0]["end_at"], "2026-10-08T23:59")

    def test_sane_end_is_untouched(self):
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-08T09:00", "end_at": "2026-10-08T23:59", "title": "京东"},
            "text",
        )
        self.assertEqual(_repair_end(acts), [])
        self.assertEqual(acts[0]["end_at"], "2026-10-08T23:59")

    def test_end_without_any_anchor_is_untouched(self):
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities({"end_at": "2026-10-07T23:59", "title": "京东"}, "text")
        self.assertEqual(_repair_end(acts), [])

    def test_end_at_midnight_of_the_same_day_counts_as_next_midnight(self):
        """「到晚上12点」= 当天 24:00。模型写成当天 00:00 时日子看着没错，时刻是倒挂的。"""
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-08T09:00", "end_at": "2026-10-08T00:00", "title": "京东"},
            "text",
        )
        self.assertEqual(len(_repair_end(acts)), 1)
        self.assertEqual(acts[0]["end_at"], "2026-10-09T00:00")

    def test_same_day_end_earlier_than_grab_is_pushed_one_day(self):
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-08T09:00", "end_at": "2026-10-08T08:00", "title": "京东"},
            "text",
        )
        self.assertEqual(len(_repair_end(acts)), 1)
        self.assertEqual(acts[0]["end_at"], "2026-10-09T08:00")

    def test_end_exactly_at_grab_is_left_alone(self):
        """闪购「9点开抢、9点结束」自洽，不去动它（只修真正倒挂的）。"""
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities(
            {"grab_at": "2026-10-08T09:00", "end_at": "2026-10-08T09:00", "title": "京东"},
            "text",
        )
        self.assertEqual(_repair_end(acts), [])

    def test_end_is_measured_against_start_when_there_is_no_grab(self):
        from app.pipeline import _repair_end, _collect_activities

        acts = _collect_activities(
            {"start_at": "2026-10-08T10:00", "end_at": "2026-10-08T09:00", "title": "京东"},
            "text",
        )
        self.assertEqual(len(_repair_end(acts)), 1)
        self.assertEqual(acts[0]["end_at"], "2026-10-09T09:00")

    # -- 走完整条流水线 ----------------------------------------------

    def test_pipeline_records_the_day_the_user_actually_said(self):
        """模型把「明天」记成今天 —— 不管测试在几点跑，都必须落到明天。"""
        text = "明天上午9点半京东超市抢满99减50"
        script = {
            text: {
                "intent": "ingest",
                "title": "京东超市满99减50",
                "platform": "京东",
                "grab_at": iso(0, "09:30"),      # 模型算错：记成今天
                "end_at": iso(0, "23:59"),
                "needs_clarify": False,
            }
        }
        reply = self.bot(script).handle_text(text)
        self.assertIn("已记下 1 条", reply)
        rows = db.list_range(tu.today() - timedelta(days=1), tu.today() + timedelta(days=3))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["grab_at"], iso(1, "09:30"))
        self.assertEqual(rows[0]["end_at"], iso(1, "23:59"))
        # 时区/日期对不上时不要静默过期
        self.assertEqual(rows[0]["status"], "pending")

    def test_pipeline_never_stores_an_end_before_the_grab(self):
        text = "明天上午9点京东抢券"
        script = {
            text: {
                "intent": "ingest",
                "title": "京东抢券",
                "grab_at": iso(1, "09:00"),
                "end_at": iso(0, "23:59"),   # 模型把有效期算到了前一天
                "needs_clarify": False,
            }
        }
        self.bot(script).handle_text(text)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=3))
        self.assertEqual(len(rows), 1)
        grab = tu.parse_minute(rows[0]["grab_at"])
        end = tu.parse_minute(rows[0]["end_at"])
        self.assertGreaterEqual(end, grab)
        self.assertEqual(rows[0]["end_at"], iso(1, "23:59"))
        self.assertEqual(rows[0]["status"], "pending")

    def test_a_past_day_still_asks_instead_of_recording(self):
        text = "明天上午9点半京东超市抢券"
        script = {
            text: {
                "intent": "ingest",
                "title": "京东超市抢券",
                "grab_at": iso(-1, "09:30"),   # 模型算到了昨天
                "needs_clarify": False,
            }
        }
        reply = self.bot(script).handle_text(text)
        self.assertIn("已经过去了", reply)
        self.assertEqual(db.stats()["total"], 0)  # 一条都没入库


class RepeatRuleTest(unittest.TestCase):
    """重复规则解析（`app/repeat.py`）。

    用户库里那两条真券的备注原文就是这里的用例原文：「每日 09:00:00 起发放」
    「领券时间：每天 08:30:00-22:00:00」。它们被记成了**一次性**活动，于是
    「后天抢什么」永远答不出「这张券明天还能不能领」。
    """

    def setUp(self) -> None:
        tu.configure("Asia/Shanghai")

    def test_the_two_real_notes_parse(self):
        from app import repeat as rp

        r = rp.parse_repeat("每日 09:00:00 起发放，数量有限先到先得，领完即止")
        self.assertEqual(r.kind, rp.KIND_DAILY)
        self.assertEqual(r.at, "09:00")
        self.assertIsNone(r.until)

        r = rp.parse_repeat("领券时间：每天 08:30:00-22:00:00，单用户单月限领1张")
        self.assertEqual(r.kind, rp.KIND_DAILY)
        self.assertEqual(r.at, "08:30")
        self.assertEqual(r.until, "22:00")

    def test_zero_padded_seconds_do_not_shift_the_minute(self):
        """「09:00:00」的秒不能被当成分钟读成 00:00。"""
        from app import repeat as rp

        self.assertEqual(rp.parse_repeat("每日 09:00:00 起发放").at, "09:00")
        self.assertEqual(rp.parse_repeat("每晚 20:30:00 开抢").at, "20:30")

    def test_weekly_names_a_weekday(self):
        from app import repeat as rp

        r = rp.parse_repeat("每周五中午 12:00 开始发放，次周四晚 23:59:59 截止")
        self.assertEqual(r.kind, rp.KIND_WEEKLY)
        self.assertEqual(r.weekday, 4)          # 周一=0
        self.assertEqual(r.at, "12:00")

        self.assertEqual(rp.parse_repeat("每周日 10点").weekday, 6)
        self.assertEqual(rp.parse_repeat("每个礼拜天 10点").weekday, 6)
        self.assertEqual(rp.parse_repeat("每星期三 9:30").weekday, 2)

    def test_a_one_day_window_is_not_a_repeat(self):
        """「10月7日 08:30-22:00」只是某一天的时段，不是每天。"""
        from app import repeat as rp

        self.assertIsNone(rp.parse_repeat("10月7日 08:30-22:00"))
        self.assertIsNone(rp.parse_repeat("京东超市满99减50，限会员"))
        self.assertIsNone(rp.parse_repeat(""))
        self.assertIsNone(rp.parse_repeat("2026-10-07T09:00"))

    def test_a_measure_word_after_the_number_is_not_a_weekday(self):
        """真事：用户库 #24 的备注里「每人每周三**档**力度…」排在「每周五中午12:00」前面。

        取第一个「每周三」会把周五的券认成周三 —— 这正是第一次回填时踩到的，
        靠 `--all` 重判才救回来。
        """
        from app import repeat as rp

        note = ("每人每周三档力度乐园门票消费券合计限领其中1张；发放期为2026年9月25日至"
                "2026年11月13日，每周五中午12:00开始发放，次周四晚23:59:59截止")
        r = rp.parse_repeat(note)
        self.assertEqual(r.kind, rp.KIND_WEEKLY)
        self.assertEqual(r.weekday, 4)          # 周五，不是周三
        self.assertEqual(r.at, "12:00")

    def test_a_repeated_prefix_in_the_list_is_still_one_rule(self):
        """「每周六、周日」＝两天，不是只有周六（真事：#80 建行 116 券）。

        原文是「每周六、周日09:00:00至23:59:59」，列表里每一项各自带了一遍「周」，
        老写法只认到「周六」，周日那一场就永远不显示 —— 用户看到的是「clawbot 说
        已记下，日历上却没有」。
        """
        from app import repeat as rp

        r = rp.parse_repeat("领券时间：2026年8月22日至2026年12月31日，每周六、周日09:00:00至23:59:59")
        self.assertEqual(r.kind, rp.KIND_WEEKLY)
        self.assertEqual(r.days, (5, 6))        # 周六、周日
        self.assertEqual(r.at, "09:00")
        self.assertEqual(r.until, "23:59")

        self.assertEqual(rp.parse_repeat("每周六和星期日 09:00").days, (5, 6))
        self.assertEqual(rp.parse_repeat("每周二、星期四 10:00").days, (1, 3))
        self.assertEqual(rp.parse_repeat("每周二、四 10:00").days, (1, 3))   # 老写法不变
        self.assertEqual(rp.parse_repeat("每周一三五 09:00").days, (0, 2, 4))

    def test_a_frequency_is_not_a_weekday(self):
        """「每周一次」「每周三次」说的是频率，不是周一 / 周三。"""
        from app import repeat as rp

        self.assertIsNone(rp.parse_repeat("每周一次"))
        self.assertIsNone(rp.parse_repeat("每周三次"))
        self.assertEqual(rp.parse_repeat("每周三 12:00").weekday, 2)

    def test_fields_from_reads_all_four_sources(self):
        from app import repeat as rp

        anchor = datetime(2026, 10, 7, 9, 0)
        by_field = rp.fields_from("每天 09:00", "", "", anchor)
        by_note = rp.fields_from(None, "每日 09:00:00 起发放", "", anchor)
        by_text = rp.fields_from(None, None, "这个东西每天9点都能领", anchor)
        self.assertEqual(by_field, by_note)
        self.assertEqual(by_note, by_text)
        self.assertEqual(by_field["repeat_kind"], "daily")
        self.assertEqual(by_field["repeat_at"], "09:00")

        # 第四路兜底：截图 OCR 原文。截图那条路没有用户原话，模型有时只在原文里
        # 留下「每自然月10号10:00-20号23:59:59」——#74 那条工行券就是这么丢的。
        by_ocr = rp.fields_from(None, "限会员", "明天上午开抢", anchor, "领券时间为每自然月10号10:00-20号23:59:59")
        self.assertEqual(by_ocr["repeat_kind"], "monthly")
        self.assertEqual(by_ocr["repeat_month_days"], "10")
        self.assertEqual(by_ocr["repeat_at"], "10:00")

        # 配额**故意不吃 OCR 这条路**：排期认错看得见，配额认错是静默少领。
        lean = rp.fields_from(None, None, "", None, "每自然月10号 09:00 开抢，单用户单月限领1张")
        self.assertEqual(lean["repeat_kind"], "monthly")
        self.assertIsNone(lean["quota_month"])

    def test_a_natural_month_phrasing_is_still_monthly(self):
        """工行「月月刷」原文写的是「每自然月10号」——那就是「每月10号」。"""
        from app import repeat as rp

        r = rp.parse_repeat("领券时间为每自然月10号10:00-20号23:59:59，票券有效期10天")
        self.assertEqual(r.kind, "monthly")
        self.assertEqual(r.month_days, (10,))
        self.assertEqual(r.at, "10:00")     # 10 号 10:00，不是同一段里 20 号的 23:59
        self.assertIsNone(r.until)

        # 紧跟在号数后面的这些字讲的是周期结束 / 下一次，都不是开抢日
        self.assertIsNone(rp.parse_repeat("每自然月30号结束每期消费达标活动"))
        self.assertIsNone(rp.parse_repeat("次月10号(含)后可选择领取"))

    def test_the_time_is_looked_for_next_to_the_rule(self):
        """截图 OCR 第一行常是手机状态栏的钟点 —— 时刻要认排期词旁边的那个。"""
        from app import repeat as rp

        blob = "09:34 5G 70%\n工商银行信用卡 月月刷活动\n领券时间为每自然月10号10:00-20号23:59:59"
        self.assertEqual(rp.parse_repeat(blob).at, "10:00")

    def test_the_image_path_lets_the_ocr_text_supply_the_rule(self):
        """模型没写 repeat、也没写进备注时，存下来的 OCR 原文得能顶上（activity#74）。"""
        from app import pipeline as pl

        raw = {
            "title": "工商银行信用卡月月刷消费达标领券",
            "grab_at": "2026-10-10T10:00",
            "start_at": "2026-01-10T10:00",
            "end_at": "2026-12-31T23:59",
            "note": "当月消费达标20000元(含)以上，次月10号(含)后可选择领取",
            "raw_ocr": "09:34 5G 70%\n领券时间为每自然月10号10:00-20号23:59:59，每自然月30号结束每期活动",
        }
        got = pl._normalize_activity(raw, "image", "")
        self.assertEqual(got["repeat_kind"], "monthly")
        self.assertEqual(got["repeat_month_days"], "10")
        self.assertEqual(got["repeat_at"], "10:00")
        self.assertEqual(got["raw_text"], raw["raw_ocr"])   # 回溯纠错用，照存

        # 同一条数据，没有 OCR 原文时不该凭空长出规则
        self.assertIsNone(pl._normalize_activity({k: v for k, v in raw.items() if k != "raw_ocr"}, "image", "")["repeat_kind"])

    def test_no_time_falls_back_to_the_anchor(self):
        """只说了「每天」没给时刻 —— 用模型锚好的「下一次」那个时刻。"""
        from app import repeat as rp

        anchor = datetime(2026, 10, 7, 8, 30)
        f = rp.fields_from(None, "每天都能领", "", anchor)
        self.assertEqual(f["repeat_at"], "08:30")

        # 连锚都没有（grab_at 为空）→ 退化成一次性，不能瞎编一个时刻
        self.assertIsNone(rp.fields_from(None, "每天都能领", "", None)["repeat_kind"])

    def test_nobody_mentioned_it_means_one_shot(self):
        from app import repeat as rp

        f = rp.fields_from("", "限会员", "京东明天9点抢券", datetime(2026, 10, 7, 9, 0))
        self.assertEqual(f, {k: None for k in rp.FIELDS})

    def test_describe_is_human_readable(self):
        from app import repeat as rp

        daily = {"repeat_kind": "daily", "repeat_at": "09:00"}
        self.assertEqual(rp.describe(daily), "🔁 每天 09:00")
        self.assertEqual(
            rp.describe({"repeat_kind": "daily", "repeat_at": "08:30", "repeat_until": "22:00"}),
            "🔁 每天 08:30–22:00",
        )
        self.assertEqual(
            rp.describe({"repeat_kind": "weekly", "repeat_at": "12:00", "repeat_weekday": 4}),
            "🔁 每周五 12:00",
        )
        self.assertEqual(rp.describe({"repeat_kind": None}), "")

    def test_occurrence_on_only_lands_on_its_own_days(self):
        from app import repeat as rp

        row = {
            "repeat_kind": "weekly",
            "repeat_at": "12:00",
            "repeat_weekday": 4,                 # 周五
            "grab_at": "2026-10-09T12:00",       # 周五
            "end_at": "2026-11-13T23:59",
        }
        self.assertEqual(rp.occurrence_on(row, date(2026, 10, 9)), datetime(2026, 10, 9, 12, 0))
        self.assertEqual(rp.occurrence_on(row, date(2026, 10, 16)), datetime(2026, 10, 16, 12, 0))
        self.assertIsNone(rp.occurrence_on(row, date(2026, 10, 10)))   # 周六
        self.assertIsNone(rp.occurrence_on(row, date(2026, 10, 2)))    # 系列还没开始

        ended = dict(row, end_at="2026-10-16T12:30")
        self.assertIsNotNone(rp.occurrence_on(ended, date(2026, 10, 16)))
        self.assertIsNone(rp.occurrence_on(ended, date(2026, 10, 23)))  # 系列已结束

    def test_the_series_starts_at_the_activity_start_not_the_next_grab(self):
        """系列起点取 `start_at` 与 `grab_at` 里**早的那个**（真事：#80）。

        模型把「每周六、周日」这条的 ``grab_at`` 填成了周日 10-11 —— 拿它当门槛，
        今天（周六 10-10）那一场就被「系列还没开始」吃掉，日历上什么都没有。
        """
        from app import repeat as rp

        row = {
            "repeat_kind": "weekly",
            "repeat_at": "09:00",
            "repeat_until": "23:59",
            "repeat_days": "5,6",                 # 周六、周日
            "grab_at": "2026-10-11T09:00",        # 周日：模型给的「下一次」
            "start_at": "2026-08-22T09:00",       # 活动真正开始的那天
            "end_at": "2026-12-31T23:59",
        }
        self.assertEqual(rp.occurrence_on(row, date(2026, 10, 10)), datetime(2026, 10, 10, 9, 0))
        self.assertEqual(rp.occurrence_on(row, date(2026, 10, 11)), datetime(2026, 10, 11, 9, 0))
        self.assertIsNone(rp.occurrence_on(row, date(2026, 10, 12)))    # 周一
        self.assertIsNone(rp.occurrence_on(row, date(2026, 8, 15)))     # 活动开始前的周六

        # 只有 grab_at 的老行照旧：`grab_at` 就是它唯一知道的起点
        only_grab = {"repeat_kind": "daily", "repeat_at": "09:00", "grab_at": "2026-10-09T09:00"}
        self.assertIsNone(rp.occurrence_on(only_grab, date(2026, 10, 8)))
        self.assertIsNotNone(rp.occurrence_on(only_grab, date(2026, 10, 9)))

    def test_a_one_shot_row_has_no_occurrence(self):
        """一次性活动不归这儿管 —— 返回 None，调用方按 grab_at 判。"""
        from app import repeat as rp

        self.assertIsNone(rp.occurrence_on({"grab_at": "2026-10-07T09:00"}, date(2026, 10, 7)))

    def test_occurrences_between_lists_every_instance(self):
        from app import repeat as rp

        row = {"repeat_kind": "daily", "repeat_at": "09:00", "grab_at": "2026-10-07T09:00"}
        got = rp.occurrences_between(row, date(2026, 10, 7), date(2026, 10, 9))
        self.assertEqual([o["day"] for o in got], ["2026-10-07", "2026-10-08", "2026-10-09"])
        self.assertEqual({o["at"] for o in got}, {"09:00"})
        # 一天都不在范围内 → 空
        self.assertEqual(rp.occurrences_between(row, date(2026, 10, 1), date(2026, 10, 6)), [])


class RepeatQueryTest(unittest.TestCase):
    """重复活动在「按天查询」里的表现（机器人 + 日历必须一致）。"""

    WED = date(2026, 10, 7)

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"rep-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")
        self.cfg = dict(CFG)
        self.cfg["db_path"] = self.db_file

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def bot(self, script: dict) -> Bot:
        return Bot(self.cfg, FakeLLM(script))

    def _daily(self, title: str = "每日签到券", at: str = "09:00", start_offset: int = 1):
        """一张「每天 09:00」的券，第一次在 start_offset 天后。"""
        db.insert_activity({
            "title": title,
            "grab_at": iso(start_offset, at),
            "end_at": iso(60, "23:59"),
            "repeat_kind": "daily",
            "repeat_at": at,
        })

    def _weekly(self, title: str = "每周五门票券", weekday: int = 4, at: str = "12:00"):
        """一张「每周五 12:00」的券，锚在下一次周五。"""
        delta = (weekday - tu.today().weekday()) % 7 or 7
        anchor = tu.today() + timedelta(days=delta)
        db.insert_activity({
            "title": title,
            "grab_at": f"{anchor.isoformat()}T{at}",
            "end_at": iso(60, "23:59"),
            "repeat_kind": "weekly",
            "repeat_at": at,
            "repeat_weekday": weekday,
        })
        return anchor

    # -- 机器人 ---------------------------------------------------------

    def test_a_daily_coupon_opens_every_day(self):
        """明天的券，问后天也该说「有」—— 这正是用户报的那个 bug。"""
        self._daily()
        for offset in (1, 2, 3, 9):
            day = tu.today() + timedelta(days=offset)
            script = {f"问{offset}": {"intent": "query", "date": day.isoformat()}}
            r = self.bot(script).handle_text(f"问{offset}")
            self.assertIn("每日签到券", r, f"{offset} 天后应该还在开抢清单里")
            self.assertIn("条开抢", r)

    def test_the_row_shows_that_days_time(self):
        """清单一条 = 那一天的时刻（单独一行、加粗）+ 标题。

        用户 2026-10-07 定的规矩：微信里只发摘要，平台 / 重复规则 / 有效期 / 备注 /
        链接都在日历页面上看 —— 所以这里刻意不断言 🔁，那是**移走**的东西。
        """
        self._daily(at="09:00")
        day = tu.today() + timedelta(days=2)
        script = {"后天抢什么": {"intent": "query", "date": day.isoformat()}}
        r = self.bot(script).handle_text("后天抢什么")
        self.assertIn(f"   {pl.bold_time('09:00')}\n   每日签到券", r)   # 那天的时刻，不是别天的
        self.assertNotIn("🔁", r)

    def test_the_same_time_is_written_once(self):
        """同一时刻的三条只写一遍时刻 —— 用户 2026-10-07 给的三张 08:30 那张截图。"""
        for i in (1, 2, 3):
            db.insert_activity({"title": f"港城券{i}", "grab_at": iso(1, "08:30")})
        db.insert_activity({"title": "午间券", "grab_at": iso(1, "12:00")})
        r = self.bot({}).handle_text("明天抢什么")
        self.assertEqual(r.count(pl.bold_time("08:30")), 1)
        self.assertEqual(r.count(pl.bold_time("12:00")), 1)
        self.assertNotIn("08:30  ", r)          # 旧的「时刻 + 两个空格 + 标题」不再出现
        self.assertEqual(r.count("港城券"), 3)
        # 时刻在上、标题在下：08:30 和它的第一条之间只有那一个换行
        self.assertIn(f"   {pl.bold_time('08:30')}\n   港城券1", r)
        self.assertIn(f"   {pl.bold_time('12:00')}\n   午间券", r)
        # 同一个时刻里保持库里的先后（稳定排序），不按标题重排：标题排序看着像随机
        self.assertLess(r.index("港城券1"), r.index("港城券2"))
        self.assertLess(r.index("港城券2"), r.index("港城券3"))

    def test_a_time_inside_a_label_is_the_only_part_that_gets_bold(self):
        """`bold_time` 只动结尾的 `HH:MM`，别的字一个不碰。"""
        self.assertEqual(pl.bold_time("08:30"), "𝟎𝟖:𝟑𝟎")
        self.assertEqual(pl.bold_time("3天后（周六）09:00"), "3天后（周六）𝟎𝟗:𝟎𝟎")
        self.assertEqual(pl.bold_time("时间未定"), "时间未定")
        self.assertEqual(pl.bold_time("??:??"), "??:??")
        # 真·粗体数字，不是 markdown 星号（微信只收纯文本，星号会原样显示）
        self.assertNotIn("*", pl.bold_time("08:30"))
        self.assertNotIn("08:30", pl.bold_time("08:30"))

    def test_the_upcoming_list_groups_the_same_time_too(self):
        """「/即将」同一个时刻也只写一遍，且时刻加粗。"""
        for i in (1, 2):
            db.insert_activity({"title": f"周六券{i}", "grab_at": iso(3, "10:00")})
        r = self.bot({}).handle_text("/即将")
        self.assertEqual(r.count(pl.bold_time("10:00")), 1)
        self.assertIn("周六券1", r)
        self.assertIn("周六券2", r)

    def test_a_weekly_coupon_only_shows_on_its_weekday(self):
        anchor = self._weekly()
        script = {"那天": {"intent": "query", "date": anchor.isoformat()}}
        r = self.bot(script).handle_text("那天")
        self.assertIn("每周五门票券", r)

        other = anchor + timedelta(days=1)     # 周六
        script = {"那天": {"intent": "query", "date": other.isoformat()}}
        r = self.bot(script).handle_text("那天")
        # 只断言「那天的清单」里没有它；末尾「🔎 接下来要抢的」是另一回事（有帮助的）
        self.assertNotIn("每周五门票券", r.split("🔎")[0])

    def test_a_repeat_is_never_listed_as_merely_claimable(self):
        """「每周五」的券不该在周六被列进「还能领的」。"""
        anchor = self._weekly()
        sat = anchor + timedelta(days=1)
        script = {"那天": {"intent": "query", "date": sat.isoformat()}}
        r = self.bot(script).handle_text("那天")
        self.assertNotIn("还能领", r)

    def test_a_range_repeats_the_daily_coupon_every_day(self):
        """区间查询里，「每天」的券每一天都该出现一次（不是七倍重复，是七场）。"""
        self._daily()
        d_from = tu.today() + timedelta(days=1)
        d_to = tu.today() + timedelta(days=3)
        script = {"这三天": {
            "intent": "query", "date_from": d_from.isoformat(), "date_to": d_to.isoformat(),
        }}
        r = self.bot(script).handle_text("这三天")
        self.assertEqual(r.count("每日签到券"), 3)
        for offset in (1, 2, 3):
            self.assertIn(tu.human_day(tu.today() + timedelta(days=offset)), r)

    # -- 录入侧 ---------------------------------------------------------

    def test_ingest_keeps_the_rule_from_the_models_field(self):
        text = "京东超市满99减50，每天9点都能领"
        script = {text: {
            "intent": "ingest",
            "title": "京东超市满99减50",
            "grab_at": iso(1, "09:00"),
            "repeat": "每天 09:00",
            "needs_clarify": False,
        }}
        self.bot(script).handle_text(text)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=3))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeat_kind"], "daily")
        self.assertEqual(rows[0]["repeat_at"], "09:00")
        # 原话要存下来，否则事后没法知道这条规则是从哪儿来的
        self.assertEqual(rows[0]["raw_text"], text)

    def test_ingest_recognizes_the_rule_from_the_note_when_the_model_forgets(self):
        """模型漏填 repeat，但 note 里抄着「每日 09:00:00 起发放」—— 也得认出来。"""
        text = "云闪付临沂交行券"
        script = {text: {
            "intent": "ingest",
            "title": "云闪付临沂交行券",
            "grab_at": iso(1, "09:00"),
            "note": "每日 09:00:00 起发放；单用户单周限领1张；领券时间：每天 08:30:00-22:00:00",
            "needs_clarify": False,
        }}
        self.bot(script).handle_text(text)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=3))
        self.assertEqual(rows[0]["repeat_kind"], "daily")
        self.assertEqual(rows[0]["repeat_at"], "08:30")
        self.assertEqual(rows[0]["repeat_until"], "22:00")

    def test_a_one_shot_ingest_is_left_exactly_as_before(self):
        text = "明天上午9点半京东抢券"
        script = {text: {
            "intent": "ingest",
            "title": "京东抢券",
            "grab_at": iso(1, "09:30"),
            "needs_clarify": False,
        }}
        self.bot(script).handle_text(text)
        row = db.list_range(tu.today(), tu.today() + timedelta(days=3))[0]
        from app import repeat as rp

        for key in rp.FIELDS:
            self.assertIsNone(row[key], f"{key} 该是空的")
        # 一次性活动不该被当成重复活动
        self.assertEqual(rp.describe(row), "")

    def test_the_confirm_step_does_not_lose_the_rule(self):
        """模型在确认环节只被问「对不对」，很可能不回声 repeat —— 规则不能丢。"""
        text = "每天9点京东领券"
        script = {
            text: {
                "intent": "ingest",
                "title": "京东领券",
                "grab_at": None,
                "repeat": "每天 09:00",
                "needs_clarify": True,
                "clarify_question": "几点开抢？",
            },
            "__confirm__:对": {
                "confirmed": True,
                "activities": [{"title": "京东领券", "grab_at": iso(1, "09:00")}],
            },
        }
        b = self.bot(script)
        b.handle_text(text, user_id="u1")
        r = b.handle_text("对", user_id="u1")
        self.assertIn("已记下 1 条", r)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=3))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeat_kind"], "daily")
        self.assertEqual(rows[0]["repeat_at"], "09:00")

    # -- 老数据回填 -----------------------------------------------------

    def test_backfill_fills_the_two_real_coupons(self):
        """`main.py backfill-repeat` 必须能认出用户库里那两条券。"""
        db.insert_activity({
            "title": "云闪付临沂金融助力交通银行满100元减10元券",
            "grab_at": iso(1, "09:00"),
            "start_at": iso(0, "09:00"),
            "end_at": iso(60, "23:59"),
            "note": "每日 09:00:00 起发放，数量有限先到先得（今日已抢完，下一场为明日09:00）",
        })
        db.insert_activity({
            "title": "港城欢乐购·中国银行信用卡满1000元减50元券",
            "grab_at": iso(1, "08:30"),
            "start_at": iso(0, "08:30"),
            "end_at": iso(60, "23:59"),
            "note": "领券时间：每天 08:30:00-22:00:00，单用户单月限领1张",
        })
        db.insert_activity({"title": "一次性秒杀", "grab_at": iso(2, "10:00")})

        from main import cmd_backfill_repeat

        args = argparse.Namespace(dry_run=False)
        self.assertEqual(cmd_backfill_repeat(None, args), 0)

        rows = {r["title"]: r for r in db.list_range(tu.today(), tu.today() + timedelta(days=9))}
        self.assertEqual(rows["云闪付临沂金融助力交通银行满100元减10元券"]["repeat_kind"], "daily")
        self.assertEqual(rows["港城欢乐购·中国银行信用卡满1000元减50元券"]["repeat_at"], "08:30")
        self.assertEqual(rows["港城欢乐购·中国银行信用卡满1000元减50元券"]["repeat_until"], "22:00")
        self.assertIsNone(rows["一次性秒杀"]["repeat_kind"])

    def test_backfill_dry_run_does_not_write(self):
        db.insert_activity({"title": "每日券", "grab_at": iso(1, "09:00"), "note": "每天 09:00 可领"})
        from main import cmd_backfill_repeat

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=True)), 0)
        self.assertIsNone(db.rows_without_repeat()[0]["repeat_kind"])

    def test_backfill_all_rejudges_a_row_that_already_has_a_rule(self):
        """解析器修好之后，老库里的错规则得能重判（`--all`），否则用户只能一条条手改。"""
        aid = db.insert_activity({
            "title": "被认错的券",
            "grab_at": iso(3, "12:00"),
            "note": "每人每周三档力度乐园门票消费券；每周五中午12:00开始发放",
            "repeat_kind": "weekly",
            "repeat_at": "12:00",
            "repeat_weekday": 2,            # 错的：周三
        })
        from main import cmd_backfill_repeat

        # 不带 --all 时它「已经有规则了」，不该被碰
        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False)), 0)
        self.assertEqual(db.get(aid)["repeat_weekday"], 2)

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False, all=True)), 0)
        self.assertEqual(db.get(aid)["repeat_weekday"], 4)

    def test_backfill_all_never_clears_a_rule_it_cannot_reproduce(self):
        """`--all` 只写「这次认得出规则」的行 —— 用户在日历上手工填的规则不能被清掉。"""
        aid = db.insert_activity({
            "title": "用户手工填的规则",
            "grab_at": iso(3, "20:00"),
            "repeat_kind": "daily",
            "repeat_at": "20:00",
        })
        from main import cmd_backfill_repeat

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False, all=True)), 0)
        row = db.get(aid)
        self.assertEqual(row["repeat_kind"], "daily")
        self.assertEqual(row["repeat_at"], "20:00")

    def test_backfill_repairs_a_row_whose_rule_only_the_ocr_text_has(self):
        """#74 工行券：备注里没有「每月」，规则只留在截图 OCR 原文里 —— 回填得补上。"""
        db.insert_activity({
            "title": "工商银行信用卡月月刷消费达标领券",
            "grab_at": iso(1, "10:00"),
            "start_at": iso(0, "10:00"),
            "end_at": iso(60, "23:59"),
            "note": "当月消费达标20000元(含)以上，次月10号(含)后可选择领取",
            "raw_text": "09:34 5G 70%\n领券时间为每自然月10号10:00-20号23:59:59，每自然月30号结束每期活动",
        })
        from main import cmd_backfill_repeat

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False)), 0)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=60))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeat_kind"], "monthly")
        self.assertEqual(rows[0]["repeat_month_days"], "10")
        self.assertEqual(rows[0]["repeat_at"], "10:00")

    def test_the_db_gains_the_columns_on_an_old_file(self):
        """老库（没有那几列）要能原地补列 —— 用户库里已经有真数据，不能重建表。"""
        from app import repeat as rp

        old = self.tmp / "old.db"
        # 先按当前 schema 建好，再把那几列删掉 —— 这就是用户库升级前的样子
        db.init(str(old))
        db.insert_activity({"title": "老活动", "grab_at": iso(1, "09:00")})
        with db.connect() as conn:
            for key in rp.FIELDS:
                conn.execute(f"ALTER TABLE activity DROP COLUMN {key}")

        db.init(str(old))          # 再 init 一次：应该把列补回来
        with db.connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(activity)")}
        for key in rp.FIELDS:
            self.assertIn(key, cols)
        # 数据还在（没被 DROP 掉整张表重建）
        self.assertEqual(db.stats()["total"], 1)
        rows = db.list_range(tu.today(), tu.today() + timedelta(days=3))
        self.assertEqual(rows[0]["title"], "老活动")
        self.assertIsNone(rows[0]["repeat_kind"])


class QuotaTest(unittest.TestCase):
    """「每周几次、每月几次」—— 排期（哪几天）和配额（能领几次）是两件事。

    这一组用例钉三件事：这类备注认得出、进度按**自然周/自然月**算、以及排期和
    配额**互不干扰**（给老行补配额不能顺手把用户手工填的排期重判掉）。
    """

    MON = date(2026, 10, 5)        # 周一 —— 这一周的起头
    TUE = date(2026, 10, 6)
    WED = date(2026, 10, 7)
    THU = date(2026, 10, 8)
    NEXT_TUE = date(2026, 10, 13)
    OCT15 = date(2026, 10, 15)
    NOV01 = date(2026, 11, 1)

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"quota-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _weekly(self, **kw) -> int:
        data = {
            "title": "每周二、四的券",
            "grab_at": "2026-10-06T10:00",
            "end_at": "2026-12-31T23:59",
            "repeat_kind": "weekly",
            "repeat_at": "10:00",
            "repeat_days": "1,3",
        }
        data.update(kw)
        return db.insert_activity(data)

    # -- 排期 ----------------------------------------------------------

    def test_a_weekly_rule_can_land_on_several_weekdays(self):
        """「每周二、四」是一条规则，不是两条 —— 老的四列装不下，所以加了 repeat_days。"""
        from app import repeat as rp

        row = {
            "grab_at": "2026-10-06T10:00",
            "end_at": "2026-12-31T23:59",
            "repeat_kind": "weekly",
            "repeat_at": "10:00",
            "repeat_days": "1,3",
        }
        occ = rp.occurrences_between(row, self.MON, self.THU)
        self.assertEqual([o["day"] for o in occ], ["2026-10-06", "2026-10-08"])
        self.assertEqual([o["at"] for o in occ], ["10:00", "10:00"])
        self.assertEqual(rp.describe(row), "🔁 每周二、四 10:00")

    def test_a_monthly_rule_lands_on_the_named_days(self):
        from app import repeat as rp

        row = {
            "grab_at": "2026-10-01T09:00",
            "end_at": "2026-12-31T23:59",
            "repeat_kind": "monthly",
            "repeat_at": "09:00",
            "repeat_month_days": "1,15",
        }
        self.assertIsNotNone(rp.occurrence_on(row, date(2026, 10, 1)))
        self.assertIsNotNone(rp.occurrence_on(row, self.OCT15))
        self.assertIsNone(rp.occurrence_on(row, date(2026, 10, 2)))
        self.assertIsNotNone(rp.occurrence_on(row, self.NOV01))   # 下个月同一天还在
        self.assertEqual(rp.describe(row), "🔁 每月1日、15日 09:00")

    # -- 从真备注里认配额 -------------------------------------------------

    def test_a_real_note_yields_both_the_schedule_and_the_quota(self):
        """用户库里那两条真券的备注原文（一字不改），两样都得认出来。"""
        from app import repeat as rp

        got = rp.fields_from(
            "", "每日 09:00:00 起发放；单用户单周限领1张；领券时间：每天 08:30:00-22:00:00", "", None
        )
        self.assertEqual(got["repeat_kind"], "daily")
        self.assertEqual(got["repeat_at"], "08:30")
        self.assertEqual(got["repeat_until"], "22:00")
        self.assertEqual(got["quota_week"], 1)
        self.assertIsNone(got["quota_month"])

        got = rp.fields_from(
            "", "领券时间：每天 08:30:00-22:00:00，单用户单月限领1张", "", None
        )
        self.assertEqual(got["repeat_kind"], "daily")
        self.assertEqual(got["quota_month"], 1)
        self.assertIsNone(got["quota_week"])

    def test_a_wordy_quota_is_still_a_quota(self):
        """「限**选择领取**1张」也要认 —— #74 那条工行券的真备注就是这么写的。

        中间多一个词就整句认不出来，配额列于是留空；而这条活动又是重复型，
        「月限1次」空着的后果是领满之后那个月剩下的日子还标着「待抢」。
        """
        from app import repeat as rp

        self.assertEqual(rp.parse_quota("单用户单月限选择领取1张"), rp.Quota(None, 1))
        self.assertEqual(rp.parse_quota("单用户单月最多选择领取1张"), rp.Quota(None, 1))
        self.assertEqual(rp.parse_quota("每人每月限领取2次"), rp.Quota(None, 2))
        self.assertEqual(rp.parse_quota("单用户单月限选择领取若干张"), rp.Quota(None, None))

    def test_the_word_after_the_number_decides_quota_or_schedule(self):
        """「每周三次」是频率、不是周三；「每周三档」是力度档位、不是配额也不是周三。"""
        from app import repeat as rp

        self.assertIsNone(rp.parse_repeat("每周三次"))
        self.assertIsNone(rp.parse_repeat("每人每周三档力度乐园门票消费券"))
        self.assertEqual(rp.parse_quota("每人每周两次"), rp.Quota(week=2, month=None))
        self.assertEqual(rp.parse_quota("每人每周三档力度乐园门票消费券"), rp.Quota(None, None))

    # -- 按自然周 / 自然月算进度 -------------------------------------------

    def test_quota_is_counted_per_calendar_week_and_resets_next_week(self):
        from app import repeat as rp

        row = {
            "grab_at": "2026-10-06T10:00",
            "end_at": "2026-12-31T23:59",
            "repeat_kind": "weekly",
            "repeat_at": "10:00",
            "repeat_days": "1,3",
            "quota_week": 1,
        }
        state = rp.quota_state(row, self.WED, ["2026-10-06"])["week"]
        self.assertEqual((state["limit"], state["done"], state["left"]), (1, 1, 0))
        self.assertTrue(state["reached"])
        # 自然周从周一起 —— 否则「每周一、日各一次」会被算进两个不同的周
        self.assertEqual((state["from"], state["to"]), ("2026-10-05", "2026-10-11"))
        # 这周二领到了，本周四那场就不该再说「待抢」（点进去也领不到）
        self.assertEqual(rp.occurrence_status(row, self.THU, ["2026-10-06"]), "done")
        # 下周二自动恢复
        self.assertEqual(rp.occurrence_status(row, self.NEXT_TUE, ["2026-10-06"]), "pending")

    def test_quota_is_counted_per_calendar_month(self):
        from app import repeat as rp

        row = {
            "grab_at": "2026-10-01T09:00",
            "end_at": "2026-12-31T23:59",
            "repeat_kind": "daily",
            "repeat_at": "09:00",
            "quota_month": 2,
        }
        done = ["2026-10-01", "2026-10-02"]
        self.assertEqual(rp.occurrence_status(row, date(2026, 10, 3), done), "done")
        self.assertEqual(rp.occurrence_status(row, self.OCT15, done), "done")
        self.assertEqual(rp.occurrence_status(row, self.NOV01, done), "pending")
        state = rp.quota_state(row, self.OCT15, done)["month"]
        self.assertEqual((state["from"], state["to"]), ("2026-10-01", "2026-10-31"))

    def test_a_row_level_done_does_not_kill_a_whole_repeating_series(self):
        """一格 status 装不下几十场：用户以前误点过一次完成，不能整条都废掉。"""
        from app import repeat as rp

        row = {
            "grab_at": "2026-10-06T10:00",
            "repeat_kind": "daily",
            "repeat_at": "10:00",
            "status": "done",
        }
        self.assertEqual(rp.occurrence_status(row, self.WED, []), "pending")
        # 但整个系列过期了还是得认
        self.assertEqual(rp.occurrence_status(dict(row, status="expired"), self.WED, []), "expired")

    def test_the_edit_box_never_gets_the_quota_tacked_on(self):
        """编辑框回填的是「纯排期」那句 —— 带着「· 周限1次」回填，用户再存一次
        就会被当成规则文本重新解析，配额的写法迟早被改坏。"""
        from app import repeat as rp

        row = {
            "grab_at": "2026-10-06T10:00",
            "repeat_kind": "weekly",
            "repeat_at": "10:00",
            "repeat_days": "1,3",
            "quota_week": 1,
        }
        self.assertEqual(rp.rule_text(row), "🔁 每周二、四 10:00")
        self.assertEqual(rp.describe(row), "🔁 每周二、四 10:00 · 周限1次")

    # -- 完成台账 --------------------------------------------------------

    def test_marking_a_day_is_per_day_and_idempotent(self):
        aid = self._weekly()
        self.assertTrue(db.mark_done(aid, self.TUE))
        self.assertFalse(db.mark_done(aid, self.TUE), "同一天记两次不该变成两次")
        self.assertTrue(db.mark_done(aid, self.THU))
        self.assertEqual(db.done_days(aid), ["2026-10-06", "2026-10-08"])
        # 行级 status 没被动过 —— 重复活动根本不看它
        self.assertEqual(db.get(aid)["status"], "pending")
        self.assertEqual(db.done_map([aid]), {aid: ["2026-10-06", "2026-10-08"]})

    def test_a_done_day_can_be_taken_back(self):
        aid = self._weekly()
        db.mark_done(aid, self.TUE)
        self.assertTrue(db.unmark_done(aid, self.TUE))
        self.assertFalse(db.unmark_done(aid, self.TUE))
        self.assertEqual(db.done_days(aid), [])

    def test_a_date_with_a_time_counts_as_that_day(self):
        """前端传的是「哪一天」，但接口上流过来的可能是带时刻的 ISO 串。"""
        aid = self._weekly()
        db.mark_done(aid, "2026-10-06T10:00")
        self.assertEqual(db.done_days(aid), ["2026-10-06"])
        self.assertFalse(db.mark_done(aid, "不是日期"))

    def test_deleting_an_activity_takes_its_done_days_with_it(self):
        """孤儿台账会让「本周已经领了几次」虚高，删活动时得一起清。"""
        aid = self._weekly()
        db.mark_done(aid, self.TUE)
        db.delete_activity(aid)
        self.assertEqual(db.done_days(aid), [])
        self.assertEqual(db.done_map([aid]), {})
        self.assertEqual(db.clear_done(aid), 0)

    # -- 补老数据时排期和配额互不干扰 ----------------------------------------

    def test_backfill_adds_the_quota_without_rejudging_the_schedule(self):
        """不带 --all 就是「不动已有排期」—— 补配额时也不能顺手把它重判掉。

        这条行是照用户库那条真券造的：备注里同时有「每周三档」（量词）和
        「每周五中午12:00」（真的星期几），排期存的是用户手工填的周三。
        """
        aid = db.insert_activity({
            "title": "被认错的券",
            "grab_at": iso(3, "12:00"),
            "note": "每人每周三档力度乐园门票消费券；每周五中午12:00开始发放；单用户单周限领1张",
            "repeat_kind": "weekly",
            "repeat_at": "12:00",
            "repeat_weekday": 2,            # 用户手工填的：周三
        })
        from main import cmd_backfill_repeat

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False)), 0)
        row = db.get(aid)
        self.assertEqual(row["repeat_weekday"], 2, "排期不该被重判 —— 那要 --all 才动")
        self.assertEqual(row["quota_week"], 1, "配额该补上")
        self.assertEqual(row["repeat_at"], "12:00", "时刻也不该被改")

    def test_backfill_never_clears_a_quota_it_cannot_reproduce(self):
        """重读原文只是「补」：原文里没有配额的旧行，不能被补成空。"""
        aid = db.insert_activity({
            "title": "手工配额",
            "grab_at": iso(3, "12:00"),
            "note": "每周五中午12:00开始发放",
            "repeat_kind": "weekly",
            "repeat_at": "12:00",
            "repeat_weekday": 4,
            "quota_week": 3,
        })
        from main import cmd_backfill_repeat

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False)), 0)
        self.assertEqual(db.get(aid)["quota_week"], 3)

    def test_backfill_keeps_a_model_filled_quota_the_text_cannot_explain(self):
        """这条行**还没有排期**，回填要顺手把排期写进去 —— 但不能连配额一起照写。

        #74 那条工行券的真事：模型把「月限1次」填进了 `quota_month`，备注原文
        「单用户单月限选择领取1张」当时解析器认不出来；回填把 `fields_from` 的结果
        整组写库，就把那一格清成了空 —— 空掉是**静默**的，用户只会看到领满之后
        那个月剩下的日子还标着「待抢」。
        """
        aid = db.insert_activity({
            "title": "工行月月刷",
            "grab_at": iso(10, "10:00"),
            "raw_text": "领券时间为每自然月10号10:00-20号23:59:59，票券有效期10天",
            "note": "每位持卡人本期内仅可参与本项目一次，不叠加",
            "quota_month": 1,                 # 模型自己填的，原文里没有能认出来的写法
        })
        from main import cmd_backfill_repeat

        self.assertEqual(cmd_backfill_repeat(None, argparse.Namespace(dry_run=False)), 0)
        row = db.get(aid)
        self.assertEqual(row["repeat_kind"], "monthly", "原文里的排期该补上")
        self.assertEqual(row["repeat_month_days"], "10")
        self.assertEqual(row["repeat_at"], "10:00")
        self.assertEqual(row["quota_month"], 1, "认不出来的配额不许被清掉")


class DeleteByKeywordTest(unittest.TestCase):
    """「删除梦享」这条路：列出来 → 回编号 → 才真删。

    这一组用例的重心不在「删得掉」，而在**删之前什么都不该发生**：删除不可逆，
    所以宁可多问一句。下面每个用例都在钉这件事。
    """

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"del-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")
        self.cfg = dict(CFG)
        self.cfg["db_path"] = self.db_file

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def bot(self, script: dict) -> Bot:
        return Bot(self.cfg, FakeLLM(script))

    def seed(self) -> list[int]:
        """三条能靠关键词区分开的活动。"""
        ids = []
        for title, kw in (("云闪付 乐园门票梦享券 满1000减120", "梦享"),
                          ("云闪付临沂金融助力交通银行满100元减10元", "临沂"),
                          ("港城欢乐购·中国银行信用卡满1000元减50元券", "港城")):
            ids.append(db.insert_activity({
                "title": title,
                "platform": "云闪付" if kw != "港城" else "中国银行",
                "note": f"{kw} 相关",
                "grab_at": iso(1, "09:00"),
            }))
        return ids

    DELETE = {"intent": "delete", "keyword": "梦享"}

    def test_a_delete_never_happens_before_the_user_picks(self):
        ids = self.seed()
        b = self.bot({"删除梦享": self.DELETE})
        reply = b.handle_text("删除梦享", user_id="u1")
        self.assertIn("找到 1 条带「梦享」的活动", reply)
        # 每条都给编号和 id，用户才知道要回哪个数字
        self.assertIn("1. ", reply)
        self.assertIn(f"<#{ids[0]}>", reply)
        self.assertIn("回复编号就删", reply)
        self.assertEqual(db.stats()["total"], 3)  # 一条都没动

    def test_replying_with_the_number_deletes_exactly_that_one(self):
        ids = self.seed()
        b = self.bot({"删除梦享": self.DELETE})
        b.handle_text("删除梦享", user_id="u1")
        reply = b.handle_text("1", user_id="u1")
        self.assertIn("已删掉 1 条", reply)
        self.assertIn("梦享券", reply)
        self.assertIsNone(db.get(ids[0]))
        self.assertIsNotNone(db.get(ids[1]))
        self.assertIsNotNone(db.get(ids[2]))
        self.assertIn("还剩 2 条", reply)

    def test_cancel_deletes_nothing(self):
        ids = self.seed()
        b = self.bot({"删除临沂": {"intent": "delete", "keyword": "临沂"}})
        b.handle_text("删除临沂", user_id="u1")
        reply = b.handle_text("取消", user_id="u1")
        self.assertIn("一条都没动", reply)
        self.assertEqual(db.stats()["total"], 3)
        for aid in ids:
            self.assertIsNotNone(db.get(aid))

    def test_an_out_of_range_number_changes_nothing_and_asks_again(self):
        """回「9」不能变成「那就删第 1 条」——猜错的代价是不可逆的。"""
        ids = self.seed()
        b = self.bot({"删除临沂": {"intent": "delete", "keyword": "临沂"}})
        b.handle_text("删除临沂", user_id="u1")
        reply = b.handle_text("9", user_id="u1")
        self.assertIn("没听清", reply)
        self.assertEqual(db.stats()["total"], 3)
        # pending 要存回去：下一句「1」还得有人接
        reply2 = b.handle_text("1", user_id="u1")
        self.assertIn("已删掉 1 条", reply2)
        self.assertIsNone(db.get(ids[1]))

    def test_all_of_them_can_be_deleted_in_one_go(self):
        ids = self.seed()
        b = self.bot({"删除": {"intent": "delete", "keyword": ""}})
        reply = b.handle_text("删除", user_id="u1")
        self.assertIn("找到 3 条", reply)
        reply = b.handle_text("都删", user_id="u1")
        self.assertIn("已删掉 3 条", reply)
        self.assertEqual(db.stats()["total"], 0)
        for aid in ids:
            self.assertIsNone(db.get(aid))

    def test_two_numbers_delete_two(self):
        ids = self.seed()
        b = self.bot({"删除": {"intent": "delete", "keyword": ""}})
        b.handle_text("删除", user_id="u1")
        reply = b.handle_text("1 2", user_id="u1")
        self.assertIn("已删掉 2 条", reply)
        self.assertIsNone(db.get(ids[0]))
        self.assertIsNone(db.get(ids[1]))
        self.assertIsNotNone(db.get(ids[2]))

    def test_a_chinese_number_works_too(self):
        ids = self.seed()
        b = self.bot({"删除": {"intent": "delete", "keyword": ""}})
        b.handle_text("删除", user_id="u1")
        self.assertIn("已删掉 1 条", b.handle_text("第一条", user_id="u1"))
        self.assertIsNone(db.get(ids[0]))

    def test_a_keyword_that_matches_nothing_says_so(self):
        self.seed()
        b = self.bot({"删除不存在的东西": {"intent": "delete", "keyword": "不存在的东西"}})
        reply = b.handle_text("删除不存在的东西", user_id="u1")
        self.assertIn("没找到", reply)
        self.assertEqual(db.stats()["total"], 3)
        # 没找到东西就不该留下一个「等你回编号」的坑：否则用户接下来无论说什么，
        # 都会被当成在回答一个他根本没见过的清单。
        self.assertIsNone(db.pop_pending("u1"))
        self.assertNotIn("回复编号就删", reply)

    def test_the_model_saying_other_still_deletes(self):
        """模型没认出删除意图时靠动词兜底 —— 说了要删却毫无反应是最差的体验。"""
        self.seed()
        b = self.bot({"删除港城": {"intent": "other", "reply": "你是想说什么？"}})
        reply = b.handle_text("删除港城", user_id="u1")
        self.assertIn("找到 1 条带「港城」的活动", reply)
        self.assertEqual(db.stats()["total"], 3)  # 仍然只是列出来

    def test_a_normal_message_is_never_hijacked_by_a_stray_verb(self):
        """「明天9点京东抢券」里没有删除动词，不该被当成删除。"""
        self.assertEqual(pl._fallback_keyword("明天9点京东超市满99减50抢券"), "")
        self.assertEqual(pl._fallback_keyword("周二上午9点有什么"), "")
        self.assertEqual(pl._fallback_keyword("这周有哪些活动"), "")

    def test_an_ingest_with_a_stray_delete_verb_still_records(self):
        """模型认成 ingest 时不让兜底抢道 —— 那句话的重点是记下来。"""
        text = "明天9点京东抢券，把旧的那条删了"
        script = {text: {"intent": "ingest", "title": "京东抢券", "grab_at": iso(1, "09:00"),
                         "needs_clarify": False}}
        reply = self.bot(script).handle_text(text, user_id="u1")
        self.assertIn("已记下 1 条", reply)
        self.assertEqual(db.stats()["total"], 1)

    def test_without_a_user_id_it_deletes_nothing(self):
        """没有会话标识就没法存待确认 —— 这时候宁可什么都不做。"""
        self.seed()
        b = self.bot({"删除梦享": self.DELETE})
        reply = b.handle_text("删除梦享")
        self.assertIn("找到 1 条", reply)
        self.assertIn("没法往下走", reply)
        self.assertEqual(db.stats()["total"], 3)

    def test_help_mentions_how_to_delete(self):
        """用户 2026-10-11 又整段换了一版：删活动只剩「发『删+关键词』，如：删迪士尼
        （调用AI）。推荐进日历地址手动删除」—— 回编号／取消／都删那三句不再写给用户看，
        但机器人照旧认它们（那是 `test_delete_*` 那一片盯的事）。"""
        self.assertIn("删活动", pl.HELP_TEXT)
        self.assertIn("删+关键词", pl.HELP_TEXT)
        self.assertIn("手动删除", pl.HELP_TEXT)

    def test_the_prompt_tells_the_model_it_cannot_delete(self):
        """提示词里必须写死「模型只抽关键词」——它一旦以为自己在删，就会输出活动列表。"""
        from app.llm import UNDERSTAND_PROMPT
        self.assertIn('"delete"', UNDERSTAND_PROMPT)
        self.assertIn("没有删除能力", UNDERSTAND_PROMPT)
        self.assertIn('"keyword": "梦享"', UNDERSTAND_PROMPT)


class FixedQueryTest(unittest.TestCase):
    """用户 2026-10-07 定的规矩：查询用固定句式说，不调模型，也只回标题。

    「本周六抢什么」「明天8:30抢什么」这种句子里的日子和时刻全是确定性的，Python
    解得比模型准（模型在日期上错过，`_fallback_day` 就是为那个写的），还省一次往返。
    纪律是**只有整句话正好是那个形状**才走快路：多一个字就交回模型，否则
    「明天9点京东超市满99减50抢券」会被当成查询，用户看到「那天没有活动」而不是
    「已记下」。
    """

    CFG = dict(CFG)

    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"fixed-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        tu.configure("Asia/Shanghai")
        self.cfg = dict(self.CFG)
        self.cfg["db_path"] = self.db_file
        self.today = tu.today()
        st.reset_cache()

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def bot(self, script: dict) -> Bot:
        return Bot(self.cfg, FakeLLM(script))

    # -- 解析 ---------------------------------------------------------

    def test_the_shapes_the_user_named_parse(self):
        """用户点名的三个例子，加上等价说法。"""
        cases = {
            "今天抢什么": (self.today, None),
            "明天抢什么": (self.today + timedelta(days=1), None),
            "后天抢什么": (self.today + timedelta(days=2), None),
            "本周六抢什么": (pl._fallback_day("本周六", self.today), None),
            "明天8:30抢什么": (self.today + timedelta(days=1), "08:30"),
            "本周五8:30抢什么": (pl._fallback_day("本周五", self.today), "08:30"),
            "明天8点半抢什么": (self.today + timedelta(days=1), "08:30"),
            "明天晚上8点抢什么": (self.today + timedelta(days=1), "20:00"),
            "10月9号抢什么": (date(self.today.year, 10, 9), None),
        }
        for text, (day, clock) in cases.items():
            with self.subTest(text=text):
                got = pl.parse_fixed_query(text, self.today)
                self.assertIsNotNone(got, f"{text!r} 该走快路")
                assert got is not None
                self.assertEqual(got["intent"], "query")
                self.assertEqual(got["date"], day.isoformat())
                self.assertEqual(got.get("time"), clock)

    def test_a_chinese_numeral_clock_is_understood(self):
        """用户 2026-10-07 点名的「今天十五点抢什么」—— 中文数字也要认。

        他要问的是 15:00 那一刻开抢的，识别不出来就会掉进模型那条路，而模型
        在时刻上不保证准，快路的意义就没了。
        """
        cases = {
            "今天十五点抢什么": "15:00",
            "今天15:00抢什么": "15:00",
            "今天15点抢什么": "15:00",
            "十一点抢什么": "11:00",
            "九点二十抢什么": "09:20",
            "晚上八点抢什么": "20:00",
            "今天十点抢什么": "10:00",
            "今天二十三点抢什么": "23:00",
            "十二点抢什么": "12:00",
        }
        for text, clock in cases.items():
            with self.subTest(text=text):
                got = pl.parse_fixed_query(text, self.today)
                self.assertIsNotNone(got, f"{text!r} 该走快路")
                assert got is not None
                self.assertEqual(got["date"], self.today.isoformat())
                self.assertEqual(got.get("time"), clock)

    def test_the_chinese_number_helper(self):
        for text, want in (("十", 10), ("十五", 15), ("二十", 20), ("二十三", 23),
                           ("九", 9), ("三十", 30), ("零", 0), ("两", 2)):
            with self.subTest(text=text):
                self.assertEqual(pl._cn_int(text), want)
        for text in ("", "廿", "十十", "abc", "十百"):
            with self.subTest(text=text):
                self.assertIsNone(pl._cn_int(text))

    def test_anything_extra_goes_back_to_the_model(self):
        """多一个字就不是纯查询 —— 这是快路唯一的护栏，写死了才不会误吞录入。"""
        for text in (
            "明天9点京东超市满99减50抢券",   # 录入，不是查询
            "明天上午9点半淘宝秒杀开抢",
            "删除梦享",
            "帮我看看明天抢什么",             # 「帮我看看」没被认出来
            "后天有啥券",                     # 查询词不在表里
            "明天抢什么好呢",                 # 「好」不是语气词
        ):
            with self.subTest(text=text):
                self.assertIsNone(pl.parse_fixed_query(text, self.today))

    def test_a_bare_time_still_works_without_a_day_word(self):
        """「9点半抢什么」是老句式，日子缺省是今天。"""
        got = pl.parse_fixed_query("9点半抢什么", self.today)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["date"], self.today.isoformat())
        self.assertEqual(got["time"], "09:30")

    def test_an_absolute_date_months_back_rolls_to_next_year(self):
        """12 月说「1月5号」，指的是明年 —— 就近取，不是去年。"""
        got = pl.parse_fixed_query("1月5号抢什么", date(2026, 12, 20))
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["date"], "2027-01-05")

    # -- 不调模型 ------------------------------------------------------

    def test_the_model_is_never_asked(self):
        class NoModelLLM(FakeLLM):
            def understand(self, text):  # type: ignore[override]
                raise AssertionError(f"固定句式不该调模型：{text!r}")

        db.insert_activity({"title": "京东整点券", "grab_at": iso(1, "08:30")})
        r = Bot(self.cfg, NoModelLLM({})).handle_text("明天8:30抢什么")
        self.assertIn("京东整点券", r)
        self.assertIn(pl.bold_time("08:30"), r)

    def test_a_normal_sentence_still_reaches_the_model(self):
        db.insert_activity({"title": "京东整点券", "grab_at": iso(0, "09:00")})
        script = {"帮我看看今天抢什么": {"intent": "query", "date": self.today.isoformat()}}
        llm = FakeLLM(script)
        r = Bot(self.cfg, llm).handle_text("帮我看看今天抢什么")
        self.assertEqual([c[0] for c in llm.calls], ["understand"])
        self.assertIn("京东整点券", r)

    # -- 回复只给时间 + 标题 --------------------------------------------

    def _seed_noisy(self):
        # 标题里刻意**不含**平台名，这样断言「京东 不在回复里」才真的在测
        # 「平台没被带出来」，而不是被标题自己命中。
        db.insert_activity({
            "title": "整点券",
            "platform": "京东",
            "grab_at": iso(1, "08:30"),
            "start_at": iso(1, "08:30"),
            "end_at": iso(1, "23:59"),
            "note": "每人限领1张，需先领券再下单",
            "url": "https://example.com/coupon",
            "status": "pending",
        })

    def test_the_query_reply_is_only_time_and_title(self):
        """用户明确的取舍：微信里只看标题，完整内容去日历页面看。"""
        self._seed_noisy()
        r = self.bot({}).handle_text("明天8:30抢什么")
        self.assertIn("整点券", r)
        self.assertIn(pl.bold_time("08:30"), r)
        for noisy in ("京东", "📝", "🔗", "example.com", "限领", "至 ", "<#"):
            with self.subTest(noisy=noisy):
                self.assertNotIn(noisy, r)

    def test_the_day_reply_is_only_time_and_title(self):
        self._seed_noisy()
        r = self.bot({}).handle_text("明天抢什么")
        self.assertIn("整点券", r)
        self.assertIn(pl.bold_time("08:30"), r)
        self.assertNotIn("📝", r)
        self.assertNotIn("https://", r)
        self.assertNotIn("京东", r)

    def test_the_calendar_command_hands_back_the_real_address(self):
        """`/日历` 回来了（用户 2026-10-09：「同步增加"/日历"指令」）。

        2026-10-07 删掉它，是因为它在 `pipeline.py` 里自己拼了个写死的
        `http://<你的NAS地址>:8080/`。现在它和启动日志、口令提示页走**同一个**
        `settings.front_url()`，所以「地址跟着域名和口令自动变」是能验的。
        """
        r = self.bot({}).handle_text("/日历")
        self.assertTrue(r.endswith("http://127.0.0.1:8080/"), r)
        self.assertNotIn("你的NAS地址", r)          # 当年那个占位符不许回来

        # 口令一设，地址里立刻带上 —— 点开就能进，不用手抄
        st.update({"web_token": "my-pass"})
        r2 = self.bot({}).handle_text("/日历")
        self.assertIn("?token=my-pass", r2)

        # 外网域名开着就用域名：端口和局域网地址都不该再出现
        st.update({"domain_enabled": True, "domain_url": "wool.example.com"})
        r3 = self.bot({}).handle_text("/日历")
        self.assertIn("https://wool.example.com/", r3)
        self.assertNotIn("127.0.0.1", r3)

        self.assertEqual(self.bot({}).handle_text("/calendar"), r3)
        self.assertIn("/日历", pl.HELP_TEXT)        # 帮助里重新宣传它

    def test_the_ingest_receipt_hands_back_the_calendar_address(self):
        """2026-10-10 用户要求：**每次通过微信录入成功后**，回执里要附上日历地址
        （原话：「每次通过 clawbot 录入活动以后（只有这个情况下），回复内容里应该附带
        日历地址，这样每次录完都能方便去前端检查」）。

        地址同样只走 `settings.front_url()`（`/日历` 指令、启动日志用的是同一个），
        位置照他给的样例：在「问一句「今天抢什么」就能查。」之后、末尾那句一言之前
        —— 一言是 `wechat.py` 贴的，这里看不到它，所以只钉最前面那句的顺序。
        """
        text = "明天上午9点半京东超市满99减50领券，限会员"
        script = {
            text: {
                "intent": "ingest",
                "title": "京东超市满99减50领券",
                "platform": "京东",
                "grab_at": iso(1, "09:30"),
                "needs_clarify": False,
            },
            "后天10点淘宝秒杀": {
                "intent": "ingest",
                "title": "淘宝秒杀",
                "platform": "淘宝",
                "grab_at": iso(2, "10:00"),
                "needs_clarify": False,
            },
        }
        r = self.bot(script).handle_text(text)
        self.assertIn("已记下 1 条", r)
        self.assertIn(f"📅 日历页面：{pl._front_url(self.cfg)}", r)
        self.assertLess(r.index("就能查。"), r.index("日历页面："), r)

        # 口令一设，回执里也跟着带 —— 点开就能进，不用手抄
        st.update({"web_token": "my-pass"})
        r2 = self.bot(script).handle_text("后天10点淘宝秒杀")
        self.assertIn("已记下 1 条", r2)
        self.assertIn("?token=my-pass", r2)

        # **只有录入这条路上带**：查询、统计、帮助、寒暄都不许顺手贴一个地址
        for t in ("/统计", "/help", "你好"):
            self.assertNotIn("日历页面：", self.bot({}).handle_text(t))

    def test_the_help_word_returns_help_without_the_model(self):
        """用户 2026-10-07 的要求：发「使用帮助」直接回帮助，不走模型。"""
        class NoModelLLM(FakeLLM):
            def understand(self, text):  # type: ignore[override]
                raise AssertionError(f"「使用帮助」不该调模型：{text!r}")

        for word in ("使用帮助", "帮助", "/help", "使用说明", "怎么用"):
            with self.subTest(word=word):
                r = Bot(self.cfg, NoModelLLM({})).handle_text(word)
                self.assertEqual(r, pl.HELP_TEXT)
                self.assertIn("核心操作", r)

    def test_the_help_text_only_teaches_what_to_send(self):
        """用户 2026-10-07：「这个不应该展示给我看吧」—— 帮助只讲「你能怎么问」+ 指令，
        机器人内部的取舍规则（那一刻只回什么、没有就不凑数、清单怎么分组）
        留在 README 里，不再写给用户看。

        **「走不走模型」这一项：2026-10-09 由用户点名写进帮助，2026-10-10 与
        2026-10-11 他又两次亲手换掉整段文案**（现在是末尾那句「💡 标准句式或快捷指令
        = 不调用AI；截图、大白话、非标句式 = 调用AI。」）。下面这份黑名单仍然是
        「行为取舍不外泄」，一个字都没动。
        """
        for head in ("1. 核心操作", "2. 一言设置", "3. 快捷指令"):
            with self.subTest(head=head):
                self.assertIn(head, pl.HELP_TEXT)
        self.assertIn("明天8:30抢什么", pl.HELP_TEXT)
        self.assertIn("/即将", pl.HELP_TEXT)
        for rule in ("那一刻开抢", "没有就是没有", "不拿别的日子凑数", "只能领不能抢",
                     "按时刻分组", "完整内容", "回复只给时间",
                     # 2026-10-09 用户点名删掉的结尾那句（「我只会如实报库里有的东西，不会编。」）
                     "如实报", "不会编"):
            with self.subTest(rule=rule):
                self.assertNotIn(rule, pl.HELP_TEXT)

    def test_the_help_text_says_which_path_skips_the_model(self):
        """用户 2026-10-09：「写明白怎么问是不调用 ai 直接走脚本回复，怎么问才会调 ai，
        专业的术语表达」；**2026-10-10 与 2026-10-11 他两次亲手把整段帮助换成自己写的
        那份**，术语（规则通道／模型通道）没了，只剩末尾一句大白话。

        这是对 2026-10-09 那条要求的**定点反转**：判据从「写了术语」变成「末尾那两句在」，
        同时钉住旧术语、以及上一版那三句（省流诀窍／不扣钱／扣额度）都不许悄悄爬回来。
        """
        for word in ("标准句式或快捷指令 = 不调用AI",
                     "截图、大白话、非标句式 = 调用AI"):
            with self.subTest(word=word):
                self.assertIn(word, pl.HELP_TEXT)
        for gone in ("规则通道", "模型通道", "不发模型请求", "消耗一次模型调用",
                     "省流诀窍", "不扣钱", "扣额度"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, pl.HELP_TEXT)

    def test_the_sayings_categories_are_spelled_out_where_they_are_typed(self):
        """用户 2026-10-09：「根据一言官方提供的分类使用方法，在设置里面增加一言的
        自定义源，a：动画；b：漫画；…；l：抖机灵」；2026-10-10：「把一言来源的
        字母分类注释到『一言来源』后面」。

        于是字母清单从帮助里挪到了设置页那个输入框下面。**权威仍然只有一处**
        （`app/sayings.py::CATEGORIES`）：服务端把它当 `sayings_categories` 交给页面，
        页面只负责显示。清单改了而这条链子没跟上，这条用例就该红。

        2026-10-11 用户给的那版帮助连「默认四类」后面的 `d,i,k,h` 都不写了
        （只写「默认文学、诗词、哲学、影视」+「根据页面提示输入对应字母」），
        所以这里改盯那两句 —— **字母清单仍然只在设置页**，这条链子一个字没动。
        """
        from app import sayings as sy

        cats = st.public_view(None)["sayings_categories"]
        for key, name in sy.CATEGORIES.items():
            with self.subTest(key=key):
                self.assertIn(f"{key} {name}", cats)
        for word in ("一言来源", "设置", "默认文学、诗词、哲学、影视", "输入对应字母"):
            with self.subTest(word=word):
                self.assertIn(word, pl.HELP_TEXT)

    def test_the_two_channels_really_are_what_the_help_says(self):
        """帮助里那份分工不能只是文案 —— 拿真消息跑一遍，核对它到底走没走模型。

        `FakeLLM.calls` 记着每一次模型调用：规则通道那几种说法一次都不该有，
        模型通道那几类各至少一次。录入确认那条（走 `llm.confirm`）由
        `test_clarify_then_confirm` 盯着，这里不重复。
        """
        def run(text: str) -> list:
            llm = FakeLLM({})
            reply = Bot(self.cfg, llm).handle_text(text, "u1")
            self.assertNotIn("⚠️", reply, f"{text} 处理时出错了：{reply}")
            return [name for name, _ in llm.calls]

        for text in ("/今天", "/即将", "/统计", "/help", "使用帮助", "你好",
                     "明天8:30抢什么", "本周六抢什么"):
            with self.subTest(text=text):
                self.assertEqual(run(text), [], f"{text} 命中了规则通道，却调了模型")
        for text in ("明天9点京东抢券", "随便问点什么", "删除梦享"):
            with self.subTest(text=text):
                self.assertIn("understand", run(text), f"{text} 该走模型通道，却没调")

    def test_the_ingest_receipt_still_carries_the_note(self):
        """用户选了「录入回执保留备注」——瘦身只针对查询回复。"""
        script = {
            "明天9点京东超市满99减50领券": {
                "intent": "ingest",
                "title": "京东超市满99减50领券",
                "grab_at": iso(1, "09:00"),
                "note": "限会员，每人1张",
            }
        }
        r = self.bot(script).handle_text("明天9点京东超市满99减50领券", user_id="u1")
        self.assertIn("已记下", r)
        self.assertIn("📝", r)
        self.assertIn("限会员", r)

    def test_the_help_teaches_the_fixed_phrasing(self):
        """用户 2026-10-10 的新文案：查活动要「严格按『日期+时间+查什么』发」。"""
        self.assertIn("日期+时间+查什么", pl.HELP_TEXT)
        self.assertIn("明天8:30抢什么", pl.HELP_TEXT)

    # -- 闲聊开关（设置页上的「允许机器人闲聊」，默认关）-------------------

    def test_a_non_activity_message_gets_one_fixed_line(self):
        """用户 2026-10-07 选的「非活动消息一律固定话术」：连模型自己回的那句也不要。"""
        b = self.bot({})  # 脚本里没有这句 → 模型回 intent=other + reply「没听懂」
        r = b.handle_text("今天天气怎么样")
        self.assertEqual(r, pl.CHAT_OFF_HINT)
        self.assertNotIn("没听懂", r)
        self.assertIn("使用帮助", r)
        # 模型还是被问了这一次 —— 自由句子和活动录入在调模型之前长得一样，
        # 只有模型能分辨，所以这一次省不掉（省掉的是原来那次 llm.chat）。
        self.assertEqual([c[0] for c in b.llm.calls], ["understand"])

    def test_a_greeting_is_answered_without_touching_the_model(self):
        """常见寒暄本地就认下来：整句命中 _SMALL_TALK，一次调用都不花。"""

        class NoModelLLM(FakeLLM):
            def understand(self, text):  # type: ignore[override]
                raise AssertionError(f"寒暄不该调模型：{text!r}")

            def chat(self, text):  # type: ignore[override]
                raise AssertionError(f"闲聊已关，不该调 chat：{text!r}")

        for word in ("你好", "您好", "在吗", "谢谢", "你是谁", "OK", " 嗯 "):
            with self.subTest(word=word):
                r = Bot(self.cfg, NoModelLLM({})).handle_text(word)
                self.assertEqual(r, pl.CHAT_OFF_GREETING)

    def test_only_a_whole_message_can_be_small_talk(self):
        """「你好，明天9点京东抢券」是录入，不能被寒暄表吞掉。"""
        self.assertFalse(pl._is_small_talk("你好，明天9点京东抢券"))
        self.assertFalse(pl._is_small_talk("谢谢，帮我删掉梦享"))
        self.assertTrue(pl._is_small_talk("你好"))
        text = "你好，明天9点京东抢券"
        script = {text: {"intent": "ingest", "title": "京东抢券", "grab_at": iso(1, "09:00")}}
        r = self.bot(script).handle_text(text, user_id="u1")
        self.assertIn("已记下", r)

    def test_turning_the_chat_back_on_restores_the_model_reply(self):
        """设置页把开关打开后，就是老行为：先用模型这次给的 reply，没有再补一次 chat。"""
        self.addCleanup(st.reset_cache)
        st.update({"chat_enabled": True})
        b = self.bot({})
        self.assertEqual(b.handle_text("今天天气怎么样"), "没听懂")

        class OnlyUnderstand(FakeLLM):
            def understand(self, text):  # type: ignore[override]
                self.calls.append(("understand", text))
                return {"intent": "other"}

        b2 = Bot(self.cfg, OnlyUnderstand({}))
        self.assertEqual(b2.handle_text("讲个笑话"), "chat:讲个笑话")

    def test_the_switch_does_not_touch_ingest_or_delete(self):
        """用户问的那件事：关掉闲聊**不影响**录入和删除。"""
        text = "明天9点京东超市满99减50领券"
        script = {text: {"intent": "ingest", "title": "京东超市满99减50领券",
                         "grab_at": iso(1, "09:00"), "note": "限会员"}}
        b = self.bot({**script, "删除京东": {"intent": "delete", "keyword": "京东"}})
        self.assertFalse(st.chat_enabled())  # 默认就是关着的
        r = b.handle_text(text, user_id="u1")
        self.assertIn("已记下 1 条", r)
        self.assertEqual(db.stats()["total"], 1)
        r = b.handle_text("删除京东", user_id="u1")
        self.assertIn("找到 1 条带「京东」的活动", r)   # 删除前的清单照旧
        self.assertEqual(db.stats()["total"], 1)        # 还没回编号，一条都没动


if __name__ == "__main__":
    unittest.main(verbosity=2)
