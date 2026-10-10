"""对着一台**真的在跑**的 woolbot 把接口打一遍 —— 每次部署完拿它确认。

和 `tests/` 的区别不在覆盖面，在「打的是谁」：测试跑在本机的假服务器上，验的是
代码写对了没有；这个脚本打的是你刚部署的那台机器，验的是**那台机器上真的在跑的
东西**。两者都会失败，但失败的含义不一样 —— 比如「改了前端但忘了 `--build`」
只有这个脚本抓得到，因为镜像里烤着的是旧的 `web/`。

用法：

    python tools/smoke.py --url http://192.0.2.165:16886 --token 你的页面口令
    python tools/smoke.py --url http://127.0.0.1:16886        # 没配 token 时

（端口 16886 = `app/config.py::DEFAULT_WEB_PORT`，也是 `docker-compose.yml` 里映射的号。）
    python tools/smoke.py --url http://127.0.0.1:16886        # 没配 token 时

不给 `--token` 就试着从 `config.json` 里读 `web.token`。

它只读，外加一次**自建自删**的写回环，不会在你的日历里留下垃圾。
退出码 0 表示全过，1 表示有失败项。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import timeutil as tu  # noqa: E402


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，下面的 ✅/❌ 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


class Smoke:
    def __init__(self, base: str, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.results: list[tuple[bool, str, str]] = []
        self.made: list[int] = []

    # -- 基础设施 ------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        *,
        auth: bool = True,
        headers: Optional[dict] = None,
    ) -> tuple[int, bytes, dict]:
        url = self.base + urllib.parse.quote(path, safe="/?&=%")
        if auth and self.token:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode({"token": self.token})
        body = None
        hdrs = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except Exception as exc:  # noqa: BLE001 — 连不上也算一条失败项，不要抛出去
            return 0, f"{type(exc).__name__}: {exc}".encode(), {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((bool(ok), name, detail))
        return bool(ok)

    def json_of(self, code: int, body: bytes) -> dict:
        if code == 0:
            return {}
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return {}

    # -- 各项检查 ------------------------------------------------------

    def run(self) -> int:
        self.check_reachable()
        self.check_health()
        self.check_auth()
        self.check_frontend()
        self.check_settings()
        self.check_crud()
        self.check_repeat()
        self.print_report()
        return 0 if all(ok for ok, _, _ in self.results) else 1

    def check_reachable(self) -> None:
        code, _, _ = self.call("GET", "/api/health", auth=False)
        self.check("服务能连上", code != 0, self.base if code else "连不上（地址/端口/容器没起来？）")

    def check_health(self) -> None:
        code, body, _ = self.call("GET", "/api/health", auth=False)
        data = self.json_of(code, body)
        self.check("/api/health 免鉴权可用", code == 200 and data.get("ok") is True, f"HTTP {code}")
        self.check(
            "/api/health 报的时区是配置里的",
            bool(data.get("tz")),
            f"tz={data.get('tz')} time={data.get('time')}",
        )

    def _server_token_required(self) -> Optional[bool]:
        """服务端自己说要不要口令 —— 它比「我们这次带没带 token」权威。

        用户完全可以在设置页里把口令关掉；那时匿名访问 200 是**正确状态**，
        拿「我手里有 token」去要求 401 只会每次都误报。
        """
        code, body, _ = self.call("GET", "/api/settings")
        if code != 200:
            return None
        data = self.json_of(code, body)
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        view = data.get("data") or {}
        val = view.get("token_required")
        return bool(val) if isinstance(val, bool) else None

    def check_auth(self) -> None:
        required = self._server_token_required()
        code, _, _ = self.call("GET", "/api/stats", auth=False)
        if required is False:
            self.check(
                "服务器上关掉了口令（匿名可访问）",
                code == 200,
                f"HTTP {code} —— 想收紧就在设置里打开「打开页面需要口令」",
            )
        elif self.token:
            self.check("不带 token 会被拒", code == 401, f"HTTP {code}")
        else:
            # 没配 token 的时候页面是敞开的，这里只能把这件事说出来
            self.check("未配 token（接口敞开）", code == 200, f"HTTP {code} —— 想收紧就设 web.token")
        code, body, _ = self.call("GET", "/api/stats")
        data = self.json_of(code, body)
        self.check("带 token 能读统计", code == 200 and data.get("ok") is True, f"HTTP {code}")
        if data.get("ok"):
            self.check("数据库读得出来", isinstance(data["data"], dict), str(data.get("data")))

    def check_frontend(self) -> None:
        code, body, headers = self.call("GET", "/")
        ctype = headers.get("Content-Type", "")
        self.check("首页能打开", code == 200 and b"<html" in body.lower(), f"HTTP {code} {len(body)}B")
        self.check("首页是 HTML", "text/html" in ctype, ctype)
        self.check(
            "访问时顺手种下了 cookie（刷新就不用再带 token）",
            (not self.token) or ("Set-Cookie" in headers),
            "有 Set-Cookie" if "Set-Cookie" in headers else "没有 Set-Cookie",
        )

        for path, want in (("/app.js", "javascript"), ("/style.css", "text/css")):
            code, body, headers = self.call("GET", path)
            ctype = headers.get("Content-Type", "")
            self.check(f"{path} 能下载", code == 200 and len(body) > 0, f"HTTP {code} {len(body)}B")
            self.check(f"{path} 类型是 {want}", want in ctype, ctype)
            # 少了 charset 浏览器会拿页面编码去猜，中文会变乱码
            self.check(
                f"{path} 带了 charset=utf-8",
                "charset=utf-8" in ctype.lower(),
                ctype or "（没有 Content-Type）",
            )

        code, _, _ = self.call("GET", "/../config.json")
        self.check("目录穿越被挡", code in (403, 404), f"HTTP {code}")

        code, _, _ = self.call("GET", "/api/definitely-not-a-route")
        self.check("不存在的接口回 404", code == 404, f"HTTP {code}")

    def check_settings(self) -> None:
        """「在页面上配模型」这条链路的可读部分 —— 只读，不动别人的配置。"""
        code, body, _ = self.call("GET", "/api/settings")
        data = self.json_of(code, body)
        self.check("设置接口能读", code == 200 and data.get("ok") is True, f"HTTP {code}")
        if code != 200:
            return
        payload = json.dumps(data, ensure_ascii=False)
        self.check(
            "设置里没有明文 key",
            "api_key" not in (data.get("data") or {}),
            f"字段：{sorted((data.get('data') or {}).keys())}",
        )
        self.check(
            "页面上能看出模型配没配",
            "configured" in (data.get("data") or {}),
            f"configured={((data.get('data') or {}).get('configured'))}",
        )
        self.check("设置里没有泄漏 sqlite 路径之类", "db_path" not in payload, "已过滤")

        # 刷新二维码：登录之后应当明确说「不需要」
        code, body, _ = self.call("POST", "/api/qr/refresh", payload={})
        data = self.json_of(code, body)
        note = str(data.get("note") or data.get("error") or "")
        self.check(
            "刷新二维码有回应（登录后会说不用扫）",
            code == 200 and bool(note),
            note or f"HTTP {code}",
        )

    def check_crud(self) -> None:
        """建一条、改一条、标完成、删掉 —— 页面上那四个按钮走的就是这几步。"""
        title = f"冒烟检查 {tu.to_minute(tu.now())}"
        code, body, _ = self.call(
            "POST",
            "/api/activity",
            {"title": title, "platform": "冒烟", "grab_at": f"{tu.today().isoformat()}T09:00"},
        )
        data = self.json_of(code, body)
        if code != 201 or not data.get("id"):
            self.check("新建一条活动", False, f"HTTP {code} {body[:120]}")
            return
        aid = int(data["id"])
        self.made.append(aid)
        self.check("新建一条活动", True, f"id={aid}")

        code, body, _ = self.call("GET", f"/api/activity/{aid}")
        row = self.json_of(code, body).get("data") or {}
        self.check("读回来是刚建的那条", row.get("title") == title, f"HTTP {code} title={row.get('title')!r}")

        # 列表要在这条**还带着开抢时刻**的时候查：日历的范围是按 grab_at/start_at
        # 算的，下面那步会把 grab_at 清空，清空之后它本来就不该出现在范围里。
        code, body, _ = self.call("GET", "/api/activities")
        data = self.json_of(code, body)
        ids = [r.get("id") for r in (data.get("data") or [])]
        self.check("列表里能找到刚建的那条", code == 200 and aid in ids, f"HTTP {code} count={data.get('count')}")
        first = (data.get("data") or [{}])[0]
        self.check(
            "列表没有把 datetime 对象塞进 JSON",
            isinstance(first.get("grab_at"), (str, type(None))),
            f"grab_at={first.get('grab_at')!r}",
        )

        # 日历格子那行小字（农历/节气/节日/休班）：真机上表和算法都在容器里，
        # 这里挑几天公示过的日子，确认线上算出来的和写死的一致。
        code, body, _ = self.call("GET", "/api/calendar?from=2026-09-30&to=2026-10-11")
        data = self.json_of(code, body)
        days = data.get("days") or {}
        self.check("日历接口能读（农历/节气/节日）", code == 200 and len(days) == 12, f"HTTP {code} days={len(days)}")
        self.check(
            "国庆节那天报「休」",
            (days.get("2026-10-01") or {}).get("festival") == "国庆节"
            and (days.get("2026-10-01") or {}).get("holiday") == "休",
            str(days.get("2026-10-01")),
        )
        self.check(
            "节气报得对（10月8日寒露）",
            (days.get("2026-10-08") or {}).get("term") == "寒露",
            str(days.get("2026-10-08")),
        )
        self.check(
            "调休上班报「班」（10月10日）",
            (days.get("2026-10-10") or {}).get("holiday") == "班",
            str(days.get("2026-10-10")),
        )

        # 这条是历史 bug 的回归：编辑保存曾经因为一个漏掉的星号永远 500
        code, body, _ = self.call("POST", f"/api/activity/{aid}", {"note": "冒烟改的", "grab_at": ""})
        data = self.json_of(code, body)
        self.check("改一条活动（编辑保存）", code == 200 and data.get("ok"), f"HTTP {code} {body[:120]}")
        code, body, _ = self.call("GET", f"/api/activity/{aid}")
        row = self.json_of(code, body).get("data") or {}
        self.check("改动真的落库了", row.get("note") == "冒烟改的" and row.get("grab_at") == "", str(row.get("grab_at")))

        code, body, _ = self.call("POST", f"/api/activity/{aid}/status", {"status": "done"})
        self.check("标记完成", code == 200, f"HTTP {code} {body[:120]}")
        code, body, _ = self.call("POST", f"/api/activity/{aid}/status", {"status": "乱写的"})
        self.check("非法状态被拒", code == 400, f"HTTP {code}")

        code, body, _ = self.call("DELETE", f"/api/activity/{aid}")
        self.check("删掉这条", code == 200, f"HTTP {code} {body[:120]}")
        if code == 200:
            self.made.remove(aid)
        code, _, _ = self.call("GET", f"/api/activity/{aid}")
        self.check("删掉之后读不到了", code == 404, f"HTTP {code}")

        code, body, _ = self.call("GET", "/api/status")
        data = self.json_of(code, body)
        if code == 200:
            bot = (data.get("data") or {}).get("bot")
            self.check("机器人状态接口有回应", bot in ("on", "off", "error"), f"bot={bot}")
        else:
            self.check("机器人状态接口有回应", False, f"HTTP {code}")

    def check_repeat(self) -> None:
        """重复规则：建一条「每天 09:00」的，日历接口该给出场次（occurrences）。

        这一项只有**对着真在跑的那台**才有意义：重复规则横跨数据库列、接口载荷和
        前端三处，任何一处是旧代码，日历上这张券就会只在第一天出现。
        """
        title = f"冒烟重复 {tu.to_minute(tu.now())}"
        code, body, _ = self.call(
            "POST",
            "/api/activity",
            {"title": title, "grab_at": f"{tu.today().isoformat()}T09:00", "repeat": "每天 09:00"},
        )
        data = self.json_of(code, body)
        if code != 201 or not data.get("id"):
            self.check("新建一条「每天 09:00」的活动", False, f"HTTP {code} {body[:120]}")
            return
        aid = int(data["id"])
        self.made.append(aid)
        self.check("新建一条「每天 09:00」的活动", True, f"id={aid}")

        code, body, _ = self.call("GET", f"/api/activity/{aid}")
        row = self.json_of(code, body).get("data") or {}
        self.check("读回来带着重复规则", bool(row.get("repeat")), f"repeat={row.get('repeat')!r}")

        code, body, _ = self.call("GET", "/api/activities")
        rows = self.json_of(code, body).get("data") or []
        mine = [r for r in rows if r.get("id") == aid]
        occ = (mine[0].get("occurrences") if mine else None) or []
        self.check("日历接口把规则展开了", len(occ) >= 2, f"occurrences={len(occ)}")
        self.check(
            "每个场次带当天的时刻",
            bool(occ) and bool(occ[0].get("at")) and bool(occ[0].get("day")),
            f"{occ[:1]}",
        )

        # 认不出的规则必须报错：默默存成一次性是这类功能里最难发现的错
        code, body, _ = self.call("POST", f"/api/activity/{aid}", {"repeat": "看心情"})
        self.check("认不出的重复规则被拒（不是默默降级）", code == 400, f"HTTP {code} {body[:80]}")

        code, _, _ = self.call("DELETE", f"/api/activity/{aid}")
        if code == 200:
            self.made.remove(aid)

    def cleanup(self) -> None:
        for aid in list(self.made):
            code, _, _ = self.call("DELETE", f"/api/activity/{aid}")
            if code == 200:
                self.made.remove(aid)

    def print_report(self) -> None:
        width = max((len(n) for _, n, _ in self.results), default=10)
        print()
        for ok, name, detail in self.results:
            mark = "✅" if ok else "❌"
            print(f"{mark} {name.ljust(width)}  {detail}")
        bad = [n for ok, n, _ in self.results if not ok]
        print()
        if bad:
            print(f"❌ {len(bad)}/{len(self.results)} 项没过：{'、'.join(bad)}")
        else:
            print(f"✅ {len(self.results)} 项全过（{self.base}）")


def _token_from_config() -> str:
    try:
        raw: Any = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        return str((raw.get("web") or {}).get("token") or "")
    except Exception:  # noqa: BLE001
        return ""


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(description="对着一台在跑的 woolbot 打一遍接口")
    parser.add_argument("--url", default="http://127.0.0.1:16886",
                        help="服务地址（默认端口 = app/config.py::DEFAULT_WEB_PORT）")
    parser.add_argument("--token", default=None, help="不给就读 config.json 里的 web.token")
    args = parser.parse_args(argv)

    token = args.token if args.token is not None else _token_from_config()
    if args.token is None and token:
        print(f"（--token 没给，从 config.json 读到了 web.token）")

    tu.configure("Asia/Shanghai")
    smoke = Smoke(args.url, token)
    try:
        return smoke.run()
    finally:
        smoke.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
