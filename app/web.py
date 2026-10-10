"""前端日历的 HTTP 服务：标准库 http.server，零额外依赖。

设计取舍：不用 FastAPI/uvicorn，是因为本机 Python 3.12 没装、且 NAS 上跑的容器
要和本机行为完全一致。标准库够用 —— 并发量就是一个人点几下。

安全：`web.token` 非空时所有请求都要带 token（`?token=` 或 `X-Token` 头，
首次带 query 参数会种下 cookie，之后就不用管了）。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from html import escape as html_escape
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import db, lunar, pipeline as pl, repeat as rp, timeutil as tu

log = logging.getLogger("woolbot.web")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE_NAME = "woolbot_token"

#: 机器人状态回调。`main.py all` 会把 ClawBot 的实况挂进来，页面就能显示
#: 登录状态和当前二维码。不挂的时候网页照常用，只是看不到机器人状态。
_STATUS_PROVIDER: dict[str, Any] = {"fn": None}

#: 页面能触发的小命令（目前只有「换一张二维码」）。同样由 `main.py` 挂进来。
_CONTROLS: dict[str, Any] = {"qr_refresh": None, "wechat_logout": None}

#: 页面上的配置来源。`settings.public_view` 会用上它拿 config.json 的底值。
_CONFIG: dict[str, Any] = {"cfg": None}

#: 开抢提醒器（`app/reminder.py`）。设置页一保存就调它的 `sync()`，
#: 于是「关掉提醒」是当场把线程收掉，不用等容器重启。
_REMINDER: dict[str, Any] = {"obj": None}


def set_status_provider(fn: Optional[Any]) -> None:
    """注册一个返回状态字典的函数，用于 `GET /api/status`。"""
    _STATUS_PROVIDER["fn"] = fn


def set_qr_refresh(fn: Optional[Any]) -> None:
    """注册「重新申请一张登录二维码」的动作，用于 `POST /api/qr/refresh`。"""
    _CONTROLS["qr_refresh"] = fn


def set_wechat_logout(fn: Optional[Any]) -> None:
    """注册「退出微信（丢掉 NAS 本地这份会话）」的动作，用于 `POST /api/wechat/logout`。

    和 `set_qr_refresh` 分开：那个是「换一张码」，这个是「这份会话不要了」。
    """
    _CONTROLS["wechat_logout"] = fn


def set_config(cfg: Optional[Any]) -> None:
    """把生效的配置挂进来，页面上的设置面板才能显示底值（模型名、base_url）。"""
    _CONFIG["cfg"] = cfg


def set_reminder(obj: Optional[Any]) -> None:
    """注册开抢提醒器。设置页保存后调它的 `sync()`，开关立刻生效。

    没挂的时候页面照常用，只是改「提前几分钟」要等下次启动才轮到提醒线程。
    """
    _REMINDER["obj"] = obj


def _sync_reminder() -> None:
    """按最新设置起停提醒线程。**保存设置之后必须调**，否则开关只是躺在库里。"""
    obj = _REMINDER["obj"]
    if obj is None:
        return
    try:
        obj.sync()
    except Exception as exc:  # noqa: BLE001 —— 设置已经存下了，起停失败不该让它回滚
        log.error("起停开抢提醒失败：%s", exc)


def _token_required(default: bool = True) -> bool:
    try:
        from . import settings as st

        return st.token_required(default)
    except Exception as exc:  # noqa: BLE001
        log.debug("读 token_required 失败，按默认 %s：%s", default, exc)
        return default


def _front_base(cfg: dict[str, Any]) -> str:
    """站点根地址（不带口令）。域名关着就是局域网地址。

    唯一的老主顾是那张「这个页面要口令」的提示页 —— 用户 2026-10-09 让它伪装成
    登录页，那张页面整个没了，所以这里现在**没有调用方**。留着是因为「地址只在
    一个地方算」这条规矩还成立：谁要印地址就调 `st.front_url()`，这个薄壳管的是
    「地址算不出来也不能崩」。
    """
    try:
        from . import settings as st

        return st.front_url(cfg, with_token=False).rstrip("/")
    except Exception as exc:  # noqa: BLE001 —— 印地址绝不能因为地址算不出来就崩
        log.debug("算前端地址失败：%s", exc)
        return ""


#: 秘钥错误保护：`"范围:标识" -> [这一轮错了几次, 解禁时刻(monotonic)]`。
#: **两个范围都记** —— `ip:` 管「同一个 IP」，`dev:` 管「同一台设备」（浏览器里那个
#: `woolbot_device` cookie）。用户 2026-10-09 定的：**两者满足其一**就整天不许访问。
#: **只在内存里** —— 它防的是脚本爆破，不是防重启；重启丢干净反而省事，
#: 也是「被自己关在门外」时唯一的自救办法。
_LOGIN_FAILS: dict[str, list] = {}
_LOGIN_LOCK = threading.Lock()


def _login_blocked(keys: list) -> int:
    """这些身份里有任何一个被禁着，就回还要等多少秒。0 = 现在就能进。"""
    now = time.monotonic()
    left = 0
    with _LOGIN_LOCK:
        for key in keys:
            rec = _LOGIN_FAILS.get(key)
            if rec and rec[1] > now:
                left = max(left, int(rec[1] - now + 0.999))
    return left


def _login_record_fail(keys: list) -> None:
    """记一次秘钥错误。每个身份各数各的，谁先满 `LOGIN_MAX_FAILS` 谁先被禁。"""
    from . import settings as st

    now = time.monotonic()
    with _LOGIN_LOCK:
        for key in keys:
            count, until = _LOGIN_FAILS.get(key, [0, 0.0])
            # `until` 为 0 表示「还没禁过」，这时候**不能**把 count 清零 —— 否则每次
            # 失败都从 1 重新数，永远到不了 `LOGIN_MAX_FAILS`，这层保护等于没写。
            if until and until <= now:
                count, until = 0, 0.0  # 上一轮禁期已经过了，重新数
            count += 1
            if count >= st.LOGIN_MAX_FAILS:
                count = 0
                until = now + st.LOGIN_BAN_SECONDS
            _LOGIN_FAILS[key] = [count, until]


def _login_reset(keys: list) -> None:
    with _LOGIN_LOCK:
        for key in keys:
            _LOGIN_FAILS.pop(key, None)


def reset_login_fails() -> None:
    """清空错误记录。测试用；正常运行不需要。"""
    with _LOGIN_LOCK:
        _LOGIN_FAILS.clear()


def _ban_text(left: int) -> str:
    """「还要等多久」说成人话。整天禁着的时候说清楚是几个小时。"""
    mins = max(1, int(left / 60 + 0.999))
    hours, mins = divmod(mins, 60)
    if hours and mins:
        return f"{hours} 小时 {mins} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{mins} 分钟"


def _truthy(value: Any) -> bool:
    """页面上送来的开关值可能是真布尔，也可能是 "1"/"true"/"on"（表单送的就是字符串）。

    自己写一遍而不是 `bool(value)`：字符串 `"0"` 在 Python 里是**真值**，
    拿它判断等于把「关」当成「开」。
    """
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
        "off",
    )


def _same(a: Any, b: Any) -> bool:
    """常量时间比较。口令是短字符串，逐字符比较理论上能一个字符一个字符试出来。"""
    a, b = str(a or ""), str(b or "")
    return bool(a) and bool(b) and secrets.compare_digest(a, b)


def _token_cookie(token: str) -> str:
    """口令 cookie。**值必须转义**：自定义口令里完全可能有个 `;` 或空格，
    裸拼进 `Set-Cookie` 会把这条头切断，浏览器只拿到半截 —— 表现就是
    「口令明明填对了，刷新一下又让我输」。
    """
    from urllib.parse import quote as _quote

    return f"{COOKIE_NAME}={_quote(token, safe='')}; Path=/; Max-Age=31536000; SameSite=Lax"


def _device_cookie(device: str) -> str:
    """设备 cookie。值是 `secrets.token_hex(16)`，不会有需要转义的字符，
    所以直接拼 —— 但 `HttpOnly` 必须加：它只是给秘钥错误保护记账用的，
    前端一行 JS 都不该碰它。

    名字和有效期都从 `settings` 拿，**不在两个地方各写一份**。
    """
    from . import settings as st

    return (
        f"{st.DEVICE_COOKIE}={device}; Path=/; Max-Age={st.DEVICE_MAX_AGE}; "
        "SameSite=Lax; HttpOnly"
    )


#: **没有「口令提示页」这个常量了**。用户 2026-10-09：设置了口令以后，没带口令
#: 时看到的那张页面**要伪装成秘钥登录页**（就是下面这张 `_LOGIN_HTML`）。以前它
#: 是单独一张「🔒 这个页面要口令」，上面写着「把口令接在地址后面」「口令在
#: config.json 的 web.token 里」—— 等于给每个路过的人发说明书：这里有口令这道门、
#: 口令去哪儿找。现在两层门长得一模一样，路过的人分不出自己卡在哪一层。
#:
#: **口令提示只留在 README 和排障表里**（那两处主人自己会去翻），页面上一个字
#: 都不留。代价是主人忘了口令时页面上没有线索 —— 这是用户明确要的取舍。

#: 登录页。同样是内联 HTML，而且**不用 JavaScript** —— 一个表单 POST 就够，
#: 页面上脚本越少，出问题的地方越少。
#: 只有一个输入框：**用户 2026-10-09 把用户名去掉了**，整页只剩一把访问秘钥。
_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>羊毛日历 · 登录</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:#faf9f7;color:#2f2a25}
 .box{width:20rem;padding:2.5rem;background:#fff;border:1px solid #e8e2da;
      border-radius:14px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
 h1{margin:0 0 1.4rem;font-size:1.35rem}
 label{display:block;margin:0 0 1rem;font-size:.92rem;color:#6b6259}
 input{display:block;width:100%;box-sizing:border-box;margin-top:.35rem;padding:.55rem .7rem;
       font:inherit;border:1px solid #ddd5cb;border-radius:9px;background:#fff;color:inherit}
 button{width:100%;margin-top:.5rem;padding:.6rem;font:inherit;font-weight:600;
        color:#fff;background:#c2410c;border:0;border-radius:9px;cursor:pointer}
 .msg{margin:0 0 1rem;padding:.55rem .7rem;border-radius:9px;font-size:.9rem;
      background:#fef3c7;color:#b45309}
 @media(prefers-color-scheme:dark){
  body{background:#1b1917;color:#eceae7}
  .box{background:#232020;border-color:#38332e}
  label{color:#9a9088}
  input{background:#2c2825;border-color:#3d3733}
  button{background:#fb923c}
  .msg{background:#3a2f16;color:#fbbf24}
 }
</style></head><body><div class="box">
<h1>🔑 请输入访问秘钥</h1>
{msg}<form method="post" action="/api/login">
<label>秘钥<input name="key" type="password" autocomplete="current-password" autofocus></label>
<button type="submit">进去</button>
</form>
</div></body></html>
"""


