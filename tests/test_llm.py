"""app/llm.py 的 HTTP 层测试。

`tests/test_core.py` 用的是 FakeLLM 桩，整个 HTTP 层被绕过去了 —— 也就是说
「请求发得像不像 OpenAI 接口」「4xx 该不该重试」「模型回了一坨废话能不能抠出
JSON」这些从来没被真正跑过。这里起一个假的 OpenAI 兼容端点来补上。

不需要 API key，也不出网：全部打 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import unittest
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app import pipeline  # noqa: E402
from app import timeutil as tu  # noqa: E402
from app.llm import (  # noqa: E402
    CONFIRM_PROMPT,
    IMAGE_PROMPT,
    UNDERSTAND_PROMPT,
    LLM,
    LLMError,
    _extract_json,
    _fill,
)


def _reply(content: str) -> dict:
    """一个最小可用的 chat/completions 响应体。"""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakeAPI:
    """假的 OpenAI 兼容端点，按脚本回话并记录收到的请求。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 别把访问日志打到测试输出里
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = raw
                fake.calls.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "content_type": self.headers.get("Content-Type"),
                        "body": body,
                    }
                )

                idx = min(len(fake.calls) - 1, len(fake.responses) - 1)
                spec = fake.responses[idx] if fake.responses else (200, _reply("{}"))
                status, payload = spec[0], spec[1]
                if isinstance(payload, (dict, list)):
                    data, ctype = json.dumps(payload), "application/json"
                else:
                    data, ctype = str(payload), spec[2] if len(spec) > 2 else "text/plain"
                blob = data.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- 断言助手 -----------------------------------------------------

    def body(self, i: int = 0) -> dict:
        return self.calls[i]["body"]

    def count(self) -> int:
        return len(self.calls)


def make_llm(api: FakeAPI, **over) -> LLM:
    provider = {
        "base_url": api.base_url,
        "api_key": "sk-test-123",
        "model": "deepseek-v4-flash",
        "temperature": 0,
        "timeout": 10,
    }
    provider.update(over)
    return LLM(provider)


