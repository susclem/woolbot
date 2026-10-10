#!/usr/bin/env python3
"""ClawBot 协议层的测试。

**不碰真实的微信服务**：本文件起一台假 iLink 服务器（标准库 http.server），
按 `docs/clawbot-protocol.md` 里记录的响应形状回话，并把收到的每个请求原样记下来。
这样能把最容易出错、又最难在真机上复现的部分钉死：

- 登录状态机：wait → scaned → confirmed，以及 need_verifycode 的配对码回路；
- `sendmessage` 的必备字段（`from_user_id=""`、唯一 `client_id`、顶层 `base_info`）——
  上游文档说少了这几个字段就是「HTTP 200 但微信收不到」；
- `get_updates_buf` 游标在重启后还能接上；
- `ret=-14` 必须变成 `StaleToken` 而不是被当成普通错误吞掉；
- 入站图片的 CDN 下载 + AES-128-ECB 解密，两种 `aes_key` 编码都要能解。

跑法：python tests/test_wechat.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
import threading
import time
import unittest
import uuid
import requests
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import sayings as sayings_mod  # noqa: E402
from app import wechat  # noqa: E402

#: 一张真的 1x1 PNG，用来验证「下载 → 解密 → 魔数识别」整条链路
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AGtE0zaAAAAAElFTkSuQmCC"
)
AES_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")


def aes_encrypt(data: bytes, key: bytes) -> bytes:
    """测试侧独立实现，避免「用被测代码加密再用被测代码解密」这种自证。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    return AES.new(key, AES.MODE_ECB).encrypt(pad(data, AES.block_size))


class FakeServer:
    """够用就好的假 iLink 服务器。"""

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.qr_states: list[dict] = []          # get_qrcode_status 依次返回
        self.updates: list[dict] = []            # getupdates 依次返回
        self.downloads: dict[str, bytes] = {}    # encrypt_query_param -> 密文
        self.force_ret: dict[str, dict] = {}     # path -> 强制响应（测错误分支）
        self.typing_ticket = "TICKET-abc"
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    # -- 生命周期 -----------------------------------------------------

    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                pass

            # 收请求 → 记下来 → 回话
            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    payload = {"__raw__": raw.decode("utf-8", "replace")}
                record = {
                    "method": method,
                    "path": parsed.path,
                    "query": parse_qs(parsed.query),
                    "headers": {k: v for k, v in self.headers.items()},
                    "payload": payload,
                }
                outer.log.append(record)

                status, body, ctype = outer.respond(method, parsed.path, record)
                if isinstance(body, bytes):
                    blob = body
                else:
                    blob = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def do_POST(self):  # noqa: N802
                self._handle("POST")

            def do_GET(self):  # noqa: N802
                self._handle("GET")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)

    # -- 回话逻辑 -----------------------------------------------------

    def respond(self, method: str, path: str, record: dict) -> tuple[int, object, str]:
        # 所有业务响应都用 octet-stream —— 真实服务器就是这样，
        # aiohttp 的 .json() 会因此报错，requests 不会（上游踩坑 2）
        octet = "application/octet-stream"

        if path in self.force_ret:
            return 200, self.force_ret[path], octet
        if path == "/qr.png":
            return 200, PNG_1PX, "image/png"
        if path == "/c2c/download":
            param = (record["query"].get("encrypted_query_param") or [""])[0]
            blob = self.downloads.get(param)
            if blob is None:
                return 404, {"error": "no such param"}, octet
            return 200, blob, octet
        if path == "/ilink/bot/get_bot_qrcode":
            base = record["headers"].get("Host", "127.0.0.1")
            return 200, {"qrcode": "QR-1", "qrcode_img_content": f"http://{base}/qr.png"}, octet
        if path == "/ilink/bot/get_qrcode_status":
            if self.qr_states:
                return 200, self.qr_states.pop(0), octet
            return 200, {"status": "wait"}, octet
        if path == "/ilink/bot/getupdates":
            if self.updates:
                return 200, self.updates.pop(0), octet
            return 200, {"ret": 0, "msgs": [], "get_updates_buf": "CURSOR-0"}, octet
        if path == "/ilink/bot/getconfig":
            return 200, {"ret": 0, "typing_ticket": self.typing_ticket}, octet
        if path in (
            "/ilink/bot/sendtyping",
            "/ilink/bot/sendmessage",
            "/ilink/bot/msg/notifystart",
            "/ilink/bot/msg/notifystop",
        ):
            return 200, {"ret": 0, "errmsg": ""}, octet
        return 404, {"error": f"unhandled {path}"}, octet

    # -- 断言辅助 -----------------------------------------------------

    def calls(self, path: str) -> list[dict]:
        return [r for r in self.log if r["path"] == path]


class FakeBot:
    """替代 pipeline.Bot，只记录收到了什么、回了什么。"""

    def __init__(self, reply: str = "🐑 收到") -> None:
        self.reply = reply
        self.texts: list[tuple[str, str, str]] = []
        self.images: list[tuple[bytes, str, str, str]] = []

    def handle_text(self, text: str, user_id: str = "", msg_id: str = "") -> str:
        self.texts.append((text, user_id, msg_id))
        return self.reply

    def handle_image(self, data: bytes, mime: str = "image/jpeg", user_id: str = "",
                     msg_id: str = "") -> str:
        self.images.append((data, mime, user_id, msg_id))
        return self.reply


class FakeSayings:
    """替代 sayings.Sayings：不连网，回一句固定的。"""

    def __init__(self, text: str = "心之所向，素履以往。") -> None:
        self.text = text
        self.calls = 0

    def tail(self) -> str:
        self.calls += 1
        return f"\n\n**{self.text}**"


class FakeResponse:
    """假的 requests 响应，够 sayings 用。"""

    def __init__(self, data, status: int = 200) -> None:
        self._data = data
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class WeChatTestCase(unittest.TestCase):
    """基类：每个用例一台干净的假服务器 + 一份干净的配置。"""
    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)
        self.server = FakeServer()
        self.base = self.server.start()

        # mkdtemp() 在这台机器的沙箱下不可写，必须自己 mkdir
        root = Path(__file__).parent / ".tmp"
        self.tmp = root / f"wx-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)

        self.bot = FakeBot()
        #: 每条回复末尾那句一言。用例里一律用它 —— 真的 `sayings.Sayings()`
        #: 会去请求 https://v1.hitokoto.cn/，几十个用例各自连一次外网既慢又不确定。
        self.sayings = sayings_mod.NoSayings()
        self.cfg = {
            "wechat": {
                "state_file": str(self.tmp / "state.json"),
                "media_dir": str(self.tmp / "media"),
                "cdn_base": f"{self.base}/c2c",
                # 把取二维码的入口也指向假服务器，否则测试会去打真实微信
                "entry_base": self.base,
                "save_media": True,
                "allowed_users": [],
            }
        }

    def tearDown(self) -> None:
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        logging.disable(logging.NOTSET)

    def make_client(self) -> wechat.ClawBot:
        client = self.new_client()
        # 登录后的业务请求要打到假服务器上
        client.state["baseurl"] = self.base
        return client

    def new_client(self) -> wechat.ClawBot:
        """新建一个客户端（用例里手动塞 state 时用）。**必须带上假的 sayings**。"""
        return wechat.ClawBot(self.cfg, self.bot, self.sayings)

    def login(self, client: wechat.ClawBot, states: list[dict] | None = None) -> None:
        self.server.qr_states = list(states or [
            {"status": "wait"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "bot_token": "TOKEN-xyz",
                "ilink_bot_id": "BOT-1",
                "ilink_user_id": "USER-1",
                "baseurl": self.base,
            },
        ])
        self.assertTrue(client.login(timeout_total=20))


