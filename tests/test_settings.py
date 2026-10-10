"""设置面板（AI 模型）的测试：存储层 + 页面真正会调的那几个接口。

为什么专门开一个文件：用户扫完码、日历也能看了，反馈的第一件事是
**「你没有设计配置 ai 模型的入口，我没法配模型」** —— 整个机器人等于不可用。
这个入口的存在本身就是需求，所以它必须被测试锁住，尤其是：

1. **明文 API key 绝不出现在任何响应里**（页面只该拿到掩码）。这个面板是全家
   唯一的密钥入口，一旦回吐，任何能打开页面的人都能把 key 抄走。
2. **保存后立刻生效**，不需要重启容器。这条是设计目的：配置存在 SQLite 而不是
   config.json，就是为了让正在跑的那个 `LLM` 实例下次调用时读到新值。
3. 用户点「保存」时页面回显的是掩码，**不能把 `sk-1…cdef` 当成新 key 存进去**。

不依赖任何真实模型服务，跑法：`python tests/test_settings.py`。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS.parent))

from app import db, llm as llm_mod, settings as st, timeutil as tu, web as webmod  # noqa: E402
from test_llm import FakeAPI, _reply  # noqa: E402


class SettingsStoreTest(unittest.TestCase):
    """存储层：优先级、掩码、回落。"""

    def setUp(self) -> None:
        tu.configure("Asia/Shanghai")
        base = TESTS / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"settings-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        db.init(str(self.tmp / "t.db"))
        st.reset_cache()

    def tearDown(self) -> None:
        st.reset_cache()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_unknown_keys_are_ignored(self):
        saved = st.update({"model": "m1", "evil": "rm -rf /", "providers": {"a": 1}})
        self.assertEqual(saved, {"model": "m1"})
        self.assertEqual(st.get("evil"), "")
        self.assertNotIn("providers", st.all_values())

    def test_empty_value_clears_and_falls_back(self):
        st.update({"model": "m1"})
        self.assertEqual(st.provider_override()["model"], "m1")
        st.update({"model": ""})
        self.assertNotIn("model", st.provider_override())

    def test_bool_is_stored_as_flag(self):
        st.update({"web_token_required": True})
        self.assertTrue(st.token_required(default=False))
        st.update({"web_token_required": False})
        self.assertFalse(st.token_required(default=True))
        st.update({"web_token_required": ""})
        self.assertTrue(st.token_required(default=True))  # 回落调用方给的默认值

    def test_temperature_comes_back_as_a_number(self):
        st.update({"temperature": "0.3"})
        self.assertEqual(st.provider_override()["temperature"], 0.3)

    def test_broken_temperature_is_skipped_not_fatal(self):
        st.update({"temperature": "warm"})
        self.assertNotIn("temperature", st.provider_override())

    def test_mask_key_never_shows_the_middle(self):
        self.assertEqual(st.mask_key(""), "")
        self.assertEqual(st.mask_key("short"), "sh…")
        masked = st.mask_key("sk-1234567890abcdef")
        self.assertEqual(masked, "sk-1…cdef")
        self.assertNotIn("567890", masked)

    def test_public_view_never_returns_the_raw_key(self):
        st.update({"api_key": "sk-supersecret-value"})
        view = st.public_view(None)
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("supersecret", blob)
        self.assertTrue(view["configured"])
        self.assertTrue(view["api_key_from_page"])
        self.assertEqual(view["api_key_masked"], "sk-s…alue")

    def test_public_view_falls_back_to_the_config_file(self):
        class FakeCfg:
            active_provider_name = "deepseek"
            active_provider = {
                "base_url": "https://api.deepseek.com",
                "api_key": "sk-from-config",
                "model": "deepseek-chat",
            }

        view = st.public_view(FakeCfg())
        self.assertEqual(view["base_url"], "https://api.deepseek.com")
        self.assertEqual(view["model"], "deepseek-chat")
        self.assertTrue(view["configured"])
        self.assertFalse(view["api_key_from_page"])
        self.assertNotIn("sk-from-config", json.dumps(view))
        # vision_model 没配时回落到文字模型
        self.assertEqual(view["vision_model"], "deepseek-chat")

    def test_the_page_beats_the_config_file(self):
        """库里的设置优先级高于 config.json —— 否则页面上改了也不生效。"""

        class FakeCfg:
            active_provider_name = "deepseek"
            active_provider = {"base_url": "https://old", "api_key": "sk-old", "model": "old-model"}

        st.update({"base_url": "https://new", "model": "new-model"})
        view = st.public_view(FakeCfg())
        self.assertEqual(view["base_url"], "https://new")
        self.assertEqual(view["model"], "new-model")
        self.assertEqual(view["api_key_masked"], st.mask_key("sk-old"))  # key 没动

    def test_settings_survive_a_cache_reset(self):
        st.update({"model": "m1"})
        st.reset_cache()
        self.assertEqual(st.get("model"), "m1")

    def test_the_chat_switch_defaults_to_off(self):
        """用户 2026-10-07 选的：非活动消息一律固定话术，所以没设过就是不陪聊。"""
        self.assertFalse(st.chat_enabled())
        self.assertIs(st.public_view(None)["chat_enabled"], False)

        st.update({"chat_enabled": True})
        self.assertTrue(st.chat_enabled())
        self.assertIs(st.public_view(None)["chat_enabled"], True)

        st.update({"chat_enabled": ""})   # 页面传 None 等于清空
        self.assertFalse(st.chat_enabled())

    def test_the_sayings_switch_defaults_to_on(self):
        """与闲聊开关**故意相反**：一言是用户点名要的，装完默认就该带。"""
        self.assertTrue(st.sayings_enabled())
        self.assertIs(st.public_view(None)["sayings_enabled"], True)

        st.update({"sayings_enabled": False})
        self.assertFalse(st.sayings_enabled())
        self.assertIs(st.public_view(None)["sayings_enabled"], False)

        st.update({"sayings_enabled": ""})   # 清空 → 回到默认「开」
        self.assertTrue(st.sayings_enabled())

    def test_the_sayings_source_is_stored_here_and_interpreted_there(self):
        """用户 2026-10-09：「根据一言官方提供的分类使用方法，在设置里面增加一言的
        自定义源」。

        这一层只管**存取**：他填的那串原样存、原样回显；「这串是什么意思」由
        `app/sayings.py::source_url()` 一个地方说了算 —— 两处各解析一次，
        迟早会出现「设置里显示 d,i,k,h、实际却按别的分类取」这种事。
        """
        from app import sayings as sy

        self.assertEqual(st.sayings_source(), "")
        self.assertIn("sayings_source", st.ALL_KEYS)
        # 没设过 = 默认那四类；页面拿得到默认值和可选分类（帮助/placeholder 用）
        self.assertEqual(st.public_view(None)["sayings_source"], "")
        self.assertEqual(st.public_view(None)["sayings_source_default"], "d,i,k,h")
        self.assertIn("抖机灵", st.public_view(None)["sayings_categories"])
        self.assertEqual(sy.source_url(), "http://v1.hitokoto.cn/?c=d&c=i&c=k&c=h")

        st.update({"sayings_source": "i"})
        self.assertEqual(st.sayings_source(), "i")
        self.assertEqual(st.public_view(None)["sayings_source"], "i")
        self.assertEqual(sy.source_url(), "http://v1.hitokoto.cn/?c=i")

        st.update({"sayings_source": "https://example.com/api"})
        self.assertEqual(sy.source_url(), "https://example.com/api")

        st.update({"sayings_source": ""})    # 清空 → 回默认
        self.assertEqual(st.sayings_source(), "")
        self.assertEqual(sy.source_url(), "http://v1.hitokoto.cn/?c=d&c=i&c=k&c=h")

    def test_the_reminder_switch_defaults_to_on(self):
        """用户 2026-10-09 点名要的默认：装完就该主动提醒，不用先去设置里找开关。"""
        self.assertTrue(st.remind_enabled())
        self.assertIs(st.public_view(None)["remind_enabled"], True)

        st.update({"remind_enabled": False})
        self.assertFalse(st.remind_enabled())
        self.assertIs(st.public_view(None)["remind_enabled"], False)

        st.update({"remind_enabled": ""})   # 清空 → 回到默认「开」
        self.assertTrue(st.remind_enabled())

    def test_the_reminder_lead_time_defaults_to_ten_minutes(self):
        self.assertEqual(st.remind_minutes(), st.REMIND_DEFAULT_MINUTES)
        self.assertEqual(st.public_view(None)["remind_minutes"], st.REMIND_DEFAULT_MINUTES)

        st.update({"remind_minutes": 30})
        self.assertEqual(st.remind_minutes(), 30)

        st.update({"remind_minutes": ""})   # 清空 → 回到默认
        self.assertEqual(st.remind_minutes(), st.REMIND_DEFAULT_MINUTES)

    def test_a_lead_time_below_the_floor_is_clamped_on_read(self):
        """下限 5 分钟是用户定的。**读的时候也得夹** —— 接口拦不住手改过的库。"""
        self.assertEqual(st.REMIND_MIN_MINUTES, 5)
        for bad in ("1", "0", "-3"):
            st.update({"remind_minutes": bad})
            self.assertEqual(st.remind_minutes(), 5, f"{bad} 该被抬到 5")

    def test_a_lead_time_that_is_not_a_number_falls_back(self):
        """库里躺着一个 "abc"（旧版本写的、手改的）也不能让提醒线程崩在启动上。"""
        for bad in ("abc", "", "  ", "十分钟"):
            st.update({"remind_minutes": bad})
            self.assertEqual(st.remind_minutes(), st.REMIND_DEFAULT_MINUTES)

    def test_a_huge_lead_time_is_capped_at_a_day(self):
        st.update({"remind_minutes": 99999})
        self.assertEqual(st.remind_minutes(), st.REMIND_MAX_MINUTES)

    def test_a_real_config_object_is_not_silently_ignored(self):
        """`Config.get("web")` 返回的是 `Config` 对象，不是 dict —— 这里真踩过。

        `dict(Config(...))` 抛 `TypeError`，被 `except Exception` 吞掉之后
        `web.public_host` 就永远读不到：单测（传普通 dict）全过，真机上 `/日历`
        却回一个 `http://127.0.0.1:8080/`。
        """
        from app import config as cfgmod

        cfg = cfgmod.Config(
            {"web": {"public_host": "192.0.2.165", "public_port": 8080, "token": "s3cret"}}
        )
        self.assertEqual(st.front_url(cfg), "http://192.0.2.165:8080/?token=s3cret")

    def test_the_page_token_beats_the_config_one(self):
        """「打开页面需要的口令」改成可自定义 —— 库里设过就用库里的。"""
        cfg = {"web": {"token": "from-config"}}
        self.assertEqual(st.web_token(cfg), "from-config")
        st.update({"web_token": "mine-own"})
        self.assertEqual(st.web_token(cfg), "mine-own")
        # 清掉（空串 = 删除那行）就回落配置文件，老部署行为不变
        st.update({"web_token": ""})
        self.assertEqual(st.web_token(cfg), "from-config")

    def test_the_domain_gets_a_scheme_and_loses_its_trailing_slash(self):
        for given, want in (
            ("wool.example.com", "https://wool.example.com"),
            ("wool.example.com/", "https://wool.example.com"),
            ("  https://wool.example.com/  ", "https://wool.example.com"),
            ("http://192.168.1.9:8080", "http://192.168.1.9:8080"),
            ("", ""),
        ):
            with self.subTest(given=given):
                st.update({"domain_url": given})
                self.assertEqual(st.domain_url(), want)

    def test_the_login_layer_needs_the_switch_and_a_key(self):
        """少一个就当没开：开了开关没填秘钥，是「既进不去又没设防」的最糟中间态。"""
        st.update({"login_enabled": True, "login_key": ""})
        self.assertFalse(st.login_ready())
        st.update({"login_key": "S3cret-1"})
        self.assertTrue(st.login_ready())
        # 开关关掉，秘钥填得再全也不生效
        st.update({"login_enabled": False})
        self.assertFalse(st.login_ready())

    def test_the_key_has_to_have_every_kind_of_character(self):
        """用户 2026-10-09 定的规矩：大小写、数字、符号都要有，不少于 8 位。

        `key_problem()` 只负责**说哪里不合格**，绝不替人改秘钥 —— 偷偷补一位
        或者砍掉符号，用户以为自己设的是什么、实际存的是什么就对不上了。
        """
        self.assertEqual(st.LOGIN_KEY_MIN, 8)
        self.assertEqual(st.key_problem("S3cret-1"), "")
        for bad, word in (
            ("Secret-x", "数字"),
            ("s3cret-1", "大写字母"),
            ("S3CRET-1", "小写字母"),
            ("S3cret11", "符号"),
            ("S3c-1", "8 位"),
            ("", "8 位"),
        ):
            with self.subTest(given=bad):
                problem = st.key_problem(bad)
                self.assertTrue(problem, bad)
                self.assertIn(word, problem)
        # 只短、但四类都齐的，也得报长度
        self.assertIn("8 位", st.key_problem("S3c-1xY"))
        # 空格算符号：用户真想要空格，不该被当成「没填」
        self.assertEqual(st.key_problem("S3cret 1"), "")

    def test_a_key_saved_under_the_old_name_still_works(self):
        """上一版那行叫 `login_password`。改名不能让老部署的秘钥凭空消失。"""
        st.update({"login_enabled": True})
        st._write({"login_password": "Old-Key-1"})
        self.assertEqual(st.login_key(), "Old-Key-1")
        self.assertTrue(st.login_ready())
        # 但新写下去的是新名字
        st.update({"login_key": "New-Key-1"})
        self.assertEqual(st.login_key(), "New-Key-1")

    def test_the_session_value_follows_the_key(self):
        """会话值 = `sha256(盐|秘钥|版本号)`：改秘钥不用去清谁的会话，旧 cookie 自动作废。"""
        a = st.session_value("one-two")
        self.assertTrue(a)
        self.assertEqual(a, st.session_value("one-two"))
        self.assertNotEqual(a, st.session_value("one-two-3"))
        self.assertEqual(st.session_value(""), "")
        # 不传实参就用库里存着的那把
        st.update({"login_key": "stored-1"})
        self.assertEqual(st.session_value(), st.session_value("stored-1"))
        self.assertNotEqual(st.session_value(), st.session_value("stored-2"))

    def test_logging_out_moves_the_session_version(self):
        """退出登录只发 `Max-Age=0` 是不够的 —— 得让服务端也认不出旧 cookie。

        真机上验过：退出之后拿旧 cookie 打 `/api/calendar` 还是 200。
        """
        st.update({"login_key": "S3cret-1"})
        self.assertEqual(st.login_epoch(), 0)
        before = st.session_value()
        self.assertTrue(before)
        self.assertEqual(st.bump_login_epoch(), 1)
        self.assertEqual(st.login_epoch(), 1)
        self.assertNotEqual(st.session_value(), before)
        # 版本号不是给人填的键：设置接口写不进去
        self.assertNotIn("login_epoch", st.ALL_KEYS)
        st.update({"login_epoch": "99"})
        self.assertEqual(st.login_epoch(), 1)
        # 坏值一律当 0，别让一行脏数据把所有人锁在门外
        st._write({"login_epoch": "abc"})
        self.assertEqual(st.login_epoch(), 0)

    def test_the_front_url_prefers_the_domain_then_the_public_host(self):
        cfg = {"web": {"public_host": "10.0.0.5", "public_port": 9000}}
        # 域名关着、库里也没口令：用部署脚本写下的真实地址，**不是**调用方给的 127.0.0.1
        self.assertEqual(st.front_url(cfg, host="127.0.0.1", port=8080), "http://10.0.0.5:9000/")
        # 口令挂上去，而且必须转义（`&` 不转义会把参数截断）
        st.update({"web_token": "a&b"})
        self.assertEqual(
            st.front_url(cfg, host="127.0.0.1", port=8080),
            "http://10.0.0.5:9000/?token=a%26b",
        )
        # 口令层关掉，地址末尾就不该挂个空参数
        st.update({"web_token_required": False})
        self.assertEqual(st.front_url(cfg, host="127.0.0.1", port=8080), "http://10.0.0.5:9000/")
        st.update({"web_token_required": True})
        # 域名开着：域名赢了，局域网地址和端口都不再出现
        st.update({"domain_enabled": True, "domain_url": "wool.example.com"})
        self.assertEqual(
            st.front_url(cfg, host="127.0.0.1", port=8080),
            "https://wool.example.com/?token=a%26b",
        )
        # `with_token=False`（口令提示页要用它）不带口令
        self.assertEqual(st.front_url(cfg, with_token=False), "https://wool.example.com/")

    def test_the_config_token_is_the_fallback(self):
        """老部署从没碰过设置页，口令就在 config.json 里 —— 这些都不该变。"""
        st.update({"web_token": ""})
        self.assertEqual(
            st.front_url({"web": {"token": "cfg-token"}}, host="h", port=1),
            "http://h:1/?token=cfg-token",
        )

    def test_the_page_view_never_carries_a_raw_secret(self):
        st.update({
            "api_key": "sk-secret-key",
            "web_token": "secret-token",
            "login_key": "Secret-Key-1",
        })
        data = st.public_view(None)
        blob = json.dumps(data)
        for secret in ("sk-secret-key", "secret-token", "Secret-Key-1"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)
        # 但得让页面知道「设过了」，否则用户会以为没保存上
        self.assertEqual(data["web_token_masked"], st.mask_secret("secret-token"))
        self.assertEqual(data["login_key_masked"], st.mask_secret("Secret-Key-1"))
        self.assertTrue(data["login_key_set"])
        self.assertEqual(data["web_token_min"], st.WEB_TOKEN_MIN)
        self.assertEqual(data["login_key_min"], st.LOGIN_KEY_MIN)
        # **用户名没有了**：页面视图里不该再出现这个名字
        self.assertNotIn("login_user", data)
        self.assertNotIn("login_password_set", data)
        self.assertEqual(data["login_session_days"], st.LOGIN_SESSION_DAYS)
        self.assertEqual(data["login_session_days"], 7)
        self.assertEqual(data["login_ban_hours"], 24)
        self.assertEqual(data["login_max_fails"], st.LOGIN_MAX_FAILS)

    def test_saving_the_settings_wakes_the_reminder(self):
        """页面上一保存就该当场起停线程，而不是等下次重启才轮到。"""
        from app import web as webmod

        class Fake:
            def __init__(self):
                self.calls = 0

            def sync(self):
                self.calls += 1

        fake = Fake()
        webmod.set_reminder(fake)
        try:
            webmod._sync_reminder()
            self.assertEqual(fake.calls, 1)
        finally:
            webmod.set_reminder(None)   # 别把假货留给别的用例

    def test_the_page_switch_stops_the_sayings_fetches(self):
        """关掉之后不只是不贴那句话 —— 连后台预取都不该再去打那个接口。

        `wechat.ClawBot` 一启动就会调 `prefetch()`，只挡住 `tail()` 的话，
        用户明明关了开关，容器还在偷请求 v1.hitokoto.cn。
        """
        from app import sayings as sayings_mod

        calls = []

        def fake(timeout=None):
            calls.append(timeout)
            return "心之所向，素履以往。"

        s = sayings_mod.Sayings(fetcher=fake)
        st.update({"sayings_enabled": False})
        self.assertEqual(s.tail(), "")
        s.prefetch()
        time.sleep(0.05)
        self.assertEqual(calls, [], "关掉之后一次都不该取")

        st.update({"sayings_enabled": True})
        self.assertEqual(s.tail(), "\n\n**心之所向，素履以往。**")

    def test_the_env_var_beats_the_page_switch(self):
        """`WOOLBOT_NO_SAYINGS=1` 是部署时的硬开关：页面打开了也不取。"""
        from app import sayings as sayings_mod

        st.update({"sayings_enabled": True})
        s = sayings_mod.Sayings(fetcher=lambda timeout=None: "心之所向")
        with unittest.mock.patch.dict(os.environ, {"WOOLBOT_NO_SAYINGS": "1"}):
            self.assertEqual(s.tail(), "")


class LlmLiveReloadTest(unittest.TestCase):
    """核心设计目的：页面上存了 key，**正在跑的那个 LLM 立刻就能用**。"""

    def setUp(self) -> None:
        tu.configure("Asia/Shanghai")
        base = TESTS / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"llmreload-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        db.init(str(self.tmp / "t.db"))
        st.reset_cache()

    def tearDown(self) -> None:
        st.reset_cache()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def test_a_bot_built_without_a_key_picks_one_up_later(self):
        llm = llm_mod.LLM({"base_url": "http://127.0.0.1:1", "model": "m", "api_key": ""})
        self.assertFalse(llm.configured)

        st.update({"api_key": "sk-added-later"})  # 页面上点了保存

        self.assertTrue(llm.configured)
        self.assertEqual(llm.api_key, "sk-added-later")

    def test_a_changed_model_is_used_on_the_next_call(self):
        llm = llm_mod.LLM({"base_url": "http://127.0.0.1:1", "model": "old", "api_key": "k"})
        self.assertEqual(llm.model, "old")
        st.update({"model": "new"})
        llm._refresh()
        self.assertEqual(llm.model, "new")

    def test_missing_key_raises_a_hint_and_does_not_retry(self):
        llm = llm_mod.LLM({"base_url": "http://127.0.0.1:1", "model": "m", "api_key": ""})
        with self.assertRaises(llm_mod.NoKeyError) as ctx:
            llm.chat("你好")
        self.assertIn("API key", str(ctx.exception))
        self.assertIn("设置", str(ctx.exception))  # 告诉他去哪儿配

    def test_no_key_error_is_not_retried(self):
        """没 key 重试 3 次只会白等 7 秒，所以 `_post` 必须直接抛出去。"""
        llm = llm_mod.LLM({"base_url": "http://127.0.0.1:1", "model": "m", "api_key": ""})
        with unittest.mock.patch("app.llm.time.sleep") as sleeper:
            with self.assertRaises(llm_mod.NoKeyError):
                llm._post({"model": "m", "messages": []})
        sleeper.assert_not_called()

    def test_the_key_from_the_page_is_actually_sent(self):
        api = FakeAPI((200, _reply("可用")))
        try:
            st.update({"api_key": "sk-live"})
            llm = llm_mod.LLM({"base_url": api.base_url, "model": "m", "api_key": ""})
            llm.chat("你好")
            self.assertEqual(api.calls[0]["auth"], "Bearer sk-live")
        finally:
            api.close()

    def test_a_bad_key_is_recorded_so_the_page_can_say_so(self):
        """打错一个字符，黄条就消失了 —— 那是这个项目最坑的一种「看起来正常」。

        `configured` 只说明「填了 key」。key 到底管不管用，得等真打一次才知道；
        知道了就得存下来，否则页面永远以为一切正常，用户只在微信里收到一句报错。
        """
        api = FakeAPI((401, {"error": {"message": "Authentication Fails"}}))
        try:
            st.update({"api_key": "sk-typo"})
            llm = llm_mod.LLM({"base_url": api.base_url, "model": "m", "api_key": ""})
            with unittest.mock.patch("app.llm.time.sleep"):
                with self.assertRaises(llm_mod.LLMError):
                    llm.chat("你好")
        finally:
            api.close()

        err = st.last_error()
        self.assertEqual(err["kind"], "auth")
        self.assertIn("设置", err["message"])
        self.assertTrue(err["at"])  # 记了时间，页面上好说「什么时候开始不对的」
        # 页面就是从这里读到的
        self.assertEqual(st.public_view()["last_error"]["kind"], "auth")

    def test_a_rate_limit_is_not_recorded(self):
        """限流是对方的问题，页面上报红只会让用户白折腾。"""
        api = FakeAPI((429, {"error": "slow down"}))
        try:
            st.update({"api_key": "sk-ok"})
            llm = llm_mod.LLM({"base_url": api.base_url, "model": "m", "api_key": ""})
            with unittest.mock.patch("app.llm.time.sleep"):
                with self.assertRaises(llm_mod.LLMError):
                    llm.chat("你好")
        finally:
            api.close()
        self.assertEqual(st.last_error(), {})

    def test_a_successful_call_clears_the_recorded_failure(self):
        """改好了就该自己消失，不用用户再去点一次什么。"""
        st.set_last_error("auth", "旧的问题")
        api = FakeAPI((200, _reply("可用")))
        try:
            st.update({"api_key": "sk-fixed"})
            llm = llm_mod.LLM({"base_url": api.base_url, "model": "m", "api_key": ""})
            llm.chat("你好")
        finally:
            api.close()
        self.assertEqual(st.last_error(), {})
        # 没有失败时必须给 `None`，不能给 `{}` —— 空对象在 JS 里是真值，
        # 前端一句 `if (data.last_error)` 就会在模型正常时亮红条。
        self.assertIsNone(st.public_view()["last_error"])

    def test_last_error_is_never_mistaken_for_a_setting(self):
        """`llm_error_*` 是系统记的，不是用户能改的设置 —— 别让页面把它写回来。"""
        st.set_last_error("auth", "boom")
        st.update({"llm_error_kind": "whatever", "llm_error_message": "whatever"})
        self.assertEqual(st.last_error()["kind"], "auth")
        self.assertNotIn("llm_error_kind", st.ALL_KEYS)

    def test_recording_a_failure_never_breaks_the_call(self):
        """记不上就算了 —— 记录失败绝不该把一次调用拖垮。"""
        with unittest.mock.patch.object(st, "set_last_error", side_effect=RuntimeError("db 挂了")):
            llm_mod._record_problem("auth", "消息")  # 不该抛出去


class SettingsApiTest(unittest.TestCase):
    """页面真正会发的那几个请求。"""

    TOKEN = "s3cret"

    def setUp(self) -> None:
        tu.configure("Asia/Shanghai")
        base = TESTS / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"settingsapi-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        st.reset_cache()
        webmod.set_config(None)
        webmod.set_qr_refresh(None)
        webmod.set_wechat_logout(None)
        webmod.set_reminder(None)
        self.httpd = webmod.create_server(
            {"db_path": self.db_file, "web": {"host": "127.0.0.1", "port": 0, "token": self.TOKEN}}
        )
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        webmod.set_config(None)
        webmod.set_qr_refresh(None)
        webmod.set_wechat_logout(None)
        webmod.set_reminder(None)
        st.reset_cache()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def call(self, method: str, path: str, payload=None, *, token: str | None = None):
        url = self.base + urllib.parse.quote(path, safe="/?&=%")
        if token is not None:
            url += ("&" if "?" in path else "?") + "token=" + token
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def ok(self, method: str, path: str, payload=None) -> dict:
        code, body, _ = self.call(method, path, payload, token=self.TOKEN)
        self.assertEqual(code, 200, body[:400])
        return json.loads(body)

    # -- 读 ------------------------------------------------------------

    def test_get_returns_the_view_without_any_secret(self):
        st.update({"api_key": "sk-topsecret-abcdef"})
        data = self.ok("GET", "/api/settings")["data"]
        self.assertTrue(data["configured"])
        self.assertNotIn("topsecret", json.dumps(data))

    def test_settings_need_a_token(self):
        code, body, _ = self.call("GET", "/api/settings", token=None)
        self.assertEqual(code, 401, body[:200])

    # -- 写 ------------------------------------------------------------

    def test_save_then_read_back_is_masked(self):
        data = self.ok("POST", "/api/settings", {"api_key": "sk-brand-new-key", "model": "m2"})["data"]
        self.assertTrue(data["configured"])
        self.assertEqual(data["api_key_masked"], st.mask_key("sk-brand-new-key"))
        self.assertEqual(st.get("model"), "m2")

    def test_resubmitting_the_mask_does_not_overwrite_the_key(self):
        """页面回显的是 `sk-a…z`。用户只改了别的字段时，不能把掩码存成 key。"""
        st.update({"api_key": "sk-real-key-value"})
        masked = st.public_view(None)["api_key_masked"]
        self.ok("POST", "/api/settings", {"api_key": masked, "model": "m3"})
        self.assertEqual(st.get("api_key"), "sk-real-key-value")

    def test_an_empty_key_field_leaves_the_key_alone(self):
        st.update({"api_key": "sk-keep-me"})
        self.ok("POST", "/api/settings", {"api_key": "", "model": "m4"})
        self.assertEqual(st.get("api_key"), "sk-keep-me")

    def test_saving_nothing_is_rejected(self):
        code, body, _ = self.call("POST", "/api/settings", {"nonsense": 1}, token=self.TOKEN)
        self.assertEqual(code, 400, body[:200])

    def test_unknown_fields_are_dropped(self):
        self.ok("POST", "/api/settings", {"model": "m5", "shell": "rm -rf /"})
        self.assertEqual(st.get("shell"), "")

    def test_token_switch_can_be_flipped(self):
        self.ok("POST", "/api/settings", {"web_token_required": False})
        code, body, _ = self.call("GET", "/api/stats", token=None)
        self.assertEqual(code, 200, body[:200])
        self.ok("POST", "/api/settings", {"web_token_required": True})
        code, _, _ = self.call("GET", "/api/stats", token=None)
        self.assertEqual(code, 401)

    def test_chat_switch_can_be_flipped(self):
        """默认关；页面上打开之后微信那边就恢复闲聊（这一格只影响闲聊）。"""
        self.assertIs(self.ok("GET", "/api/settings")["data"]["chat_enabled"], False)
        body = self.ok("POST", "/api/settings", {"chat_enabled": True})
        self.assertIs(body["data"]["chat_enabled"], True)
        self.assertTrue(st.chat_enabled())
        body = self.ok("POST", "/api/settings", {"chat_enabled": False})
        self.assertIs(body["data"]["chat_enabled"], False)
        self.assertFalse(st.chat_enabled())

    def test_sayings_switch_can_be_flipped(self):
        """默认开；页面上关掉之后机器人不再去 v1.hitokoto.cn 取那句话。"""
        self.assertIs(self.ok("GET", "/api/settings")["data"]["sayings_enabled"], True)
        body = self.ok("POST", "/api/settings", {"sayings_enabled": False})
        self.assertIs(body["data"]["sayings_enabled"], False)
        self.assertFalse(st.sayings_enabled())
        body = self.ok("POST", "/api/settings", {"sayings_enabled": True})
        self.assertIs(body["data"]["sayings_enabled"], True)
        self.assertTrue(st.sayings_enabled())

    def test_reminder_switch_and_lead_time_can_be_set_from_the_page(self):
        self.assertIs(self.ok("GET", "/api/settings")["data"]["remind_enabled"], True)
        body = self.ok("POST", "/api/settings", {"remind_enabled": False, "remind_minutes": 30})
        self.assertIs(body["data"]["remind_enabled"], False)
        self.assertEqual(body["data"]["remind_minutes"], 30)
        self.assertFalse(st.remind_enabled())
        self.assertEqual(st.remind_minutes(), 30)

    def test_the_custom_token_can_be_set_from_the_page(self):
        """「打开页面需要的口令」改成可自定义，而且**当场生效**（不用重启）。"""
        data = self.ok("GET", "/api/settings")["data"]
        self.assertIs(data["web_token_from_page"], False)

        body = self.ok("POST", "/api/settings", {"web_token": "my-own-pass"})["data"]
        self.assertTrue(body["web_token_from_page"])
        self.assertEqual(body["web_token_masked"], st.mask_secret("my-own-pass"))
        self.assertEqual(st.web_token(None), "my-own-pass")

        # 立刻生效：旧口令不再认，新口令认
        self.assertEqual(self.call("GET", "/api/stats", token="my-own-pass")[0], 200)
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN)[0], 401)

    def test_resubmitting_the_masked_token_does_not_overwrite_it(self):
        """回显是 `my…ss`。用户只改了别的字段时，不能把掩码存成新口令。"""
        st.update({"web_token": "the-real-pass"})
        masked = st.public_view(None)["web_token_masked"]
        # 注意：口令一改，请求就得用**新**口令，旧的那个已经不认了
        code, body, _ = self.call(
            "POST", "/api/settings",
            {"web_token": masked, "model": "m6"},
            token="the-real-pass",
        )
        self.assertEqual(code, 200, body[:300])
        self.assertEqual(st.get("web_token"), "the-real-pass")

    def test_the_sayings_source_can_be_set_from_the_page(self):
        """用户 2026-10-09：「在设置里面增加一言的自定义源」。

        存进来之前**当场校验**：分类字母只认官方那 11 个。填一个不认的字母要
        大声报错（顺带把可选项列出来），绝不能悄悄丢掉它 —— 用户以为设的是
        `d,j`、实际只按 d 取，这种安静的错误比报错难查一百倍。
        """
        from app import sayings as sy

        data = self.ok("GET", "/api/settings")["data"]
        self.assertEqual(data["sayings_source"], "")
        self.assertEqual(data["sayings_source_default"], "d,i,k,h")

        body = self.ok("POST", "/api/settings", {"sayings_source": "d,i"})["data"]
        self.assertEqual(body["sayings_source"], "d,i")
        self.assertEqual(st.sayings_source(), "d,i")
        self.assertEqual(sy.source_url(), "http://v1.hitokoto.cn/?c=d&c=i")

        # 自定义源那半边：一个完整网址原样存、原样用
        body = self.ok("POST", "/api/settings",
                       {"sayings_source": "http://v1.hitokoto.cn/?c=k"})["data"]
        self.assertEqual(body["sayings_source"], "http://v1.hitokoto.cn/?c=k")

        code, body, _ = self.call("POST", "/api/settings",
                                  {"sayings_source": "d,j"}, token=self.TOKEN)
        self.assertEqual(code, 400, body[:200])
        text = body.decode("utf-8")
        self.assertIn("j", text)
        self.assertIn("抖机灵", text)
        self.assertEqual(st.sayings_source(), "http://v1.hitokoto.cn/?c=k",
                         "被拒的那次不能改库")

        # 清空 = 回到默认四类（这里的「留空」是有意义的值，不是「别动这个框」）
        body = self.ok("POST", "/api/settings", {"sayings_source": ""})["data"]
        self.assertEqual(body["sayings_source"], "")
        self.assertEqual(st.sayings_source(), "")

    def test_a_too_short_token_is_refused_loudly(self):
        """要口令而口令只有一个字符，只是自欺欺人；真不想要可以关掉那个开关。"""
        code, body, _ = self.call("POST", "/api/settings", {"web_token": "ab"}, token=self.TOKEN)
        self.assertEqual(code, 400, body[:200])
        self.assertIn("口令", body.decode("utf-8"))
        self.assertEqual(st.get("web_token"), "")

    def test_the_domain_switch_needs_an_address(self):
        data = self.ok("GET", "/api/settings")["data"]
        self.assertIs(data["domain_enabled"], False)     # 默认关
        self.assertEqual(data["domain_url"], "")

        # 开着却不填地址 = 地址算不出来，宁可当场拒掉
        code, body, _ = self.call(
            "POST", "/api/settings", {"domain_enabled": True}, token=self.TOKEN
        )
        self.assertEqual(code, 400, body[:200])
        self.assertIn("域名", body.decode("utf-8"))
        self.assertFalse(st.domain_enabled())

        body = self.ok("POST", "/api/settings",
                       {"domain_enabled": True, "domain_url": "wool.example.com"})["data"]
        self.assertTrue(body["domain_enabled"])
        self.assertEqual(st.domain_url(), "https://wool.example.com")
        body = self.ok("POST", "/api/settings", {"domain_enabled": False})["data"]
        self.assertIs(body["domain_enabled"], False)

    def test_the_login_switch_needs_a_strong_key(self):
        data = self.ok("GET", "/api/settings")["data"]
        self.assertIs(data["login_enabled"], False)      # 默认关
        self.assertFalse(data["login_ready"])

        # 开着却一把秘钥都没有：当场拒掉，别留个「看着protected其实门大开」的状态
        code, body, _ = self.call(
            "POST", "/api/settings", {"login_enabled": True}, token=self.TOKEN
        )
        self.assertEqual(code, 400, body[:200])
        self.assertIn("8 位", body.decode("utf-8"))
        self.assertFalse(st.login_enabled())

        # 够长、而且缺符号 —— 用户 2026-10-09 定的规矩是四类字符缺一不可
        code, body, _ = self.call(
            "POST", "/api/settings",
            {"login_enabled": True, "login_key": "S3cretxx"},
            token=self.TOKEN,
        )
        self.assertEqual(code, 400, body[:200])
        self.assertIn("符号", body.decode("utf-8"))
        self.assertEqual(st.login_key(), "")

        # 秘钥错在「短」上，就只说短，不要顺嘴多报一句
        code, body, _ = self.call("POST", "/api/settings", {"login_key": "S3-1"}, token=self.TOKEN)
        self.assertEqual(code, 400, body[:200])
        self.assertIn("8 位", body.decode("utf-8"))
        self.assertEqual(st.login_key(), "")

        # 先只存秘钥、开关还关着：这时候页面视图该说「设过了」，但绝不回明文。
        # （顺序有讲究：开关一开，后面每次 `/api/settings` 都要先登录，这个用例
        # 后面几步就不方便再直接打接口了。）
        body = self.ok("POST", "/api/settings", {"login_key": "S3cret-1"})["data"]
        self.assertIs(body["login_enabled"], False)
        self.assertTrue(body["login_key_set"])
        self.assertNotIn("S3cret-1", json.dumps(body))   # 明文绝不回传
        self.assertEqual(body["login_key_masked"], st.mask_secret("S3cret-1"))
        # 页面把掩码原样发回来 = 「没改」，不能把它当成新秘钥存进去
        body = self.ok("POST", "/api/settings", {"login_key": body["login_key_masked"]})["data"]
        self.assertEqual(st.login_key(), "S3cret-1")
        self.assertIs(body["login_key_set"], True)
        # **用户名没有了**：页面视图里不该再出现这个名字
        self.assertNotIn("login_user", body)
        self.assertNotIn("login_password_set", body)
        self.assertNotIn("s3cret-1", json.dumps(body, ensure_ascii=False).lower())

        # 现在开开关：秘钥已经合规，一次就过
        body = self.ok("POST", "/api/settings", {"login_enabled": True})["data"]
        self.assertIs(body["login_enabled"], True)
        self.assertTrue(body["login_ready"])
        self.assertIs(body["login_session_days"], 7)     # 30 天改成 7 天
        self.assertIs(body["login_ban_hours"], 24)
        # 2 次（用户 2026-10-09：口令页伪装成登录页之后谁都能试，5 次太宽容）
        self.assertEqual(body["login_max_fails"], st.LOGIN_MAX_FAILS)
        self.assertEqual(st.LOGIN_MAX_FAILS, 2)

    def test_a_lead_time_below_five_minutes_is_refused_loudly(self):
        """夹紧成 5 是错的 —— 用户填 1、页面回 5，那是偷偷改人的数。"""
        for bad in (1, 0, -1, "1", "2.5", "abc", ""):
            code, body, _ = self.call(
                "POST", "/api/settings", {"remind_minutes": bad}, token=self.TOKEN
            )
            self.assertEqual(code, 400, f"{bad!r} 该被拒：{body[:160]}")
            self.assertIn("分钟", body.decode("utf-8"))
        self.assertEqual(st.remind_minutes(), st.REMIND_DEFAULT_MINUTES, "被拒的没写进库")

    def test_saving_the_reminder_settings_syncs_the_thread(self):
        """保存不该只是「躺在库里」—— 提醒线程要当场跟着起停。"""
        from app import web as webmod

        class Fake:
            def __init__(self):
                self.calls = 0

            def sync(self):
                self.calls += 1

        fake = Fake()
        webmod.set_reminder(fake)
        try:
            self.ok("POST", "/api/settings", {"remind_enabled": False})
            self.assertEqual(fake.calls, 1)
            self.ok("POST", "/api/settings", {"remind_minutes": 20})
            self.assertEqual(fake.calls, 2)
            # 只改模型名的时候别去动它 —— 每次保存都起停线程是白折腾。
            self.ok("POST", "/api/settings", {"model": "another-model"})
            self.assertEqual(fake.calls, 2)
        finally:
            webmod.set_reminder(None)

    # -- 测试连接 -------------------------------------------------------

    def test_test_endpoint_without_a_key_says_so(self):
        body = self.ok("POST", "/api/settings/test", {"base_url": "http://127.0.0.1:1"})
        self.assertFalse(body["ok"])
        self.assertIn("API key", body["error"])

    def test_test_endpoint_really_calls_the_model(self):
        api = FakeAPI((200, _reply("可用")))
        try:
            body = self.ok(
                "POST",
                "/api/settings/test",
                {"base_url": api.base_url, "model": "m", "api_key": "sk-x"},
            )
            self.assertTrue(body["ok"], body)
            self.assertIn("可用", body["reply"])
        finally:
            api.close()

    def test_test_endpoint_reports_a_bad_key_instead_of_500(self):
        api = FakeAPI((401, {"error": "bad key"}))
        try:
            body = self.ok(
                "POST",
                "/api/settings/test",
                {"base_url": api.base_url, "model": "m", "api_key": "sk-bad"},
            )
        finally:
            api.close()
        self.assertFalse(body["ok"])
        self.assertTrue(body["error"])
        # 给人看的人话，不是 "LLMError: ..." —— 页面就照原样显示这一行
        self.assertNotIn("LLMError", body["error"])
        self.assertIn("API key", body["error"])

    def test_a_failed_test_is_reflected_on_the_page(self):
        """保存了一把坏 key，点「测试连接」失败之后，页面得能看到红条要显示的东西。

        注意顺序：得先保存。`configured` 说的是「库里存了 key」，光在弹层里敲一把
        没保存的 key 不算 —— 那种情况下弹层自己会把错误显示在按钮旁边。
        """
        api = FakeAPI((401, {"error": {"message": "bad key"}}))
        try:
            self.ok(
                "POST",
                "/api/settings",
                {"base_url": api.base_url, "model": "m", "api_key": "sk-bad"},
            )
            self.ok("POST", "/api/settings/test", {})
        finally:
            api.close()
        view = self.ok("GET", "/api/settings")["data"]
        self.assertTrue(view["configured"])
        self.assertEqual(view["last_error"]["kind"], "auth")
        self.assertIn("API key", view["last_error"]["message"])

    def test_a_failed_test_without_saving_shows_no_banner(self):
        """没保存过 key 就去测试：`configured` 还是假，页面该继续显示黄条。

        这条锁的是「弹层里试一把坏 key」不该把主界面变成红条 —— 用户还没决定保存。
        """
        api = FakeAPI((401, {"error": "bad"}))
        try:
            self.ok(
                "POST",
                "/api/settings/test",
                {"base_url": api.base_url, "model": "m", "api_key": "sk-bad"},
            )
        finally:
            api.close()
        view = self.ok("GET", "/api/settings")["data"]
        self.assertFalse(view["configured"])

    def test_saving_a_new_key_gives_it_a_clean_slate(self):
        """用户刚换了一把 key，之前记的失败就不该继续挂在他脸上。"""
        api = FakeAPI((401, {"error": "bad"}))
        try:
            self.ok("POST", "/api/settings", {"api_key": "sk-bad", "base_url": api.base_url, "model": "m"})
            self.ok("POST", "/api/settings/test", {})
        finally:
            api.close()
        self.assertTrue(self.ok("GET", "/api/settings")["data"]["last_error"])
        view = self.ok("POST", "/api/settings", {"api_key": "sk-new"})["data"]
        self.assertFalse(view["last_error"])

    def test_a_successful_test_clears_the_red_banner(self):
        bad = FakeAPI((401, {"error": "bad"}))
        good = FakeAPI((200, _reply("可用")))
        try:
            self.ok("POST", "/api/settings", {"api_key": "sk-bad", "base_url": bad.base_url, "model": "m"})
            self.ok("POST", "/api/settings/test", {})
            self.assertTrue(self.ok("GET", "/api/settings")["data"]["last_error"])

            self.ok("POST", "/api/settings", {"base_url": good.base_url, "api_key": "sk-good"})
            body = self.ok("POST", "/api/settings/test", {})
            self.assertTrue(body["ok"], body)
        finally:
            bad.close()
            good.close()
        self.assertFalse(self.ok("GET", "/api/settings")["data"]["last_error"])

    def test_a_clean_state_sends_null_not_an_empty_object(self):
        """干净状态必须是 JSON `null`，不能是 `{}`。

        真机上的表现：模型一切正常，页面顶部却一直挂着「⚠️ AI 模型用不了」，
        而且旁边还写着「✅ 通了」—— 就因为空对象在 JS 里是真值。
        """
        self.ok("POST", "/api/settings", {"api_key": "sk-good"})
        self.assertIsNone(self.ok("GET", "/api/settings")["data"]["last_error"])

    def test_the_page_wires_up_the_red_banner(self):
        """静态断言：前端真的会把 last_error 显示出来。

        JS 里的东西没有测试兜着，最容易在重构里悄悄丢掉 —— 而丢掉的表现是
        「看着一切正常」，正是这个项目最怕的那种坏。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("last_error", js)
        self.assertIn("AI 模型用不了", js)
        self.assertIn("setup-panel danger", js)
        self.assertIn(".setup-panel.danger", css)
        for ident in ("setupTitle", "setupBody"):
            self.assertIn(f'id="{ident}"', html)
            self.assertIn(f"'{ident}'", js)
        # 失败原因单独一行，不跟「留空则不改动已存的 key」粘成一句
        self.assertIn('id="sKeyError"', html)
        self.assertIn("sKeyError", js)
        self.assertIn(".field-note", css)
        # 判据必须落在「这条记录有没有内容」上。曾经写的是
        # `data.configured && data.last_error`，而后端那时回的是空对象 `{}` ——
        # 空对象在 JS 里是真值，于是模型好好的、红条却一直亮着。
        self.assertIn("function hasError(", js)
        self.assertIn("hasError(data.last_error)", js)
        self.assertNotIn("data.configured && data.last_error", js)

    def test_the_banner_refreshes_itself(self):
        """红条必须是活的，否则会和「✅ 通了」并排显示到用户关掉页面。

        真机截图里就出现过这一幕：顶部「⚠️ AI 模型用不了」，弹层里「✅ 通了」。
        根因是 `pollSettings` 只在打开页面和保存设置时跑，而且点「测试连接」成功后
        没有重新拉一次设置。
        """
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("setInterval(pollSettings", js)
        # 「测试连接」成功那条分支里要重新拉设置：定位到 testModel 内部再找
        test_fn = js.split("async function testModel()", 1)[1].split("\nasync function", 1)[0]
        self.assertIn("pollSettings()", test_fn)
        self.assertIn("$('sKeyError').hidden = true", test_fn)

    def test_a_red_banner_never_shows_a_generic_sentence_alone(self):
        """形状不对（旧载荷 / 只有 kind）时也得有一句能照做的话。

        曾经 `last_error()` 只认 `llm_error_message`，缺了它就返回空串，前端只能退回
        「模型调用失败了，到设置里检查一下。」—— 用户看到的是「这软件自相矛盾」。
        """
        st.set_last_error("auth", "")  # 只有 kind，没有 message（旧版本写的行）
        self.addCleanup(st.clear_last_error)
        err = st.last_error()
        self.assertEqual(err["kind"], "auth")
        self.assertTrue(err["message"])
        self.assertIn("key", err["message"])
        # 前端拿到字符串形状也不能退化成通用话
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function lastErrorText", js)
        self.assertIn("typeof err === 'string'", js)

    def test_test_endpoint_uses_the_key_saved_earlier(self):
        """保存过 key 之后，再点「测试」时 key 框是空的（只显掩码）。

        这时候测试必须用库里存的那把 key，不能回「请先填 API key」——
        否则用户会以为保存没生效，再填一遍。
        """
        api = FakeAPI((200, _reply("可用")))
        try:
            self.ok("POST", "/api/settings", {"base_url": api.base_url, "model": "m", "api_key": "sk-saved"})
            # 第二次请求不带 api_key，就像页面上没重填一样
            body = self.ok("POST", "/api/settings/test", {"base_url": api.base_url, "model": "m"})
            self.assertTrue(body["ok"], body)
            self.assertEqual(api.calls[0]["auth"], "Bearer sk-saved")
        finally:
            api.close()

    # -- 二维码刷新 -----------------------------------------------------

    def test_qr_refresh_without_a_bot_says_why(self):
        body = self.ok("POST", "/api/qr/refresh")
        self.assertFalse(body["ok"])
        self.assertIn("机器人没在跑", body["error"])

    def test_qr_refresh_calls_the_registered_action(self):
        calls = []
        webmod.set_qr_refresh(lambda: calls.append(1) or "已请求换一张二维码")
        body = self.ok("POST", "/api/qr/refresh")
        self.assertTrue(body["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIn("换一张", body["note"])

    def test_qr_refresh_survives_an_exploding_action(self):
        def boom():
            raise RuntimeError("上游挂了")

        webmod.set_qr_refresh(boom)
        body = self.ok("POST", "/api/qr/refresh")
        self.assertFalse(body["ok"])
        self.assertIn("上游挂了", body["error"])

    # -- 退出微信 -------------------------------------------------------

    def test_wechat_logout_without_a_bot_says_why(self):
        """机器人没在跑的时候点「退出微信」，得说清是没在跑，不是「退不了」。"""
        body = self.ok("POST", "/api/wechat/logout")
        self.assertFalse(body["ok"])
        self.assertIn("机器人没在跑", body["error"])

    def test_wechat_logout_calls_the_registered_action(self):
        calls = []
        webmod.set_wechat_logout(lambda: calls.append(1) or "已退出，几秒后会出现新的二维码")
        body = self.ok("POST", "/api/wechat/logout")
        self.assertTrue(body["ok"])
        self.assertEqual(len(calls), 1)
        self.assertIn("新的二维码", body["note"])

    def test_wechat_logout_survives_an_exploding_action(self):
        def boom():
            raise RuntimeError("上游挂了")

        webmod.set_wechat_logout(boom)
        body = self.ok("POST", "/api/wechat/logout")
        self.assertFalse(body["ok"])
        self.assertIn("上游挂了", body["error"])

    def test_the_two_controls_do_not_share_a_slot(self):
        """两个动作各自注册、互不覆盖。

        共用同一个槽位的话，`set_wechat_logout` 会把「刷新二维码」挤掉 —— 页面上
        两颗按钮，一颗就变成点不动的装饰（而且因为返回 `ok:false`，还会弹一句
        「机器人没在跑」，明明在跑）。
        """
        seen = []
        webmod.set_qr_refresh(lambda: seen.append("qr") or "已请求换一张二维码")
        webmod.set_wechat_logout(lambda: seen.append("logout") or "已退出")
        self.assertTrue(self.ok("POST", "/api/qr/refresh")["ok"])
        self.assertTrue(self.ok("POST", "/api/wechat/logout")["ok"])
        self.assertEqual(seen, ["qr", "logout"])

    # -- 没带 token 的浏览器 --------------------------------------------

    def test_anonymous_browser_gets_a_readable_page_not_json(self):
        """用户反馈「不带 token 也进不去」—— 至少得回一张**能操作**的页面，不是一片 JSON。

        **但一个字的线索都不给**：以前那两版分别把 `?token=…`、以及「口令在
        config.json 的 web.token 里」印在页面上，任何能打开这个地址的人照抄一遍
        就走进去了，口令等于没设。用户 2026-10-09 定的是**伪装成秘钥登录页**。
        """
        code, body, headers = self.call("GET", "/", token=None)
        self.assertEqual(code, 401)
        text = body.decode("utf-8")
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertNotIn(self.TOKEN, text)
        for leak in ("token=", "config.json", "口令"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)
        # 认得出来、也真能用：就是那张秘钥表单
        self.assertIn('name="key"', text)
        self.assertIn('action="/api/login"', text)

    def test_anonymous_api_call_still_gets_json(self):
        code, body, _ = self.call("GET", "/api/stats", token=None)
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body)["ok"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
