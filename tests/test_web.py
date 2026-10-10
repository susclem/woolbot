"""`app/web.py` 的 HTTP 层测试：日历页面真正在用的那些读写接口。

为什么专门开一个文件：这套接口此前只有「手工用 urllib 打一遍」验过。
而当初手工打的时候才发现 `POST /api/activity/<id>`（也就是页面上「编辑 → 保存」
那个按钮）少写了一个星号、每次都 500 —— 也就是说页面上「改时间」这个功能
当时是坏的，日历却照常显示。

手工验过的东西不留测试，下次还会再犯一遍，而且下次未必有人去手工打。
所以这里按「页面会发的请求」逐条锁死，包括那些 400/403/404 的拒绝路径 ——
拒绝路径平时没人看，坏了也发现不了。

不依赖任何外部服务，跑法：`python tests/test_web.py`。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

from app import config as cfgmod, db, llm, pipeline as pl, sayings as sayings_mod, settings as st, timeutil as tu, web as webmod  # noqa: E402


def iso(day_offset: int, hhmm: str = "09:00") -> str:
    """相对今天 n 天的分钟串，和库里存的格式一致。"""
    d = tu.today() + timedelta(days=day_offset)
    return f"{d.isoformat()}T{hhmm}"


def _css_block(css: str, selector: str) -> str:
    """抠出某个选择器的整段规则体（大括号配平，能容纳嵌套）。

    深浅两套配色的变量名必须一一对应 —— 少写一个，那一处就悄悄留着白天的颜色
    （深色面板上蹦出一块浅色），光看截图不一定看得出是哪个变量漏了。
    """
    at = css.find(selector + " {")
    if at < 0:
        raise AssertionError(f"CSS 里没找到 {selector}")
    start = css.index("{", at)
    depth = 0
    for j in range(start, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[start:j + 1]
    raise AssertionError(f"{selector} 的大括号没配平")


def _font_px(css: str, selector: str) -> float:
    """抠出某个选择器的 font-size（只服务「日期字号要大于数量字号」那一条断言）。

    `[^}]*` 不会跨过 `}`，所以 `.cell.today .cell-day { color: … }` 这种同选择器
    但不带字号的规则不会被误当成答案。
    """
    m = re.search(re.escape(selector) + r"\s*\{[^}]*font-size:\s*([\d.]+)px", css)
    if not m:
        raise AssertionError(f"CSS 里没找到 {selector} 的 font-size")
    return float(m.group(1))


class WebApiTestBase(unittest.TestCase):
    TOKEN = "s3cret"

    def setUp(self) -> None:
        tu.configure("Asia/Shanghai")
        base = TESTS / ".tmp"
        base.mkdir(parents=True, exist_ok=True)
        # 不用 tempfile.mkdtemp()：本机沙箱下它建出来的目录不可写
        self.tmp = base / f"webapi-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_file = str(self.tmp / "t.db")
        db.init(self.db_file)
        # 设置是按库缓存的：换了库不丢缓存，上一个用例写的 web_token_required
        # 会跟着飘到下一个用例里（真踩过：冒烟用例读到上一个用例关掉的口令）。
        st.reset_cache()

        # port 0 必须真的走系统分配。以前 create_server 写的是
        # `int(port or 8080)`，0 被当成假值吃掉了，于是每个用例都去抢 8080，
        # 只有在 8080 恰好空着的时候才通过。
        self.httpd = webmod.create_server(
            {
                "db_path": self.db_file,
                "web": {"host": "127.0.0.1", "port": 0, "token": self.TOKEN},
            }
        )
        self.port = int(self.httpd.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        webmod.set_status_provider(None)
        st.reset_cache()
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- 请求助手 ------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        payload=None,
        *,
        token: str | None = None,
        raw: bytes | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """发一个请求，连 4xx 一起返回（不让 urllib 抛异常）。"""
        url = self.base + urllib.parse.quote(path, safe="/?&=%")
        if token is not None:
            url += ("&" if "?" in path else "?") + "token=" + token
        body = None
        hdrs = dict(headers or {})
        if raw is not None:
            body = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        elif payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), self._headers(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), self._headers(exc)

    @staticmethod
    def _headers(resp) -> dict[str, str]:
        """`dict(resp.headers)` 会**丢掉重复的 `Set-Cookie`，只留第一条**。

        服务端一次响应可能同时种两个 cookie（会话 + 设备号），所以这里把
        所有 `Set-Cookie` 拼成一条，用的人再按 `", "` 拆。
        """
        out = dict(resp.headers)
        all_cookies = resp.headers.get_all("Set-Cookie")
        if all_cookies:
            out["Set-Cookie"] = ", ".join(all_cookies)
        return out

    def ok(self, method: str, path: str, payload=None, **kw) -> dict:
        """带 token 发请求，断言 200，返回解析后的 JSON。"""
        kw.setdefault("token", self.TOKEN)
        code, body, _ = self.call(method, path, payload, **kw)
        self.assertEqual(code, 200, body[:400])
        return json.loads(body)

    def failing(self, code_want: int, method: str, path: str, payload=None, **kw) -> dict:
        """断言被拒绝，返回错误响应体。"""
        kw.setdefault("token", self.TOKEN)
        code, body, _ = self.call(method, path, payload, **kw)
        self.assertEqual(code, code_want, f"{method} {path} -> {code}: {body[:400]}")
        data = json.loads(body)
        self.assertFalse(data.get("ok"))
        self.assertIn("error", data)
        return data

    def seed(self, **over) -> int:
        data = {"title": "演示活动", "platform": "京东", "grab_at": iso(0, "09:00")}
        data.update(over)
        new_id = db.insert_activity(data)
        assert new_id, f"插入失败：{over}"
        return new_id


class ActivityWriteTest(WebApiTestBase):
    """页面上「新增 / 编辑 / 标记完成 / 删除」四个按钮走的路。"""

    def test_create_returns_201_and_is_readable(self):
        code, body, _ = self.call(
            "POST",
            "/api/activity",
            {"title": "京东 满99减50", "platform": "京东", "grab_at": iso(0, "09:30")},
            token=self.TOKEN,
        )
        self.assertEqual(code, 201, body[:400])
        new_id = json.loads(body)["id"]
        row = self.ok("GET", f"/api/activity/{new_id}")["data"]
        self.assertEqual(row["title"], "京东 满99减50")
        self.assertEqual(row["platform"], "京东")
        self.assertEqual(row["grab_at"], iso(0, "09:30"))
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["source"], "manual")  # 手工加的，不是消息来的

    def test_create_trims_the_title(self):
        new_id = json.loads(
            self.call("POST", "/api/activity", {"title": "  空格里逃生  "}, token=self.TOKEN)[1]
        )["id"]
        self.assertEqual(self.ok("GET", f"/api/activity/{new_id}")["data"]["title"], "空格里逃生")

    def test_create_without_a_title_is_rejected(self):
        for payload in ({}, {"title": ""}, {"title": "   "}, {"title": None}):
            with self.subTest(payload=payload):
                self.failing(400, "POST", "/api/activity", payload)
        self.assertEqual(db.stats()["total"], 0)

    def test_create_ignores_fields_that_are_not_columns(self):
        """前端多传 / 有人手改请求，不该让服务端按人家的字段写库。"""
        body = json.loads(
            self.call(
                "POST",
                "/api/activity",
                {"title": "带私货", "id": 999, "evil": "DROP TABLE", "created_at": "1970-01-01T00:00"},
                token=self.TOKEN,
            )[1]
        )
        self.assertNotEqual(body["id"], 999)
        row = self.ok("GET", f"/api/activity/{body['id']}")["data"]
        self.assertEqual(row["title"], "带私货")
        self.assertNotIn("evil", row)

    def test_create_with_a_body_that_is_not_json_becomes_an_empty_one(self):
        self.failing(400, "POST", "/api/activity", raw="这不是 JSON")
        self.failing(400, "POST", "/api/activity", raw=b"\xff\xfe\x00bad")

    def test_update_changes_exactly_the_fields_that_were_sent(self):
        """这条是回归测试：以前这里少写星号，永远 500。"""
        aid = self.seed(title="原标题", platform="京东", note="原备注", category="数码")
        res = self.ok("POST", f"/api/activity/{aid}", {"title": "新标题", "grab_at": iso(1, "20:00")})
        self.assertEqual(res["updated"], ["grab_at", "title"])
        row = self.ok("GET", f"/api/activity/{aid}")["data"]
        self.assertEqual(row["title"], "新标题")
        self.assertEqual(row["grab_at"], iso(1, "20:00"))
        self.assertEqual(row["platform"], "京东")  # 没传的字段不许被清掉
        self.assertEqual(row["note"], "原备注")
        self.assertEqual(row["category"], "数码")

    def test_update_can_clear_a_field_by_sending_an_empty_string(self):
        aid = self.seed(grab_at=iso(0, "09:00"))
        self.ok("POST", f"/api/activity/{aid}", {"grab_at": ""})
        self.assertEqual(self.ok("GET", f"/api/activity/{aid}")["data"]["grab_at"], "")

    def test_update_with_only_unknown_fields_is_rejected(self):
        aid = self.seed()
        self.failing(400, "POST", f"/api/activity/{aid}", {"nope": 1, "id": 5})
        self.assertEqual(self.ok("GET", f"/api/activity/{aid}")["data"]["title"], "演示活动")

    def test_update_with_an_empty_body_is_rejected(self):
        aid = self.seed()
        self.failing(400, "POST", f"/api/activity/{aid}", {})

    def test_update_on_a_missing_row_is_404(self):
        self.failing(404, "POST", "/api/activity/99999", {"title": "x"})

    def test_update_with_a_non_numeric_id_is_400(self):
        for bad in ("abc", "1.5", "1a"):
            with self.subTest(bad=bad):
                self.failing(400, "POST", f"/api/activity/{bad}", {"title": "x"})

    def test_status_accepts_exactly_the_three_legal_values(self):
        aid = self.seed()
        for status in ("done", "pending", "expired"):
            with self.subTest(status=status):
                res = self.ok("POST", f"/api/activity/{aid}/status", {"status": status})
                self.assertEqual(res["status"], status)
                self.assertEqual(self.ok("GET", f"/api/activity/{aid}")["data"]["status"], status)

    def test_status_rejects_anything_else_without_touching_the_row(self):
        aid = self.seed()
        for bad in ("finished", "DONE", "", None, "删库", "done;drop"):
            with self.subTest(bad=bad):
                self.failing(400, "POST", f"/api/activity/{aid}/status", {"status": bad})
        self.assertEqual(self.ok("GET", f"/api/activity/{aid}")["data"]["status"], "pending")

    def test_status_tolerates_surrounding_whitespace(self):
        """表单/手输难免带空格，剥掉再用；大小写则不放行（见上一条）。"""
        aid = self.seed()
        self.assertEqual(self.ok("POST", f"/api/activity/{aid}/status", {"status": " done "})["status"], "done")

    def test_status_on_a_missing_row_is_404(self):
        self.failing(404, "POST", "/api/activity/99999/status", {"status": "done"})

    def test_delete_then_the_row_is_gone(self):
        aid = self.seed()
        self.assertEqual(self.ok("DELETE", f"/api/activity/{aid}")["deleted"], aid)
        self.failing(404, "GET", f"/api/activity/{aid}")
        self.assertEqual(db.stats()["total"], 0)

    def test_delete_is_idempotent_only_the_first_time(self):
        aid = self.seed()
        self.ok("DELETE", f"/api/activity/{aid}")
        self.failing(404, "DELETE", f"/api/activity/{aid}")

    def test_delete_with_a_non_numeric_id_is_400(self):
        self.failing(400, "DELETE", "/api/activity/abc")


class ActivityReadTest(WebApiTestBase):
    """列表与筛选 —— 页面上的搜索框、状态下拉、平台下拉。"""

    def test_list_defaults_to_this_month_with_a_40_day_window(self):
        self.seed(grab_at=iso(0, "09:00"))
        res = self.ok("GET", "/api/activities")
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["from"], tu.today().replace(day=1).isoformat())
        self.assertEqual(res["today"], tu.today().isoformat())
        self.assertEqual(len(res["data"]), 1)

    def test_list_filters_by_status(self):
        self.seed(title="待抢的", grab_at=iso(0, "09:00"))
        done = self.seed(title="已完成的", grab_at=iso(0, "10:00"))
        db.set_status(done, "done")
        titles = lambda res: sorted(r["title"] for r in res["data"])  # noqa: E731
        self.assertEqual(titles(self.ok("GET", "/api/activities")), ["已完成的", "待抢的"])
        self.assertEqual(titles(self.ok("GET", "/api/activities?status=done")), ["已完成的"])
        self.assertEqual(titles(self.ok("GET", "/api/activities?status=pending")), ["待抢的"])
        # "" 与 all 都是「不过滤」，页面下拉的默认项
        self.assertEqual(len(self.ok("GET", "/api/activities?status=all")["data"]), 2)
        self.assertEqual(len(self.ok("GET", "/api/activities?status=")["data"]), 2)

    def test_list_filters_by_platform(self):
        self.seed(title="a", platform="京东", grab_at=iso(0, "09:00"))
        self.seed(title="b", platform="淘宝", grab_at=iso(0, "10:00"))
        res = self.ok("GET", "/api/activities?platform=淘宝")
        self.assertEqual([r["title"] for r in res["data"]], ["b"])
        self.assertEqual(self.ok("GET", "/api/activities?platform=all")["count"], 2)
        self.assertEqual(self.ok("GET", "/api/activities?platform=拼多多")["count"], 0)

    def test_list_keyword_matches_several_fields_case_insensitively(self):
        self.seed(title="京东超市 满99减50", platform="京东", grab_at=iso(0, "09:00"))
        self.seed(title="淘宝百亿补贴", platform="淘宝", note="限时2小时", grab_at=iso(0, "10:00"))
        hit = lambda kw: [  # noqa: E731
            r["title"] for r in self.ok("GET", f"/api/activities?keyword={kw}")["data"]
        ]
        self.assertEqual(hit("京东"), ["京东超市 满99减50"])  # 标题命中
        self.assertEqual(hit("限时"), ["淘宝百亿补贴"])  # 备注命中
        self.assertEqual(hit("补贴"), ["淘宝百亿补贴"])
        self.assertEqual(hit("不存在的东西"), [])
        self.assertEqual(len(self.ok("GET", "/api/activities?keyword=补%20贴")["data"]), 0)

    def test_list_swaps_a_reversed_range_instead_of_returning_nothing(self):
        self.seed(grab_at=iso(1, "09:00"))
        res = self.ok("GET", f"/api/activities?from={iso(3)[:10]}&to={iso(-3)[:10]}")
        self.assertEqual(res["from"], iso(-3)[:10])
        self.assertEqual(res["to"], iso(3)[:10])
        self.assertEqual(res["count"], 1)

    def test_list_ignores_garbage_dates(self):
        self.seed(grab_at=iso(0, "09:00"))
        for q in ("?from=昨天&to=明天", "?from=2026-13-45", "?from=&to="):
            with self.subTest(q=q):
                res = self.ok("GET", "/api/activities" + q)
                self.assertEqual(res["from"], tu.today().replace(day=1).isoformat())
                self.assertEqual(res["count"], 1)

    def test_list_respects_the_window(self):
        self.seed(title="今天", grab_at=iso(0, "09:00"))
        self.seed(title="很久以后", grab_at=iso(200, "09:00"))
        res = self.ok("GET", f"/api/activities?from={iso(0)[:10]}&to={iso(3)[:10]}")
        self.assertEqual([r["title"] for r in res["data"]], ["今天"])

    def test_list_never_leaks_datetime_objects(self):
        """`_jsonable` 要把解析出来的 *_dt 丢掉：JSON 里出现 datetime 说明漏了。"""
        self.seed(grab_at=iso(0, "09:00"), end_at=iso(0, "23:59"))
        row = self.ok("GET", "/api/activities")["data"][0]
        self.assertNotIn("grab_dt", row)
        self.assertNotIn("start_dt", row)
        self.assertNotIn("end_dt", row)
        self.assertIsInstance(row["grab_at"], str)

    def test_stats(self):
        self.seed(grab_at=iso(0, "09:00"))
        done = self.seed(grab_at=iso(1, "09:00"))
        db.set_status(done, "done")
        res = self.ok("GET", "/api/stats")["data"]
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["pending"], 1)
        self.assertEqual(res["done"], 1)

    def test_the_calendar_route_answers_with_lunar_and_holiday(self):
        """日历格子那行小字：农历、节气、节日、休/班。算法在 app/lunar.py，这里查接线。"""
        res = self.ok("GET", "/api/calendar?from=2026-09-30&to=2026-10-11")
        self.assertEqual(res["from"], "2026-09-30")
        self.assertEqual(res["to"], "2026-10-11")
        self.assertEqual(res["count"], 12)
        days = res["days"]
        self.assertEqual(len(days), 12)
        self.assertEqual(days["2026-10-01"]["festival"], "国庆节")
        self.assertEqual(days["2026-10-01"]["holiday"], "休")
        self.assertEqual(days["2026-10-01"]["kind"], "festival")
        self.assertEqual(days["2026-10-07"]["lunar_full"], "八月廿七")
        self.assertEqual(days["2026-10-08"]["term"], "寒露")
        self.assertEqual(days["2026-10-08"]["text"], "寒露")
        # 调休上班：10/10 是周六但要上班，农历九月初一
        self.assertEqual(days["2026-10-10"]["holiday"], "班")
        self.assertEqual(days["2026-10-10"]["text"], "九月")
        self.assertEqual(days["2026-10-11"]["holiday"], "")

    def test_the_calendar_defaults_to_the_same_window_as_the_list(self):
        """默认区间必须和 `/api/activities` 一致，否则格子边上一圈没有附注。"""
        res = self.ok("GET", "/api/calendar")
        today = tu.today()
        self.assertEqual(res["from"], today.replace(day=1).isoformat())
        listing = self.ok("GET", "/api/activities")
        self.assertEqual(res["from"], listing["from"])
        self.assertEqual(res["to"], listing["to"])

    def test_the_calendar_swaps_a_reversed_range_and_caps_the_span(self):
        res = self.ok("GET", "/api/calendar?to=2026-10-07&from=2026-10-01")
        self.assertEqual((res["from"], res["to"]), ("2026-10-01", "2026-10-07"))
        self.assertEqual(res["count"], 7)
        # 有人拿 from/to 拉满一个世纪也只是慢一点，不会把服务拖死
        huge = self.ok("GET", "/api/calendar?from=1900-01-01&to=2100-01-01")
        self.assertLessEqual(huge["count"], webmod._CALENDAR_MAX_DAYS + 1)

    def test_the_calendar_ignores_garbage_and_only_hands_back_strings(self):
        res = self.ok("GET", "/api/calendar?from=nonsense&to=nonsense")
        self.assertTrue(res["days"])
        for key, info in res["days"].items():
            self.assertRegex(key, r"^\d{4}-\d{2}-\d{2}$")
            for name, value in info.items():
                self.assertIsInstance(value, str, f"{key}.{name}")

    def test_the_calendar_survives_days_its_table_does_not_cover(self):
        """表只管 1900–2100：范围外面回空串，不能抛异常（一个月视图不该 500）。"""
        res = self.ok("GET", "/api/calendar?from=1899-12-25&to=1899-12-31")
        self.assertEqual(res["count"], 7)
        self.assertEqual(res["days"]["1899-12-31"]["text"], "")
        self.assertEqual(res["days"]["1899-12-31"]["lunar_full"], "")

    def test_the_day_panel_renders_what_is_still_claimable(self):
        """光后端给出 `claimable_days` 不够 —— 页面上得真的摆出来，不然还是白给。"""
        js = (TESTS.parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("claimable_days", js)
        self.assertIn("还能领的", js)
        self.assertIn('<div class="empty">这天没有开抢安排</div>', js)
        self.assertNotIn('<div class="empty">这天没有活动</div>', js)
        # 「还能领的」那节摆的是别的日子开抢的活动：详情里的时刻必须带上日子，
        # 否则会被读成「今天 10:00 开抢」。
        self.assertIn("grabDay !== day", js)
        self.assertIn("claim.map((r) => actRow(r))", js)

    def test_the_month_view_is_a_calendar_beside_a_list(self):
        """月视图 = 左边小日历 + 右边标题清单，点标题进详情（用户 2026-10-07 的要求）。

        旧版是一屏大日历 + 底下一条当日面板；「日历不要占据整个屏」就是冲着它来的。
        """
        html = (TESTS.parent / "web" / "index.html").read_text(encoding="utf-8")
        css = (TESTS.parent / "web" / "style.css").read_text(encoding="utf-8")
        js = (TESTS.parent / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="split"', html)
        for marker in ("sidePane", "sideHead", "sideBody", "monthView"):
            with self.subTest(marker=marker):
                self.assertIn(f'id="{marker}"', html)
        # 旧的当日面板必须整个消失，免得留两个入口各自渲染一套
        for gone in ("dayPanel", "dayList", "dayTitle"):
            with self.subTest(gone=gone):
                self.assertNotIn(f'id="{gone}"', html)
        # 日历侧的宽度是封顶的，不是自由拉伸的一整屏
        self.assertIn("minmax(0, 660px)", css)
        for rule in (".split", ".side-pane", ".act", ".detail", ".tgroup"):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)
        for fn in ("renderSidePane", "renderDetail", "backToList", "deleteRow"):
            with self.subTest(fn=fn):
                self.assertIn(f"function {fn}(", js)
        # ?id=25 深链：和 ?settings=1 / ?help=1 一样，方便发链接，也让截图验证点得进去
        self.assertIn("searchParams.get('id')", js)
        # ?day=2026-10-10 + syncUrl() 把它写回地址栏：换天也能分享
        self.assertIn("searchParams.get('day')", js)
        self.assertIn("url.searchParams.set('day', state.selected)", js)
        # 格子里没有时刻了（用户 2026-10-07：「把日历上的时间去掉，只保留日期右上角的
        # 活动数量小数字」）—— 时刻只在右栏清单里；CSS 里那几个类一起删，别留没人用的。
        self.assertIn('class="cell-day"', js)
        self.assertIn('class="cell-cnt"', js)
        self.assertNotIn('class="marks"', js)
        self.assertNotIn(".marks", css)
        self.assertNotIn(".mark ", css)
        # 两个数字分开摆：几号在左上、条数在右下，且「日期数字大一点、数量数字小一点」
        # （用户 2026-10-07：「挪到右下角吧，现在看着有点眼花」）。别再合成一个 cell-head。
        self.assertNotIn('class="cell-head"', js)
        self.assertNotIn(".cell-head", css)
        self.assertIn("justify-content: space-between", css)
        self.assertGreater(_font_px(css, ".cell-day"), _font_px(css, ".cell-cnt"),
                           "日期字号必须大于数量字号")
        # 格子里再加一条：农历/节气/节日（左下，只显示一条）+ 法定节假日的「休/班」小标
        # （右上角）。用户 2026-10-07：「给日历视图加入农历、二十四节气、中国节日、法定节假日」。
        self.assertIn('class="cell-note', js)
        self.assertIn('class="cell-top"', js)
        self.assertIn('class="cell-foot"', js)
        self.assertIn('class="cell-rest', js)
        self.assertIn(".cell-note", css)
        self.assertIn(".cell-rest", css)
        # 附注是后端算好的（app/lunar.py），页面只是取回来摆位置 —— 一份事实源。
        self.assertIn("/api/calendar", js)
        self.assertIn("state.days", js)
        self.assertIn("function dayNote(", js)
        # 右栏标题（点格子之后）和列表视图的日期标题都要带上这一行，不然切到列表就没了
        self.assertEqual(js.count("const note = dayNote(day);"), 2)
        self.assertIn("dayNote(key)", js)
        # 农历是按 42 格网格的首尾单独取的，不是用活动那个 ±7 天的区间：网格尾巴最长
        # 能伸到下个月 14 天，用活动区间末尾几格就只剩一个孤零零的几号（真机撞到过）。
        self.assertIn("calFrom", js)
        self.assertIn("calTo", js)
        self.assertIn("api(`/api/calendar?from=${calFrom}&to=${calTo}`)", js)
        # 顶栏那个「10月7日 周三」整个去掉了（用户 2026-10-07：「确定没有任何关联，就直接
        # 把那个10月7日周三去掉，桌面端也不要」）。它固定显示真实今天、不跟选中的日期走，
        # 留着容易让人以为跟当前视图有关；今天是哪天由日历上的高亮和「回到今天」表示。
        self.assertNotIn("todayLabel", html)
        self.assertNotIn("todayLabel", js)
        # 行首的 `.today`（顶栏那条）要没了；`.cell.today` / `.agenda-day.today` 是另一回事
        self.assertNotIn("\n.today {", css)

    def test_no_two_top_level_names_in_the_page_script(self):
        """同一个作用域里声明两次同名函数不会报错，只会**静默顶掉**前面那个。

        2026-10-07 加详情里的时间格式时踩的：新写的 `fmtDay(day)`（接 ISO 字符串）
        把上面那个老的 `fmtDay(date)`（接 Date 对象、`visibleRange()` 靠它拼查询区间）
        顶掉了 —— 于是 `/api/activities?from=NaN月NaN日`，整个页面白屏。
        而这套测试当时**全绿**：静态断言只看关键名字在不在。所以这里直接查重名。
        """
        js = (TESTS.parent / "web" / "app.js").read_text(encoding="utf-8")
        names = re.findall(r"^(?:async\s+)?(?:function|const|let)\s+([A-Za-z_$][\w$]*)", js, re.M)
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], f"app.js 顶层重名（后者会把前者静默顶掉）：{dupes}")

    def test_the_top_bar_swaps_the_title_for_a_day_night_switch(self):
        """左上角的「羊毛日历」换成白天/夜间开关（用户 2026-10-11）。

        两套配色本来就有（原来是 `@media (prefers-color-scheme: dark)`），这里翻成
        由 `<html data-theme="…">` 驱动 —— 系统偏好只在**没自己点过**的时候算数，
        否则按钮根本管不动一个媒体查询。
        """
        html = (TESTS.parent / "web" / "index.html").read_text(encoding="utf-8")
        css = (TESTS.parent / "web" / "style.css").read_text(encoding="utf-8")
        js = (TESTS.parent / "web" / "app.js").read_text(encoding="utf-8")

        # 四个字去掉，开关补上（`$('btnTheme')` 拿到 null 的话一点就报错）
        self.assertNotIn("<h1>", html)
        self.assertNotIn('class="brand"', html)
        self.assertIn('id="btnTheme"', html)
        self.assertIn("$('btnTheme').addEventListener('click', toggleTheme)", js)
        # 符号跟着**当前**模式走，白天 ☀️、夜间 🌙
        self.assertIn("btn.textContent = dark ? '🌙' : '☀️';", js)
        # ☼/☽ 是上一版的单色符号，2026-10-09 用户点名换成彩色 emoji。
        # 两版混着最难受：一个纯色一个彩色，切换时按钮会忽大忽小忽明忽暗。
        self.assertNotIn("☼", html)
        self.assertNotIn("☽", html)
        self.assertNotIn("☼", js)
        self.assertNotIn("☽", js)
        # HTML 里那个初始符号得和 JS 的白天分支一致 —— 它虽然只在一帧内可见，
        # 但对不上就是「先闪一下月亮再变太阳」
        self.assertIn(">☀️</button>", html)
        self.assertIn("localStorage.setItem(THEME_KEY, next)", js)
        # 没自己选过时跟随系统，但存过之后就只认存的那个
        self.assertIn("if (!themeIsSaved()) setTheme(", js)

        # <html data-theme> 得赶在样式表之前定好，否则深色会先闪一帧白底
        self.assertLess(html.index("data-theme"), html.index('rel="stylesheet"'))
        # 模式不能再由 CSS 的媒体查询决定（那样按钮就管不了它）
        self.assertNotIn("prefers-color-scheme", css)
        self.assertIn(':root[data-theme="dark"] {', css)
        # 原生控件（滚动条、下拉框、日期选择器）也得跟着翻
        self.assertIn("color-scheme: dark", _css_block(css, ':root[data-theme="dark"]'))

        light = set(re.findall(r"(--[\w-]+)\s*:", _css_block(css, ":root")))
        dark = set(re.findall(r"(--[\w-]+)\s*:", _css_block(css, ':root[data-theme="dark"]')))
        self.assertTrue(light, "第一段 :root 里应当就是白天的配色变量")
        # --radius 是圆角尺寸不是颜色，一套通用，不必在深色里重写
        self.assertEqual(light - dark - {"--radius"}, set(),
                         "这些变量夜间漏了定义，会留着白天的颜色")
        self.assertEqual(dark - light, set(), "夜间配色里有白天没有的变量")

    def test_get_with_a_non_numeric_id_is_400(self):
        self.failing(400, "GET", "/api/activity/oops")

    def test_get_on_a_missing_row_is_404(self):
        self.failing(404, "GET", "/api/activity/424242")

    # -- 「还能领的」要和机器人说的一样 ----------------------------------

    def test_a_long_running_coupon_is_claimable_on_the_days_it_does_not_open(self):
        """一张开抢日早过了、有效期到年底的券：日历得知道这段日子它天天能领。

        机器人会说「📌 不过这一天还能领的」，日历以前只说「这天没有活动」——
        两边各写一套日子判据就是这个下场。
        """
        self.seed(title="长效券", grab_at=iso(-3, "09:00"),
                  start_at=iso(-3, "09:00"), end_at=iso(20, "23:59"))
        row = self.ok("GET", f"/api/activities?from={iso(0)[:10]}&to={iso(2)[:10]}")["data"][0]
        self.assertEqual(row["claimable_days"], [iso(0)[:10], iso(1)[:10], iso(2)[:10]])

    def test_the_opening_day_itself_is_never_also_claimable(self):
        """同一天不能既进「开抢」又进「还能领」—— 那正是以前重复列的病根。"""
        self.seed(title="今天开抢", grab_at=iso(0, "09:00"),
                  start_at=iso(0, "09:00"), end_at=iso(3, "23:59"))
        row = self.ok("GET", f"/api/activities?from={iso(0)[:10]}&to={iso(2)[:10]}")["data"][0]
        self.assertNotIn(iso(0)[:10], row.get("claimable_days", []))
        self.assertIn(iso(1)[:10], row["claimable_days"])

    def test_a_one_shot_without_an_end_is_never_claimable(self):
        """没有有效期的一次性活动：过期之后就没有「还能领」这回事。"""
        self.seed(title="只开抢一次", grab_at=iso(-3, "09:00"))
        row = self.ok("GET", f"/api/activities?from={iso(-3)[:10]}&to={iso(2)[:10]}")["data"][0]
        self.assertNotIn("claimable_days", row)

    def test_a_coupon_that_has_not_opened_yet_is_not_claimable(self):
        """开抢在后天、有效期也到后天的券，今天不该出现在「还能领的」里。

        `end_at >= 今天` 曾经把它当成「已经在有效期内」，于是 10月6日 的面板里
        摆着一张写着「🎯 10:00 开抢」的卡 —— 会被读成今天 10:00。
        """
        self.seed(title="后天才开抢", grab_at=iso(2, "10:00"), end_at=iso(2, "22:00"))
        row = self.ok("GET", f"/api/activities?from={iso(0)[:10]}&to={iso(2)[:10]}")["data"][0]
        self.assertNotIn("claimable_days", row)

    def test_a_repeat_row_does_not_get_claimable_days(self):
        """重复型只在它自己那几天算开抢，不该再被说成「还能领」。"""
        self.seed(title="每天券", grab_at=iso(0, "09:00"),
                  repeat_kind="daily", repeat_at="09:00")
        row = self.ok("GET", f"/api/activities?from={iso(0)[:10]}&to={iso(2)[:10]}")["data"][0]
        self.assertNotIn("claimable_days", row)
        self.assertEqual(len(row["occurrences"]), 3)


class QuotaApiTest(WebApiTestBase):
    """「每周几次、每月几次」在接口这一层的样子。

    这一组钉的不是算法（那在 `test_core.py` 的 `QuotaTest` 里），而是**两组字段
    别串味**：排期那句 `repeat` 是要被重新解析的，配额 `quota_week`/`quota_month`
    是数字。混在一起写，用户再存一次就会被解析器改写。
    """

    def _create(self, data: dict) -> int:
        code, body, _ = self.call("POST", "/api/activity", data, token=self.TOKEN)
        self.assertEqual(code, 201, body[:400])
        return json.loads(body)["id"]

    def _daily(self, **over) -> int:
        data = {
            "title": "每天都能领的券",
            "grab_at": iso(0, "10:00"),
            "end_at": iso(20, "23:59"),
            "repeat": "每天 10:00",
        }
        data.update(over)
        return self._create(data)

    def _row(self, aid: int) -> dict:
        return self.ok("GET", f"/api/activity/{aid}")["data"]

    # -- 两组字段各走各的 --------------------------------------------------

    def test_the_quota_never_leaks_into_the_schedule_text(self):
        aid = self._daily(quota_week="2")
        row = self._row(aid)
        self.assertEqual(row["quota_week"], 2)
        self.assertEqual(row["repeat"], "🔁 每天 10:00 · 周限2次")
        # 回填给编辑框的是纯排期那句：带着「· 周限2次」填回去，用户再存一次就完了
        self.assertEqual(row["repeat_input"], "🔁 每天 10:00")

    def test_saving_the_schedule_again_does_not_disturb_the_quota(self):
        """编辑框改了时间、配额那两格没动（前端会整个省掉 key）—— 配额得留着。"""
        aid = self._daily(quota_week="2")
        self.ok("POST", f"/api/activity/{aid}", {"repeat": "每天 09:30"})
        row = self._row(aid)
        self.assertEqual(row["quota_week"], 2)
        self.assertEqual(row["repeat_at"], "09:30")

    def test_a_quota_read_out_of_the_note_survives_an_edit(self):
        """备注里认出来的配额不能被「箱子里空着」抹掉。

        前端只在「本来就有配额」时才发空串去清空，所以服务端这边收到的就是
        「压根没这个 key」——那种情况必须一个字节都不动。
        """
        aid = self._create({
            "title": "备注里写着配额的券",
            "grab_at": iso(0, "09:00"),
            "repeat": "每天 09:00",
            "note": "每日 09:00:00 起发放；单用户单周限领1张",
        })
        self.assertEqual(self._row(aid)["quota_week"], 1)
        self.ok("POST", f"/api/activity/{aid}", {"title": "改个名"})
        self.assertEqual(self._row(aid)["quota_week"], 1)

    def test_an_empty_quota_box_clears_it(self):
        aid = self._daily(quota_week="2")
        self.ok("POST", f"/api/activity/{aid}", {"quota_week": ""})
        self.assertIsNone(self._row(aid)["quota_week"])

    def test_clearing_the_schedule_clears_the_quota_too(self):
        """退回一次性活动之后，「每周限 2 次」就是个没有意义的残留。"""
        aid = self._daily(quota_week="2")
        self.ok("POST", f"/api/activity/{aid}", {"repeat": ""})
        row = self._row(aid)
        self.assertIsNone(row["quota_week"])
        self.assertNotIn("repeat", row)

    def test_a_bad_quota_is_rejected_loudly(self):
        """静默丢掉配额的后果是页面天天显示「还能领」，用户白跑一趟。"""
        for bad in ("abc", "0", "100", "-1"):
            self.failing(400, "POST", "/api/activity", {"title": "券", "quota_week": bad})
            self.failing(400, "POST", "/api/activity", {"title": "券", "quota_month": bad})

    # -- 按天记「领到了」 --------------------------------------------------

    def test_marking_a_day_feeds_the_weekly_quota(self):
        aid = self._daily(quota_week="1")
        today = iso(0)[:10]
        got = self.ok("POST", f"/api/activity/{aid}/done", {"day": today})
        self.assertEqual(got["done_days"], [today])
        self.assertEqual(got["status"], "done")
        week = got["quota"]["week"]
        self.assertEqual((week["limit"], week["done"], week["left"]), (1, 1, 0))
        self.assertTrue(week["reached"])
        # 自然周：周一到周日
        self.assertEqual(date.fromisoformat(week["from"]).weekday(), 0)
        self.assertEqual(
            date.fromisoformat(week["to"]) - date.fromisoformat(week["from"]),
            timedelta(days=6),
        )
        # 行级 status 一动不动 —— 重复活动根本不看它
        self.assertEqual(self._row(aid)["status"], "pending")

    def test_the_rest_of_a_full_week_reads_as_done(self):
        aid = self._daily(quota_week="1")
        today = iso(0)[:10]
        self.ok("POST", f"/api/activity/{aid}/done", {"day": today})
        rows = self.ok("GET", f"/api/activities?from={today}&to={iso(20)[:10]}")["data"]
        occ = rows[0]["occurrences"]
        self.assertTrue(occ, "每天 10:00 在这段区间里不该一场都没有")
        first = rows[0]["done_days"][0]
        for item in occ:
            self.assertEqual(
                item["status"], "done" if item["day"] == first or _same_week(item["day"], first) else "pending",
                item,
            )
        self.assertEqual([i["day"] for i in occ if i["done"]], [first])

    def test_a_full_week_drops_out_of_the_pending_list(self):
        """配额领满之后，本周剩下那几天不能再出现在「待抢」里 —— 点进去也领不到。"""
        aid = self._daily(quota_week="1")
        today = iso(0)[:10]
        got = self.ok("POST", f"/api/activity/{aid}/done", {"day": today})
        lo, hi = got["quota"]["week"]["from"], got["quota"]["week"]["to"]
        pending = self.ok("GET", f"/api/activities?from={lo}&to={hi}&status=pending")["data"]
        self.assertEqual([r["id"] for r in pending], [])
        done = self.ok("GET", f"/api/activities?from={lo}&to={hi}&status=done")["data"]
        self.assertEqual([r["id"] for r in done], [aid])

    def test_next_week_comes_back_by_itself(self):
        aid = self._daily(quota_week="1")
        today = iso(0)[:10]
        got = self.ok("POST", f"/api/activity/{aid}/done", {"day": today})
        after = date.fromisoformat(got["quota"]["week"]["to"]) + timedelta(days=1)
        pending = self.ok(
            "GET", f"/api/activities?from={after.isoformat()}&to={after.isoformat()}&status=pending"
        )["data"]
        self.assertEqual([r["id"] for r in pending], [aid], "下周一该自己恢复待抢")

    def test_a_done_day_can_be_taken_back(self):
        aid = self._daily(quota_week="1")
        today = iso(0)[:10]
        self.ok("POST", f"/api/activity/{aid}/done", {"day": today})
        got = self.ok("POST", f"/api/activity/{aid}/undo", {"day": today})
        self.assertEqual(got["done_days"], [])
        self.assertEqual(got["status"], "pending")
        self.assertFalse(got["quota"]["week"]["reached"])

    def test_the_day_has_to_be_a_date(self):
        aid = self._daily()
        self.failing(400, "POST", f"/api/activity/{aid}/done", {"day": "下周三"})
        self.failing(400, "POST", f"/api/activity/{aid}/done", {})

    def test_a_one_shot_still_flips_the_row_like_before(self):
        """一次性活动没有「哪一天」这回事 —— 老路 /status 的行为一个字都不能变。"""
        aid = self.seed(title="只抢一次", grab_at=iso(0, "09:00"))
        self.ok("POST", f"/api/activity/{aid}/done", {"day": iso(0)[:10]})
        self.assertEqual(self._row(aid)["status"], "done")
        self.ok("POST", f"/api/activity/{aid}/undo", {"day": iso(0)[:10]})
        self.assertEqual(self._row(aid)["status"], "pending")

    def test_done_days_come_back_with_the_row(self):
        aid = self._daily()
        self.ok("POST", f"/api/activity/{aid}/done", {"day": iso(0)[:10]})
        self.assertEqual(self._row(aid)["done_days"], [iso(0)[:10]])

    def test_a_one_shot_has_no_done_days_at_all(self):
        aid = self.seed(title="只抢一次", grab_at=iso(0, "09:00"))
        self.assertNotIn("done_days", self._row(aid))

    def test_deleting_an_activity_takes_its_done_days_with_it(self):
        aid = self._daily()
        day = iso(0)[:10]
        self.ok("POST", f"/api/activity/{aid}/done", {"day": day})
        self.ok("DELETE", f"/api/activity/{aid}")
        self.assertEqual(db.done_days(aid), [])


def _same_week(a: str, b: str) -> bool:
    """两个 ``YYYY-MM-DD`` 是不是同一自然周（周一起）。"""
    return date.fromisoformat(a).isocalendar()[:2] == date.fromisoformat(b).isocalendar()[:2]


class AuthTest(WebApiTestBase):
    """`web.token` 设了之后，除了 /api/health 都得带凭证。"""

    def test_every_api_route_refuses_an_anonymous_caller(self):
        aid = self.seed()
        cases = [
            ("GET", "/api/stats", None),
            ("GET", "/api/status", None),
            ("GET", "/api/help", None),
            ("GET", "/api/qr.png", None),
            ("GET", "/api/activities", None),
            ("GET", "/api/calendar", None),
            ("GET", f"/api/activity/{aid}", None),
            ("POST", "/api/activity", {"title": "偷偷加的"}),
            ("POST", f"/api/activity/{aid}", {"title": "偷偷改的"}),
            ("POST", f"/api/activity/{aid}/status", {"status": "done"}),
            ("DELETE", f"/api/activity/{aid}", None),
        ]
        for method, path, payload in cases:
            with self.subTest(method=method, path=path):
                # token=None 是关键：failing() 默认会带上 token
                data = self.failing(401, method, path, payload, token=None)
                # 反过来断言：错误里**一个字都不能提**。匿名打接口的人连「这个站有
                # 口令这回事」都不该知道（页面那层已经装成秘钥登录页了，JSON 里再
                # 提一句 `?token=` 就白装了）。
                self.assertEqual(data["error"], "未授权")
        # 一个字节都没被写进去
        self.assertEqual(self.ok("GET", f"/api/activity/{aid}")["data"]["title"], "演示活动")

    def test_health_is_reachable_without_a_token(self):
        """Docker 的 HEALTHCHECK 不带 token，它必须能过。"""
        code, body, _ = self.call("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_a_wrong_token_is_still_refused(self):
        self.failing(401, "GET", "/api/stats", token="wrong")

    def test_the_x_token_header_works(self):
        code, _, _ = self.call("GET", "/api/stats", headers={"X-Token": self.TOKEN})
        self.assertEqual(code, 200)

    def test_a_visit_with_the_token_sets_a_cookie_that_replaces_it(self):
        code, _, headers = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"{webmod.COOKIE_NAME}={self.TOKEN}", cookie)
        self.assertIn("HttpOnly" if "HttpOnly" in cookie else "SameSite=Lax", cookie)
        # 之后靠 cookie 就能访问，不用再带 token（页面刷新就是这个场景）
        code, _, _ = self.call("GET", "/api/stats", headers={"Cookie": f"{webmod.COOKIE_NAME}={self.TOKEN}"})
        self.assertEqual(code, 200)

    def test_the_status_row_and_qr_endpoint_are_reachable_with_the_token(self):
        code, body, _ = self.call("GET", "/api/status", token=self.TOKEN)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["data"], {"bot": "off"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """别自己跟着 303 走。

    urllib 默认会跟着重定向，而 `Set-Cookie` 是在**那一个** 303 响应里的：
    跟过去之后拿到的是一次没有 cookie 的 GET `/`，结果是 401 口令页 ——
    看起来像「登录接口坏了」，其实是测试自己把答案丢了。
    """

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


class LoginGateTest(WebApiTestBase):
    """访问秘钥这一层（用户 2026-10-09 定的，默认关）。

    拍板记在这里，改代码之前先读一遍：
    1. **整个页面都要登录才能看** —— 不是只管「设置」弹窗；
    2. **叠两层**：先过口令（`web.token`），再过秘钥，两个开关各自独立；
    3. 登录一次管 **7 天**（`st.LOGIN_SESSION_DAYS`，原来是 30 天）；
    4. **用户名没有了**：整页只剩一把秘钥；
    5. 秘钥错误保护：**同一 IP 或同一设备**任一错满 `LOGIN_MAX_FAILS` 次，
       **整站禁止访问 24 小时**（`/api/health` 除外，否则容器永远 unhealthy）；
    6. **口令页伪装成秘钥页**（用户 2026-10-09：「设置访问口令以后，这个页面要
       伪装成秘钥进入」；当天又返工一次：「如果开了口令，那个不带口令进的登录页
       就是假的，填正确的秘钥也不能通过，不能放行」）。于是**两张页面外观一模一样，
       认的东西不同**：没带对口令时看见的那张**什么都不认**（填对秘钥也一律「秘钥
       不对」，而且每次提交都算一次失败）；口令带对了之后看见的才是真登录页，
       认得秘钥。也正因为那张假页面谁都能看见、谁都能试，错误上限从 5 降到 2。
    """

    KEY = "S3cret-1"

    def setUp(self) -> None:
        super().setUp()
        webmod.reset_login_fails()   # 限流是模块级内存，用例之间会串

    def tearDown(self) -> None:
        webmod.reset_login_fails()
        super().tearDown()

    # -- 小工具 ---------------------------------------------------------

    def _enable(self, **over) -> None:
        """把秘钥登录开关打开。`st.update()` 会顺手让缓存失效，不用再 reset。"""
        values = {"login_enabled": True, "login_key": self.KEY}
        values.update(over)
        st.update(values)

    def _form(self, key=None, field="key", cookie=None, token=True) -> tuple[int, bytes, dict[str, str]]:
        """像浏览器那样发一次表单登录（`Content-Type` 是关键，服务端靠它分岔）。

        `token=False` 模拟**没带对口令时看见的那张假页面**：它是浏览器在没带对口令
        的时候看见的，提交上来自然也没有 `?token=`。这条路径必须**能到达**
        （`/api/login` 因此排在 `_check_auth` 前面），但它**什么都进不去** ——
        填对秘钥也一律「秘钥不对」，见 `test_the_disguised_page_will_not_take_the_key`。
        """
        body = urllib.parse.urlencode({field: self.KEY if key is None else key})
        url = self.base + "/api/login" + ("?token=" + self.TOKEN if token else "")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            method="POST",
            headers=headers,
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, resp.read(), WebApiTestBase._headers(resp)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), WebApiTestBase._headers(exc)

    @staticmethod
    def _header_cookie(headers: dict[str, str], name: str) -> str:
        """只从响应头里找某个 cookie 的名字，**找不到也不炸** —— 断言「没发」用。"""
        raw = headers.get("Set-Cookie", "")
        for one in raw.split(", "):
            name_, _, value = one.strip().partition("=")
            if name_ == name and value.split(";", 1)[0]:
                return value.split(";", 1)[0]
        return ""

    @staticmethod
    def _cookie(headers: dict[str, str], name: str) -> str:
        """从 `Set-Cookie` 里抠出 `name=…` 那一对（urllib 不给 CookieJar）。

        `_headers()` 把两个 cookie 拼在了一条里，所以先按 `", "` 拆开再找 ——
        直接 split(";") 会把第二条 cookie 的名字和第一条的 `HttpOnly` 粘在一起。
        """
        raw = headers.get("Set-Cookie", "")
        for one in raw.split(", "):
            name_, _, value = one.strip().partition("=")
            value = value.split(";", 1)[0]
            if name_ == name and value:
                return f"{name_}={value}"
        raise AssertionError(f"没种上 {name}：{raw!r}")

    @classmethod
    def _session_cookie(cls, headers: dict[str, str]) -> str:
        return cls._cookie(headers, st.SESSION_COOKIE)

    @classmethod
    def _other_cookie(cls, headers: dict[str, str]) -> str:
        """设备 cookie —— 秘钥错误保护按「同一台设备」封人靠的就是它。"""
        return cls._cookie(headers, st.DEVICE_COOKIE)

    # -- 用例 -----------------------------------------------------------

    def test_nothing_is_readable_before_login(self):
        """口令过了也不够 —— 没登录的人一个接口、一个页面都拿不到。"""
        self._enable()
        self.seed()
        for path in ("/api/stats", "/api/activities", "/api/calendar", "/api/help",
                     "/api/settings", "/api/status"):
            with self.subTest(path=path):
                data = self.failing(401, "GET", path)
                self.assertIn("登录", data["error"])
                self.assertTrue(data["login_required"])
        # 页面本身也换成登录表单，而不是把日历发下去再靠前端挡住
        code, body, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 401)
        page = body.decode("utf-8")
        self.assertIn('name="key"', page)
        self.assertNotIn('name="password"', page)   # 用户名 + 密码那一套没有了
        self.assertNotIn("monthView", page)

    def test_the_health_check_still_gets_through(self):
        """Docker 的 HEALTHCHECK 不带 cookie。它被拦下 = 容器永远 unhealthy。"""
        self._enable()
        code, body, _ = self.call("GET", "/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_the_token_page_never_prints_the_token(self):
        """口令页原来把 `?token=…`、config.json、settings 表**全印在页面上**。

        谁看见谁就知道有这么一道门、该去哪儿找钥匙。用户 2026-10-09 要的是
        「伪装成秘钥进入」，所以现在它和登录页是同一张脸。
        """
        self.seed()
        code, body, _ = self.call("GET", "/")
        self.assertEqual(code, 401)
        page = body.decode("utf-8")
        self.assertNotIn(self.TOKEN, page)
        self.assertNotIn("token=", page)
        # 一个字的口令线索都不给：不提 config.json、不提 settings 表、不提 web_token
        for leak in ("config.json", "settings 表", "web_token", "口令"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, page)
        # 装的这一层得是**能用的**：就是那张秘钥表单
        self.assertIn('name="key"', page)
        self.assertIn('action="/api/login"', page)

    def test_the_disguised_page_will_not_take_the_key(self):
        """没带对口令时那张是**假的**：填对的秘钥也一律「秘钥不对」。

        用户 2026-10-09 返工定的规矩 —— 第一版是「在那张表单上输秘钥真能进来」，
        那等于口令这层白设了。假页面唯一的功能是**消耗猜的人**。原文：「如果开了
        口令，那个不带口令进的登录页就是假的，填正确的秘钥也不能通过，不能放行」。
        """
        self._enable()                                  # 两层都开
        code, body, headers = self._form(token=False)    # 表单没有 ?token=，秘钥是对的
        self.assertEqual(code, 401)
        text = body.decode("utf-8")
        self.assertIn("秘钥不对", text)
        # 一个 cookie 都别想拿到：既没有会话 cookie，也没有口令 cookie
        self.assertEqual(self._header_cookie(headers, st.SESSION_COOKIE), "")
        self.assertEqual(self._header_cookie(headers, "woolbot_token"), "")
        # 而且长得和真登录页**一模一样** —— 看不出上面还有一层口令
        self.assertIn("请输入访问秘钥", text)
        self.assertIn('name="key"', text)

    def test_the_disguised_page_will_not_take_the_token_either(self):
        """假页面上连**口令本身**也不算数 —— 「什么都不认」就是什么都不认。

        只开口令、没开秘钥登录的机器上，那张页面同样是假的：主人靠 `?token=` 进来，
        站在这张假页面前面敲什么都没用。
        """
        self.seed()                                     # 只开口令，不开秘钥登录
        self.assertFalse(st.login_ready())
        code, body, headers = self._form(key=self.TOKEN, token=False)
        self.assertEqual(code, 401)
        self.assertIn("秘钥不对", body.decode("utf-8"))
        self.assertEqual(self._header_cookie(headers, "woolbot_token"), "")
        self.assertEqual(self._header_cookie(headers, st.SESSION_COOKIE), "")

    def test_the_real_page_takes_the_key_and_plants_both_cookies(self):
        """带对了口令之后看到的那张**才是真的**：输一次秘钥就一路进到里面。

        口令页不种口令 cookie（种了这层就等于不存在），所以成功的响应必须**自己
        补上**：会话 cookie（进秘钥那层）+ 口令 cookie（进大门口那层）。
        """
        self._enable()
        code, _, headers = self._form(token=True)        # 带 ?token= 的那张真页面
        self.assertEqual(code, 303)
        self.assertEqual(headers.get("Location"), "/")
        session = self._session_cookie(headers)
        token_cookie = self._header_cookie(headers, "woolbot_token")
        self.assertEqual(token_cookie, self.TOKEN)
        # 把这一趟拿到的两个 cookie 都带上，就是一整趟浏览器的样子
        both = {"Cookie": f"{webmod.COOKIE_NAME}={token_cookie}; {session}"}
        self.assertEqual(self.call("GET", "/api/stats", headers=both)[0], 200)
        code, body, _ = self.call("GET", "/", headers=both)
        self.assertEqual(code, 200)
        self.assertIn("monthView", body.decode("utf-8"))

    def test_a_wrong_key_on_the_real_page_says_so(self):
        """真登录页上填错 —— 只说「秘钥不对」，一样不提口令。"""
        self._enable()
        code, body, headers = self._form(key="nope", token=True)
        self.assertEqual(code, 401)
        text = body.decode("utf-8")
        self.assertIn("秘钥不对", text)
        for leak in ("口令", "token=", "config.json"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)
        self.assertEqual(self._header_cookie(headers, st.SESSION_COOKIE), "")

    def test_the_real_page_after_the_token_says_please_log_in(self):
        """口令那层认了、秘钥那层还没过时，别**沉默地**再渲染同一张表单。

        浏览器转一圈又回到**长得一模一样**的页面，一句话都没有的话看着就像
        「口令填错了」，再乱试两次就吃 24 小时禁令 —— 所以补一句「请先登录」，
        而且不能把「上面还有一层口令」说出来。
        """
        self._enable()                                   # 两层都开
        code, body, _ = self.call("GET", "/", token=self.TOKEN)
        self.assertEqual(code, 401)
        text = body.decode("utf-8")
        self.assertIn("请先登录", text)
        self.assertIn('name="key"', text)
        for leak in ("口令", "token=", "config.json"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)

    def test_two_mistakes_on_the_disguised_page_ban_the_ip(self):
        """伪装页谁都能看见，所以错误上限是 **2** 次（用户 2026-10-09 定）。"""
        self.seed()
        self.assertEqual(st.LOGIN_MAX_FAILS, 2)
        for _ in range(2):
            code, body, _ = self._form(key="guess", token=False)
            self.assertEqual(code, 401)
            self.assertNotIn(self.TOKEN, body.decode("utf-8"))
        # 第三次连正确的口令一起拒 —— 而且**整站**，不是只锁那张表单
        code, body, _ = self._form(key=self.TOKEN, token=False)
        self.assertEqual(code, 403)
        self.assertIn("暂时不许访问", body.decode("utf-8"))
        # 接口回 JSON（前端靠 `banned` 认出来），页面回那张「暂时不许访问」
        code, body, _ = self.call("GET", "/api/calendar", token=self.TOKEN)
        self.assertEqual(code, 403)
        payload = json.loads(body)
        self.assertTrue(payload["banned"])
        # 文案本身也算约定：「24 小时」后面**不要**多一个空格（`_ban_text` 自己带
        # 空格分隔，模板里再写一个就成了「还要等 24 小时 才能再访问」）
        self.assertEqual(payload["error"], "秘钥错太多次了，还要等 24 小时才能再访问。")
        self.assertEqual(payload["retry_after"], st.LOGIN_BAN_SECONDS)
        code, body, _ = self.call("GET", "/")
        self.assertEqual(code, 403)
        self.assertIn("暂时不许访问", body.decode("utf-8"))
        # Docker 的健康检查照旧放行
        self.assertEqual(self.call("GET", "/api/health")[0], 200)

    def test_the_disguised_page_records_fails_even_with_the_key_switch_off(self):
        """只开了口令、秘钥登录没开 —— 那张页面照样是「猜钥匙的地方」，一样记账。"""
        self.seed()
        for _ in range(2):
            self._form(key="guess", token=False)
        self.assertGreater(webmod._login_blocked(["ip:127.0.0.1"]), 23 * 3600)

    def test_logging_in_with_the_form_sets_a_session_cookie(self):
        self._enable()
        code, _, headers = self._form()
        # 303：刷新页面不能再问一次「要不要重新提交表单」
        self.assertEqual(code, 303)
        self.assertEqual(headers.get("Location"), "/")
        cookie = self._session_cookie(headers)
        # 7 天（用户 2026-10-09 从 30 天改的）
        self.assertEqual(st.LOGIN_SESSION_DAYS, 7)
        self.assertIn("Max-Age=" + str(st.LOGIN_SESSION_DAYS * 86400), headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        # 顺手也种了设备号 —— 没有它，秘钥错误保护就只能按 IP 记账
        self._other_cookie(headers)
        # 拿着这个 cookie，接口和页面都通了
        code, _, _ = self.call("GET", "/api/stats", token=self.TOKEN, headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        code, _, _ = self.call("GET", "/?token=" + self.TOKEN, headers={"Cookie": cookie})
        self.assertEqual(code, 200)

    def test_the_login_page_hands_out_a_device_number(self):
        """第一次打开登录页就发一个设备号 —— 秘钥错误保护要靠它认出「同一台设备」。"""
        self._enable()
        code, _, headers = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 401)
        device = self._other_cookie(headers)
        # 带着它再来一次，不该再发一个新的
        code, _, headers = self.call(
            "GET", "/?token=" + self.TOKEN, headers={"Cookie": device}
        )
        self.assertEqual(code, 401)
        self.assertNotIn(st.DEVICE_COOKIE, headers.get("Set-Cookie", ""))

    def test_a_wrong_key_is_refused(self):
        self._enable()
        code, body, _ = self._form(key="not-the-key")
        self.assertEqual(code, 401)
        self.assertIn("秘钥不对", body.decode("utf-8"))
        # 不告诉人「错在哪儿」的细节（哪个字符对、长度对不对），只说不符
        self.assertNotIn(self.KEY, body.decode("utf-8"))

    def test_the_old_password_field_still_works(self):
        """老书签／老脚本发的是 `password`。改名不该让它们一夜之间全失效。"""
        self._enable()
        # `_NoRedirect` 让 303 走 HTTPError 分支 —— 那正是「登录成功」的样子
        code, _, headers = self._form(field="password")
        self.assertEqual(code, 303)
        self.assertEqual(headers.get("Location"), "/")

    def test_too_many_failures_ban_the_ip_for_a_day(self):
        """开了外网域名，这个页面就挂在公网上；不限流等于把秘钥交给字典攻击。

        用户 2026-10-09 要的是「禁止访问 24 小时」—— 所以错满之后连**正确**的
        秘钥也不认，而且**整站**都进不去，不是只把登录按钮锁掉。
        """
        self._enable()
        # 先正常登进来拿一个**有效**的会话 cookie —— 后面要验它也会被禁
        cookie = self._session_cookie(self._form()[2])
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN,
                                   headers={"Cookie": cookie})[0], 200)

        for _ in range(st.LOGIN_MAX_FAILS):
            self._form(key="wrong")
        self.assertEqual(st.LOGIN_BAN_SECONDS, 24 * 3600)

        code, body, _ = self._form()                 # 正确的秘钥（第 3 次）
        self.assertEqual(code, 403)
        self.assertIn("24 小时", body.decode("utf-8"))
        self.assertIn("暂时不许访问", body.decode("utf-8"))

        # 「禁止访问」是整站的：接口也是 403，不是 401
        for path in ("/api/stats", "/api/calendar", "/api/settings"):
            with self.subTest(path=path):
                code, body, _ = self.call("GET", path, token=self.TOKEN)
                self.assertEqual(code, 403)
                data = json.loads(body)
                self.assertTrue(data["banned"])
                self.assertIn("24 小时", data["error"])
                self.assertGreater(data["retry_after"], 23 * 3600)

        # **带着有效会话 cookie 也一样被拦** —— 用户要的是「禁止访问 24 小时」，
        # 不是「没登录的才禁止」。所以禁令那一步排在会话检查前面。
        for path in ("/api/stats", "/", "/app.js"):
            with self.subTest(path=path, cookie="valid"):
                code, _, _ = self.call("GET", path, token=self.TOKEN,
                                       headers={"Cookie": cookie})
                self.assertEqual(code, 403)

        # 但 Docker 的健康检查得放行，否则容器永远 unhealthy
        code, _, _ = self.call("GET", "/api/health")
        self.assertEqual(code, 200)

    def test_closing_the_doors_lifts_the_ban(self):
        """禁令跟着「还有没有门」走：**两层都关掉**，门都拆了，禁什么禁。

        这也是真机上的自救通道（禁令只在进程内存里，重启容器同样能清）。只关
        「秘钥登录」那一层是不够的 —— 口令那层还站着，它照样是被猜的那张伪装页。
        """
        self._enable()
        for _ in range(st.LOGIN_MAX_FAILS):
            self._form(key="wrong")
        self.assertEqual(self._form()[0], 403)
        st.update({"login_enabled": False})
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN)[0], 403)
        st.update({"web_token_required": False})
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN)[0], 200)

    def test_the_ban_follows_the_device_not_just_the_address(self):
        """换 IP（手机切流量、家里重播宽带）也躲不掉：设备号同样在记账。"""
        self._enable()
        device = self._other_cookie(self._form()[2])
        # 设备号是**客户端带上来**才认得出来的：头一次访问的 POST 只有 IP 那条账
        # （不然所有「第一次就来 POST」的人会共用一个空号码，一个人错两次把别人一起关）
        self.assertEqual(webmod._login_blocked(["dev:" + device.split("=", 1)[1]]), 0)
        for _ in range(st.LOGIN_MAX_FAILS):
            self._form(key="wrong", cookie=device)
        # 由**另一个 IP** 来也一样 —— 测试里服务端只看得见 127.0.0.1，所以
        # 直接把设备那条账翻出来验：它确实单独记了一笔。
        self.assertGreater(
            webmod._login_blocked(["dev:" + device.split("=", 1)[1]]), 23 * 3600
        )
        # 而一台没记号的设备（换个浏览器、清掉 cookie）不受设备那条影响
        self.assertEqual(webmod._login_blocked(["dev:brand-new"]), 0)

    def test_a_good_key_after_a_few_mistakes_clears_the_count(self):
        """错一下记一下 —— 但登进来一次就该归零，别攒着以后一起算。"""
        self._enable()
        self._form(key="wrong")
        self.assertEqual(self._form()[0], 303)
        self.assertEqual(webmod._login_blocked(["ip:127.0.0.1"]), 0)

    def test_changing_the_key_invalidates_the_old_cookie(self):
        """会话值 = `sha256(盐|秘钥|版本号)`，所以改秘钥不用去清谁的会话。"""
        self._enable()
        code, _, headers = self._form()
        self.assertEqual(code, 303)
        cookie = self._session_cookie(headers)
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN,
                                   headers={"Cookie": cookie})[0], 200)
        st.update({"login_key": "Another-1"})
        code, body, _ = self.call("GET", "/api/stats", token=self.TOKEN,
                                  headers={"Cookie": cookie})
        self.assertEqual(code, 401)
        self.assertIn("登录", body.decode("utf-8"))

    def test_logout_clears_the_cookie(self):
        self._enable()
        code, _, headers = self._form()
        cookie = self._session_cookie(headers)
        code, _, out = self.call("POST", "/api/logout", token=self.TOKEN,
                                 headers={"Cookie": cookie})
        self.assertEqual(code, 200)
        self.assertIn(f"{st.SESSION_COOKIE}=;", out.get("Set-Cookie", ""))
        self.assertIn("Max-Age=0", out.get("Set-Cookie", ""))

    def test_logout_really_kills_the_session(self):
        """光发一条 `Max-Age=0` 只是让**这个**浏览器忘掉 cookie。

        真机上验过：退出之后拿旧 cookie 再打 `/api/calendar` 还是 200 —— 会话值
        是 `sha256(盐|账号|密码|版本号)`，不换版本号，抄走过 cookie 的人一直能用。
        """
        self._enable()
        cookie = self._session_cookie(self._form()[2])
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN,
                                   headers={"Cookie": cookie})[0], 200)
        self.call("POST", "/api/logout", token=self.TOKEN, headers={"Cookie": cookie})
        code, body, _ = self.call("GET", "/api/stats", token=self.TOKEN,
                                  headers={"Cookie": cookie})
        self.assertEqual(code, 401)
        self.assertIn("登录", body.decode("utf-8"))

    def test_logging_out_on_one_device_logs_out_the_others(self):
        """版本号是全库一个数，所以「退出登录」= 所有设备一起退。

        同一个秘钥登两次拿到的是**同一个值**（会话值就是个哈希，没有随机数），
        这正是退出必须换版本号的原因。
        """
        self._enable()
        phone = self._session_cookie(self._form()[2])
        laptop = self._session_cookie(self._form()[2])
        self.assertEqual(phone, laptop)
        for cookie in (phone, laptop):
            self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN,
                                       headers={"Cookie": cookie})[0], 200)
        self.call("POST", "/api/logout", token=self.TOKEN, headers={"Cookie": phone})
        for cookie in (phone, laptop):
            self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN,
                                       headers={"Cookie": cookie})[0], 401)
        # 退出之后用同一个秘钥还能再登进来，不是把人锁在外面
        again = self._session_cookie(self._form()[2])
        self.assertNotEqual(again, phone)
        self.assertEqual(self.call("GET", "/api/stats", token=self.TOKEN,
                                   headers={"Cookie": again})[0], 200)

    def test_a_login_can_also_be_posted_as_json(self):
        """页面走表单，脚本/探针走 JSON —— 不必为了测一次登录去拼表单编码。"""
        self._enable()
        code, body, headers = self.call(
            "POST", "/api/login",
            {"key": self.KEY},
            token=self.TOKEN,
        )
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn(st.SESSION_COOKIE, headers.get("Set-Cookie", ""))

    def test_without_a_key_the_page_stays_open(self):
        """开关开了但秘钥没填：`login_ready()` 是假，**绝不能**把人锁在外面。

        「既进不去、又没设防」是最糟的中间态，所以缺一个就当没开。
        """
        self._enable(login_key="")
        self.assertFalse(st.login_ready())
        self.assertEqual(self.call("GET", "/?token=" + self.TOKEN)[0], 200)

    def test_without_the_token_the_login_page_is_still_out_of_reach(self):
        """两层是**叠着**的：先口令。

        但「没口令」和「没登进来」看见的是**同一张表单**了（用户 2026-10-09 要求
        口令页伪装成秘钥页），所以这里不能再断言「看不到 `name="key"`」—— 要断言
        的是它**装得和登录页一模一样**、且不给任何口令线索。这张是**假的**：
        填什么都不放行，见 `test_the_disguised_page_will_not_take_the_key`。
        """
        self._enable()
        code, body, _ = self.call("GET", "/")
        self.assertEqual(code, 401)
        page = body.decode("utf-8")
        self.assertIn('name="key"', page)
        self.assertNotIn(self.TOKEN, page)
        self.assertNotIn("token=", page)
        # 而且**没种口令 cookie** —— 在这一层把口令发出去，这道门就等于不存在
        _, _, headers = self.call("GET", "/")
        self.assertEqual(self._header_cookie(headers, "woolbot_token"), "")


class HelpTest(WebApiTestBase):
    """用户 2026-10-07 要的「使用帮助」：页面上能看到，微信里发一句也能拿到。

    文案 2026-10-11 由用户第三次整段换过（弹窗大标题也从「机器人使用帮助」改成
    「woolbot使用帮助」），所以这里只钉「两处是同一段字」和标题，正文内容归
    `test_core.py` 那几条盯。
    """

    def test_the_help_api_returns_exactly_what_the_bot_replies(self):
        """页面与微信必须是同一段字 —— 两边各写一份迟早会说法不一致。"""
        code, body, _ = self.call("GET", "/api/help", token=self.TOKEN)
        self.assertEqual(code, 200)
        text = json.loads(body)["data"]["text"]
        self.assertEqual(text, pl.HELP_TEXT)
        self.assertIn("核心操作", text)
        self.assertNotIn("羊毛机器人", text)      # 正文里那行自报名号的标题已按用户要求删掉

    def test_the_page_carries_the_help_button_and_modal(self):
        code, index, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        html = index.decode("utf-8")
        for marker in ("btnHelp", "helpModal", "helpText", "btnHelpClose"):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        # 按钮上不带表情符号（用户 2026-10-07：「这个"?"表情删掉」）。emoji 不写在测试里
        # 也能查：那个问号是个 PUA 之外的普通符号，直接盯住按钮的可见文字就是「使用帮助」。
        btn = re.search(r'<button id="btnHelp"[^>]*>([^<]*)</button>', html)
        self.assertIsNotNone(btn, "页面里找不到帮助按钮")
        self.assertEqual(btn.group(1).strip(), "使用帮助")
        # HELP_TEXT 里指路的那句也不该再提这个表情
        self.assertNotIn("❓", pl.HELP_TEXT)
        # 弹窗标题也一样（用户 2026-10-07 第二次点名：「帮助弹窗标题里面的…"?"删除」；
        # 2026-10-11 他又把整句改成「woolbot使用帮助」）
        title = re.search(r'id="helpModal".*?<h3>([^<]*)</h3>', html, re.S)
        self.assertIsNotNone(title, "页面里找不到帮助弹窗的标题")
        self.assertEqual(title.group(1).strip(), "woolbot使用帮助")
        for marker in ("❓", "⚙️", "🐑"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, html)

        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        script = js.decode("utf-8")
        self.assertIn("openHelp", script)
        self.assertIn("/api/help", script)

        code, css, _ = self.call("GET", "/style.css?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIn(".help-text", css.decode("utf-8"))


class ChatSwitchTest(WebApiTestBase):
    """设置页上的「允许机器人闲聊」。**默认关**（用户 2026-10-07 选的固定话术那一档）。"""

    def test_it_is_off_until_the_page_turns_it_on(self):
        code, body, _ = self.call("GET", "/api/settings", token=self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["data"]["chat_enabled"], False)

        code, body, _ = self.call(
            "POST", "/api/settings", {"chat_enabled": True}, token=self.TOKEN
        )
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["data"]["chat_enabled"], True)
        self.assertTrue(st.chat_enabled())

        code, body, _ = self.call(
            "POST", "/api/settings", {"chat_enabled": False}, token=self.TOKEN
        )
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["data"]["chat_enabled"], False)
        self.assertFalse(st.chat_enabled())

    def test_the_page_has_the_switch_wired_up(self):
        code, index, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIn("sChat", index.decode("utf-8"))

        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        script = js.decode("utf-8")
        self.assertIn("chat_enabled", script)
        self.assertIn("$('sChat').checked", script)


class SayingsSwitchTest(WebApiTestBase):
    """设置页上的「每条回复末尾带一句一言」。**默认开**（用户点名要的那句）。

    以前只能 SSH 改环境变量 `WOOLBOT_NO_SAYINGS=1`，用户 2026-10-07 要求放进页面。
    """

    def test_it_is_on_until_the_page_turns_it_off(self):
        code, body, _ = self.call("GET", "/api/settings", token=self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["data"]["sayings_enabled"], True)

        code, body, _ = self.call(
            "POST", "/api/settings", {"sayings_enabled": False}, token=self.TOKEN
        )
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["data"]["sayings_enabled"], False)
        self.assertFalse(st.sayings_enabled())
        # 关掉之后机器人真的不取那句话了
        self.assertEqual(sayings_mod.Sayings(fetcher=lambda timeout=None: "心之所向").tail(), "")

        code, body, _ = self.call(
            "POST", "/api/settings", {"sayings_enabled": True}, token=self.TOKEN
        )
        self.assertEqual(code, 200)
        self.assertIs(json.loads(body)["data"]["sayings_enabled"], True)
        self.assertTrue(st.sayings_enabled())

    def test_the_page_has_the_switch_wired_up(self):
        code, index, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIn("sSayings", index.decode("utf-8"))
        # 「一言来源」：用户 2026-10-09 要在设置里自己挑分类/换源
        self.assertIn('id="sSayingsSource"', index.decode("utf-8"))
        # 字母分类表就注释在它下面（用户 2026-10-10：「把一言来源的字母分类注释到
        # 『一言来源』后面」）—— 内容由脚本从 `/api/settings` 的 `sayings_categories`
        # 填进去，HTML 里只是一个空 span，所以这里只查元素在不在。
        self.assertIn('id="sSayingsCats"', index.decode("utf-8"))

        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        script = js.decode("utf-8")
        self.assertIn("sayings_enabled", script)
        self.assertIn("$('sSayings').checked", script)
        # 摆了框、脚本不认 = 点了没反应的框（这个项目里踩过好几次）
        self.assertIn("$('sSayingsSource').value = data.sayings_source || '';", script)
        self.assertIn("sayings_source: $('sSayingsSource').value.trim(),", script)
        # 同上：注释那行也得真被填上，否则设置页上就是个永远空着的 span
        self.assertIn(
            "$('sSayingsCats').textContent = data.sayings_categories || '';", script
        )


class SettingsSheetTest(WebApiTestBase):
    """设置弹窗只放控件，不放解释文字。

    用户 2026-10-09 把三个开关下面各挂的那段说明、以及弹窗顶上那句接口说明一起划掉，
    原话是「不应该展示出来」。同一天他还把标题从「AI 模型设置」改成了「设置」。
    这条用例是**反向**盯着：谁的默认值、谁的后果，写进 README，别爬回页面上。
    """

    #: 用户划掉的那几句的原话片段。任何一句重新出现都是回退。
    GONE = (
        "任何 OpenAI 兼容接口都行",      # 弹窗顶上的接口说明（原来是 app.js 里的 settingsHint）
        "关掉之后同局域网",              # 「需要口令」下面的说明
        "默认关：不是活动",              # 「允许机器人闲聊」下面的说明
        "默认开：机器人每条回复",        # 「一言」下面的说明
    )

    def _sheet(self):
        code, body, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        html = body.decode("utf-8")
        start = html.index('id="settingsModal"')
        end = html.index('id="helpModal"')       # 下一个弹窗就是它的边界
        return html[start:end]

    def test_the_explanations_are_gone(self):
        sheet = self._sheet()
        for line in self.GONE:
            with self.subTest(line=line):
                self.assertNotIn(line, sheet)

    def test_the_title_is_just_settings(self):
        sheet = self._sheet()
        self.assertIn("<h3>设置</h3>", sheet)
        self.assertNotIn("<h3>AI 模型设置</h3>", sheet)

    def test_the_dead_hint_element_is_removed_everywhere(self):
        """光删 HTML 里的空 `<p>` 不够 —— app.js 里那两行赋值会把它写回来。"""
        self.assertNotIn("settingsHint", self._sheet())
        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertNotIn("settingsHint", js.decode("utf-8"))

    def test_only_the_controls_and_the_needed_echoes_remain(self):
        sheet = self._sheet()
        for el in ("sBaseUrl", "sApiKey", "sModel", "sTemp",
                   "sTokenRequired", "sChat", "sSayings", "sSayingsSource", "sSayingsCats",
                   "sRemind", "sRemindMinutes",
                   "sWebToken", "sDomain", "sDomainUrl", "sLogin", "sLoginKey",
                   # 微信那一套 2026-10-11 从顶栏和页面上搬进来的（见下面那条用例）
                   "wxBlock", "botPill", "btnWxLogout", "loginPanel", "qrImg", "qrLink",
                   "loginHint", "btnRefreshQr",
                   "btnSettingsCancel", "btnTest", "btnSettingsSave"):
            with self.subTest(el=el):
                self.assertIn(f'id="{el}"', sheet)
        # **模型只剩一个框了**（用户 2026-10-10：「把设置里面模型框的『文字模型』和
        # 『图片模型』合并成一个」）—— 老的那个框连 id 带标签一起消失。
        self.assertNotIn("sVision", sheet)
        self.assertNotIn("文字模型", sheet)
        self.assertNotIn("图片模型", sheet)
        # **用户名没有了**（用户 2026-10-09：「不要用户名了」）
        self.assertNotIn("sLoginUser", sheet)
        self.assertNotIn("sLoginPassword", sheet)
        # 「留空则不改动已存的 key」不写就填不对，属于必要回显，保留
        self.assertIn("留空则不改动已存的 key", sheet)
        # dim 段落只剩三处：①「留空则不改动已存的 key」那句回显；②「测试连接」的结果；
        # ③2026-10-11 搬进来的 `#loginHint`（动态状态，跟②同类）。`#qrLink` 写的是
        # `class="dim login-link"`，匹配不上 `class="dim"` 这个子串，所以别把它算进来 ——
        # 这条闸门要的是「说明文字不许爬回页面」，不是「dim 越少越好」。
        self.assertEqual(sheet.count('class="dim"'), 3)
        # temperature 那行注释是用户点名要的（「给它注释上『默认别动』」）——
        # 它是个「改了就抽错字段」的参数，说明别被人顺手删掉。
        self.assertIn("默认别动", sheet)

    def test_the_sheet_is_split_into_function_model_and_security(self):
        """设置弹窗分 功能 / 模型 / 安全 三个小页（用户 2026-10-10 的要求）。

        那天他先说「设置页分成功能、模型、安全三段」，同一天又要求「不能做成三个在
        『设置』内的同级别小独立页吗」—— 于是三段各包一个 `<section class="sheet-pane">`，
        顶上 `#settingsTabs` 一次只露一页。分页是**位置**上的事，所以这里量下标：
        每个控件必须落在它自己那一页里。只把三个页签写进页面、控件还乱着，这条就该红。
        """
        sheet = self._sheet()
        panes = ('<section class="sheet-pane" id="paneFunc" role="tabpanel">',
                 '<section class="sheet-pane" id="paneModel" role="tabpanel" hidden>',
                 '<section class="sheet-pane" id="paneSafe" role="tabpanel" hidden>')
        at = [sheet.index(p) for p in panes]
        self.assertEqual(at, sorted(at), "三页的顺序必须是 功能 → 模型 → 安全")
        # 三页都在，且开局只有第一页露着（`hidden` 由 app.js 翻页时切）
        self.assertEqual(sheet.count('<section class="sheet-pane"'), 3)
        groups = {
            "功能": ("wxBlock", "botPill", "btnWxLogout", "loginPanel", "qrImg", "qrLink",
                     "loginHint", "btnRefreshQr",
                     "sChat", "sRemind", "sRemindMinutes", "sSayings",
                     "sSayingsSource", "sSayingsCats"),
            "模型": ("sBaseUrl", "sApiKey", "sKeyState", "sKeyError",
                     "sModel", "sTemp", "btnTest", "testResult"),
            "安全": ("sTokenRequired", "sWebToken", "sDomain", "sDomainUrl",
                     "sLogin", "sLoginKey", "btnLogout"),
        }
        bounds = {"功能": (at[0], at[1]), "模型": (at[1], at[2]),
                  "安全": (at[2], len(sheet))}
        for name, ids in groups.items():
            lo, hi = bounds[name]
            for el in ids:
                with self.subTest(group=name, el=el):
                    self.assertGreater(sheet.index(f'id="{el}"'), lo, f"{el} 排在自己那一页前面了")
                    self.assertLess(sheet.index(f'id="{el}"'), hi, f"{el} 跑到下一页去了")
        # 页签：一个页签按钮指一页，名字对得上
        self.assertIn('id="settingsTabs"', sheet)
        for label, pane in (("功能", "paneFunc"), ("模型", "paneModel"), ("安全", "paneSafe")):
            with self.subTest(tab=label):
                self.assertIn(f'data-pane="{pane}">{label}</button>', sheet)
        # 取消/保存是弹窗自己的页脚，三页之外
        self.assertGreater(sheet.index('id="btnSettingsCancel"'), at[2])
        self.assertGreater(sheet.index('id="btnSettingsSave"'), at[2])

    def test_the_wechat_block_sits_above_the_chat_switch(self):
        """微信那一套在「功能」页最上面 —— 排在「允许机器人闲聊」**前面**。

        用户 2026-10-11：「把微信扫码绑定的操作还有『微信连接状态』都挪到『设置』页的
        『功能』页里 …… 刚说的这些都要放到『功能』设置页『允许机器人闲聊』的前面」。
        机器人到底连没连上微信，比「要不要闲聊」这种偏好更靠前。

        这里既量位置（都在功能页里、且排在 `sChat` 之前），也量**独占**：整页
        `id="botPill"` / `id="loginPanel"` 各自只能有一处 —— 顶栏那颗胶囊和页面上
        那条扫码横幅是搬走、不是复制；留一份旧的在，`$('botPill')` 会认到先出现的那颗，
        于是「页面上的状态」和「设置里的状态」各说各话。
        """
        sheet = self._sheet()
        func = sheet.index('<section class="sheet-pane" id="paneFunc"')
        chat = sheet.index('id="sChat"')
        for el in ("wxBlock", "botPill", "btnWxLogout", "loginPanel", "qrImg", "qrLink",
                   "loginHint", "btnRefreshQr"):
            with self.subTest(el=el):
                self.assertGreater(sheet.index(f'id="{el}"'), func, f"{el} 没在功能页里")
                self.assertLess(sheet.index(f'id="{el}"'), chat, f"{el} 跑到闲聊开关后面了")
        code, body, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        html = body.decode("utf-8")
        self.assertEqual(html.count('id="botPill"'), 1)
        self.assertEqual(html.count('id="loginPanel"'), 1)
        # 顶栏（`<header class="top">`）里不该再留着那颗胶囊
        top = html[html.index('class="top-right"'):html.index('</header>')]
        self.assertNotIn("botPill", top)
        self.assertNotIn("loginPanel", top)

    def test_the_three_pages_are_wired_to_the_tabs(self):
        """三个小页光有 HTML 不够 —— 点页签得真的翻页，而且翻页只是切 `hidden`。

        藏起来的控件**不能**被「读的时候跳过」：保存发的是整张弹窗的值，跟此刻露着
        哪一页无关（`settingsPayload()` 直接按 id 取）。这条盯着 app.js 里那几个字。
        """
        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        text = js.decode("utf-8")
        self.assertIn("function showSheetPane(which)", text)
        self.assertIn("$('settingsTabs').querySelectorAll('.tab')", text)
        self.assertIn("tab.dataset.pane === which", text)
        self.assertIn("$('settingsModal').querySelectorAll('.sheet-pane')", text)
        # 打开时落在哪一页：模型没配好落在「模型」页（那是唯一要动的东西），配好了落在「功能」页
        self.assertIn("showSheetPane(configured ? 'paneFunc' : 'paneModel')", text)

    def test_the_merged_model_box_feeds_both_keys(self):
        """合并成一个框不等于「图片模型不用了」：发设置时两个键写同一个值。

        库里 `model` 与 `vision_model` 仍然是两列（图片解析读后者），所以这条链子
        要盯住 —— 不然就是「合并了，但图片模型永远是空的」。
        """
        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        text = js.decode("utf-8")
        self.assertIn("model: $('sModel').value.trim(),", text)
        self.assertIn("vision_model: $('sModel').value.trim(),", text)
        self.assertNotIn("sVision", text)


    def test_the_reminder_controls_are_wired_to_the_page(self):
        """有控件还不够 —— app.js 得真的回填它、真的把它发出去。

        和 `settingsHint` 那次是同一个教训：HTML 里摆了元素，脚本不认它，
        页面上就是一个点了没反应的框。
        """
        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        text = js.decode("utf-8")
        self.assertIn("$('sRemind').checked = !!data.remind_enabled;", text)
        self.assertIn("$('sRemindMinutes').value", text)
        self.assertIn("remind_enabled: $('sRemind').checked,", text)
        self.assertIn("remind_minutes: $('sRemindMinutes').value.trim(),", text)

    def test_the_login_controls_are_wired_to_the_page(self):
        """和提醒那次同一个教训：摆了元素，脚本不认，就是点了没反应的框。"""
        code, body, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        html = body.decode("utf-8")
        # 「退出」住在**设置弹窗**里（跟秘钥放一起）。它原来在顶栏，但画它显示
        # 还是隐藏的那行代码只在打开弹窗时才跑，于是「点设置 → 冒出个退出」，
        # 看着像设置的附属品（用户 2026-10-09 报的）。
        sheet = self._sheet()
        self.assertIn('id="btnLogout"', sheet)
        self.assertIn('id="btnLogout" class="btn ghost"', sheet)
        # 按钮自带的那句说明（用户 2026-10-09：「退出所有设备（非必要不要点）」）。
        # 写在按钮**里面**是有意的：它跟着按钮一起 hidden，不用多一行 JS。
        self.assertIn("退出所有设备（非必要不要点）", sheet)
        top = html[html.index('<div class="top-right">'):html.index("</header>")]
        self.assertNotIn("btnLogout", top)

        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        text = js.decode("utf-8")
        for line in (
            "$('sWebToken').placeholder = data.web_token_masked",
            "$('sDomain').checked = !!data.domain_enabled;",
            "$('sDomainUrl').value = data.domain_url || '';",
            "$('sLogin').checked = !!data.login_enabled;",
            "$('sLoginKey').placeholder = data.login_key_set",
            "out.hidden = !data.login_ready;",
            "domain_enabled: $('sDomain').checked,",
            "domain_url: $('sDomainUrl').value.trim(),",
            "login_enabled: $('sLogin').checked,",
            "if (webToken) payload.web_token = webToken;",
            "if (loginKey) payload.login_key = loginKey;",
            "$('btnLogout').addEventListener('click', logout);",
        ):
            with self.subTest(line=line):
                self.assertIn(line, text)
        # 401 一律回服务端要一次登录页，前端不自己画登录框
        self.assertIn("body.login_required", text)
        # 被「秘钥错误保护」关了 24 小时也一样 —— 那张页面由服务端给
        self.assertIn("body.banned", text)
        # 用户名那一套不能留在脚本里
        self.assertNotIn("sLoginUser", text)
        self.assertNotIn("login_user", text)


class StaticFileTest(WebApiTestBase):
    """前端三件套与目录穿越。"""

    def test_index_is_served(self):
        code, body, headers = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<html", body.lower())

    def test_javascript_gets_a_utf8_charset(self):
        """少 charset 会让浏览器按 GBK 猜，页面上的中文会变乱码。"""
        code, body, headers = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn("charset=utf-8", headers["Content-Type"])
        self.assertIn(b"function", body)

    def test_css_is_served(self):
        code, body, headers = self.call("GET", "/style.css?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertIn("text/css", headers["Content-Type"])
        self.assertIn(b"{", body)

    def test_the_page_has_no_sheep_emoji(self):
        """页面上的两个羊表情（标题旁、微信已连接那个小胶囊）用户不要（2026-10-07：
        「把这两个"羊表情"删了」）。**只说页面**：微信回复里的「🐑」是机器人自己的口吻，
        那是另一回事，别顺手一起删。"""
        for path in ("/", "/app.js", "/style.css"):
            code, body, _ = self.call("GET", path + "?token=" + self.TOKEN)
            self.assertEqual(code, 200)
            with self.subTest(path=path):
                self.assertNotIn("🐑", body.decode("utf-8"))
        code, css, _ = self.call("GET", "/style.css?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        # 没了那个 span，`.logo` 那条样式也别留着（没人用的规则比没有更糟）
        self.assertNotIn(".logo", css.decode("utf-8"))
        code, index, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertNotIn('class="logo"', index.decode("utf-8"))

    def test_the_settings_button_is_plain_text(self):
        """设置按钮上的齿轮也去掉了（用户 2026-10-07：「设置上的齿轮表情也删掉」）。
        按钮、弹窗标题、以及指路的话（「到设置里核对一下」）得一起改 —— 屏幕上没有
        齿轮了，回复里还写着「⚙️ 设置」会让人在页面上找那个不存在的图标。"""
        code, index, _ = self.call("GET", "/?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        html = index.decode("utf-8")
        btn = re.search(r'<button id="btnSettings"[^>]*>([^<]*)</button>', html)
        self.assertIsNotNone(btn, "页面里找不到设置按钮")
        self.assertEqual(btn.group(1).strip(), "设置")
        self.assertNotIn("⚙️", html)
        self.assertNotIn("<h3>⚙️", html)
        code, js, _ = self.call("GET", "/app.js?token=" + self.TOKEN)
        self.assertEqual(code, 200)
        self.assertNotIn("⚙️", js.decode("utf-8"))
        # 微信里那几句指路的话也不能再带齿轮
        for text in (llm.NO_KEY_HINT, st.KIND_TEXT["auth"], st.KIND_TEXT["notfound"],
                     st.KIND_TEXT["model"]):
            with self.subTest(text=text[:12]):
                self.assertNotIn("⚙️", text)

    def test_directory_traversal_is_forbidden(self):
        for attack in (
            "/../config.json",
            "/../../etc/passwd",
            "/..%2fconfig.json",
            "/%2e%2e/config.json",
            "/../app/db.py",
        ):
            with self.subTest(attack=attack):
                code, body, _ = self.call("GET", attack + "?token=" + self.TOKEN)
                self.assertIn(code, (403, 404), body[:200])
                # 绝不能把工作区里的文件吐出来
                self.assertNotIn(b"WOOLBOT_API_KEY", body)
                self.assertNotIn(b"def insert_activity", body)

    def test_a_missing_static_file_is_404(self):
        self.failing(404, "GET", "/nope.js")

    def test_an_unknown_api_route_is_404(self):
        for method, path in (
            ("GET", "/api/whatever"),
            ("POST", "/api/whatever"),
            ("DELETE", "/api/whatever"),
            ("POST", "/api/activity/1/nonsense"),
        ):
            with self.subTest(method=method, path=path):
                self.failing(404, method, path, {"x": 1} if method == "POST" else None)


class StartupUrlTest(WebApiTestBase):
    """启动日志里那行地址 —— 和 `/日历`、`print_url()` 走的是同一个函数。

    排障表里曾经写着「日志里打印 `http://0.0.0.0:8080/` 说明跑的是旧镜像」，
    那是因为 `web.start_background()` **自己拼**了一行，于是「地址怎么算」
    又有了第二个实现。这里反向盯着它别退回去。
    """

    def _cfg(self, **web) -> dict:
        base = {"host": "127.0.0.1", "port": 8080, "token": self.TOKEN}
        base.update(web)
        return {"db_path": self.db_file, "web": base}

    def test_a_wildcard_bind_address_is_never_printed(self):
        url = webmod._startup_url(self._cfg(host="0.0.0.0"), "0.0.0.0", 8080)
        self.assertNotIn("0.0.0.0", url)
        self.assertIn("?token=" + self.TOKEN, url)

    def test_the_deployed_host_is_used_when_it_is_known(self):
        cfg = self._cfg(public_host="192.0.2.165", public_port=8080)
        self.assertEqual(
            webmod._startup_url(cfg, "0.0.0.0", 8080),
            f"http://192.0.2.165:8080/?token={self.TOKEN}",
        )

    def test_the_domain_wins_over_the_lan_address(self):
        st.update({"domain_enabled": True, "domain_url": "wool.example.com"})
        cfg = self._cfg(public_host="192.0.2.165", public_port=8080)
        self.assertEqual(
            webmod._startup_url(cfg, "0.0.0.0", 8080),
            "https://wool.example.com/?token=" + self.TOKEN,
        )

    def test_a_custom_token_shows_up_in_the_startup_line(self):
        st.update({"web_token": "my-pass"})
        cfg = self._cfg(public_host="nas.local")
        self.assertEqual(
            webmod._startup_url(cfg, "0.0.0.0", 8080),
            "http://nas.local:8080/?token=my-pass",
        )


class PortBindingTest(unittest.TestCase):
    """`web.port: 0` 必须真的让系统分配端口。

    这条独立于上面所有用例，因为它测的正是「上面那些用例为什么不会互相抢端口」。
    以前 `create_server` 写的是 `int(port or 8080)`，0 是假值 → 全落到 8080。
    """

    def test_zero_means_let_the_os_choose(self):
        first = webmod.create_server({"web": {"host": "127.0.0.1", "port": 0}})
        second = webmod.create_server({"web": {"host": "127.0.0.1", "port": 0}})
        self.addCleanup(first.server_close)
        self.addCleanup(second.server_close)
        a, b = int(first.server_address[1]), int(second.server_address[1])
        self.assertNotEqual(a, b, "两个服务拿到了同一个端口，说明 0 没生效")

    def test_the_resolver_keeps_zero_and_fills_in_the_default(self):
        # 兜底值只有一个来源：`app/config.py::DEFAULT_WEB_PORT`（现在 16886）。
        # 这里故意不写死数字 —— 端口再改时，只有那一个常量要动。
        default = cfgmod.DEFAULT_WEB_PORT
        self.assertEqual(webmod.resolve_port({}), default)
        self.assertEqual(webmod.resolve_port({"port": None}), default)
        self.assertEqual(webmod.resolve_port({"port": ""}), default)
        self.assertEqual(webmod.resolve_port({"port": 0}), 0)
        self.assertEqual(webmod.resolve_port({"port": "0"}), 0)
        self.assertEqual(webmod.resolve_port({"port": 8080}), 8080)
        self.assertEqual(webmod.resolve_port({"port": "9001"}), 9001)

    def test_an_explicit_port_is_actually_used(self):
        probe = webmod.create_server({"web": {"host": "127.0.0.1", "port": 0}})
        taken = int(probe.server_address[1])
        probe.server_close()
        again = webmod.create_server({"web": {"host": "127.0.0.1", "port": taken}})
        self.addCleanup(again.server_close)
        self.assertEqual(int(again.server_address[1]), taken)


class SmokeToolTest(WebApiTestBase):
    """`tools/smoke.py` 自己也要有测试 —— 它验的是「那台机器上真的在跑的东西」。

    check_auth 以前只看「我们这次带没带 token」：用户把页面口令关掉之后，
    本该是正确状态的匿名 200 会被判成失败，于是每次冒烟都有一条假失败。
    真正说了算的是服务端自己的 `token_required`。
    """

    def _smoke(self):
        from tools import smoke as smokemod

        s = smokemod.Smoke(self.base, self.TOKEN)
        s.run()
        return s

    def test_it_passes_against_a_healthy_server(self):
        s = self._smoke()
        bad = [r for r in s.results if not r[0]]
        self.assertEqual(bad, [], f"冒烟检查不该有失败项：{bad}")

    def test_a_server_that_does_not_ask_for_a_token_is_not_a_failure(self):
        st.update({"web_token_required": "0"})
        s = self._smoke()
        names = [name for ok, name, _ in s.results if not ok]
        self.assertEqual(names, [], f"关掉口令之后不该有失败项：{names}")
        self.assertTrue(
            any("关掉了口令" in name for _, name, _ in s.results),
            "该认出「服务器自己关掉了口令」，而不是要求匿名访问被拒",
        )

    def test_it_still_wants_a_401_when_the_server_asks_for_a_token(self):
        s = self._smoke()
        hit = [detail for _, name, detail in s.results if name == "不带 token 会被拒"]
        self.assertEqual(hit, ["HTTP 401"], f"口令开着时匿名必须被拒：{hit}")

    def test_the_repeat_rule_is_part_of_the_smoke_run(self):
        """重复规则横跨数据库列 / 接口 / 前端三处，任何一处是旧代码都要能被冒烟抓到。"""
        names = [name for _, name, _ in self._smoke().results]
        self.assertTrue(any("每天 09:00" in n for n in names), f"冒烟里没有重复规则那一项：{names}")
        self.assertTrue(any("场次" in n for n in names), f"冒烟没检查日历有没有展开规则：{names}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