def run_one_poll_cycle(client) -> None:
    """让 `_poll_loop()` 真的跑一轮就退出。

    直接 `client._stop.set()` 再调 `_poll_loop()` 是**什么都没测**：循环体的
    第一句就是 `while not self._stop.is_set()`。必须等第一轮跑完（或者抛了）再把
    停止标志立起来。
    """
    real = client.poll_once

    def wrapper():
        try:
            return real()
        finally:
            client._stop.set()

    client.poll_once = wrapper
    client._poll_loop()


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------


class TestSayings(unittest.TestCase):
    """回复末尾那句一言（用户 2026-10-07 要的）。

    这个模块的全部风险都在「它是个外网彩蛋」上：请求错了地方、失败了把回复弄丢、
    或者偷偷让测试去连外网。下面每条各钉一个。
    """

    def setUp(self) -> None:
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)

    def test_it_asks_the_url_the_user_named(self):
        """用户点的是 `v1.hitokoto.cn`。走 http 而不是他写的 https，是量出来的：
        `probe_sayings6.sh` 在容器里连打 6 次，https 只成 3/6（成功平均 5.87s，其余 8s 超时），
        http 6/6（平均 2.62s）—— 这是公开玩笑接口，不值得为 TLS 牺牲「每条回复都带一句」。"""
        self.assertEqual(sayings_mod.HITOKOTO_URL, "http://v1.hitokoto.cn/")
        s = sayings_mod.Sayings()
        with mock.patch.object(s._session, "get") as get:
            get.return_value = FakeResponse({"hitokoto": "心之所向，素履以往。"})
            self.assertEqual(s.fetch(), "心之所向，素履以往。")
        # 没设过「一言来源」= 默认那四类，且是**重复 c=** 的写法（见下一条用例）
        self.assertEqual(get.call_args[0][0], "http://v1.hitokoto.cn/?c=d&c=i&c=k&c=h")
        self.assertEqual(get.call_args[1]["timeout"], sayings_mod.TIMEOUT)
        # 真正要紧的是**回复路径上**的等待预算：现场取只肯等 WAIT 秒
        # （真机上这个接口 0.65~9 秒都出现过，见 probe_sayings3.sh）。
        self.assertLessEqual(sayings_mod.WAIT, 2.0)
        # 后台预取可以慢一点，但也不能无限等（它挂着的是后台线程）
        self.assertLessEqual(sayings_mod.TIMEOUT, 10.0)
        self.assertEqual(s.take.__defaults__, (None,))

    def test_the_categories_go_in_as_repeated_c_params(self):
        """用户 2026-10-09：「根据一言官方提供的分类使用方法，在设置里面增加一言的
        自定义源」。他先给的写法是 `?c=d,i,k,h` —— **真机回 HTTP 400**
        （`{"status":400,"message":"很抱歉，没有分类有句子符合长度区间。"}`），
        而 `?c=d` 单类正常。同一个参数重复给才是能用的写法：真机上照这个写法连打
        10 次，回来的 `type` 就是 d/i/k/h 混着的。所以这里钉死「重复 c=、没有逗号」。"""
        self.assertEqual(sayings_mod.source_url("d,i,k,h"),
                         "http://v1.hitokoto.cn/?c=d&c=i&c=k&c=h")
        self.assertNotIn(",", sayings_mod.source_url("d,i,k,h"))
        self.assertEqual(sayings_mod.source_url("i"), "http://v1.hitokoto.cn/?c=i")
        # 大小写、空格、顿号都认；重复的字母只算一次，顺序按用户写的
        self.assertEqual(sayings_mod.source_url("I k i"), "http://v1.hitokoto.cn/?c=i&c=k")
        self.assertEqual(sayings_mod.source_url("k、d"), "http://v1.hitokoto.cn/?c=k&c=d")
        # 留空 = 默认那四类（设置页上「留空＝d,i,k,h」就是这个意思）
        self.assertEqual(sayings_mod.source_url(""), sayings_mod.source_url("d,i,k,h"))
        # **完整网址原样用** —— 这就是「自定义源」那半边
        self.assertEqual(sayings_mod.source_url("https://example.com/x?a=1"),
                         "https://example.com/x?a=1")

    def test_an_unknown_category_is_an_error_not_a_silent_drop(self):
        """不认的字母要**报错**，不能悄悄丢掉：用户以为设了 `d,j`，实际只生效 d，
        这种安静的错误比报错难查一百倍（和 `key_problem()` 一个态度）。"""
        problem = sayings_mod.source_problem("d,j")     # j（网易云）不在用户给的清单里
        self.assertIn("j", problem)
        self.assertIn("抖机灵", problem)                # 顺手把可选项列出来
        self.assertEqual(sayings_mod.source_problem("d, i k"), "")
        self.assertEqual(sayings_mod.source_problem(""), "")            # 留空合法
        self.assertEqual(sayings_mod.source_problem("http://x/y"), "")  # 网址合法
        self.assertIn("空格", sayings_mod.source_problem("http://x/ y"))
        self.assertIn("网址", sayings_mod.source_problem("###"))        # 既不是字母也不是网址

    def test_it_keeps_one_connection_for_all_its_requests(self):
        """真机上每次重建 TLS 要多花 1.5 秒（probe_sayings2.sh：TCP 0.27s、TLS 1.47s），
        所以取句子必须走同一条 `requests.Session`，不能每次裸调 `requests.get`。"""
        s = sayings_mod.Sayings()
        self.assertIsInstance(s._session, requests.Session)
        with mock.patch.object(sayings_mod.requests, "get", side_effect=AssertionError("又裸调了")):
            with mock.patch.object(s._session, "get") as get:
                get.return_value = FakeResponse({"hitokoto": "复用连接"})
                s.fetch()
                s.fetch()
        self.assertEqual(get.call_count, 2, "两次都该走同一个 session")

    def test_a_prefetched_sentence_is_handed_over_without_asking_again(self):
        calls = []

        def fake(timeout=None):
            calls.append(timeout)
            return "池子里的句子"

        s = sayings_mod.Sayings(fetcher=fake)
        s._pool.append("池子里的句子")
        self.assertEqual(s.tail(), "\n\n**池子里的句子**")
        # 拿池子里的那句时不该同步请求（后台预取线程可能已经跑起来了，
        # 它调 fetcher 是不带 timeout 的，所以只看有没有 WAIT 那次同步请求）
        self.assertNotIn(sayings_mod.WAIT, calls)

    def test_only_a_short_wait_when_the_pool_is_empty(self):
        seen = []

        def fake(timeout=None):
            seen.append(timeout)
            return "现场取的句子"

        s = sayings_mod.Sayings(fetcher=fake)
        self.assertEqual(s.tail(), "\n\n**现场取的句子**")
        self.assertEqual(seen[0], sayings_mod.WAIT, "现场那次只等 WAIT 秒")

    def test_the_prefetch_keeps_at_most_a_couple_of_sentences(self):
        def fake(timeout=None):
            return "补一句"

        s = sayings_mod.Sayings(fetcher=fake)
        for _ in range(4):
            s._fill()  # 直接调同步那半段，绕开线程
        self.assertEqual(s._pool, ["补一句"] * sayings_mod.POOL)

    def test_one_prefetch_tops_the_whole_pool_up(self):
        """池子是一次补满的：真机上那接口时快时慢，一条回复只补一句的话，
        下一句回复经常还是空的。"""
        calls = []

        def fake(timeout=None):
            calls.append(timeout)
            return "补一句 %d" % len(calls)

        s = sayings_mod.Sayings(fetcher=fake)
        s.prefetch()
        for _ in range(100):  # 等后台线程收工
            if not s._busy and len(s._pool) >= sayings_mod.POOL:
                break
            time.sleep(0.02)
        self.assertEqual(s._pool, ["补一句 1", "补一句 2"][: sayings_mod.POOL])
        self.assertEqual(len(calls), sayings_mod.POOL, "补满就停手，不多打")

    def test_the_tail_is_the_sentence_after_a_blank_line(self):
        s = sayings_mod.Sayings(fetcher=lambda timeout=None: "心之所向，素履以往。")
        self.assertEqual(s.tail(), "\n\n**心之所向，素履以往。**")

    def test_a_sentence_with_newlines_cannot_break_the_layout(self):
        def fake(timeout=None):
            return " ".join(" 第一行\n第二行 ".split())

        s = sayings_mod.Sayings(fetcher=fake)
        self.assertEqual(s.tail(), "\n\n**第一行 第二行**")
        # 真正的换行归一在 fetch() 里，也钉一下
        s2 = sayings_mod.Sayings()
        with mock.patch.object(s2._session, "get") as get:
            get.return_value = FakeResponse({"hitokoto": " 第一行\n第二行 "})
            self.assertEqual(s2.fetch(), "第一行 第二行")

    def test_every_kind_of_failure_is_just_an_empty_tail(self):
        s = sayings_mod.Sayings()
        cases = [
            ("网络超时", requests.Timeout("timed out")),
            ("连不上", requests.ConnectionError("boom")),
        ]
        for name, exc in cases:
            with self.subTest(name):
                with mock.patch.object(s._session, "get", side_effect=exc):
                    self.assertEqual(s.fetch(), "")
        # HTTP 500
        with mock.patch.object(s._session, "get") as get:
            get.return_value = FakeResponse({}, status=500)
            self.assertEqual(s.fetch(), "")
        # 回了不是 JSON
        with mock.patch.object(s._session, "get") as get:
            get.return_value = FakeResponse(ValueError("not json"))
            self.assertEqual(s.fetch(), "")
        # JSON 里没有那句话 / 空白
        for payload in ({}, {"hitokoto": ""}, {"hitokoto": "   "}, [], None):
            with self.subTest(payload):
                with mock.patch.object(s._session, "get") as get:
                    get.return_value = FakeResponse(payload)
                    self.assertEqual(s.fetch(), "")

    def test_a_broken_fetcher_is_caught_one_layer_up(self):
        """`Sayings` 自己那个 HTTP 版本从不抛；但注入的实现万一抛，得有人在上面兜住
        —— 那就是 `handle_inbound` 里那层 try（见下面的收发用例）。"""

        def boom(timeout=None):
            raise RuntimeError("接口炸了")

        s = sayings_mod.Sayings(fetcher=boom)
        with self.assertRaises(RuntimeError):
            s.tail()

    def test_the_env_switch_turns_it_off_without_asking(self):
        calls = []

        def fake(timeout=None):
            calls.append(timeout)
            return "心之所向"

        s = sayings_mod.Sayings(fetcher=fake)
        with mock.patch.dict(os.environ, {"WOOLBOT_NO_SAYINGS": "1"}):
            self.assertEqual(s.tail(), "")
        # 关掉之后连后台预取都不该发（只看同步那次也算数，反正一次都不该有）
        self.assertEqual(calls, [], "关掉之后一次都不该取")
        self.assertEqual(s.tail(), "\n\n**心之所向**")

    def test_the_shipped_stub_never_touches_the_network(self):
        """`NoSayings` 是几百个用例赖以不连外网的东西，钉住它的行为。"""
        with mock.patch.object(sayings_mod.requests, "get") as get:
            self.assertEqual(sayings_mod.NoSayings().tail(), "")
        self.assertEqual(get.call_count, 0)

    def test_a_real_client_gets_the_real_sayings_by_default(self):
        """线上（`main.py` 里 `wechat.ClawBot(cfg.as_dict(), bot)`）用的是真家伙。"""
        client = wechat.ClawBot({"wechat": {"state_file": "data/none.json",
                                           "media_dir": "data/none"}}, FakeBot())
        self.assertIsInstance(client.sayings, sayings_mod.Sayings)


