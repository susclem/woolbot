"""main.py 的入口级测试。

写这个文件的原因：`main.py` 是唯一一个此前**没有任何测试碰过**的模块 ——
命令行参数、`boot()` 的装配顺序、`check` 的输出、`chat` 的对话循环，
全靠用户自己跑一遍才会发现问题。而它偏偏是用户唯一会直接敲的那一层。

这里用「假 OpenAI 端点 + 真配置文件 + 真 SQLite」把入口整条走完，
不需要 API key，也不出网。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import unittest
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))

import main as mainmod  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app import llm as llm_mod  # noqa: E402
from app import settings as st  # noqa: E402
from app import db  # noqa: E402
from app import timeutil as tu  # noqa: E402
from test_llm import FakeAPI, _reply  # noqa: E402


def run_main(argv: list[str], stdin_lines: list[str] | None = None) -> tuple[int, str]:
    """跑一次入口，把 stdout 抓回来。

    `_force_utf8()` 会对 sys.stdout 调 reconfigure —— 被 redirect 成 StringIO
    之后没有这个方法，它会走 except 分支，正好也是我们要覆盖的行为。
    """
    buf = io.StringIO()
    error: BaseException | None = None
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            if stdin_lines is None:
                code = mainmod.main(argv)
            else:
                with mock.patch("builtins.input", side_effect=stdin_lines):
                    code = mainmod.main(argv)
        except BaseException as exc:  # noqa: BLE001 - SystemExit 也要抓
            error = exc
    if error is not None:
        raise error
    return code, buf.getvalue()


class MainTestBase(unittest.TestCase):
    def setUp(self):
        tu.configure("Asia/Shanghai")
        base = TESTS / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        # 不能用 tempfile.mkdtemp()：本机沙箱下建出来的目录不可写
        self.tmp = base / f"main-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        # 设置表是模块级缓存的，用例之间必须清干净，否则上一个用例存的 key 会漏过来
        st.reset_cache()

    def tearDown(self):
        st.reset_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config_file(self, base_url: str, api_key: str = "sk-test-123", name: str = "config.json") -> Path:
        raw = {
            "timezone": "Asia/Shanghai",
            "db_path": str(self.tmp / "woolbot.db"),
            "provider": "deepseek",
            "providers": {
                "deepseek": {
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": "deepseek-v4-flash",
                }
            },
            "web": {"host": "127.0.0.1", "port": 0, "token": "tok"},
            # 测试输出里不需要日志刷屏
            "log": {"level": "CRITICAL"},
        }
        path = self.tmp / name
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return path

    def tomorrow(self) -> str:
        return (tu.today() + timedelta(days=1)).isoformat()


class ReminderWiringTest(MainTestBase):
    """`main.py` 得把开抢提醒真的接上 —— 不接就是「设置里有个开关，点了没用」。

    这里不跑真的 `client.run()`（那会去连微信），只把装配那几步走完：提醒器有没有
    交给网页、有没有按设置起停、退出时有没有收掉。
    """

    def _httpd(self):
        class FakeHTTPD:
            server_address = ("127.0.0.1", 8080)

            def __init__(self):
                self.shutdown_called = False

            def shutdown(self):
                self.shutdown_called = True

        return FakeHTTPD()

    def test_all_hands_the_reminder_to_the_page_and_stops_it_on_the_way_out(self):
        from app import web as webmod

        cfg = cfgmod.load(str(self.config_file("http://127.0.0.1:1")))
        db.init(cfg.get("db_path"))
        httpd = self._httpd()
        seen = {}

        class FakeClient:
            def run(self):
                seen["wired"] = webmod._REMINDER["obj"]
                seen["running"] = seen["wired"].running

        with mock.patch.object(mainmod, "_make_bot", return_value=object()), \
                mock.patch("app.wechat.ClawBot", return_value=FakeClient()), \
                mock.patch.object(webmod, "start_background", return_value=(httpd, None)):
            webmod.set_reminder(None)
            # 控制台不是 UTF-8 的话，`print_url` 里那个 emoji 会炸 —— 那是终端的事，
            # 不是装配的事，重定向掉它。
            with contextlib.redirect_stdout(io.StringIO()):
                code = mainmod.cmd_all(cfg, mock.Mock())

        self.assertEqual(code, 0)
        self.assertIsNotNone(seen["wired"], "提醒器没交给网页，设置页的开关就是死的")
        self.assertTrue(seen["running"], "默认「开」，启动时就该跑起来")
        self.assertFalse(seen["wired"].running, "退出时得把线程收掉")
        self.assertTrue(httpd.shutdown_called)
        webmod.set_reminder(None)

    def test_a_reminder_that_is_switched_off_is_never_started(self):
        """用户 2026-10-09 要的：没开就**不要**那些额外的线程和轮询。"""
        from app import web as webmod

        cfg = cfgmod.load(str(self.config_file("http://127.0.0.1:1")))
        db.init(cfg.get("db_path"))
        st.update({"remind_enabled": False})
        httpd = self._httpd()
        seen = {}

        class FakeClient:
            def run(self):
                seen["running"] = webmod._REMINDER["obj"].running

        with mock.patch.object(mainmod, "_make_bot", return_value=object()), \
                mock.patch("app.wechat.ClawBot", return_value=FakeClient()), \
                mock.patch.object(webmod, "start_background", return_value=(httpd, None)):
            webmod.set_reminder(None)
            with contextlib.redirect_stdout(io.StringIO()):
                mainmod.cmd_all(cfg, mock.Mock())

        self.assertIs(seen["running"], False, "关着的时候连线程都不该存在")
        webmod.set_reminder(None)


class DisplayUrlTest(unittest.TestCase):
    """绑在 0.0.0.0 上时打印出来的地址得是人能点开的。"""

    def test_wildcard_host_is_replaced_by_a_real_address(self):
        url = mainmod._display_url("0.0.0.0", 8080, "abc")
        self.assertNotIn("0.0.0.0", url)
        self.assertTrue(url.startswith("http://"))
        self.assertIn(":8080/", url)
        self.assertTrue(url.endswith("?token=abc"))

    def test_wildcard_spellings_do_not_leave_a_dangling_colon(self):
        """写错的话会拼出 `http://:80/` —— 主机名部分是空的。"""
        for host in ("", "*", "::"):
            with self.subTest(host=host):
                url = mainmod._display_url(host, 80)
                self.assertTrue(url.startswith("http://"), url)
                self.assertNotEqual(url.split("//", 1)[1][:1], ":", url)

    def test_concrete_host_passes_through(self):
        self.assertEqual(mainmod._display_url("127.0.0.1", 9000), "http://127.0.0.1:9000/")

    def test_token_is_optional(self):
        self.assertEqual(mainmod._display_url("nas.local", 80, ""), "http://nas.local:80/")

    def test_container_does_not_print_a_bridge_address(self):
        """容器里猜出来的「局域网 IP」是 172.x 网桥地址，手机连不上它。"""
        with mock.patch.object(mainmod, "_in_container", return_value=True), \
                mock.patch.object(mainmod, "_lan_ip", return_value="172.26.0.2"):
            url = mainmod._display_url("0.0.0.0", 8080, "abc")
        self.assertNotIn("172.26.0.2", url)
        self.assertIn("NAS", url)
        self.assertTrue(url.endswith("?token=abc"), url)

    def test_print_url_explains_the_placeholder(self):
        buf = io.StringIO()
        with mock.patch.object(mainmod, "_in_container", return_value=True):
            with contextlib.redirect_stdout(buf):
                mainmod.print_url("日历页面", "0.0.0.0", 8080, "abc")
        out = buf.getvalue()
        self.assertIn("局域网", out)

    def test_print_url_is_quiet_outside_a_container(self):
        buf = io.StringIO()
        with mock.patch.object(mainmod, "_in_container", return_value=False):
            with contextlib.redirect_stdout(buf):
                mainmod.print_url("日历页面", "127.0.0.1", 8080)
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 1)

    def test_lan_ip_never_raises(self):
        self.assertIsInstance(mainmod._lan_ip(), str)


class StartupTokenTest(MainTestBase):
    """启动日志里的口令必须是**现在生效的那把**。

    以前 `cmd_web` 和 all-in-one 都写 `token = <config.json 里那个 web.token>`，
    而 `settings.front_url()` 一旦收到 token 就不再读设置 —— 用户在页面「设置」里
    改过口令之后，**同一个进程的两行日志印出两个不同的 token**，第二行那个谁点谁
    进不去（2026-10-10 真机上就是这么发现的）。修法是让调用方传 `st.web_token(cfg)`。
    """

    def test_the_live_token_wins_over_the_one_in_config_json(self):
        # config.json 里写的是 "tok"，页面「设置」里存的是 "live-token"
        cfg = cfgmod.load(str(self.config_file("http://127.0.0.1:1")))
        db.init(cfg.get("db_path"))
        st.update({"web_token": "live-token"})

        url = mainmod._display_url("192.0.2.165", cfgmod.DEFAULT_WEB_PORT,
                                   st.web_token(cfg), cfg)
        self.assertIn("live-token", url)
        self.assertNotIn("token=tok", url)

    def test_both_startup_paths_ask_the_settings_for_the_token(self):
        """源码级：`main.py` 里不许再把 config.json 的 token 当成日志里的口令。"""
        src = Path(mainmod.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count('token = st.web_token(cfg) or ""'), 2, src)
        self.assertNotIn('web_raw.get("token")', src)
        self.assertNotIn('.get("web") or {}).get("token")', src)


class DeploymentPortTest(unittest.TestCase):
    """端口在几个文件里各写了一遍，必须同号。

    2026-10-10 把容器端口从 8080 改成了 16886（NAS 上 8080 被别的东西惦记上了）。
    这几处漏一处就是「页面打不开」或者「健康检查永远 unhealthy」，而且都要等部署完
    才看得出来 —— 让测试替人记着。
    """

    ROOT = TESTS.parent

    def test_the_port_is_the_same_everywhere(self):
        port = str(cfgmod.DEFAULT_WEB_PORT)
        self.assertEqual(str(cfgmod.DEFAULTS["web"]["port"]), port)

        compose = (self.ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(f'"{port}:{port}"', compose, "docker-compose.yml 的映射没跟上")

        dockerfile = (self.ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"EXPOSE {port}", dockerfile, "Dockerfile 的 EXPOSE 没跟上")
        # 健康检查现读 config.json 里的端口，不写死一个数字（写死了改端口就会假装 unhealthy）
        self.assertIn("/app/config.json", dockerfile)

        example = json.loads((self.ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(str(example["web"]["port"]), port)
        self.assertEqual(str(example["web"]["public_port"]), port)


class CheckCommandTest(MainTestBase):
    def test_check_says_model_is_reachable(self):
        api = FakeAPI((200, _reply("可用")))
        self.addCleanup(api.close)
        code, out = run_main(["check", "-c", str(self.config_file(api.base_url))])

        self.assertEqual(code, 0, out)
        self.assertIn("✅ 模型连通", out)
        self.assertIn("deepseek-v4-flash", out)
        self.assertIn("前端文件就绪", out)
        self.assertIn("一切正常", out)
        self.assertNotIn("0.0.0.0", out)
        self.assertEqual(api.count(), 1, "check 只该打一次模型")

    def test_check_fails_loudly_without_a_key(self):
        cfg = self.config_file("http://127.0.0.1:1", api_key="")
        code, out = run_main(["check", "-c", str(cfg)])

        self.assertEqual(code, 1)
        self.assertIn("没有 api_key", out)
        self.assertIn("见上面 ❌", out)

    def test_check_sees_a_key_that_was_saved_from_the_page(self):
        """用户 2026-10-07 报的假故障：key 是在网页上填的（存在 settings 表里，
        config.json 里没有），`check` 却红着脸说「没有 api_key」并退 1 —— 而机器人
        其实一直在正常干活。自检必须看「真正在用的那把 key」。
        """
        api = FakeAPI((200, _reply("可用")))
        self.addCleanup(api.close)
        cfg = self.config_file(api.base_url, api_key="")
        # 模拟「页面里填过 key」：同一个库（config.json 里的 db_path）里存一把
        db.init(str(self.tmp / "woolbot.db"))
        st.update({"api_key": "sk-from-the-page"})
        st.reset_cache()

        code, out = run_main(["check", "-c", str(cfg)])

        self.assertEqual(code, 0, out)
        self.assertIn("✅ 模型连通", out)
        self.assertIn("页面里填的", out)
        self.assertNotIn("没有 api_key", out)
        self.assertEqual(api.count(), 1)

    def test_check_reports_a_broken_model_instead_of_crashing(self):
        api = FakeAPI((500, {"error": "boom"}))
        self.addCleanup(api.close)
        cfg = self.config_file(api.base_url)
        with mock.patch("app.llm.time.sleep"):
            code, out = run_main(["check", "-c", str(cfg)])

        self.assertEqual(code, 1)
        self.assertIn("❌ 模型调用失败", out)


class ChatCommandTest(MainTestBase):
    def test_chat_records_then_answers(self):
        """入口级的完整来回：录入一句 → 再问一句 → 拿到刚记的那条。"""
        tomorrow = self.tomorrow()
        ingest = {
            "intent": "ingest",
            "activities": [
                {
                    "title": "京东超市 满99减50",
                    "platform": "京东",
                    "grab_at": f"{tomorrow}T09:00",
                    "end_at": f"{tomorrow}T23:59",
                    "confidence": 0.9,
                }
            ],
        }
        # 查询故意不给日期，逼 _fallback_day 从「明天」两个字里自己解出日期
        query = {"intent": "query", "time": "09:00"}
        api = FakeAPI((200, _reply(json.dumps(ingest, ensure_ascii=False))),
                      (200, _reply(json.dumps(query, ensure_ascii=False))))
        self.addCleanup(api.close)

        code, out = run_main(
            ["chat", "-c", str(self.config_file(api.base_url))],
            # 查询故意用**非**固定句式（「有哪些…券」多一个「券」字）—— 固定句式
            # （「明天9点抢什么」）现在由 Python 直接查库，不会走模型，那样这条
            # 就测不到「问一句也经过模型」这个来回。
            stdin_lines=["京东超市明天上午9点抢满99减50的券", "明天9点有哪些券", "quit"],
        )

        self.assertEqual(code, 0, out)
        self.assertIn("🐑 羊毛机器人", out)
        self.assertIn("京东超市", out, "录入回执里应该有标题")
        self.assertIn("09:00", out)
        self.assertEqual(api.count(), 2)
        self.assertIn("【用户消息】", api.body(0)["messages"][1]["content"])
        self.assertIn("京东超市", api.body(0)["messages"][1]["content"])

    def test_chat_survives_the_model_blowing_up(self):
        api = FakeAPI((200, "这不是 JSON，是网关的 HTML 页面"), (200, _reply("{}")))
        self.addCleanup(api.close)

        code, out = run_main(
            ["chat", "-c", str(self.config_file(api.base_url))],
            stdin_lines=["随便说点什么", "quit"],
        )

        self.assertEqual(code, 0, out)
        self.assertNotIn("Traceback", out)

    def test_chat_quits_on_eof(self):
        api = FakeAPI((200, _reply("{}")))
        self.addCleanup(api.close)
        with mock.patch("builtins.input", side_effect=EOFError):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = mainmod.main(["chat", "-c", str(self.config_file(api.base_url))])

        self.assertEqual(code, 0)
        self.assertIn("🐑 羊毛机器人", buf.getvalue())


class NoKeyToleranceTest(MainTestBase):
    """没配 key 时日历和扫码都得照样能用，只有「读懂消息」才需要 key。"""

    def booted(self, api_key: str = "") -> cfgmod.Config:
        # 走真入口的 boot()：配置 + 时区 + 数据库初始化一次装好
        with contextlib.redirect_stdout(io.StringIO()):
            return mainmod.boot(str(self.config_file("http://127.0.0.1:1", api_key=api_key)))

    def test_require_key_true_still_refuses(self):
        cfg = self.booted()
        with self.assertRaises(SystemExit) as ctx:
            mainmod._make_bot(cfg)
        self.assertIn("还没配 API key", str(ctx.exception))

    def test_require_key_false_still_yields_a_working_bot(self):
        """没 key 时装配的是一个**真** LLM，不是一个只会报错的桩。"""
        cfg = self.booted()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bot = mainmod._make_bot(cfg, require_key=False)

        self.assertIsInstance(bot.llm, llm_mod.LLM)
        self.assertFalse(bot.llm.configured)
        self.assertIn("日历页照常可用", buf.getvalue())

        # 固定句式连 key 都不需要 —— 它根本不调模型（这是快路顺带的好处）
        self.assertNotIn("API key", bot.handle_text("今天抢什么", user_id="cli"))

        # 别的说法仍要模型，于是给出那句可照做的「去设置里填 key」提示
        reply = bot.handle_text("帮我看看今天有哪些活动", user_id="cli")
        self.assertIn("API key", reply)
        self.assertNotIn("Traceback", reply)

    def test_the_key_can_be_added_at_runtime_without_rebuilding_the_bot(self):
        """这是设置面板存在的全部意义：填完就能用，不能要求重启容器。

        早先有个 `_NoKeyLLM` 桩，启动时装配它 —— 用户在页面上填了 key 也没用，
        机器人还是只会回那句提示，而进程看起来一切正常。
        """
        cfg = self.booted()
        with contextlib.redirect_stdout(io.StringIO()):
            bot = mainmod._make_bot(cfg, require_key=False)
        self.assertFalse(bot.llm.configured)

        st.update({"api_key": "sk-saved-from-the-page"})  # 页面上点了保存

        self.assertTrue(bot.llm.configured)
        self.assertEqual(bot.llm.api_key, "sk-saved-from-the-page")

    def test_no_key_never_leaks_a_raw_traceback(self):
        cfg = self.booted()
        with contextlib.redirect_stdout(io.StringIO()):
            bot = mainmod._make_bot(cfg, require_key=False)
        reply = bot.handle_text("京东明天9点抢券", user_id="cli")
        self.assertIn("API key", reply)
        self.assertNotIn("Traceback", reply)


class ParserTest(unittest.TestCase):
    def test_every_printed_command_exists(self):
        for name in ("web", "chat", "bot", "all", "check"):
            self.assertIn(name, mainmod.COMMANDS)

    def test_unknown_command_exits_with_usage(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                run_main(["nope"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
