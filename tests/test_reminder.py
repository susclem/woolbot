"""开抢提醒自测：不起线程、不发网络请求，时间由用例自己给。

跑法：
    python tests/test_reminder.py

这里**故意不碰 `app/wechat.py`** —— 提醒的全部逻辑（算时刻、拼字、去重、跳过）
都在 `app/reminder.py` 里，收件人和发送动作只是它调用的两三个方法。用一个假的
client 顶上去，用例才能把「现在几点」捏在手里；真去连微信的话，测的就不是提醒了。
真机的收发现成已经有 `tests/test_wechat.py` 在盯。
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, reminder as rm, sayings as sayings_mod, settings as st, timeutil as tu  # noqa: E402


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


class FakeClient:
    """顶掉 `wechat.ClawBot`：只留提醒真正用到的几个东西。

    `sayings` **故意不挂** —— 真的 `ClawBot` 一定有（`wechat.py:247`），但提醒
    得能在「对面没这个东西」时照发。要测一言的用例自己挂一个
    `sayings.Sayings(fetcher=…)`（绝不连网）。
    """

    def __init__(self, users=("u1",), allowed=(), logged_in=True):
        self.state = {"context_tokens": {u: "tok-" + u for u in users}}
        self.allowed = list(allowed)
        self._logged_in = logged_in
        self.sent: list[tuple[str, str]] = []
        self.sessions = 0

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def push_session(self):
        self.sessions += 1
        return f"session-{self.sessions}"

    def send_text(self, to_user_id, text, context_token="", session=None):
        self.sent.append((to_user_id, text))
        return True


class ReminderTest(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"remind-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        db.init(str(self.tmp / "t.db"))
        tu.configure("Asia/Shanghai")
        st.reset_cache()
        self.now = datetime(2026, 10, 9, 9, 0)

    def tearDown(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def add(self, **kw) -> int:
        data = {
            "title": "整点券",
            "platform": "京东",
            "grab_at": iso(self.now + timedelta(minutes=9)),
            "end_at": iso(self.now + timedelta(hours=12)),
        }
        data.update(kw)
        return db.insert_activity(data)

    def at(self, minutes: int, **kw) -> int:
        return self.add(grab_at=iso(self.now + timedelta(minutes=minutes)), **kw)

    # -- 什么时候该响 ---------------------------------------------------

    def test_only_the_ones_inside_the_window_are_due(self):
        self.at(9)                      # 9 分钟后开抢 → 在 10 分钟窗口里
        self.at(30)                     # 半小时后 → 还早
        due, wait = rm.Reminder(FakeClient()).scan(self.now)
        self.assertEqual([r["title"] for _, r in due], ["整点券"])
        self.assertLessEqual(wait, rm.SLEEP_MAX)

    def test_a_window_that_has_already_opened_is_not_reminded(self):
        """grab_at 正好等于现在（或者已经过去）就不该推 —— 那不是提醒，是讣告。"""
        self.at(0)
        rem = rm.Reminder(FakeClient())
        self.assertEqual(rem.scan(self.now)[0], [])
        self.assertEqual(rem.tick(self.now), rem.tick(self.now))  # 不炸就行

    def test_the_window_follows_the_setting(self):
        self.at(30)
        rem = rm.Reminder(FakeClient())
        self.assertEqual(rem.scan(self.now)[0], [], "默认 10 分钟，半小时外的还早")
        st.update({"remind_minutes": 60})
        self.assertEqual(len(rem.scan(self.now)[0]), 1, "改成 60 分钟就该轮到它了")

    def test_rows_that_are_not_pending_are_left_alone(self):
        """`list_range` 默认把过期/完成的也算进来（日历要它们），提醒不要。"""
        self.at(9, title="过期的券", status="expired")
        self.assertEqual(rm.Reminder(FakeClient()).scan(self.now)[0], [])

    # -- 发出去的是什么 -------------------------------------------------

    def test_the_message_says_how_long_is_left(self):
        self.at(9, title="京东整点券")
        client = FakeClient()
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1)
        user, body = client.sent[0]
        self.assertEqual(user, "u1")
        self.assertIn("还有 9 分钟开抢", body)
        self.assertIn("京东整点券", body)
        self.assertIn((self.now + timedelta(minutes=9)).strftime("%H:%M"), body)

    def test_several_at_the_same_moment_come_as_one_message(self):
        """10:00 同时开四个活动很常见 —— 同一时刻要合并成一条，不是四条。"""
        self.at(9, title="甲券")
        self.at(9, title="乙券")
        self.at(9, title="丙券")
        client = FakeClient()
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1, "一个收件人一轮只该收到一条")
        body = client.sent[0][1]
        for title in ("甲券", "乙券", "丙券"):
            self.assertIn(title, body)
        self.assertIn("共 3 场", body)

    def test_a_later_moment_does_not_ride_along(self):
        """用户 2026-10-09 的规矩：8点20分只提醒 8点半的，9 点的不许带出来。"""
        self.at(6, title="六分钟后的券")
        self.at(9, title="九分钟后的券")
        client = FakeClient()
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1)
        self.assertIn("六分钟后的券", client.sent[0][1])
        self.assertNotIn("九分钟后的券", client.sent[0][1], "更晚的时刻不该跟着出来")

    def test_the_next_moment_gets_its_own_message(self):
        self.at(6, title="六分钟后的券")
        self.at(9, title="九分钟后的券")
        client = FakeClient()
        rem = rm.Reminder(client)
        rem.tick(self.now)
        rem.tick(self.now + timedelta(minutes=1))
        self.assertEqual(len(client.sent), 2, "晚一点的那批该有自己的那条")
        second = client.sent[1][1]
        self.assertIn("九分钟后的券", second)
        self.assertNotIn("六分钟后的券", second, "已经提醒过的不重复列")

    def test_a_later_moment_is_not_held_up_by_an_already_sent_one(self):
        """顺序很关键：先按台账滤掉发过的，再取最近。

        反过来写的话，08:30 那批发完之后，08:33 得一直等到 08:30 过去才轮得上，
        等于白丢了 08:33 自己的提前量。
        """
        self.at(6, title="六分钟后的券")
        self.at(9, title="九分钟后的券")
        client = FakeClient()
        rem = rm.Reminder(client)
        rem.tick(self.now)
        rem.tick(self.now + timedelta(minutes=1))     # 这时 6 分钟那场还没开抢
        self.assertEqual(len(client.sent), 2, "不等早的那场开抢，晚的那场就该响")

    def test_the_quota_note_rides_along(self):
        """配额尾巴必须和日历卡片、和 `/即将` 同源 —— 提醒里也不能只说「快抢」。"""
        self.at(9, title="每周券", quota_week=2)
        client = FakeClient()
        rm.Reminder(client).tick(self.now)
        self.assertIn("本周 0/2", client.sent[0][1])

    # -- 末尾那句「一言」 -------------------------------------------------

    def test_the_saying_rides_along_like_a_reply(self):
        """用户 2026-10-09 要的：提醒末尾也带一句一言，格式和机器人回复一模一样。"""
        self.at(9, title="整点券")
        client = FakeClient()
        client.sayings = sayings_mod.Sayings(fetcher=lambda timeout=None: "心之所向，素履以往。")
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1)
        self.assertTrue(
            client.sent[0][1].endswith(sayings_mod.with_sentence("心之所向，素履以往。")),
            client.sent[0][1],
        )

    def test_the_saying_follows_the_page_switch(self):
        """设置里那个「一言」是**同一个**开关 —— 关掉之后连一句都不该去取。"""
        calls = []

        def fetcher(timeout=None):
            calls.append(timeout)
            return "心之所向，素履以往。"

        self.at(9, title="第一张券")
        client = FakeClient()
        client.sayings = sayings_mod.Sayings(fetcher=fetcher)
        st.update({"sayings_enabled": False})
        rm.Reminder(client).tick(self.now)
        self.assertNotIn("**", client.sent[0][1], "关着还贴了一言")
        self.assertEqual(calls, [], "关着的时候不该去请求那个接口")

        st.update({"sayings_enabled": True})
        self.at(9, title="第二张券")      # 换一张，别被台账挡掉
        rm.Reminder(client).tick(self.now + timedelta(minutes=1))
        self.assertTrue(client.sent[1][1].endswith(sayings_mod.with_sentence("心之所向，素履以往。")))
        self.assertTrue(calls, "开着就该去取一句")

    def test_a_broken_saying_does_not_take_the_reminder_with_it(self):
        """彩蛋接口出任何意外，提醒本身必须照发（和 `wechat.py` 贴一言同一个态度）。"""

        class Boom:
            def tail(self):
                raise RuntimeError("彩蛋接口炸了")

        self.at(9, title="整点券")
        client = FakeClient()
        client.sayings = Boom()
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1)
        self.assertIn("还有 9 分钟开抢", client.sent[0][1])

    # -- 只发一次 -------------------------------------------------------

    def test_a_reminder_is_not_sent_twice(self):
        aid = self.at(9)
        client = FakeClient()
        rem = rm.Reminder(client)
        rem.tick(self.now)
        rem.tick(self.now)
        rem.tick(self.now + timedelta(minutes=1))
        self.assertEqual(len(client.sent), 1)
        self.assertTrue(db.seen_msg(rm.ledger_key("u1", db.get(aid), self.now + timedelta(minutes=9))))

    def test_the_ledger_is_kept_per_person(self):
        self.at(9)
        client = FakeClient(users=("u1", "u2"))
        rm.Reminder(client).tick(self.now)
        self.assertEqual(sorted(u for u, _ in client.sent), ["u1", "u2"])

    def test_tomorrows_round_of_the_same_activity_comes_back(self):
        """「每天 09:09」今天提醒过，明天还得提醒 —— 所以台账的键必须带场次时刻。"""
        day = self.now.date()
        db.insert_activity({
            "title": "每日港城券",
            "grab_at": iso(self.now - timedelta(days=1)),
            "end_at": iso(self.now + timedelta(days=30)),
            "repeat_kind": "daily",
            "repeat_at": (self.now + timedelta(minutes=9)).strftime("%H:%M"),
        })
        client = FakeClient()
        rem = rm.Reminder(client)
        rem.tick(self.now)
        self.assertEqual(len(client.sent), 1, "今天这一场")
        # 第二天同一时刻再看：又是一场新的
        tomorrow = self.now + timedelta(days=1)
        rem.tick(tomorrow)
        self.assertEqual(len(client.sent), 2, "明天那一场还得响")
        self.assertNotEqual(day, tomorrow.date())

    # -- 谁收得到 -------------------------------------------------------

    def test_nothing_goes_out_before_the_bot_logs_in(self):
        aid = self.at(9)
        client = FakeClient(logged_in=False)
        rm.Reminder(client).tick(self.now)
        self.assertEqual(client.sent, [])
        # **没记台账**：登录回来时这一场要是还没开抢，还得提醒。
        self.assertFalse(db.seen_msg(rm.ledger_key("u1", db.get(aid), self.now + timedelta(minutes=9))))
        client._logged_in = True
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1)

    def test_nobody_to_tell_is_not_an_error(self):
        self.at(9)
        client = FakeClient(users=())
        rm.Reminder(client).tick(self.now)
        self.assertEqual(client.sent, [])

    def test_the_whitelist_narrows_the_recipients(self):
        self.at(9)
        client = FakeClient(users=("u1", "u2"), allowed=["u2"])
        rm.Reminder(client).tick(self.now)
        self.assertEqual([u for u, _ in client.sent], ["u2"])

    # -- 配额领满就不催了 -------------------------------------------------

    def test_a_quota_already_met_is_not_worth_a_trip(self):
        """「每周限领 1 次」这周领过了，再推一条「快去抢」是让人白跑一趟。"""
        aid = self.at(9, title="每周券", quota_week=1)
        db.mark_done(aid, self.now.date())
        client = FakeClient()
        rm.Reminder(client).tick(self.now)
        self.assertEqual(client.sent, [], "本期已领满，不该再提醒")

    def test_a_quota_with_room_left_still_goes_out(self):
        aid = self.at(9, title="每周券", quota_week=2)
        db.mark_done(aid, self.now.date())
        client = FakeClient()
        rm.Reminder(client).tick(self.now)
        self.assertEqual(len(client.sent), 1, "还有一次没领，得提醒")

    # -- 起停 -----------------------------------------------------------

    def test_sync_starts_and_stops_the_thread_with_the_setting(self):
        client = FakeClient()  # 库里没有活动，线程跑起来也不会发东西
        rem = rm.Reminder(client)
        self.assertFalse(rem.running, "一开始就没起")

        st.update({"remind_enabled": False})
        self.assertFalse(rem.sync())
        self.assertFalse(rem.running, "关着的时候连线程都不该存在")

        st.update({"remind_enabled": True})
        self.assertTrue(rem.sync())
        self.assertTrue(rem.running)

        self.assertTrue(rem.sync(), "已经跑着就保持跑着")
        self.assertTrue(rem.stop())
        self.assertFalse(rem.running)
        self.assertFalse(rem.stop(), "本来就没跑，再停一次什么都不做")

    def test_the_default_is_on(self):
        self.assertTrue(st.remind_enabled(), "没设过 = 开（用户点名要的默认）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