class TestPureFunctions(unittest.TestCase):
    def test_decode_aes_key_accepts_both_encodings(self):
        # base64(16 字节原始 key)
        self.assertEqual(
            wechat.decode_aes_key(base64.b64encode(AES_KEY).decode()), AES_KEY
        )
        # base64(32 字符 hex)
        self.assertEqual(
            wechat.decode_aes_key(base64.b64encode(AES_KEY.hex().encode()).decode()), AES_KEY
        )
        # 直接给 hex
        self.assertEqual(wechat.decode_aes_key(AES_KEY.hex()), AES_KEY)
        with self.assertRaises(ValueError):
            wechat.decode_aes_key("")

    def test_decrypt_matches_independent_implementation(self):
        cipher = aes_encrypt(PNG_1PX, AES_KEY)
        self.assertEqual(len(cipher) % 16, 0)
        self.assertEqual(wechat.decrypt_media(cipher, AES_KEY), PNG_1PX)

    def test_split_text_respects_limit(self):
        self.assertEqual(wechat.split_text(""), [])
        self.assertEqual(wechat.split_text("短"), ["短"])
        text = "\n".join(f"第{i}行" + "x" * 30 for i in range(80))
        chunks = wechat.split_text(text, limit=200)
        self.assertTrue(all(len(c) <= 200 for c in chunks), [len(c) for c in chunks])
        self.assertEqual("".join(c.replace("\n", "") for c in chunks),
                         text.replace("\n", ""))
        # 单行超长要硬切而不是原样吐出去
        self.assertTrue(all(len(c) <= 50 for c in wechat.split_text("y" * 500, limit=50)))

    def test_image_mime_sniffs_magic(self):
        self.assertEqual(wechat.image_mime(PNG_1PX), "image/png")
        self.assertEqual(wechat.image_mime(b"\xff\xd8\xff\xe0" + b"0" * 8), "image/jpeg")
        self.assertEqual(wechat.image_mime(b"RIFF0000WEBPVP8 "), "image/webp")
        self.assertEqual(wechat.image_mime(b"\x00\x01\x02\x03"), "image/jpeg")  # 兜底

    def test_classify_network_error(self):
        import requests

        self.assertEqual(wechat._classify(requests.exceptions.ConnectTimeout("x")), "timeout")
        self.assertEqual(
            wechat._classify(requests.exceptions.ConnectionError("getaddrinfo failed")), "dns"
        )
        self.assertEqual(
            wechat._classify(requests.exceptions.SSLError("certificate verify failed")), "tls"
        )
        self.assertEqual(
            wechat._classify(requests.exceptions.ConnectionError("Connection refused")), "tcp"
        )
        self.assertEqual(wechat._classify(RuntimeError("なんか")), "unknown")

    def test_random_uin_is_base64_of_decimal(self):
        value = wechat._rand_uin()
        self.assertEqual(base64.b64decode(value).decode().isdigit(), True)


# --------------------------------------------------------------------------
# 二维码渲染
# --------------------------------------------------------------------------