class ExtractJsonTest(unittest.TestCase):
    """模型回话的容错。真跑起来最常见的失败就是这里。"""

    def test_plain(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_fence_without_language(self):
        self.assertEqual(_extract_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_surrounded_by_chatter(self):
        self.assertEqual(_extract_json('好的，结果如下：\n{"a": 1}\n希望有帮助'), {"a": 1})

    def test_trailing_comma(self):
        self.assertEqual(_extract_json('{"a": 1, "b": [1, 2,],}'), {"a": 1, "b": [1, 2]})

    def test_nested_braces_survive(self):
        got = _extract_json('前言 {"a": {"b": 2}} 后语')
        self.assertEqual(got, {"a": {"b": 2}})

    def test_empty_raises(self):
        with self.assertRaises(LLMError):
            _extract_json("")

    def test_no_json_raises(self):
        with self.assertRaises(LLMError):
            _extract_json("我不知道该怎么回答")


class EndpointTest(unittest.TestCase):
    """base_url 写法的兼容 —— 用户会从各家文档里抄不同的形状。"""

    def _llm(self, base: str) -> LLM:
        return LLM({"base_url": base, "model": "m", "api_key": "k"})

    def test_bare_host_gets_v1_prefix(self):
        self.assertEqual(
            self._llm("https://api.deepseek.com")._endpoint(),
            "https://api.deepseek.com/v1/chat/completions",
        )

    def test_trailing_slash_is_tolerated(self):
        self.assertEqual(
            self._llm("https://api.deepseek.com/")._endpoint(),
            "https://api.deepseek.com/v1/chat/completions",
        )

    def test_explicit_v1(self):
        self.assertEqual(
            self._llm("https://x.test/v1")._endpoint(), "https://x.test/v1/chat/completions"
        )

    def test_full_path_passes_through(self):
        self.assertEqual(
            self._llm("https://x.test/v1/chat/completions")._endpoint(),
            "https://x.test/v1/chat/completions",
        )

    def test_missing_base_url_raises(self):
        with self.assertRaises(LLMError):
            LLM({"model": "m"})

    def test_missing_model_raises(self):
        with self.assertRaises(LLMError):
            LLM({"base_url": "https://x.test"})

    def test_vision_model_defaults_to_model(self):
        self.assertEqual(self._llm("https://x.test").vision_model, "m")


class UnderstandTest(unittest.TestCase):
    """录入方向：自然语言 → 结构化字段。"""

    def setUp(self):
        self.api = None

    def tearDown(self):
        if self.api:
            self.api.close()

    def test_posts_openai_shaped_payload(self):
        self.api = FakeAPI(
            (
                200,
                _reply(
                    json.dumps(
                        {
                            "intent": "ingest",
                            "activities": [{"title": "京东 满99减50", "grab_at": "2026-10-07T09:00"}],
                        },
                        ensure_ascii=False,
                    )
                ),
            )
        )
        got = make_llm(self.api).understand("京东明天9点满99减50")

        self.assertEqual(self.api.count(), 1)
        call = self.api.calls[0]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer sk-test-123")
        self.assertEqual(call["content_type"], "application/json")

        body = self.api.body()
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        # 提示词里要有系统时间锚点，否则「明天9点」算不出来
        self.assertIn("当前时间", body["messages"][1]["content"])
        # 也要带上用户原话
        self.assertIn("京东明天9点满99减50", body["messages"][1]["content"])
        self.assertEqual(got["intent"], "ingest")
        self.assertEqual(got["activities"][0]["grab_at"], "2026-10-07T09:00")

    def test_fenced_response_is_parsed(self):
        self.api = FakeAPI((200, _reply('```json\n{"intent": "query", "time": "09:30"}\n```')))
        got = make_llm(self.api).understand("9点半抢什么")
        self.assertEqual(got["intent"], "query")
        self.assertEqual(got["time"], "09:30")

    def test_missing_intent_falls_back_to_other(self):
        """模型漏了 intent 时不能让上层 KeyError。"""
        self.api = FakeAPI((200, _reply('{"reply": "我不知道"}')))
        self.assertEqual(make_llm(self.api).understand("你好")["intent"], "other")

    def test_confirm_sends_question_and_payload(self):
        self.api = FakeAPI((200, _reply('{"confirmed": true}')))
        got = make_llm(self.api).confirm(
            "是明天9点吗？", {"title": "京东 满99减50", "grab_at": "2026-10-07T09:00"}, "对"
        )
        self.assertTrue(got["confirmed"])
        prompt = self.api.body()["messages"][1]["content"]
        self.assertIn("是明天9点吗？", prompt)
        self.assertIn("京东 满99减50", prompt)
        self.assertIn("对", prompt)


class ImageTest(unittest.TestCase):
    """截图方向。发给模型的必须是多模态 parts，不是一段文字。"""

    def setUp(self):
        self.api = None

    def tearDown(self):
        if self.api:
            self.api.close()

    def test_sends_data_url_and_vision_model(self):
        self.api = FakeAPI(
            (200, _reply('{"intent": "ingest", "activities": [{"title": "淘宝 秒杀"}]}'))
        )
        png = b"\x89PNG\r\n\x1a\n" + b"payload"
        got = make_llm(self.api, vision_model="some-vl-model").understand_image(png, "image/png")

        body = self.api.body()
        self.assertEqual(body["model"], "some-vl-model")
        parts = body["messages"][1]["content"]
        self.assertIsInstance(parts, list)
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"), url[:40])
        # base64 部分要能还原回原图
        import base64 as b64

        self.assertEqual(b64.b64decode(url.split(",", 1)[1]), png)
        self.assertEqual(got["activities"][0]["title"], "淘宝 秒杀")

    def test_default_mime_is_jpeg(self):
        self.api = FakeAPI((200, _reply('{"intent": "ingest"}')))
        make_llm(self.api).understand_image(b"x")
        parts = self.api.body()["messages"][1]["content"]
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.api = None

    def tearDown(self):
        if self.api:
            self.api.close()

    def test_returns_plain_text_stripped(self):
        self.api = FakeAPI((200, _reply("  我可以帮你记活动。  ")))
        self.assertEqual(make_llm(self.api).chat("你能干啥"), "我可以帮你记活动。")

    def test_chat_does_not_require_json(self):
        """闲聊的回复本来就该是纯文字，不该被 _extract_json 卡住。"""
        self.api = FakeAPI((200, _reply("好的")))
        self.assertEqual(make_llm(self.api).chat("在吗"), "好的")


class ExplainHttpTest(unittest.TestCase):
    """错误码 → 人话。原来是把 DeepSeek 的英文报错原样甩进微信。"""

    def _explain(self, status, body=""):
        from app.llm import _explain_http

        return _explain_http(status, body, base_url="https://api.deepseek.com", model="m1")

    def test_401_tells_the_user_to_refill_the_key(self):
        kind, msg = self._explain(401, '{"error":{"message":"Authentication Fails"}}')
        self.assertEqual(kind, "auth")
        self.assertIn("API key", msg)
        self.assertIn("设置", msg)  # 得告诉用户去哪儿改
        self.assertIn("Authentication Fails", msg)  # 原文留一点，方便排查

    def test_403_counts_as_auth_too(self):
        self.assertEqual(self._explain(403)[0], "auth")

    def test_404_points_at_the_base_url(self):
        kind, msg = self._explain(404)
        self.assertEqual(kind, "notfound")
        self.assertIn("https://api.deepseek.com", msg)
        self.assertIn("/v1", msg)

    def test_400_blames_the_model_name(self):
        kind, msg = self._explain(400)
        self.assertEqual(kind, "model")
        self.assertIn("m1", msg)  # 报出当前用的模型名，用户才知道改什么

    def test_429_and_5xx_are_not_the_users_fault(self):
        self.assertEqual(self._explain(429)[0], "rate")
        self.assertIn("过一会儿", self._explain(429)[1])
        self.assertEqual(self._explain(503)[0], "server")
        self.assertIn("不是你配置的问题", self._explain(503)[1])

    def test_only_actionable_kinds_are_flagged_for_the_page(self):
        """限流和 5xx 不该在页面上报红 —— 报了用户也只能等。"""
        from app.llm import PAGE_ALERT_KINDS

        self.assertIn("auth", PAGE_ALERT_KINDS)
        self.assertIn("notfound", PAGE_ALERT_KINDS)
        self.assertIn("model", PAGE_ALERT_KINDS)
        self.assertNotIn("rate", PAGE_ALERT_KINDS)
        self.assertNotIn("server", PAGE_ALERT_KINDS)

    def test_unknown_status_still_says_something(self):
        kind, msg = self._explain(418, "teapot")
        self.assertEqual(kind, "other")
        self.assertIn("418", msg)
        self.assertIn("teapot", msg)

    def test_a_huge_body_is_truncated(self):
        _, msg = self._explain(401, "x" * 5000)
        self.assertLess(len(msg), 500)


class CheckExtractToolTest(unittest.TestCase):
    """`tools/check_extract.py` 自己也要有测试。

    它是对着真模型跑的，判错的代价是「以为预测对了」—— 那比没有它更糟。
    """

    def setUp(self):
        from tools import check_extract

        self.tool = check_extract
        self.today = date(2026, 10, 7)  # 周三

    def _case(self, **over):
        case = {"name": "x", "text": "明天上午9点半京东满99减50", "intent": "ingest"}
        case.update(over)
        return case

    def _got(self, **over):
        data = {
            "intent": "ingest",
            "activities": [{"title": "京东满99减50", "grab_at": "2026-10-08T09:30"}],
        }
        data.update(over)
        return data

    def _check(self, case, got, text=""):
        return self.tool._check(case, got, self.today, text)

    def test_a_correct_extraction_passes(self):
        problems, notes = self._check(self._case(grab=(1, "09:30")), self._got())
        self.assertEqual(problems, [])
        self.assertEqual(notes, [])

    def test_a_wrong_day_is_caught(self):
        """这正是这个工具存在的理由：模型把「明天」算成了今天。"""
        got = self._got(activities=[{"title": "x", "grab_at": "2026-10-07T09:30"}])
        # 不带原话（text=""）时没有兜底，就该直接报错
        problems, _notes = self._check(self._case(grab=(1, "09:30")), got)
        self.assertEqual(len(problems), 1)
        self.assertIn("2026-10-08T09:30", problems[0])
        self.assertIn("2026-10-07T09:30", problems[0])

    def test_a_day_the_fallback_fixes_becomes_a_note_not_a_failure(self):
        """模型把「明天」算成今天，但原话里写着「明天」→ Python 会拨回去。

        用户看到的结果是对的，所以不该报失败；可是模型那步不可靠，得留一句。
        """
        got = self._got(activities=[{"title": "x", "grab_at": "2026-10-07T09:30"}])
        problems, notes = self._check(
            self._case(grab=(1, "09:30")), got, self._case()["text"]
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("兜底", notes[0])

    def test_a_missing_grab_is_caught(self):
        got = self._got(activities=[{"title": "x"}])
        problems, _notes = self._check(self._case(grab=(1, "09:30")), got)
        self.assertTrue(problems)

    def test_a_wrong_intent_is_caught(self):
        problems, _notes = self._check(self._case(intent="query"), self._got())
        self.assertTrue(any("intent" in p for p in problems))

    def test_a_delete_rule_the_model_misses_is_a_note_not_a_failure(self):
        """模型把「删除梦享」判成了 query，但 Python 的动词兜底接得住 —— 用户照样能删。"""
        case = self._case(intent="delete", keyword="梦享", text="删除梦享")
        problems, notes = self._check(case, self._got(intent="query"), "删除梦享")
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("兜底", notes[0])

    def test_a_wrong_delete_keyword_is_caught(self):
        case = self._case(intent="delete", keyword="梦享", text="删除梦享")
        problems, _notes = self._check(case, {"intent": "delete", "keyword": ""}, "删除梦享")
        self.assertTrue(any("keyword" in p for p in problems))

    def test_a_sloppier_keyword_that_still_locates_it_passes(self):
        """关键词是拿去 LIKE 检索的，不是拿去比对的 —— 「梦享券」能定位「梦享」就够。"""
        case = self._case(intent="delete", keyword="梦享", text="删除梦享")
        problems, _notes = self._check(case, {"intent": "delete", "keyword": "梦享券"}, "删除梦享")
        self.assertEqual(problems, [])

    def test_a_wrong_clarify_flag_is_caught(self):
        case = self._case(clarify=True)
        problems, _notes = self._check(case, self._got())
        self.assertTrue(problems)
        problems, _notes = self._check(case, self._got(needs_clarify=True))
        self.assertEqual(problems, [])

    def test_a_wrong_multi_activity_count_is_caught(self):
        problems, _notes = self._check(self._case(count=2), self._got())
        self.assertTrue(problems)

    def test_an_empty_title_is_caught(self):
        """模型漏标题时 `_normalize_activity` 会兜底成「未命名活动」—— 必须被抓出来。"""
        got = self._got(activities=[{"title": "  ", "grab_at": "2026-10-08T09:30"}])
        problems, _notes = self._check(self._case(grab=(1, "09:30")), got)
        self.assertTrue(any("标题" in p for p in problems), problems)
        self.assertTrue(any(pipeline.TITLE_FALLBACK in p for p in problems), problems)

    def test_an_empty_title_is_not_flagged_when_the_bot_is_going_to_ask_back(self):
        """等着反问的用例，原话里本来就没有标题，而且反问不会落库。

        真踩到：用例「10 点开抢，到晚上 12 点结束」被判失败，可模型给的
        `needs_clarify` 完全正确 —— 这条「没抽到标题」是误报。
        """
        got = self._got(needs_clarify=True, activities=[{"title": "  ", "grab_at": "2026-10-08T09:30"}])
        problems, notes = self._check(self._case(clarify=True, grab=(1, "09:30")), got)
        self.assertEqual(problems, [])
        self.assertTrue(any("标题" in n for n in notes), notes)

    def test_a_reasonable_title_is_not_flagged(self):
        got = self._got(activities=[{"title": "京东超市满99减50", "grab_at": "2026-10-08T09:30"}])
        problems, _notes = self._check(self._case(grab=(1, "09:30")), got)
        self.assertEqual(problems, [])

    def test_a_case_without_expectations_only_checks_what_it_says(self):
        """没写期望的用例不该乱报 —— 模型换个说法也算对。"""
        problems, notes = self._check(self._case(), self._got())
        self.assertEqual(problems, [])
        self.assertEqual(notes, [])

    def test_the_builtin_cases_are_well_formed(self):
        names = [c["name"] for c in self.tool.CASES]
        self.assertEqual(len(names), len(set(names)), "用例名字重复了")
        for case in self.tool.CASES:
            self.assertTrue(case["name"])
            self.assertTrue(case["text"].strip())
            self.assertIn(case.get("intent"), ("ingest", "query", "other", "confirm", "delete"))

    def test_the_ingest_preview_keeps_everything_when_nothing_needs_fixing(self):
        """`_align_day` / `_repair_end` 是原地改、只返回「改过的那几条」。

        拿返回值覆盖 `acts` 的话，一条都不用改的时候预览会变成空的 —— 那正是
        「看着没问题、其实什么都没显示」的典型。这条把它钉住。
        """
        text = "明天上午9点半京东满99减50"
        got = {
            "intent": "ingest",
            "activities": [
                {"title": "京东满99减50", "grab_at": "2026-10-08T09:30", "end_at": "2026-10-08T23:59"}
            ],
        }
        with mock.patch("app.timeutil.today", return_value=self.today):
            preview = self.tool._ingest_preview(text, got)
        self.assertIn("京东满99减50", preview)
        self.assertIn("2026-10-08T09:30", preview)
        self.assertIn("2026-10-08T23:59", preview)

    def test_the_ingest_preview_walks_the_real_steps(self):
        """预览走的是入库前那几步（拨正日期、补 end、判过期），停在入库前。"""
        text = "明天上午9点半京东满99减50"
        got = {
            "intent": "ingest",
            "activities": [
                {
                    "title": "京东满99减50",
                    "grab_at": "2026-10-07T09:30",
                    "end_at": "2026-10-07T00:00",
                }
            ],
        }
        with mock.patch("app.timeutil.today", return_value=self.today):
            preview = self.tool._ingest_preview(text, got)
        self.assertIn("入库后会是这样", preview)
        # 「明天」把日子拨到了 8 号；end 写在当天 00:00 被修成次日 00:00
        self.assertIn("2026-10-08T09:30", preview)
        self.assertIn("2026-10-09T00:00", preview)


class ErrorHandlingTest(unittest.TestCase):
    """出错时的行为：什么该重试、什么该立刻放弃。"""

    def setUp(self):
        self.api = None

    def tearDown(self):
        if self.api:
            self.api.close()

    def _no_sleep(self):
        """退避是真等 1s+2s+4s，测试里打桩掉。"""
        return mock.patch("app.llm.time.sleep")

    def test_http_400_is_not_retried(self):
        """参数写错了，重试三次只是白等三倍时间。"""
        self.api = FakeAPI((400, {"error": "bad model"}))
        with self._no_sleep():
            with self.assertRaises(LLMError):
                make_llm(self.api).chat("hi")
        self.assertEqual(self.api.count(), 1)

    def test_http_401_is_not_retried(self):
        """key 不对也一样 —— 立刻报错，别让用户在微信里等。"""
        self.api = FakeAPI((401, {"error": "invalid key"}))
        with self._no_sleep():
            with self.assertRaises(LLMError):
                make_llm(self.api).chat("hi")
        self.assertEqual(self.api.count(), 1)

    def test_http_500_is_retried_then_succeeds(self):
        self.api = FakeAPI(
            (500, "server exploded"),
            (500, "server exploded"),
            (200, _reply("恢复了")),
        )
        with self._no_sleep():
            got = make_llm(self.api).chat("hi")
        self.assertEqual(got, "恢复了")
        self.assertEqual(self.api.count(), 3)

    def test_http_429_is_retried(self):
        """限流是唯一值得重试的 4xx。"""
        self.api = FakeAPI(
            (429, {"error": "rate limited"}),
            (200, _reply("好了")),
        )
        with self._no_sleep():
            got = make_llm(self.api).chat("hi")
        self.assertEqual(got, "好了")
        self.assertEqual(self.api.count(), 2)

    def test_always_failing_raises_after_retries(self):
        self.api = FakeAPI((503, "down"))
        with self._no_sleep():
            with self.assertRaises(LLMError):
                make_llm(self.api).chat("hi")
        self.assertEqual(self.api.count(), 3)

    def test_non_json_body_raises(self):
        """网关返回一页 HTML 是常见事故。"""
        self.api = FakeAPI((200, "<html>502 Bad Gateway</html>", "text/html"))
        with self.assertRaises(LLMError) as ctx:
            make_llm(self.api).chat("hi")
        self.assertIn("不是 JSON", str(ctx.exception))

    def test_error_field_in_200_raises(self):
        self.api = FakeAPI((200, {"error": {"message": "quota exceeded"}}))
        with self.assertRaises(LLMError) as ctx:
            make_llm(self.api).chat("hi")
        self.assertIn("quota exceeded", str(ctx.exception))

    def test_missing_choices_raises(self):
        self.api = FakeAPI((200, {"id": "x", "object": "chat.completion"}))
        with self.assertRaises(LLMError) as ctx:
            make_llm(self.api).chat("hi")
        self.assertIn("结构异常", str(ctx.exception))

    def test_empty_choices_raises(self):
        self.api = FakeAPI((200, {"choices": []}))
        with self.assertRaises(LLMError):
            make_llm(self.api).chat("hi")

    def test_blank_content_reaches_extract_json_and_raises(self):
        """模型回了个空串：要给得出错信息，不能静默当成 {}。"""
        self.api = FakeAPI((200, _reply("")))
        with self.assertRaises(LLMError):
            make_llm(self.api).understand("京东明天9点")

    def test_model_output_not_json_raises_with_snippet(self):
        self.api = FakeAPI((200, _reply("我觉得京东那个挺划算的")))
        with self.assertRaises(LLMError) as ctx:
            make_llm(self.api).understand("京东明天9点")
        self.assertIn("找不到 JSON", str(ctx.exception))

    def test_connection_error_is_retried(self):
        """端口没人听 = 网络错误，属于该重试的那类。"""
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()

        llm = LLM(
            {
                "base_url": f"http://127.0.0.1:{dead_port}",
                "api_key": "k",
                "model": "m",
                "timeout": 2,
            }
        )
        with self._no_sleep() as sleeper:
            with self.assertRaises(LLMError) as ctx:
                llm.chat("hi")
        self.assertEqual(sleeper.call_count, 3, "三次尝试各退避一次")
        self.assertIn("网络错误", str(ctx.exception))


class PromptTemplateTest(unittest.TestCase):
    """提示词里嵌着 JSON 示例，填充模板时不能把大括号当格式字段。

    这是真炸过一次的：`UNDERSTAND_PROMPT.format(anchor=...)` 抛
    `KeyError: '\\n  "intent"'`，于是 `understand()` **每次调用**都异常，
    录入和查询全废 —— 而桩掉 LLM 的测试完全看不见。
    """

    def test_fill_replaces_known_fields(self):
        self.assertEqual(_fill("现在是 {anchor} 哦", anchor="2026-10-06"), "现在是 2026-10-06 哦")

    def test_fill_leaves_json_braces_alone(self):
        got = _fill('示例：{"intent": "ingest"}', anchor="X")
        self.assertIn('{"intent": "ingest"}', got)

    def test_fill_leaves_unknown_fields_alone(self):
        self.assertEqual(_fill("{unknown} {anchor}", anchor="X"), "{unknown} X")

    def test_fill_does_not_rescan_inserted_text(self):
        """替换进去的值里若含 {anchor}，不能再被替换一遍。"""
        self.assertEqual(_fill("{anchor}", anchor="{anchor}"), "{anchor}")

    def test_every_prompt_survives_filling(self):
        for name, template, kwargs in (
            ("UNDERSTAND_PROMPT", UNDERSTAND_PROMPT, {"anchor": "A"}),
            (
                "CONFIRM_PROMPT",
                CONFIRM_PROMPT,
                {"anchor": "A", "question": "Q", "payload": "{}"},
            ),
            ("IMAGE_PROMPT", IMAGE_PROMPT, {"anchor": "A"}),
        ):
            with self.subTest(prompt=name):
                out = _fill(template, **kwargs)
                self.assertIn("A", out, f"{name} 没被填充")
                self.assertNotIn("{anchor}", out, f"{name} 里还有没换掉的 {{{{anchor}}}}")

    def test_understand_prompt_keeps_its_json_example(self):
        """示例必须原样出现在发给模型的提示词里，否则模型不知道要输出什么形状。"""
        out = _fill(UNDERSTAND_PROMPT, anchor="A")
        self.assertIn('"intent": "ingest"', out)
        self.assertIn('"grab_at": "2026-10-07T09:00"', out)
        self.assertNotIn("{{", out, "示例里出现了双大括号，模型会照着抄")

    def test_confirm_prompt_has_no_leftover_double_braces(self):
        """以前为了绕过 str.format 写了 {{{{ }}}}，改用 _fill 后必须还原成单括号。"""
        out = _fill(CONFIRM_PROMPT, anchor="A", question="Q", payload="P")
        self.assertNotIn("{{", out)
        self.assertNotIn("}}", out)
        self.assertIn('{"confirmed": true', out)

    def test_image_prompt_has_no_leftover_double_braces(self):
        out = _fill(IMAGE_PROMPT, anchor="A")
        self.assertNotIn("{{", out)
        self.assertNotIn("}}", out)
        self.assertIn('"raw_ocr"', out)


class ScreenshotEndToEndTest(unittest.TestCase):
    """截图录入：从「协议层解出来的图片字节」一路走到 SQLite 里的一行。

    这是 R3 的验收路径，也是唯一一条没人看着的缝：`test_wechat.py` 只验到
    「解密出来的字节交给了 handle_image」，`test_llm.py` 的 ImageTest 只验到
    「understand_image 发的请求对不对」，中间 `Bot.handle_image` 从没被跑过。
    """

    def setUp(self):
        import uuid

        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = base / f"shot-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        db.init(str(self.tmp / "t.db"))
        tu.configure("Asia/Shanghai")
        self.api = None

    def tearDown(self):
        if self.api:
            self.api.close()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def _api_returns(self, content: str) -> None:
        self.api = FakeAPI((200, _reply(content)))

    def _handle(self, png: bytes, msg_id: str = "") -> str:
        from app.pipeline import Bot

        bot = Bot(
            {"query": {"show_ongoing": True}}, make_llm(self.api)
        )
        return bot.handle_image(png, "image/png", "u1", msg_id)

    def test_screenshot_is_recognised_and_stored(self):
        day = tu.now().strftime("%Y-%m-%d")
        self._api_returns(
            json.dumps(
                {
                    "intent": "ingest",
                    "activities": [
                        {
                            "title": "淘宝百亿补贴 秒杀",
                            "platform": "淘宝",
                            "category": "数码",
                            "grab_at": f"{day}T09:30",
                            "end_at": f"{day}T12:00",
                            "confidence": 0.8,
                            "raw_ocr": "9:30 开抢 百亿补贴 秒杀 截止12:00",
                        }
                    ],
                    "needs_clarify": False,
                },
                ensure_ascii=False,
            )
        )
        png = b"\x89PNG\r\n\x1a\n" + b"fake-screenshot-bytes"
        reply = self._handle(png)

        self.assertIn("淘宝百亿补贴 秒杀", reply)
        rows = db.list_range(
            tu.parse_minute(f"{day}T00:00"), tu.parse_minute(f"{day}T23:59")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "image", "得记下它是从截图来的")
        self.assertEqual(rows[0]["grab_at"], f"{day}T09:30")
        self.assertEqual(rows[0]["end_at"], f"{day}T12:00")
        self.assertEqual(rows[0]["platform"], "淘宝")

    def test_screenshot_without_a_date_asks_instead_of_guessing(self):
        """图里没有日期时不能瞎猜一个时刻塞进日历。"""
        self._api_returns(
            json.dumps(
                {
                    "intent": "ingest",
                    "activities": [{"title": "京东 领券", "grab_at": None, "confidence": 0.4}],
                    "needs_clarify": True,
                    "clarify_question": "图里没写哪一天，是哪天抢？",
                },
                ensure_ascii=False,
            )
        )
        reply = self._handle(b"\x89PNG\r\n\x1a\nno-date")
        self.assertIn("哪一天", reply)
        self.assertEqual(db.stats()["total"], 0, "该问的时候不要入库")

    def test_screenshot_msg_id_is_idempotent(self):
        """同一条消息被重复投递（长轮询会重放）不能入两次库。"""
        day = tu.now().strftime("%Y-%m-%d")
        self._api_returns(
            json.dumps(
                {
                    "intent": "ingest",
                    "activities": [{"title": "美团 满30减15", "grab_at": f"{day}T10:00"}],
                },
                ensure_ascii=False,
            )
        )
        png = b"\x89PNG\r\n\x1a\nsame"
        first = self._handle(png, msg_id="9001#0")
        again = self._handle(png, msg_id="9001#0")
        self.assertIn("美团", first)
        self.assertEqual(again, "", "重复的消息应该被静默丢掉")
        self.assertEqual(db.stats()["total"], 1)
        self.assertEqual(self.api.count(), 1, "重复消息不该再打一次模型")

    def test_vision_model_failure_reports_instead_of_crashing(self):
        """模型挂了要在微信里说清楚，不能让长轮询线程断掉。"""
        from app.pipeline import Bot

        self.api = FakeAPI((500, "boom"))
        bot = Bot({}, make_llm(self.api))
        with mock.patch("app.llm.time.sleep"):
            reply = bot.handle_image(b"\x89PNG\r\n\x1a\nx", "image/png", "u1", "m9")
        self.assertIn("图片识别失败", reply)
        self.assertEqual(db.stats()["total"], 0)


class EndToEndTest(unittest.TestCase):
    """真实 HTTP 串一遍：模型回一段脏话（围栏+废话），流水线照样入库。"""

    def setUp(self):
        self.api = None

    def tearDown(self):
        if self.api:
            self.api.close()

    def test_pipeline_ingest_over_real_http(self):
        import uuid

        from app.pipeline import Bot

        base = Path(__file__).resolve().parent / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / f"llm-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            db.init(str(tmp / "t.db"))
            tu.configure("Asia/Shanghai")

            # 原话和模型都给「明天 09:00」，两处必须一致：录入侧有一道
            # 「日子按原话拨正」（`_align_day`），模型给今天而原话说「明天」
            # 时会被拨到明天。故意让它们打架的话，这条断言就跟跑测试的时刻有关了。
            target = (tu.now() + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
            day = target.date().isoformat()
            # 模型这次很脏：带 markdown 围栏、带前后废话、还有尾随逗号
            messy = (
                "好的，我帮你整理：\n```json\n"
                '{"intent": "ingest", "activities": [{"title": "京东超市 满99减50",'
                f' "platform": "京东", "category": "日用", "grab_at": "{day}T09:00",'
                ' "end_at": null, "confidence": 0.9,},], "needs_clarify": false}\n```\n'
                "需要的话我还能加提醒。"
            )
            self.api = FakeAPI((200, _reply(messy)))

            bot = Bot({}, make_llm(self.api))
            reply = bot.handle_text("京东明天9点满99减50", "u1", "m1")

            self.assertTrue(self.api.count() >= 1)
            self.assertIn("京东超市 满99减50", reply)
            rows = db.list_range(target.date(), target.date())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["grab_at"], f"{day}T09:00")
            self.assertEqual(rows[0]["platform"], "京东")
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