#: 秘钥错太多次被禁时的页面。**故意不给表单** —— 禁期内交表单也是白交，摆一个
#: 按不动的按钮只会让人以为页面坏了。
_BANNED_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>羊毛日历 · 暂时不许访问</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
      font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
      background:#faf9f7;color:#2f2a25}
 .box{width:22rem;padding:2.5rem;background:#fff;border:1px solid #e8e2da;
      border-radius:14px;box-shadow:0 2px 12px rgba(0,0,0,.05)}
 h1{margin:0 0 1rem;font-size:1.25rem}
 p{margin:0;font-size:.92rem;color:#6b6259;line-height:1.8}
 @media(prefers-color-scheme:dark){
  body{background:#1b1917;color:#eceae7}
  .box{background:#232020;border-color:#38332e}
  p{color:#9a9088}
 }
</style></head><body><div class="box">
<h1>🚫 暂时不许访问</h1>
<p>{msg}</p>
</div></body></html>
"""


def _bot_status() -> dict[str, Any]:
    fn = _STATUS_PROVIDER["fn"]
    if fn is None:
        return {"bot": "off"}
    try:
        data = dict(fn() or {})
    except Exception as exc:  # noqa: BLE001 — 状态查询绝不能把页面拖垮
        log.warning("取机器人状态失败：%s", exc)
        return {"bot": "error", "error": str(exc)}
    data["qr_ready"] = bool(data.get("qr_path"))
    data.pop("qr_path", None)  # 文件路径不外泄
    return data


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k.endswith("_dt"):
            continue  # 解析出来的 datetime 不用外传，grab_at 字符串已经够了
        if isinstance(v, (datetime, date)):
            out[k] = tu.to_minute(v) if isinstance(v, datetime) else v.isoformat()
        else:
            out[k] = v
    return out


def _dt(value: Any) -> Optional[datetime]:
    """接口给的 "YYYY-MM-DDTHH:MM" 字符串 → datetime（认不出就当没有）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:16])
    except ValueError:
        return None


#: 排期 + 配额占的八个列，见 `app/repeat.py`
_REPEAT_COLS = rp.FIELDS
#: 其中真正是「配额」的两个。编辑框里「每周二、四 10:00」和「每周限领 1 次」是
#: 两回事：排期那段文字是要被重新解析的，配额一旦混进同一句话里，用户再存一次
#: 就会被解析器改写（或者干脆认不出），所以接口上分成两组字段。
_QUOTA_COLS = rp.QUOTA_FIELDS


def _repeat_fields(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把接口给的「重复规则」人话翻成八个列。

    没提 ``repeat`` 就返回 None（**不要动已经存着的规则**）；空串表示明确清空、
    退回一次性（配额一并清掉 —— 一次性的活动没有「每周几次」这回事）。
    规则看不懂时抛 ``ValueError``，由调用方回 400 —— 绝不能默默存成
    一次性：用户明明写了「每天」却没生效，是这类功能里最难发现的一种错。
    """
    if "repeat" not in data:
        return None
    raw = str(data.get("repeat") or "").strip()
    if not raw:
        return {k: None for k in _REPEAT_COLS}
    if rp.parse_repeat(raw) is None:
        raise ValueError(
            "看不懂这个重复规则。写成「每天 09:00」「每天 08:30-22:00」「每周二、四 10:00」"
            "「每月1日、15日 10:00」这样，留空就表示只发生一次。"
        )
    anchor = _dt(data.get("grab_at") or data.get("start_at"))
    return rp.fields_from(raw, str(data.get("note") or ""), "", anchor)


def _quota_fields(data: dict[str, Any]) -> dict[str, Any]:
    """把接口给的 ``quota_week`` / ``quota_month`` 收敛成合法的整数或 None。

    只有**显式出现**的 key 才动：前端把「箱子里没填、原来也没配额」的列整个省掉，
    好让 ``_repeat_fields`` 从备注里认出来的配额留下；要清空就显式发一个空串。
    非法值抛 ``ValueError`` 回 400，理由和重复规则一样 —— 静默丢掉配额的后果是
    页面天天显示「还能领」，用户白跑一趟。
    """
    out: dict[str, Any] = {}
    for key in _QUOTA_COLS:
        if key not in data:
            continue
        raw = data.get(key)
        if raw is None or str(raw).strip() == "":
            out[key] = None
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} 只能是数字（每周/每月最多几次），留空表示不限。") from None
        if not 1 <= value <= rp.MAX_QUOTA:
            raise ValueError(f"{key} 要在 1~{rp.MAX_QUOTA} 之间，留空表示不限。")
        out[key] = value
    return out


def _merge_repeat(fields: dict[str, Any], rep: Optional[dict[str, Any]], *, replace: bool) -> None:
    """把 ``_repeat_fields`` 的结果并进待写字段。

    ``replace=False``（改一条**已有**的行）时，**认不出的配额列一律不写**：用户只是
    在编辑框里把「每天 09:00」改成「每天 09:30」，备注里又没写「每周几次」，那
    配额箱子里填着的数不能被抹成空 —— 要清空得显式发个空串（前端就是这么做的），
    那种情况走 `_quota_fields`。

    排期那几列不受此限：weekly 改成 daily 时必须把 ``repeat_weekday`` /
    ``repeat_days`` 一起写空，否则「周一」会残留下来。**整条退回一次性**（``repeat``
    发的是空串，``rep`` 里八列全 None）时也一样：一次性的活动没有「每周几次」这回事，
    配额得跟着清掉。
    """
    if not rep:
        return
    keep_quota = not replace and bool(rep.get("repeat_kind"))
    for key, value in rep.items():
        if keep_quota and value is None and key in _QUOTA_COLS:
            continue
        fields[key] = value


def _with_repeat(line: dict[str, Any]) -> dict[str, Any]:
    """序列化一行；重复型活动额外带上规则的说明文字。"""
    out = _jsonable(line)
    desc = rp.describe(line)
    if desc:
        out["repeat"] = desc
        # 编辑框回填要的是**纯排期**那句：连「· 周限1次」一起填回去，用户再存一次
        # 就会被当成规则文本重新解析，配额的写法迟早被改坏。
        out["repeat_input"] = rp.rule_text(line)
    return out


def _with_occurrences(
    line: dict[str, Any], d_from: date, d_to: date, done_days: Any = (), now: Any = None
) -> dict[str, Any]:
    """再进一步：告诉前端这条重复活动在区间里的每一场。

    日历必须按「场次」摆，而不是按 ``grab_at`` 摆 —— 一张「每天 09:00」的券只有
    一个 ``grab_at``，却要出现在区间里的每一天。规则由 `app/repeat.py` 算，前端
    不重复实现一遍（两边算法一旦分叉，日历和机器人就又会对不上）。

    每一场还带上**那一场自己的状态**：重复活动没有「整条完成」这回事，
    「每周二、四、周限 1 次」的券这周二领到了，本周四就该显示已完成。
    ``now`` 一次请求只取一次（``_api_list`` 传进来），免得同一次响应里几场
    对「现在」的看法不一致。
    """
    out = _with_repeat(line)
    if "repeat" not in out:
        return out
    done = [str(d) for d in (done_days or ())]
    occ = rp.occurrences_between(line, d_from, d_to)
    for item in occ:
        day = date.fromisoformat(item["day"])
        item["status"] = rp.occurrence_status(line, day, done, now)
        item["done"] = item["day"] in done
        state = rp.quota_state(line, day, done)
        if state["week"]["limit"] or state["month"]["limit"]:
            item["quota"] = state
    out["occurrences"] = occ
    out["done_days"] = done
    # 行上再挂一份「今天」的配额进度：详情面板要说「本周已领 1/2，还剩 1 次」，
    # 而用户点开的那天未必是一场（从「这天还能领的」进来就会这样）。
    state = rp.quota_state(line, tu.today(), done)
    if state["week"]["limit"] or state["month"]["limit"]:
        out["quota"] = state
    return out


def _with_claimable(
    line: dict[str, Any], d_from: date, d_to: date, done_days: Any = (), now: Any = None
) -> dict[str, Any]:
    """再补一条：区间里哪些天它「还能领，但不是这天开抢」。

    日历上点某一天，如果只摆「开抢」的场次，一张有效期到年底的长效券就会让页面
    说「这天没有活动」—— 而机器人同一天明明会说「📌 不过这一天还能领的」。
    两边各写一套日子判据就是这个下场，所以这里直接调 `pipeline` 里机器人用的
    那一对函数（**不是复制品**）。
    """
    out = _with_occurrences(line, d_from, d_to, done_days, now)
    days = [d.isoformat() for d in pl._days_between(d_from, d_to) if pl.claimable_on(line, d)]
    if days:
        out["claimable_days"] = days
    return out


def _filter_by_occurrence(
    lines: list[dict],
    status: str,
    d_from: date,
    d_to: date,
    marks: dict[int, list[str]],
    now: Any = None,
) -> list[dict]:
    """按「有没有某一场的状态就是 status」筛重复活动。

    行级的 ``status`` 一格装不下几十场：这周的额度用完了，行还是 ``pending``，
    可「待抢」列表里就不该再有它。所以重复活动按场次筛，一次性活动仍按行筛。
    ``missed``（未抢到）也走这里 —— 上周六没抢到的那一场，就是靠它出现在列表里。
    """
    if status == "expired":
        # 「已过期」是**整条系列**的事（`db.refresh_expired()` 按 end_at 标的），
        # 不是某一场：不加这一条，选「已过期」会把所有重复活动一股脑列出来 ——
        # 真机上试过，31 行全出来，而库里一条 expired 都没有。
        return [r for r in lines if str(r.get("status") or "") == rp.STATUS_EXPIRED]
    if status not in ("pending", "done", "missed"):
        return lines
    kept: list[dict] = []
    for row in lines:
        done = marks.get(row["id"], ())
        for item in rp.occurrences_between(row, d_from, d_to):
            day = date.fromisoformat(item["day"])
            if rp.occurrence_status(row, day, done, now) == status:
                kept.append(row)
                break
    return kept


#: 需要带上 charset 的静态文件类型。`mimetypes` 在不同 Python/平台上对 .js 的
#: 判断不一致（`text/javascript` 还是 `application/javascript`），所以按前缀和
#: 明确名单一起判。少了 charset，浏览器会拿页面编码去猜，中文会变乱码。
_TEXT_TYPES = ("application/javascript", "application/json", "image/svg+xml")

#: `GET /api/calendar` 一次最多算多少天（月视图前后各多取一周，40 天够用；
#: 留个上限只是防止有人拿 from/to 拉满一整个世纪）。
_CALENDAR_MAX_DAYS = 400


def _content_type(target: Path) -> str:
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in _TEXT_TYPES:
        return ctype + "; charset=utf-8"
    return ctype


def _parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _apply_filters(rows: list[dict], status: str, keyword: str, platform: str) -> list[dict]:
    if status and status != "all":
        rows = [r for r in rows if (r.get("status") or "pending") == status]
    if platform and platform != "all":
        rows = [r for r in rows if (r.get("platform") or "") == platform]
    if keyword:
        k = keyword.lower()
        rows = [
            r
            for r in rows
            if k
            in " ".join(
                str(r.get(f) or "") for f in ("title", "platform", "category", "note", "url")
            ).lower()
        ]
    return rows


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "WoolbotWeb/0.1"

    # 由 create_server 注入
    config: dict[str, Any] = {}
    token: str = ""

    # -- 基础工具 -----------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict[str, Any]] = None) -> None:
        """发一个响应。`extra` 里某个头的值是 list 时，**同名头发多次** ——
        一次响应要同时种下会话 cookie 和设备 cookie，`Set-Cookie` 只能这么发。"""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            if isinstance(v, (list, tuple)):
                for one in v:
                    self.send_header(k, one)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, code: int = 200, extra: Optional[dict[str, str]] = None) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", extra)

    def _error(self, code: int, message: str) -> None:
        self._json({"ok": False, "error": message}, code)

    def _effective_token(self) -> str:
        """当前生效的口令 —— **每个请求现读**，所以在设置页改完立刻有效。

        以前它是 `create_server()` 焊在类上的属性，改口令必须重启容器；而用户
        刚自定义完口令，下一步就是刷新页面看效果，那时候旧口令还生效着，看起来
        就像「改了没用」。价格是每个请求查一次设置表，而设置本身有内存缓存，
        真正查库的次数可以忽略。
        """
        from . import settings as st

        try:
            return st.web_token(self.config)
        except Exception as exc:  # noqa: BLE001 —— 读不出来就沿用启动时那个
            log.debug("读自定义口令失败，沿用启动值：%s", exc)
            return self.token

    def _login_required(self) -> bool:
        """登录这道门现在该不该拦人。开关关着、或秘钥没配，都不拦。"""
        from . import settings as st

        try:
            return st.login_ready()
        except Exception as exc:  # noqa: BLE001
            log.debug("读登录设置失败：%s", exc)
            return False

    def _cookies(self) -> SimpleCookie:
        jar = SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            try:
                jar.load(raw)
            except Exception:  # noqa: BLE001 —— 坏 cookie 当没有，不是崩
                pass
        return jar

    def _device_id(self) -> str:
        """这台设备是谁。**没有就是空串**（不认识这台机器），不在这里发号 ——
        见 `_device_cookie`：号码在打开登录页时种下。"""
        from . import settings as st

        jar = self._cookies()
        if st.DEVICE_COOKIE not in jar:
            return ""
        return (jar[st.DEVICE_COOKIE].value or "").strip()

    def _login_fail_keys(self) -> list:
        """这次请求该按哪几个身份记账：IP 一个，设备一个。

        设备那条**只在浏览器已经带着设备 cookie 时才加** —— 否则所有「第一次
        就来 POST」的客户端会共用同一个空号码，一个人错 5 次就把别人一起关在
        门外。IP 那条永远在。
        """
        keys = [f"ip:{self.client_address[0] if self.client_address else '-'}"]
        device = self._device_id()
        if device:
            keys.append(f"dev:{device}")
        return keys

    def _login_gate(self, path: str) -> bool:
        """秘钥这道门。放行回 True；拦下了（已经回过响应）回 False。

        GET / POST / DELETE 三条路都走这里 —— 以前是三段各自抄一遍，加一层
        「秘钥错误保护」要改三个地方，迟早漏一个。

        **禁令排在会话前面**：用户 2026-10-09 要的是「禁止访问 24 小时」，
        不是「没登录的才禁止」。已经登录的浏览器照样会被拦在外面 —— 否则
        「被禁了还能用」跟「没禁」只差一个旧 cookie。
        """
        if not self._door_open():
            return True
        left = _login_blocked(self._login_fail_keys())
        if left:
            # 「禁止访问」是**整天、整站**的：不是只把登录按钮锁掉，而是什么都
            # 不给看。`/api/health` 在更前面就返回了，不受影响。
            self._deny_banned(left, path)
            return False
        if not self._login_required():
            # 只开了口令、没开秘钥登录：门过了，里面是自己的东西。
            return True
        if self._session_ok():
            return True
        if path.startswith("/api/"):
            self._json(
                {"ok": False, "error": "请先登录", "login_required": True},
                HTTPStatus.UNAUTHORIZED,
            )
        else:
            # 带了个「请先登录」的提示条，**不是**沉默地把同一张表单再渲染一遍。
            # 走到这儿说明**口令那层已经过了**（带对了 `?token=` 或口令 cookie，否则
            # 早在 `_check_auth` 就被假页面拦下了），只是会话还没过。页面上若无一个字，
            # 看着就像「填错了」，再乱试两次就吃 24 小时禁令了。提示语刻意只说
            # 「请先登录」：不提口令、不提秘钥，跟 API 那条 401 是同一句话，
            # 蒙在鼓里的人照样看不出这上面还有一层口令。
            self._login_page("请先登录")
        return False

    def _door_open(self) -> bool:
        """还有门站着吗 —— 只要有一层在拦人，秘钥错误保护就算数。

        用户 2026-10-09 要的禁令是挂在**那张假页面**上的：他先设口令，那张页面才
        开始装成秘钥页，也才开始有人来猜。所以不能只在「秘钥登录开着」的时候才禁 ——
        只开口令的那台机器，猜两次一样要停下来等 24 小时。

        两层都关掉才是真正的自救通道（禁令只在进程内存里，重启容器也能清）。
        """
        return bool(self._login_required() or self._token_door())

    def _deny_banned(self, left: int, path: str) -> None:
        msg = f"秘钥错太多次了，还要等 {_ban_text(left)}才能再访问。"
        if path.startswith("/api/"):
            self._json(
                {"ok": False, "error": msg, "banned": True, "retry_after": left},
                HTTPStatus.FORBIDDEN,
            )
            return
        html = _BANNED_HTML.replace("{msg}", html_escape(msg))
        extra = {}
        if not self._device_id():
            extra["Set-Cookie"] = _device_cookie(secrets.token_hex(16))
        self._send(
            HTTPStatus.FORBIDDEN,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            extra,
        )

    def _session_ok(self) -> bool:
        """浏览器带来的会话 cookie 还算数吗。

        cookie 里存的是 `sha256(盐|秘钥|版本号)`，所以**改一次秘钥、或者按一次
        退出，所有旧 cookie 一起作废** —— 不需要会话表，也不需要谁去清理。
        """
        from . import settings as st

        jar = self._cookies()
        if st.SESSION_COOKIE not in jar:
            return False
        try:
            got = jar[st.SESSION_COOKIE].value or ""
            want = st.session_value()
            return bool(want) and secrets.compare_digest(got, want)
        except Exception:  # noqa: BLE001
            return False

    def _need_token_page(self) -> None:
        """没带口令的浏览器访问 —— **装成秘钥登录页**，而不是给一张口令说明。

        用户 2026-10-09：「设置访问口令以后，这个页面要伪装成秘钥进入」。以前这里
        是另一张页面，写着「羊毛日历只对知道口令的人开放，把口令接在地址后面就能进：
        `?token=你的口令`」，还告诉人「口令在 config.json 的 web.token / settings 表的
        web_token 里」—— 谁看见谁就知道有这么一道门、该去哪儿找钥匙。现在它和秘钥
        登录页**共用同一份 HTML**，一个字的口令线索都没有。

        **但它是假的**（用户同一天返工定的）：「那张页面什么都不认，填正确的秘钥也
        不能通过，不能放行」。表单提交到 `_api_login`，那边一看见「口令门站着、这次
        又没带对」就直接回「秘钥不对」并记一次错误 —— 站在这张页面前面猜，唯一的
        结果是 24 小时禁令。主人要进来得先带上 `?token=`（`/日历` 回的地址里就有），
        那时候看到的还是同一张脸，但那张才认秘钥。

        `plant_token=False`：**绝不能**在这里种口令 cookie。口令页只在「口令没带对」
        的时候才会走到，在这儿把口令发给对方，等于这道门不存在。
        """
        left = _login_blocked(self._login_fail_keys())
        if left:
            # 这张页面同样是「猜秘钥」的地方，禁令一样管 —— 而且**不看秘钥登录那个
            # 开关**：口令本身也是一把可猜的钥匙，输了两次就该停下来等 24 小时。
            self._deny_banned(left, self.path)
            return
        self._login_page(plant_token=False)

    def _login_page(self, message: str = "", plant_token: bool = True) -> None:
        """给浏览器一张登录表单。`message` 非空就是上一次没登进去。

        `plant_token=False` 给假页面用（见 `_need_token_page`）—— 那时候对方还没
        证明自己知道口令，不能再把口令 cookie 发给他。
        """
        msg = f'<p class="msg">{html_escape(message)}</p>' if message else ""
        html = _LOGIN_HTML.replace("{msg}", msg)
        cookies = []
        token = self._effective_token()
        if token and plant_token:
            # 顺手把口令 cookie 种上：这样表单 POST 到 /api/login 时不用再带参数，
            # 「先口令、后秘钥」两层就自然串起来了。只有**已经带对了口令**的请求
            # 才会走到这里（`plant_token=True`），所以这不等于把口令发给陌生人。
            cookies.append(_token_cookie(token))
        if not self._device_id():
            # 第一次来就发个设备号。秘钥错误保护要按「同一台设备」封人，而浏览器
            # 自己不带身份 —— 这一趟不给号，后面那几次错误就只能算在 IP 头上。
            cookies.append(_device_cookie(secrets.token_hex(16)))
        extra = {"Set-Cookie": cookies} if cookies else None
        self._send(
            HTTPStatus.UNAUTHORIZED,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            extra,
        )

    def _raw_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _body(self) -> dict[str, Any]:
        raw = self._raw_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _token_door(self) -> bool:
        """口令那道门站着吗 —— 设了口令**并且**开关是开的。

        「这层门算不算数」和「这次带没带对口令」是两件事：只有前者为真，没带对时
        才谈得上「假页面」（见 `_api_login`）。所以 `_token_ok` 先问它。
        """
        return bool(self._effective_token() and _token_required(True))

    def _token_ok(self) -> bool:
        """这次请求有没有带对的口令 —— 查询串、`X-Token` 头、cookie 三选一。

        **口令的三种带法只在这里判断一次**：`_check_auth`（拦不拦）、`_door_open`
        （禁令算不算数）和 `_api_login`（这张是假页面还是真登录页）都要问同一个
        问题，各写一遍迟早对不上。门没站着的时候一律 False。
        """
        if not self._token_door():
            return False
        token = self._effective_token()
        qs = parse_qs(urlparse(self.path).query)
        if _same((qs.get("token") or [""])[0], token):
            return True
        if _same(self.headers.get("X-Token") or "", token):
            return True
        jar = self._cookies()
        return COOKIE_NAME in jar and _same(unquote(jar[COOKIE_NAME].value or ""), token)

    def _check_auth(self, parsed) -> Optional[str]:
        """返回错误响应字符串；通过校验返回 None。

        `parsed` 现在用不着了（具体判断在 `_token_ok`），留着是为了三条路由的调用
        点不用跟着改。
        """
        if not self._token_door():
            # 没设口令，或者页面上把「需要口令」关掉了。自用的家用 NAS 上这很常见 ——
            # 每次换台设备都要从日志里翻 token，实在太烦。
            return None
        if self._token_ok():
            return None
        # 一句话都不许多说：匿名打 /api/xxx 的人连「这个站有口令这回事」都不该知道
        # （页面那层已经假装成秘钥登录页了，JSON 里再提一句 ?token= 就白装了）。
        return "未授权"

    def _serve_static(self, path: str, extra: Optional[dict[str, str]] = None) -> None:
        rel = unquote(path).lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "路径不合法")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, f"没有这个文件：{rel}")
            return
        self._send(HTTPStatus.OK, target.read_bytes(), _content_type(target), extra)

    # -- 路由 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        # /api/health 不校验 token：Docker 的 HEALTHCHECK 不带 token 来打它，
        # 放在鉴权后面会导致容器永远 unhealthy。它只回时间和时区，不泄露什么。
        if path == "/api/health":
            self._json({"ok": True, "time": tu.to_minute(tu.now()), "tz": tu.tz_name()})
            return

        err = self._check_auth(parsed)
        if err:
            if not path.startswith("/api/"):
                self._need_token_page()
            else:
                self._error(HTTPStatus.UNAUTHORIZED, err)
            return

        if not self._login_gate(path):
            # 「整个页面都要登录才能看」（用户 2026-10-09 定的）——
            # 所以这里拦的是**所有** GET，不只是设置页。接口回 JSON 让前端
            # 能认出是没登录（而不是别的一堆错），页面回登录表单。
            return

        qs = parse_qs(parsed.query)
        first = lambda key, default="": (qs.get(key) or [default])[0]  # noqa: E731

        try:
            if path == "/api/settings":
                self._api_settings_get()
                return
            if path == "/api/stats":
                self._json({"ok": True, "data": db.stats()})
                return
            if path == "/api/status":
                self._json({"ok": True, "data": _bot_status()})
                return
            if path == "/api/help":
                # 帮助只有一份，就存在 `pipeline.HELP_TEXT` 里 —— 微信里发
                # 「使用帮助」拿到的和页面这条按钮点开的是**同一段字**，
                # 免得两边各写一份、改一边忘一边。
                self._json({"ok": True, "data": {"text": pl.HELP_TEXT}})
                return
            if path == "/api/qr.png":
                self._api_qr()
                return
            if path == "/api/activities":
                self._api_list(first)
                return
            if path == "/api/calendar":
                self._api_calendar(first)
                return
            if path.startswith("/api/activity/"):
                self._api_get(path)
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("GET %s 出错", self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
            return

        if path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "没有这个接口")
            return

        extra = {}
        token = self._effective_token()
        if token:  # 带 token 访问过就把 cookie 种上，后面刷新页面就不用再带
            extra["Set-Cookie"] = _token_cookie(token)
        self._serve_static(path, extra or None)

    def _api_qr(self) -> None:
        """把机器人当前的登录二维码发给页面。

        机器人跑在 NAS 上没有屏幕，`main.py bot` 只能把二维码存成文件；
        这个接口就是让用户掏出手机扫的那个「屏幕」。
        """
        fn = _STATUS_PROVIDER["fn"]
        path = None
        if fn is not None:
            try:
                path = (fn() or {}).get("qr_path")
            except Exception as exc:  # noqa: BLE001
                log.warning("取二维码路径失败：%s", exc)
        if not path:
            self._error(HTTPStatus.NOT_FOUND, "现在没有待扫的二维码")
            return
        target = Path(path)
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "二维码文件不在了")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        self._send(HTTPStatus.OK, target.read_bytes(),
                   ctype or "image/png")

    def _api_list(self, first) -> None:
        today = tu.today()
        d_from = _parse_day(first("from")) or today.replace(day=1)
        d_to = _parse_day(first("to")) or (d_from + timedelta(days=40))
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        rows = db.list_range(d_from, d_to, include_expired=True)
        status = first("status")

        # 一次性活动和重复活动分开筛：重复活动一行有很多场，却只有一格 status。
        once: list[dict] = []
        rep: list[dict] = []
        for row in rows:
            (rep if rp.rule_of(row) is not None else once).append(row)

        once = _apply_filters(once, status, first("keyword"), first("platform"))
        rep = _apply_filters(rep, "all", first("keyword"), first("platform"))
        if status in ("pending", "done", "missed"):
            # 系列已经过期的整条都不该出现，不管它哪一天曾经是什么状态
            rep = [r for r in rep if (r.get("status") or "pending") != "expired"]

        marks = db.done_map([r["id"] for r in rep])
        # 一次请求只取一次钟：同一次响应里所有场次对「现在」的看法要一致，
        # 否则跨秒那一刻会出现「一半待抢一半未抢到」。
        ref = tu.now()
        rep = _filter_by_occurrence(rep, status, d_from, d_to, marks, ref)
        rows = sorted(
            once + rep,
            key=lambda r: (r.get("grab_at") or "", r.get("start_at") or "", r["id"]),
        )
        self._json(
            {
                "ok": True,
                "from": d_from.isoformat(),
                "to": d_to.isoformat(),
                "today": today.isoformat(),
                "count": len(rows),
                "data": [
                    _with_claimable(r, d_from, d_to, marks.get(r["id"], ()), ref)
                    for r in rows
                ],
            }
        )

    def _api_calendar(self, first) -> None:
        """日历格子的附注：农历、节气、节日、法定节假日。

        算法在 `app/lunar.py`（纯标准库），前端只管把 `text` 摆到格子左下角、
        `holiday` 摆到右上角 —— 谁先谁后的优先级也在后端定好，免得两边各判一遍。
        """
        today = tu.today()
        d_from = _parse_day(first("from")) or today.replace(day=1)
        d_to = _parse_day(first("to")) or (d_from + timedelta(days=40))
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        if (d_to - d_from).days > _CALENDAR_MAX_DAYS:
            d_to = d_from + timedelta(days=_CALENDAR_MAX_DAYS)
        days: dict[str, dict[str, str]] = {}
        day = d_from
        while day <= d_to:
            days[day.isoformat()] = lunar.day_info(day)
            day += timedelta(days=1)
        self._json(
            {
                "ok": True,
                "from": d_from.isoformat(),
                "to": d_to.isoformat(),
                "count": len(days),
                "days": days,
            }
        )

    def _api_get(self, path: str) -> None:
        tail = path[len("/api/activity/") :]
        if not tail.isdigit():
            self._error(HTTPStatus.BAD_REQUEST, "id 必须是数字")
            return
        row = db.get(int(tail))
        if not row:
            self._error(HTTPStatus.NOT_FOUND, "没有这条活动")
            return
        out = _with_repeat(row)
        if "repeat" in out:
            out["done_days"] = db.done_days(int(tail))
        self._json({"ok": True, "data": out})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        # 登录接口排在最前面 —— 在**口令这道门之前**，也在「先登录」这道门之前。
        #
        # 口令那道门之前是必须的：口令页如今伪装成登录页（见 `_need_token_page`），
        # 浏览器是在**没带对口令**的时候才看见那张表单的，它提交上来的 POST 自然
        # 也没有 `?token=`；要是先过 `_check_auth`，主人自己都永远进不去。
        #
        # 所以 `/api/login` 得自己把两件事都办了：口令（`as_token`）和秘钥
        # （`as_key`），见 `_api_login`。
        #
        # 「先登录」那道门之前是当然的 —— 没登录才需要登录。
        #
        # 但它**不**排在「秘钥错误保护」前面：那是 `_api_login()` 自己查的，
        # 因为被禁之后连表单都不该收。
        if path == "/api/login":
            self._api_login()
            return
        err = self._check_auth(parsed)
        if err:
            self._error(HTTPStatus.UNAUTHORIZED, err)
            return
        if not self._login_gate(path):
            return
        data = self._body()

        try:
            if path == "/api/logout":
                self._api_logout()
                return
            if path == "/api/activity":
                self._api_create(data)
                return
            if path == "/api/settings":
                self._api_settings_post(data)
                return
            if path == "/api/settings/test":
                self._api_settings_test(data)
                return
            if path == "/api/qr/refresh":
                self._api_qr_refresh()
                return
            if path == "/api/wechat/logout":
                self._api_wechat_logout()
                return
            if path.startswith("/api/activity/"):
                self._api_update(path, data)
                return
        except Exception as exc:  # noqa: BLE001
            log.exception("POST %s 出错", self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")
            return
        self._error(HTTPStatus.NOT_FOUND, "没有这个接口")

    def _api_create(self, data: dict[str, Any]) -> None:
        title = (data.get("title") or "").strip()
        if not title:
            self._error(HTTPStatus.BAD_REQUEST, "title 不能为空")
            return
        payload = {k: v for k, v in data.items() if k in db.FIELDS}
        try:
            rep = _repeat_fields(data)
            quota = _quota_fields(data)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if rep:
            _merge_repeat(payload, rep, replace=True)
        payload.update(quota)
        payload["title"] = title
        payload.setdefault("source", "manual")
        new_id = db.insert_activity(payload)
        if not new_id:
            self._error(HTTPStatus.CONFLICT, "写入失败或重复")
            return
        self._json({"ok": True, "id": new_id}, HTTPStatus.CREATED)

    def _api_update(self, path: str, data: dict[str, Any]) -> None:
        tail = path[len("/api/activity/") :]
        parts = tail.split("/")
        if not parts[0].isdigit():
            self._error(HTTPStatus.BAD_REQUEST, "id 必须是数字")
            return
        aid = int(parts[0])
        row = db.get(aid)
        if not row:
            self._error(HTTPStatus.NOT_FOUND, "没有这条活动")
            return

        # /api/activity/<id>/status  {status: done}
        if len(parts) > 1 and parts[1] == "status":
            status = (data.get("status") or "").strip()
            if status not in ("pending", "done", "expired"):
                self._error(HTTPStatus.BAD_REQUEST, "status 只能是 pending/done/expired")
                return
            db.set_status(aid, status)
            self._json({"ok": True, "id": aid, "status": status})
            return

        # /api/activity/<id>/done  {day: 2026-10-07}（撤销走 /undo）
        if len(parts) > 1 and parts[1] in ("done", "undo"):
            self._toggle_done(aid, row, data, parts[1] == "done")
            return

        fields = {k: v for k, v in data.items() if k in db.FIELDS}
        try:
            rep = _repeat_fields(data)
            quota = _quota_fields(data)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        _merge_repeat(fields, rep, replace=False)
        fields.update(quota)
        if not fields:
            self._error(HTTPStatus.BAD_REQUEST, "没有可更新的字段")
            return
        if not db.update_activity(aid, **fields):
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "更新失败")
            return
        self._json({"ok": True, "id": aid, "updated": sorted(fields)})

    def _toggle_done(self, aid: int, row: dict[str, Any], data: dict[str, Any], done: bool) -> None:
        """把一条活动的某一天记成「已领」/「还没领」。

        重复活动**不能**用行级的 status：一行有几十场，一格 status 装不下，今天点
        了完成明天整条都成了完成，用户就再也看不到「这周还剩几次」。所以按天记进
        ``activity_done`` 台账，配额也按天算 —— 周额度满了，本周剩下那几天一起变
        成已完成，下周自动恢复待抢。
        """
        day = _parse_day(str(data.get("day") or "").strip())
        if day is None:
            self._error(HTTPStatus.BAD_REQUEST, "day 要写成 2026-10-07 这样的日期")
            return
        if rp.rule_of(row) is None:
            # 一次性活动没有「哪一天」这回事，直接改行状态，行为和以前完全一致
            db.set_status(aid, "done" if done else "pending")
            self._json({"ok": True, "id": aid, "day": day.isoformat(), "done": done})
            return
        if done:
            db.mark_done(aid, day)
        else:
            db.unmark_done(aid, day)
        marks = db.done_days(aid)
        self._json(
            {
                "ok": True,
                "id": aid,
                "day": day.isoformat(),
                "done": done,
                "done_days": marks,
                "status": rp.occurrence_status(row, day, marks),
                "quota": rp.quota_state(row, day, marks),
            }
        )

    # -- 登录 ----------------------------------------------------------

    def _api_login(self) -> None:
        """校验访问秘钥，种下会话 cookie。

        同一个接口吃两种请求：登录页那个**表单**（`application/x-www-form-urlencoded`）
        和脚本／测试发的 JSON。表单回 HTML、JSON 回 JSON —— 两边都不用为对方让步。

        **用户名在这一版没有了**（用户 2026-10-09 说「改成秘钥登录，不要用户名了」），
        所以比对的就是一把秘钥。老客户端发的 `password` 字段照样认，纯粹为了不让
        已经存好的书签／脚本一夜之间全失效。

        这里同时是**口令页那张页面**的落点（`_need_token_page`）。两张页面认的东西
        不一样 —— 用户 2026-10-09 把这一点问清楚之后才让动手：

        * 没带对口令时看见的那张**是假的**：**什么都不认**。框里填访问秘钥、哪怕填的
          就是那把对的秘钥，也一律「秘钥不对」，一步都别想进来。它的用处只有一个：
          让不知道口令的人在那儿试，试两次就被 24 小时错误保护关掉。
        * 口令带对了（`?token=`／`X-Token`／cookie 三选一）之后看见的才是**真登录页**，
          认得访问秘钥。只开秘钥、没开口令的机器上看到的就是这张。
          两张页面**外观一模一样**（同一份 `_LOGIN_HTML`，都写着「请输入访问秘钥」），
          所以没带口令的人察觉不到上面还有一层口令。
        * 成功时**顺手补种口令 cookie**：假页面不种（那正是它拦人的理由），真页面这
          边补上，从门口输一次秘钥就能一路进到里面，不用再回去拼网址。
        * 失败**一律记账**，不管秘钥登录开关有没有开、也不管填的是口令还是秘钥 ——
          那张假页面就是猜钥匙的地方。
        """
        from . import settings as st

        raw = self._raw_body()
        ctype = (self.headers.get("Content-Type") or "").lower()
        form = "application/x-www-form-urlencoded" in ctype
        fields: dict[str, Any] = {}
        if form:
            for key, value in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items():
                fields[key] = (value or [""])[0]
        else:
            try:
                got = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                got = {}
            if isinstance(got, dict):
                fields = got
        key = str(fields.get("key") or fields.get("password") or "")

        token = self._effective_token()
        had_token = self._token_ok()
        # **假页面**：口令那道门站着，可这一次没带对。那么提交上来的东西一概不认 ——
        # 「填对秘钥就能从这儿进去」正是用户明确否掉的（那等于口令这层白设了）。
        fake = self._token_door() and not had_token

        ip = self.client_address[0] if self.client_address else "-"
        device = self._device_id()
        pairs = self._login_fail_keys()
        left = _login_blocked(pairs)
        if left:
            # 被禁期间连表单都不收 —— 用户要的是「禁止访问 24 小时」。
            self._deny_banned(left, "/api/login" if not form else "/")
            return

        if fake:
            _login_record_fail(pairs)
            log.warning(
                "没带口令的页面上有人试钥匙，来自 %s（设备 %s）",
                ip,
                device[:8] if device else "没记号",
            )
            self._login_denied("秘钥不对", form, False)
            return

        if not st.login_ready():
            # 真页面，但这台机器上压根没有秘钥这道门（只开了口令，或者两层都关）。
            # 口令既然已经过了就不用再要什么 —— 直接把人送进去，别在这儿绕圈。
            if had_token:
                _login_reset(pairs)
                cookies = [_token_cookie(token)] if token else []
                cookies.append(_device_cookie(device or secrets.token_hex(16)))
                self._login_ok(form, cookies)
                return
            # 两层都没开。这时候**放行才是错的** —— 页面本来就没打算要登录，
            # 凭空发一张会话 cookie 只会让人以为登进去了。
            self._login_denied("这个页面还没开启秘钥登录", form, had_token)
            return

        if not _same(key, st.login_key()):
            _login_record_fail(pairs)
            log.warning(
                "秘钥不对，来自 %s（设备 %s）", ip, device[:8] if device else "没记号"
            )
            self._login_denied("秘钥不对", form, had_token)
            return

        _login_reset(pairs)
        log.info("秘钥正确，登录成功，来自 %s", ip)
        cookies = []
        if token:
            # 假页面那边不种口令 cookie（见上面 docstring），补在这儿。
            cookies.append(_token_cookie(token))
        cookies.append(
            f"{st.SESSION_COOKIE}={st.session_value()}; Path=/; "
            f"Max-Age={st.LOGIN_SESSION_DAYS * 86400}; SameSite=Lax; HttpOnly"
        )
        cookies.append(_device_cookie(device or secrets.token_hex(16)))
        self._login_ok(form, cookies)

    def _login_ok(self, form: bool, cookies: list) -> None:
        """进去了：表单回 303，脚本回 JSON。"""
        if form:
            # 表单登录回 303（不是 302）：把 POST 换成 GET，否则用户一刷新页面
            # 浏览器就问「要不要重新提交表单」。
            self._send(
                HTTPStatus.SEE_OTHER,
                b"",
                "text/html; charset=utf-8",
                {"Location": "/", "Set-Cookie": cookies},
            )
            return
        self._json({"ok": True}, extra={"Set-Cookie": cookies})

    def _login_denied(self, message: str, form: bool, plant_token: bool = False) -> None:
        """没登进去。`plant_token` 只在「对方已经证明自己知道口令」时为真 ——
        口令页上的失败绝不能顺手把口令 cookie 发出去。"""
        if form:
            self._login_page(message, plant_token=plant_token)
        else:
            self._json({"ok": False, "error": message}, HTTPStatus.UNAUTHORIZED)

    def _api_logout(self) -> None:
        """退出登录：**作废服务端会话**，再让浏览器把 cookie 抹掉。

        只发那条 `Max-Age=0` 是不够的 —— 会话值就是 `sha256(盐|秘钥|版本号)`，
        它不含时间也不含随机数，光清浏览器的话，抄走过 cookie 值的人照样能一直用
        （真机上验过：退出之后拿旧 cookie 打 `/api/calendar` 还是 200）。所以先把
        版本号 +1，所有旧会话一起失效。
        口令那层不跟着退 —— 它是浏览器记住的入场券。
        """
        from . import settings as st

        try:
            st.bump_login_epoch()
        except Exception:  # noqa: BLE001
            log.exception("退出登录时没能作废会话（cookie 仍会被清掉）")
        self._json(
            {"ok": True},
            extra={
                "Set-Cookie": (
                    f"{st.SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly"
                )
            },
        )

    # -- 设置（AI 模型） ----------------------------------------------

    def _api_settings_get(self) -> None:
        from . import settings as st

        self._json({"ok": True, "data": st.public_view(_CONFIG["cfg"])})

    def _api_settings_post(self, data: dict[str, Any]) -> None:
        from . import settings as st

        values = {k: v for k, v in data.items() if k in st.ALL_KEYS}
        incoming_key = str(values.get("api_key") or "").strip()
        if not incoming_key:
            # 空的 api_key 在页面上的意思是「我没动这个框」，不是「把 key 删掉」。
            # 真删得显式说 clear_api_key —— 否则用户只改了模型名、key 输入框留空，
            # 已经配好的 key 就被静默清掉了。
            values.pop("api_key", None)
            if data.get("clear_api_key"):
                values["api_key"] = ""
        elif incoming_key == st.public_view(_CONFIG["cfg"])["api_key_masked"]:
            # 页面回显的是掩码，用户没动它就不能把 "sk-1…cdef" 当成新 key 存进去
            values.pop("api_key", None)
        if not values:
            self._error(HTTPStatus.BAD_REQUEST, "没有可保存的设置")
            return

        # -- 自定义口令 -------------------------------------------------
        # 和 api_key 一模一样的规矩：空 = 没动这个框、掩码 = 没动这个框，
        # 想清掉得显式说 clear_web_token。**绝不能**把掩码当成新口令存进去 ——
        # 那样用户第二天会发现口令变成了「y1ql…QN」，谁都不知道自己输的是啥。
        if "web_token" in values:
            incoming = str(values.get("web_token") or "").strip()
            if not incoming:
                values.pop("web_token", None)
                if data.get("clear_web_token"):
                    values["web_token"] = ""
            elif incoming == st.public_view(_CONFIG["cfg"])["web_token_masked"]:
                values.pop("web_token", None)
            elif len(incoming) < st.WEB_TOKEN_MIN:
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    f"口令至少要 {st.WEB_TOKEN_MIN} 个字符",
                )
                return

        # -- 访问秘钥 ----------------------------------------------------
        # 用户名在这一版没有了，所以只剩这一把秘钥。**只在这里校验格式**：
        # 入库之前就挡住不合规的，绝不「偷偷帮你补一位」—— 用户以为自己设了
        # 一个弱秘钥、实际被系统改过，比直接报错难查一百倍。
        if "login_key" in values:
            incoming = str(values.get("login_key") or "")
            if not incoming:
                values.pop("login_key", None)
                if data.get("clear_login_key"):
                    values["login_key"] = ""
            elif incoming == st.public_view(_CONFIG["cfg"])["login_key_masked"]:
                values.pop("login_key", None)
            else:
                problem = st.key_problem(incoming)
                if problem:
                    self._error(HTTPStatus.BAD_REQUEST, problem)
                    return

        # -- 开秘钥登录就得能登进去 --------------------------------------
        # 只看开关是错的：开关打开、秘钥空着，`login_ready()` 会当没开，
        # 页面看着「已经保护起来了」其实门大开着。宁可当场报错让用户补齐 ——
        # 这也避免了另一个方向的坑：秘钥没配就拦人，等于把页面锁死，谁都进不去。
        if _truthy(values.get("login_enabled")):
            key_now = str(values.get("login_key") or "") or st.login_key()
            problem = st.key_problem(key_now)
            if problem:
                # 包括「秘钥还是空的」：`key_problem("")` 回「秘钥至少要 8 位」，
                # 对着空框说这句正好，不用再单写一条「请先填秘钥」。
                self._error(HTTPStatus.BAD_REQUEST, problem)
                return

        # -- 开外网域名就得有域名 ----------------------------------------
        if _truthy(values.get("domain_enabled")):
            url_now = str(values.get("domain_url") or "").strip() or st.domain_url()
            if not url_now:
                self._error(HTTPStatus.BAD_REQUEST, "开启外网域名要先填域名地址")
                return

        # -- 一言来源 ----------------------------------------------------
        # 用户 2026-10-09：「根据一言官方提供的分类使用方法，在设置里面增加一言的
        # 自定义源」。存的是他自己填的那串（分类字母或一个完整网址），**校验放在
        # 库里之前**：不认的字母当场报错，绝不悄悄丢掉一个字母。
        # 空串是合法值（回到默认分类），`_write` 正好把空串当「删掉这行」。
        if "sayings_source" in values:
            from . import sayings as sy

            incoming = str(values.get("sayings_source") or "").strip()
            problem = sy.source_problem(incoming)
            if problem:
                self._error(HTTPStatus.BAD_REQUEST, problem)
                return
            values["sayings_source"] = incoming

        if "remind_minutes" in values:
            try:
                minutes = int(float(str(values["remind_minutes"]).strip()))
            except (TypeError, ValueError):
                self._error(HTTPStatus.BAD_REQUEST, "提醒提前量要填分钟数")
                return
            if minutes < st.REMIND_MIN_MINUTES:
                # 夹紧成 5 是错的：用户明明填了 1，页面却回显 5，那是偷偷改人的数。
                # 拒绝、说明原因，让他自己改 —— 和配额那套「宁可大声报错」一致。
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    f"提醒提前量最少 {st.REMIND_MIN_MINUTES} 分钟",
                )
                return
            values["remind_minutes"] = minutes
        st.update(values)
        if "api_key" in values:
            # 用户刚说了一把新 key（或者刚清掉），之前记的失败原因就过期了 ——
            # 留着它会让页面上的红条继续挂着，而用户明明已经改过了。
            st.clear_last_error()
        if "remind_enabled" in values or "remind_minutes" in values:
            _sync_reminder()
        self._json({"ok": True, "data": st.public_view(_CONFIG["cfg"])})

    def _api_settings_test(self, data: dict[str, Any]) -> None:
        """拿页面上填的值（还没保存也行）真打一次模型，让用户当场知道对不对。"""
        from . import llm as llm_mod
        from . import settings as st

        cfg = _CONFIG["cfg"]
        provider: dict[str, Any] = {}
        if cfg is not None:
            try:
                provider = dict(cfg.active_provider)
            except Exception:  # noqa: BLE001
                provider = {}
        # 库里存的要盖住 config.json —— 否则「保存过了、但这次没重填 key」的测试
        # 会莫名其妙地说「请先填 API key」，而机器人其实早就能干活了。
        provider.update(st.provider_override())
        for key in st.PROVIDER_KEYS:
            if key in data and str(data.get(key) or "").strip():
                provider[key] = data[key]
        if not provider.get("api_key"):
            self._json({"ok": False, "error": "请先填 API key"})
            return
        try:
            answer = llm_mod.LLM(provider).chat("只回复两个字：可用")
        except Exception as exc:  # noqa: BLE001
            # `LLMError` 的消息本身就是给人看的人话（见 `_explain_http`），
            # 前面再挂个 "LLMError: " 只会显得吓人。
            if isinstance(exc, llm_mod.LLMError):
                self._json({"ok": False, "error": str(exc)})
            else:
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        # 测试通了说明 key 是好的，把之前记的失败清掉
        st.clear_last_error()
        self._json({"ok": True, "reply": (answer or "").strip()[:60]})

    def _api_qr_refresh(self) -> None:
        self._control("qr_refresh")

    def _api_wechat_logout(self) -> None:
        self._control("wechat_logout")

    def _control(self, key: str) -> None:
        """跑一个注册进来的动作（换二维码 / 退出微信），回 `{"ok", "note"}`。

        没注册＝机器人没在这台机器上跑（`python main.py bot` 那种只有命令行、
        没有页面的用法），这时得说清楚，别让页面显示一句「已退出」而其实没人接。
        """
        fn = _CONTROLS[key]
        if fn is None:
            self._json(
                {"ok": False, "error": "机器人没在跑（要用 `python main.py all` 启动）"}
            )
            return
        try:
            note = fn() or ""
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._json({"ok": True, "note": note})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        err = self._check_auth(parsed)
        if err:
            self._error(HTTPStatus.UNAUTHORIZED, err)
            return
        if not self._login_gate(path):
            return
        if path.startswith("/api/activity/"):
            tail = path[len("/api/activity/") :]
            if not tail.isdigit():
                self._error(HTTPStatus.BAD_REQUEST, "id 必须是数字")
                return
            aid = int(tail)
            if not db.get(aid):
                self._error(HTTPStatus.NOT_FOUND, "没有这条活动")
                return
            db.delete_activity(aid)
            self._json({"ok": True, "deleted": aid})
            return
        self._error(HTTPStatus.NOT_FOUND, "没有这个接口")


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def resolve_port(web_cfg: dict[str, Any]) -> int:
    """把 `web.port` 配置解析成端口号。

    不能用 `port or 8080`：那样显式写的 0 会被当成假值吃掉，而 0 的意思是
    「让系统挑一个空闲端口」—— 测试就是靠它避免去抢真端口。
    兜底值走 `config.DEFAULT_WEB_PORT`（**全项目只有一个默认端口**）。
    """
    from . import config as cfgmod

    raw = web_cfg.get("port")
    return cfgmod.DEFAULT_WEB_PORT if raw in (None, "") else int(raw)


def create_server(cfg: dict[str, Any]) -> ThreadingHTTPServer:
    web_cfg = dict(cfg.get("web") or {})
    host = web_cfg.get("host") or "127.0.0.1"
    port = resolve_port(web_cfg)
    token = str(web_cfg.get("token") or "")

    handler = type(
        "BoundHandler",
        (Handler,),
        {"config": cfg, "token": token},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def start_background(cfg: dict[str, Any]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    httpd = create_server(cfg)
    thread = threading.Thread(target=httpd.serve_forever, name="woolbot-web", daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    log.info("日历页面已启动：%s", _startup_url(cfg, host, port))
    return httpd, thread


def _startup_url(cfg: dict[str, Any], host: str, port: int) -> str:
    """启动日志里那行地址。**和 `/日历`、`print_url()` 走同一个函数。**

    这里以前自己拼 `http://{host}:{port}/`，于是容器日志里永远是一行
    `http://0.0.0.0:8080/` —— 看着像个地址，谁点谁失败。绑定地址是通配符时换成
    `<NAS的局域网IP>` 占位符（容器里）或本机局域网地址，剩下的交给 `front_url()`：
    域名开着用域名，否则用 `config.json` 里部署时写下的 `web.public_host`。
    """
    if host in ("", "0.0.0.0", "::", "::0", "*"):
        host = "<NAS的局域网IP>" if Path("/.dockerenv").exists() else ""
    from . import settings as st

    try:
        return st.front_url(cfg, host=host, port=port)
    except Exception as exc:  # noqa: BLE001 —— 地址算不出来也不该拦住启动
        log.debug("算日历地址失败，退回最简单的拼法：%s", exc)
        return f"http://{host or '127.0.0.1'}:{port}/"