class TestQrRender(unittest.TestCase):
    """登录二维码是本地画出来的。

    协议给的 `qrcode_img_content` 是一个指向 JS 网页的 https 链接，下载不到图片，
    所以必须拿这个链接当载荷自己生成二维码（上游 bot.py 的 render_generated_qr
    也是这么兜底的）。这一条要是断了，用户就扫不了码、登不进去。
    """

    def test_render_png_is_a_valid_png(self):
        from app import qr

        blob = qr.render_png("https://liteapp.weixin.qq.com/q/abc?qrcode=deadbeef&bot_type=3")
        self.assertIsNotNone(blob)
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(blob.rstrip().endswith(b"IEND\xaeB`\x82"))

    def test_different_payloads_give_different_images(self):
        from app import qr

        a = qr.render_png("https://example.com/a")
        b = qr.render_png("https://example.com/b")
        self.assertNotEqual(a, b)

    def test_empty_and_blank_return_none(self):
        from app import qr

        self.assertIsNone(qr.render_png(""))
        self.assertIsNone(qr.render_png("   "))

    def test_long_payload_does_not_explode(self):
        """链接比一般短网址长得多，不能因为容量不够就抛异常。"""
        from app import qr

        blob = qr.render_png("https://liteapp.weixin.qq.com/q/" + "x" * 400)
        self.assertIsNotNone(blob)


# --------------------------------------------------------------------------
# 登录状态机
# --------------------------------------------------------------------------


class TestLogin(WeChatTestCase):
    def test_login_state_machine_saves_token(self):
        client = self.make_client()
        self.login(client)

        self.assertTrue(client.logged_in)
        self.assertEqual(client.state["bot_token"], "TOKEN-xyz")
        self.assertEqual(client.state["ilink_bot_id"], "BOT-1")
        self.assertTrue(Path(self.cfg["wechat"]["state_file"]).is_file())

        # 二维码要落地。qrcode_img_content 是 URL 不是 base64（上游踩坑 1），
        # 而且那个 URL 指向 JS 渲染的网页、下载只会拿到 HTML —— 所以要拿
        # URL 当载荷本地生成二维码，而不是把下载到的东西当图片存下来。
        self.assertIsNotNone(client.qr_file)
        self.assertTrue(client.qr_file.is_file())
        blob = client.qr_file.read_bytes()
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"), "应该是合法 PNG")
        self.assertNotEqual(blob, PNG_1PX, "不该是下载回来的那张假图，而应该是自己生成的")
        self.assertGreater(len(blob), len(PNG_1PX))
        self.assertTrue(client.qr_url.startswith("http"), "原始链接要留着给网页兜底显示")

        # 取二维码和轮询状态都不该带 Authorization
        first = self.server.calls("/ilink/bot/get_bot_qrcode")[0]
        self.assertNotIn("Authorization", first["headers"])
        self.assertEqual(first["query"].get("bot_type"), ["3"])
        self.assertIn("local_token_list", first["payload"])
        self.assertEqual(first["payload"]["base_info"]["channel_version"], wechat.CHANNEL_VERSION)

        status_calls = self.server.calls("/ilink/bot/get_qrcode_status")
        self.assertNotIn("Authorization", status_calls[0]["headers"])
        self.assertEqual(status_calls[0]["query"]["qrcode"], ["QR-1"])

    def test_login_uses_verify_code_from_file(self):
        # 容器里没有可交互终端，配对码只能从文件读
        code_file = Path(self.cfg["wechat"]["state_file"]).parent / "verify_code.txt"
        code_file.write_text("4242", encoding="utf-8")

        client = self.make_client()
        self.login(client, states=[
            {"status": "need_verifycode"},
            {"status": "scaned"},
            {
                "status": "confirmed",
                "bot_token": "TOKEN-2",
                "ilink_bot_id": "BOT-1",
                "ilink_user_id": "USER-1",
                "baseurl": self.base,
            },
        ])
        self.assertTrue(client.logged_in)
        self.assertFalse(code_file.exists(), "配对码用过之后必须删掉")

        calls = self.server.calls("/ilink/bot/get_qrcode_status")
        # 第一轮没带配对码，第二轮带上了
        self.assertNotIn("verify_code", calls[0]["query"])
        self.assertEqual(calls[1]["query"].get("verify_code"), ["4242"])

    def test_login_retries_after_expired_qr(self):
        client = self.make_client()
        self.login(client, states=[
            {"status": "expired"},
            {"status": "wait"},
            {
                "status": "confirmed",
                "bot_token": "TOKEN-3",
                "ilink_bot_id": "BOT-1",
                "baseurl": self.base,
            },
        ])
        self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 2)
        self.assertTrue(client.logged_in)

    def test_binded_redirect_without_local_token_does_not_fake_success(self):
        client = self.make_client()
        self.login(client, states=[
            {"status": "binded_redirect"},
            {"status": "wait"},
            {
                "status": "confirmed",
                "bot_token": "TOKEN-4",
                "ilink_bot_id": "BOT-1",
                "baseurl": self.base,
            },
        ])
        # 本地没有 token 时 binded_redirect 不能被当成成功，必须重新扫码
        self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 2)

    # -- 页面上点「刷新二维码」 ------------------------------------------

    def _confirm_later(self, delay: float, token: str = "TOKEN-R") -> threading.Thread:
        """过一会儿把假服务器的状态切到 confirmed。

        登录循环里 qr_states 抽干了就永远回 wait，所以得从外面推进它。
        """
        def worker() -> None:
            time.sleep(delay)
            self.server.qr_states = [{
                "status": "confirmed",
                "bot_token": token,
                "ilink_bot_id": "BOT-1",
                "ilink_user_id": "USER-1",
                "baseurl": self.base,
            }]

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    def test_request_qr_refresh_makes_the_loop_ask_for_a_new_qr(self):
        """用户反馈过：点了「刷新二维码」没有任何反应。

        原来的按钮只是把磁盘上同一张 login_qr.png 重下一遍（qr_token 是文件
        mtime，没变就跳过），根本没有机制让登录循环重新去申请一张。
        """
        client = self.make_client()
        self.server.qr_states = [{"status": "wait"}] * 200

        threading.Thread(
            target=lambda: (time.sleep(0.4), client.request_qr_refresh()), daemon=True
        ).start()
        self._confirm_later(1.5, token="TOKEN-REFRESH")

        self.assertTrue(client.login(timeout_total=20))
        self.assertEqual(client.state["bot_token"], "TOKEN-REFRESH")
        self.assertGreaterEqual(
            len(self.server.calls("/ilink/bot/get_bot_qrcode")), 2,
            "手动刷新必须真的去申请一张新二维码",
        )

    def test_manual_refresh_is_not_counted_against_the_auto_limit(self):
        """refresh_limit 是防上游把二维码作废成死循环的，不是防用户的。

        refresh_limit=0 意味着「任何一次自动换码都算超限」，手动刷新得能穿过去。
        """
        client = self.make_client()
        self.server.qr_states = [{"status": "wait"}] * 200

        threading.Thread(
            target=lambda: (time.sleep(0.4), client.request_qr_refresh()), daemon=True
        ).start()
        self._confirm_later(1.2, token="TOKEN-MANUAL")

        self.assertTrue(client.login(timeout_total=20, refresh_limit=0))

    def test_a_stale_refresh_request_does_not_leak_into_the_next_login(self):
        """登录成功后残留的刷新请求，不能让下一次登录一进去就换码。"""
        client = self.make_client()
        client.request_qr_refresh()

        self.login(client)  # login() 开头必须 clear 掉它
        self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 1)

    def test_refresh_after_login_says_there_is_nothing_to_do(self):
        client = self.make_client()
        self.login(client)
        self.assertTrue(client.logged_in)

        client.request_qr_refresh()  # 不该抛异常，也不该影响已登录状态
        self.assertTrue(client.logged_in)
        self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 1)

    def test_logout_drops_the_stale_qr(self):
        """登出之后，那张旧二维码不能再挂在页面上。

        真机反馈的另一半：token 失效后页面还摆着那张已经作废的二维码，
        用户扫它当然没反应，还以为是手机或网络的问题。
        """
        client = self.make_client()
        self.login(client)
        self.assertIsNotNone(client.qr_file)

        client.logout(keep_cursor=True)
        self.assertIsNone(client.qr_file, "旧二维码的指针必须清掉")
        self.assertEqual(client.qr_url, "")

    def test_a_qr_refresh_wakes_the_backoff_sleep(self):
        """退避长睡期间点刷新也得立刻有反应。

        真机反馈（2026-10-07）：token 失效那条路要睡 `STALE_TOKEN_COOLDOWN`
        秒（一小时），页面上还挂着那张已经作废的旧二维码，用户点「刷新二维码」
        整整一小时没动静 —— 因为 `_sleep()` 只是 `self._stop.wait(seconds)`，
        刷新请求置了位也没人看。
        """
        client = self.make_client()
        threading.Timer(0.3, client.request_qr_refresh).start()

        t0 = time.monotonic()
        stopped = client._sleep(3600)
        self.assertFalse(stopped, "被刷新叫醒不是「该退出了」")
        self.assertLess(time.monotonic() - t0, 5.0, "必须提前醒，不能真睡满")

    def test_stop_wakes_the_backoff_sleep(self):
        """关容器时不该等满一个冷却周期。"""
        client = self.make_client()
        threading.Timer(0.3, client.stop).start()

        t0 = time.monotonic()
        self.assertTrue(client._sleep(3600))
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_the_poll_backoff_ignores_a_qr_refresh(self):
        """已经登录着的时候，刷新请求不该把长轮询的退避捅没。

        那时候人已经登录、二维码根本不需要；退了避才是对的 ——
        否则请求一直挂着，每次失败都立刻重试，白打接口。
        """
        client = self.make_client()
        client.request_qr_refresh()

        t0 = time.monotonic()
        self.assertFalse(client._sleep(1.0, wake_on_refresh=False))
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)

    def test_a_refresh_during_the_stale_token_cooldown_gets_a_new_qr(self):
        """真机那一幕的完整复现：-14 → 一小时冷却 → 途中点刷新。

        期望：刷新请求把冷却打断，立刻重新去申请一张二维码。
        """
        client = self.make_client()
        self.login(client)
        self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 1)

        # 下一次长轮询回 -14 → StaleToken → logout → 进入长冷却
        self.server.force_ret["/ilink/bot/getupdates"] = {"ret": -14, "errmsg": "expired"}
        self.server.qr_states = [{"status": "wait"}] * 500

        old = wechat.STALE_TOKEN_COOLDOWN
        wechat.STALE_TOKEN_COOLDOWN = 300.0  # 冷却真的长，只有被叫醒才可能提前
        t = threading.Thread(target=client.run, daemon=True)
        try:
            t.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and client.logged_in:
                time.sleep(0.05)
            self.assertFalse(client.logged_in, "-14 必须把它踢下线")
            time.sleep(0.3)  # 让它真的睡进冷却里

            # 冷却期间：一次新的二维码都还没申请（这就是用户看到的「没反应」）
            self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 1)

            client.request_qr_refresh()  # ← 用户点那个按钮

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and len(
                self.server.calls("/ilink/bot/get_bot_qrcode")
            ) < 2:
                time.sleep(0.05)
            self.assertGreaterEqual(
                len(self.server.calls("/ilink/bot/get_bot_qrcode")), 2,
                "冷却期间点刷新必须立刻重新申请二维码",
            )
        finally:
            wechat.STALE_TOKEN_COOLDOWN = old
            client.stop()
            t.join(timeout=10)

    def test_request_logout_drops_the_session_and_tells_wechat_to_stop(self):
        """点「退出微信」＝ 丢掉本地会话 + 跟服务端说一声「停」。

        能做的只有这些：协议里没有「解绑」，`notifystop` 是唯一那个「我不干了」
        的动作（页面上的确认框也是这么跟用户说的）。
        """
        client = self.make_client()
        self.login(client)
        self.assertTrue(client.logged_in)
        client.state["get_updates_buf"] = "CURSOR-1"  # 长轮询游标：一起丢

        client.request_logout()

        self.assertFalse(client.logged_in, "本地会话必须当场丢掉")
        # 注意清成**空串**而不是删键（`logout()` 就是这么写的），所以要断内容
        self.assertEqual(client.state.get("bot_token") or "", "")
        self.assertEqual(client.state.get("get_updates_buf") or "", "")
        self.assertTrue(client._logout.is_set(), "长轮询那一层要能看见这个标志")
        self.assertEqual(len(self.server.calls("/ilink/bot/msg/notifystop")), 1)

    def test_the_stale_token_caused_by_our_own_logout_skips_the_cooldown(self):
        """用户点「退出微信」之后那一发 -14 是**我们自己**造成的，不该吃一小时冷却。

        没有这一条，用户刚亲手断开的会话反而要替他守一小时的灵 —— 二维码
        一小时后才出现，页面上写着「正在向微信申请二维码，请稍候…」。
        """
        client = self.make_client()
        self.login(client)
        self.assertEqual(len(self.server.calls("/ilink/bot/get_bot_qrcode")), 1)

        entered = threading.Event()

        def poll_then_expire():
            # 真实时序：用户点退出时，那一发长轮询还在飞，回来是 -14
            entered.set()
            while not client._logout.is_set():
                time.sleep(0.01)
            raise wechat.StaleToken("token 被我们自己清掉了")

        client.poll_once = poll_then_expire
        self.server.qr_states = [{
            "status": "confirmed",
            "bot_token": "TOKEN-2",
            "ilink_bot_id": "BOT-1",
            "ilink_user_id": "USER-1",
            "baseurl": self.base,
        }] * 5

        old = wechat.STALE_TOKEN_COOLDOWN
        wechat.STALE_TOKEN_COOLDOWN = 300.0  # 真的长，只有跳过它才可能很快出新码
        t = threading.Thread(target=client.run, daemon=True)
        try:
            t.start()
            self.assertTrue(entered.wait(5), "长轮询没进去")

            client.request_logout()  # ← 用户点那个按钮
            self.assertFalse(client.logged_in)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(
                self.server.calls("/ilink/bot/get_bot_qrcode")
            ) < 2:
                time.sleep(0.02)
            self.assertGreaterEqual(
                len(self.server.calls("/ilink/bot/get_bot_qrcode")), 2,
                "退出微信之后必须立刻重新申请二维码，不能等一小时冷却",
            )
        finally:
            wechat.STALE_TOKEN_COOLDOWN = old
            client.stop()
            t.join(timeout=10)

    def test_the_logout_flag_does_not_leak_into_the_next_session(self):
        """标志只管到下一次登录：不然新码扫完了，一条消息都不收。

        坏掉的时序：`_logout` 一直立着 → `run()` 重新登录成功 → 进长轮询第一件事
        就是看这个标志 → 当场收工 → 回到顶上又登录状态为真 → 再进、再收工……
        表现就是「扫码成功但机器人装死」。所以 `run()` 进登录分支时必须清掉它。
        """
        client = self.make_client()
        self.login(client)                 # 会话 1
        client.request_logout()            # 用户点「退出微信」：标志立起来、token 丢掉
        self.assertTrue(client._logout.is_set())

        polled = threading.Event()

        def note_poll():
            polled.set()
            client._stop.set()             # 收到一次就够了，收工

        client.poll_once = note_poll
        self.server.qr_states = [{
            "status": "confirmed",
            "bot_token": "TOKEN-2",
            "ilink_bot_id": "BOT-1",
            "ilink_user_id": "USER-1",
            "baseurl": self.base,
        }] * 5

        t = threading.Thread(target=client.run, daemon=True)
        try:
            t.start()
            t.join(timeout=10)
            self.assertTrue(polled.is_set(), "退出后重新扫码的会话必须真的开始收消息")
            self.assertTrue(client.logged_in)
        finally:
            client.stop()
            t.join(timeout=5)


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------


class TestSend(WeChatTestCase):
    def test_business_requests_carry_auth_and_fresh_uin(self):
        client = self.make_client()
        self.login(client)
        client.send_text("USER-1", "你好", context_token="CTX-1")
        client.send_text("USER-1", "再来一条", context_token="CTX-1")

        call = self.server.calls("/ilink/bot/sendmessage")[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer TOKEN-xyz")
        self.assertEqual(call["headers"]["AuthorizationType"], "ilink_bot_token")
        self.assertEqual(call["headers"]["iLink-App-Id"], "bot")
        self.assertEqual(call["headers"]["iLink-App-ClientVersion"], wechat.CLIENT_VERSION)
        self.assertTrue(call["headers"]["X-WECHAT-UIN"])

        uins = [r["headers"]["X-WECHAT-UIN"] for r in self.server.log
                if "X-WECHAT-UIN" in r["headers"]]
        self.assertGreaterEqual(len(uins), 2)
        self.assertEqual(len(uins), len(set(uins)), "X-WECHAT-UIN 每次 POST 都要重新随机生成")

    def test_sendmessage_has_the_fields_that_make_it_actually_arrive(self):
        """上游最关键的坑：缺 from_user_id / client_id / base_info 就是静默失败。"""
        client = self.make_client()
        self.login(client)
        client.send_text("USER-1", "回复内容", context_token="CTX-9")

        record = self.server.calls("/ilink/bot/sendmessage")[0]
        payload = record["payload"]
        self.assertEqual(payload["base_info"]["channel_version"], wechat.CHANNEL_VERSION)

        msg = payload["msg"]
        self.assertEqual(msg["from_user_id"], "")
        self.assertEqual(msg["to_user_id"], "USER-1")
        self.assertTrue(msg["client_id"].startswith("woolbot-"))
        self.assertEqual(msg["message_type"], 2)
        self.assertEqual(msg["message_state"], 2)
        self.assertEqual(msg["context_token"], "CTX-9")
        self.assertEqual(msg["item_list"], [{"type": 1, "text_item": {"text": "回复内容"}}])

    def test_client_id_unique_per_send(self):
        client = self.make_client()
        self.login(client)
        client.send_text("USER-1", "一")
        client.send_text("USER-1", "二")
        ids = [r["payload"]["msg"]["client_id"] for r in self.server.calls("/ilink/bot/sendmessage")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_long_text_is_split_into_multiple_messages(self):
        client = self.make_client()
        self.login(client)
        text = "\n".join(f"第{i}条活动：京东超市满99减50，09:00 开抢" for i in range(120))
        self.assertTrue(client.send_text("USER-1", text))
        calls = self.server.calls("/ilink/bot/sendmessage")
        self.assertGreater(len(calls), 1)
        joined = "".join(c["payload"]["msg"]["item_list"][0]["text_item"]["text"] for c in calls)
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", ""))

    def test_send_returns_false_on_business_error(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/sendmessage"] = {"ret": 1001, "errmsg": "被限流"}
        self.assertFalse(client.send_text("USER-1", "试试"))
        self.assertEqual(self.server.calls("/ilink/bot/sendmessage")[0]["payload"]["msg"]["to_user_id"],
                         "USER-1")

    def test_context_token_falls_back_to_saved_one(self):
        client = self.make_client()
        self.login(client)
        client.state["context_tokens"]["USER-1"] = "CTX-SAVED"
        client.send_text("USER-1", "主动推送")
        record = self.server.calls("/ilink/bot/sendmessage")[0]
        self.assertEqual(record["payload"]["msg"]["context_token"], "CTX-SAVED")

    def test_typing_failure_does_not_block_send(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/sendtyping"] = {"ret": 999, "errmsg": "no"}
        self.assertTrue(client.send_text("USER-1", "正文照发"))
        self.assertEqual(len(self.server.calls("/ilink/bot/sendmessage")), 1)

    def test_notify_failure_is_swallowed(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/msg/notifystart"] = {"ret": 500, "errmsg": "busy"}
        client.notify("start")  # 不该抛
        self.assertEqual(len(self.server.calls("/ilink/bot/msg/notifystart")), 1)


# --------------------------------------------------------------------------
# 长轮询与错误语义
# --------------------------------------------------------------------------


class TestPolling(WeChatTestCase):
    def test_cursor_persisted_and_reused(self):
        client = self.make_client()
        self.login(client)
        self.server.updates = [{"ret": 0, "msgs": [], "get_updates_buf": "CURSOR-A"}]
        self.assertEqual(client.poll_once(), 0)

        self.assertEqual(client.state["get_updates_buf"], "CURSOR-A")
        self.assertEqual(self.server.calls("/ilink/bot/getupdates")[0]["payload"]["get_updates_buf"], "")

        # 重启后要接着上次的游标，而不是从头来
        reborn = self.new_client()
        self.assertEqual(reborn.state["get_updates_buf"], "CURSOR-A")
        client.poll_once()
        self.assertEqual(self.server.calls("/ilink/bot/getupdates")[1]["payload"]["get_updates_buf"],
                         "CURSOR-A")

    def test_server_suggested_timeout_is_adopted(self):
        client = self.make_client()
        self.login(client)
        self.server.updates = [{"ret": 0, "msgs": [], "get_updates_buf": "C",
                                "longpolling_timeout_ms": 60000}]
        client.poll_once()
        self.assertEqual(client.timeout_poll, 60.0)
        # 越界的值要被夹住
        self.server.updates = [{"ret": 0, "msgs": [], "get_updates_buf": "C",
                                "longpolling_timeout_ms": 10_000_000}]
        client.poll_once()
        self.assertLessEqual(client.timeout_poll, 120.0)

    def test_ret_14_raises_stale_token(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/getupdates"] = {"ret": -14, "errmsg": "stale token"}
        with self.assertRaises(wechat.StaleToken):
            client.poll_once()

    def test_errcode_14_also_counts_as_stale(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/sendmessage"] = {"ret": 0, "errcode": -14}
        with self.assertRaises(wechat.StaleToken):
            client.send_text("USER-1", "x")

    def test_other_ret_is_plain_error_not_stale(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/getupdates"] = {"ret": 1002, "errmsg": "别的错"}
        with self.assertRaises(wechat.WeChatError) as ctx:
            client.poll_once()
        self.assertNotIsInstance(ctx.exception, wechat.StaleToken)

    def test_non_json_response_raises_wechaterror(self):
        client = self.make_client()
        self.login(client)
        # 直接把响应替换成非 JSON
        original = self.server.respond
        self.server.respond = lambda m, p, r: (200, b"<html>502</html>", "text/html")  # type: ignore
        try:
            with self.assertRaises(wechat.WeChatError):
                client.poll_once()
        finally:
            self.server.respond = original  # type: ignore

    def test_poll_error_does_not_kill_the_loop(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/getupdates"] = {"ret": 1002, "errmsg": "抖一下"}
        run_one_poll_cycle(client)  # 不该抛
        self.assertEqual(client._consecutive_failures, 1, "失败要被记下来做退避")
        self.assertGreaterEqual(len(self.server.calls("/ilink/bot/getupdates")), 1)

    def test_network_timeout_is_normal_control_flow(self):
        """客户端超时属于长轮询的正常控制流，不能被计成失败。"""
        client = self.make_client()
        self.login(client)

        def boom(*args, **kwargs):
            import requests

            raise requests.exceptions.ReadTimeout("long poll timed out")

        client.session.post = boom  # type: ignore
        run_one_poll_cycle(client)
        self.assertEqual(client._consecutive_failures, 0, "超时不算失败")
        self.assertTrue(client.health()["polling"], "超时说明循环还活着")


class TestHealth(WeChatTestCase):
    """页面上的「微信已连接」必须反映「收得到消息」，而不只是「本地有 token」。

    token 失效后有 1 小时冷却，这段时间消息全在服务端队列里躺着，而页面上
    如果还写着「已连接」，用户会一直等 —— 这是最容易让人白等的一种错。
    """

    def test_before_any_poll_it_is_not_claimed_to_be_polling(self):
        client = self.make_client()
        self.login(client)
        h = client.health()
        self.assertTrue(h["logged_in"])
        self.assertFalse(h["polling"])
        self.assertIsNone(h["seconds_since_poll"])

    def test_a_successful_poll_marks_it_alive(self):
        client = self.make_client()
        self.login(client)
        run_one_poll_cycle(client)
        h = client.health()
        self.assertTrue(h["polling"])
        self.assertEqual(h["seconds_since_poll"], 0)
        self.assertEqual(h["last_error"], "")

    def test_a_stale_heartbeat_reports_not_polling(self):
        client = self.make_client()
        self.login(client)
        # 模拟「一小时冷却」：心跳停在很久以前
        client.last_poll_at = time.time() - 3600
        client.last_poll_error = "登录已失效，正在等待重新登录"
        h = client.health()
        self.assertTrue(h["logged_in"], "token 还在，所以仍然是已登录")
        self.assertFalse(h["polling"], "但页面要能看出收不到消息")
        self.assertGreaterEqual(h["seconds_since_poll"], 3600)
        self.assertIn("失效", h["last_error"])

    def test_the_alive_window_scales_with_the_long_poll_timeout(self):
        client = self.make_client()
        self.login(client)
        client.timeout_poll = 120.0
        client.last_poll_at = time.time() - 200  # 超过 3×35s 但没超过 3×120s
        self.assertTrue(client.health()["polling"])

    def test_a_failed_poll_records_why(self):
        client = self.make_client()
        self.login(client)
        self.server.force_ret["/ilink/bot/getupdates"] = {"ret": 1002, "errmsg": "服务端不开心"}
        run_one_poll_cycle(client)
        h = client.health()
        self.assertIn("服务端不开心", h["last_error"])
        self.assertFalse(h["polling"], "一次都没成功过，不能说自己活着")

    def test_health_never_raises_on_a_fresh_object(self):
        client = self.new_client()
        h = client.health()
        self.assertIn("polling", h)
        self.assertFalse(h["logged_in"])


# --------------------------------------------------------------------------
# 入站消息 → 业务层
# --------------------------------------------------------------------------


class TestInbound(WeChatTestCase):
    def _msg(self, **kw):
        base = {
            "message_id": "7001",
            "from_user_id": "USER-1",
            "to_user_id": "BOT-1",
            "message_type": 1,
            "message_state": 2,
            "context_token": "CTX-A",
            "item_list": [{"type": 1, "text_item": {"text": "京东超市满99减50，明天9点抢"}}],
        }
        base.update(kw)
        return base

    def test_text_message_reaches_bot_and_reply_is_sent(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg())

        self.assertEqual(len(self.bot.texts), 1)
        text, user_id, msg_id = self.bot.texts[0]
        self.assertEqual(text, "京东超市满99减50，明天9点抢")
        self.assertEqual(user_id, "USER-1")
        self.assertEqual(msg_id, "7001")

        sent = self.server.calls("/ilink/bot/sendmessage")
        self.assertEqual(sent[0]["payload"]["msg"]["item_list"][0]["text_item"]["text"],
                         self.bot.reply)
        self.assertEqual(sent[0]["payload"]["msg"]["context_token"], "CTX-A")
        self.assertEqual(sent[0]["payload"]["msg"]["to_user_id"], "USER-1")

    def test_every_reply_carries_a_hitokoto_line_at_the_end(self):
        """用户 2026-10-07：每条回复最后面隔一行附一句一言，照他写的 `**……**`。"""
        sayings = FakeSayings("心之所向，素履以往。")
        client = self.make_client()
        client.sayings = sayings
        self.login(client)
        client.handle_inbound(self._msg())

        sent = self.server.calls("/ilink/bot/sendmessage")
        self.assertEqual(sent[0]["payload"]["msg"]["item_list"][0]["text_item"]["text"],
                         self.bot.reply + "\n\n**心之所向，素履以往。**")
        self.assertEqual(sayings.calls, 1, "一次回复就该取一次")
        # 加上那一句之后仍然是纯文本一条消息，没有被拆成两条
        self.assertEqual(len(sent), 1)

    def test_a_broken_sayings_never_eats_the_reply(self):
        class Boom:
            def tail(self) -> str:
                raise RuntimeError("一言接口挂了")

        client = self.make_client()
        client.sayings = Boom()
        self.login(client)
        client.handle_inbound(self._msg())

        sent = self.server.calls("/ilink/bot/sendmessage")
        self.assertEqual(sent[0]["payload"]["msg"]["item_list"][0]["text_item"]["text"],
                         self.bot.reply, "彩蛋出问题不能把正经回复弄丢")

    def test_typing_on_before_and_off_after(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg())
        statuses = [r["payload"]["status"] for r in self.server.calls("/ilink/bot/sendtyping")]
        self.assertEqual(statuses, [1, 2])

    def test_typing_off_still_sent_when_bot_explodes(self):
        client = self.make_client()
        self.login(client)

        def boom(*a, **kw):
            raise RuntimeError("模型挂了")

        self.bot.handle_text = boom  # type: ignore
        with self.assertRaises(RuntimeError):
            client.handle_inbound(self._msg())
        statuses = [r["payload"]["status"] for r in self.server.calls("/ilink/bot/sendtyping")]
        self.assertEqual(statuses, [1, 2], "finally 里必须尽力取消输入状态")

    def test_context_token_saved_for_later_push(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg(context_token="CTX-NEW"))
        self.assertEqual(client.state["context_tokens"]["USER-1"], "CTX-NEW")
        reborn = self.new_client()
        self.assertEqual(reborn.state["context_tokens"]["USER-1"], "CTX-NEW")

    def test_group_message_is_skipped(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg(group_id="GROUP-9"))
        self.assertEqual(self.bot.texts, [])
        self.assertEqual(self.server.calls("/ilink/bot/sendmessage"), [])

    def test_unknown_item_type_is_not_treated_as_text(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg(item_list=[{"type": 99, "weird_item": {"x": 1}}]))
        self.assertEqual(self.bot.texts, [])

    def test_allowed_users_whitelist(self):
        self.cfg["wechat"]["allowed_users"] = ["USER-OK"]
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg())                       # USER-1 不在白名单
        client.handle_inbound(self._msg(from_user_id="USER-OK"))
        self.assertEqual([t[1] for t in self.bot.texts], ["USER-OK"])

    def test_bot_returning_empty_string_sends_nothing(self):
        self.bot.reply = ""
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._msg())
        self.assertEqual(self.server.calls("/ilink/bot/sendmessage"), [])

    def test_poll_once_dispatches_messages(self):
        client = self.make_client()
        self.login(client)
        self.server.updates = [{
            "ret": 0,
            "get_updates_buf": "CURSOR-B",
            "msgs": [self._msg(), self._msg(message_id="7002", from_user_id="USER-2")],
        }]
        self.assertEqual(client.poll_once(), 2)
        self.assertEqual([t[2] for t in self.bot.texts], ["7001", "7002"])
        self.assertEqual(client.state["get_updates_buf"], "CURSOR-B")

    def test_one_bad_message_does_not_stop_the_others(self):
        client = self.make_client()
        self.login(client)
        calls = {"n": 0}
        original = self.bot.handle_text

        def flaky(text, user_id="", msg_id=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("第一条炸了")
            return original(text, user_id=user_id, msg_id=msg_id)

        self.bot.handle_text = flaky  # type: ignore
        self.server.updates = [{
            "ret": 0, "get_updates_buf": "C",
            "msgs": [self._msg(), self._msg(message_id="7002")],
        }]
        self.assertEqual(client.poll_once(), 2)
        self.assertEqual(len(self.bot.texts), 1)
        self.assertEqual(self.bot.texts[0][2], "7002")


# --------------------------------------------------------------------------
# 图片：CDN 下载 + AES 解密
# --------------------------------------------------------------------------


class TestInboundImage(WeChatTestCase):
    def _image_msg(self, item_extra: dict, msg_id: str = "8001"):
        return {
            "message_id": msg_id,
            "from_user_id": "USER-1",
            "message_type": 1,
            "message_state": 2,
            "context_token": "CTX-IMG",
            "item_list": [{
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": "PARAM-1",
                        "aes_key": base64.b64encode(AES_KEY.hex().encode()).decode(),
                        "encrypt_type": 1,
                    },
                    "mid_size": len(aes_encrypt(PNG_1PX, AES_KEY)),
                    **item_extra,
                },
            }],
        }

    def setUp(self) -> None:
        super().setUp()
        self.cipher = aes_encrypt(PNG_1PX, AES_KEY)
        self.server.downloads["PARAM-1"] = self.cipher

    def test_image_is_downloaded_and_decrypted(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._image_msg({}))

        self.assertEqual(len(self.bot.images), 1)
        data, mime, user_id, msg_id = self.bot.images[0]
        self.assertEqual(data, PNG_1PX)
        self.assertEqual(mime, "image/png")
        self.assertEqual(user_id, "USER-1")
        self.assertEqual(msg_id, "8001#0")

        download = self.server.calls("/c2c/download")[0]
        self.assertEqual(download["query"]["encrypted_query_param"], ["PARAM-1"])
        self.assertEqual(download["method"], "GET")

    def test_image_aeskey_hex_field_is_supported(self):
        """image_item.aeskey 里可能直接放 hex，且优先于 media.aes_key。"""
        client = self.make_client()
        self.login(client)
        msg = self._image_msg({"aeskey": AES_KEY.hex()})
        client.handle_inbound(msg)
        self.assertEqual(self.bot.images[0][0], PNG_1PX)

    def test_image_with_base64_raw_key_is_supported(self):
        client = self.make_client()
        self.login(client)
        msg = self._image_msg({})
        msg["item_list"][0]["image_item"]["media"]["aes_key"] = base64.b64encode(AES_KEY).decode()
        client.handle_inbound(msg)
        self.assertEqual(self.bot.images[0][0], PNG_1PX)

    def test_image_with_full_url_skips_cdn_join(self):
        client = self.make_client()
        self.login(client)
        msg = self._image_msg({})
        msg["item_list"][0]["image_item"]["media"]["full_url"] = f"{self.base}/c2c/download?encrypted_query_param=PARAM-1"
        client.handle_inbound(msg)
        self.assertEqual(self.bot.images[0][0], PNG_1PX)
        self.assertEqual(self.server.calls("/c2c/download")[0]["path"], "/c2c/download")

    def test_undecryptable_image_falls_back_to_raw(self):
        """解不开时不能把整条消息丢了，退回原始字节让模型自己看。"""
        client = self.make_client()
        self.login(client)
        self.server.downloads["PARAM-1"] = b"not encrypted at all"
        client.handle_inbound(self._image_msg({}))
        self.assertEqual(self.bot.images[0][0], b"not encrypted at all")

    def test_image_download_failure_is_reported_not_swallowed(self):
        client = self.make_client()
        self.login(client)
        del self.server.downloads["PARAM-1"]           # 服务端 404
        client.handle_inbound(self._image_msg({}))
        self.assertEqual(self.bot.images, [])
        sent = self.server.calls("/ilink/bot/sendmessage")
        self.assertEqual(len(sent), 1)
        self.assertIn("没取到", sent[0]["payload"]["msg"]["item_list"][0]["text_item"]["text"])

    def test_media_saved_to_disk(self):
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._image_msg({}))
        media_dir = Path(self.cfg["wechat"]["media_dir"])
        files = list(media_dir.glob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), PNG_1PX)

    def test_media_not_saved_when_the_config_says_so(self):
        """关掉 `save_media` 之后磁盘上一个字节都不该多 —— 图还是要送给模型。

        这是 NAS 上正在跑的配置（不想让截图一直留在磁盘上），所以钉住它，
        免得将来哪次重构又悄悄开始落盘。
        """
        self.cfg["wechat"]["save_media"] = False
        client = self.make_client()
        self.login(client)
        client.handle_inbound(self._image_msg({}))
        media_dir = Path(self.cfg["wechat"]["media_dir"])
        leftovers = list(media_dir.glob("*")) if media_dir.exists() else []
        self.assertEqual(leftovers, [])
        # 不落盘 ≠ 不识别：字节照样送到了下游
        self.assertEqual(len(self.bot.images), 1)
        self.assertEqual(self.bot.images[0][0], PNG_1PX)
        self.assertEqual(self.bot.images[0][1], "image/png")

    def test_text_and_image_in_one_message_both_handled(self):
        client = self.make_client()
        self.login(client)
        msg = self._image_msg({})
        msg["item_list"].insert(0, {"type": 1, "text_item": {"text": "这个能抢吗"}})
        client.handle_inbound(msg)
        self.assertEqual(len(self.bot.texts), 1)
        self.assertEqual(len(self.bot.images), 1)
        # 两段回复合并进一条消息发出去
        record = self.server.calls("/ilink/bot/sendmessage")[0]
        self.assertIn(self.bot.reply, record["payload"]["msg"]["item_list"][0]["text_item"]["text"])

    def test_two_images_get_distinct_msg_ids(self):
        client = self.make_client()
        self.login(client)
        msg = self._image_msg({})
        msg["item_list"].append(msg["item_list"][0])
        client.handle_inbound(msg)
        self.assertEqual([i[3] for i in self.bot.images], ["8001#0", "8001#1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
