#!/usr/bin/env python3
"""羊毛机器人入口。

    python main.py web     只启动前端日历
    python main.py chat    命令行里直接和机器人对话（本机调试用，不碰微信）
    python main.py bot     只启动微信 ClawBot 通道
    python main.py all     微信通道 + 日历页面（NAS 上跑这个）
    python main.py check   自检：配置、数据库、模型连通性
    python main.py backfill-repeat   给老活动补「每天/每周」规则（不调模型，--all 连已有的一起重判）
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config as cfgmod  # noqa: E402
from app import db, settings as st, timeutil as tu  # noqa: E402
from app.llm import LLM, LLMError  # noqa: E402

log = logging.getLogger("woolbot")


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，输出里的 emoji 会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


class _TzFormatter(logging.Formatter):
    """日志时间戳用配置时区。

    容器里的系统时区是 UTC，默认 formatter 会把「消息几点收到的」写成 UTC，
    对着排时间问题要多一次心算。这里直接按 timeutil 的时区渲染。
    """

    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = tu.from_timestamp(record.created)
        return dt.strftime(datefmt) if datefmt else dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(cfg: cfgmod.Config) -> None:
    log_cfg = cfg.get("log") or {}
    level = str(log_cfg.get("level") or "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(_TzFormatter(
        fmt="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))


def boot(cfg_path: str | None) -> cfgmod.Config:
    cfgmod.ensure_config_file(Path(cfg_path) if cfg_path else None)
    cfg = cfgmod.load(Path(cfg_path) if cfg_path else None)
    tu.configure(cfg.get("timezone") or "Asia/Shanghai")
    db_path = cfg.path(cfg.get("db_path") or "data/woolbot.db")
    db.init(db_path)
    setup_logging(cfg)
    logging.getLogger("woolbot").info(
        "配置载入完成：provider=%s model=%s tz=%s db=%s",
        cfg.active_provider_name,
        (cfg.active_provider or {}).get("model"),
        tu.tz_name(),
        db_path,
    )
    return cfg


def _lan_ip() -> str:
    """猜出本机的局域网地址，只为了在日志里给出一句能直接点开的地址。

    用 UDP connect 让内核挑一个出口网卡——**不会真的发包**，
    所以断网也不会卡住。挑不出来就返回空串。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()


def _in_container() -> bool:
    """是不是跑在容器里。

    容器里的「本机局域网 IP」是 docker 网桥地址（172.x），手机连不上它 ——
    这种地址比 0.0.0.0 更坏，因为看着像是能用的。
    """
    return Path("/.dockerenv").exists()


def _display_url(host: str, port: int, token: str = "", cfg: Any = None) -> str:
    """日历页面的地址。**统一由 `settings.front_url()` 算，这里不再自己拼。**

    `host` / `port` 只是兜底：容器里绑的是 0.0.0.0，看出来的宿主机地址是 docker
    网桥的 172.x，手机连不上它 —— 那种地址比 0.0.0.0 更坏，因为看着像是能用的。
    所以 `deploy/deploy_woolbot.py` 会把 NAS 的真实地址和映射端口写进 config.json 的
    `web.public_host` / `web.public_port`，`front_url()` 优先用它们。

    这个函数以前自己拼 `http://{host}:{port}/?token={token}`，于是「地址怎么算」
    有了第二个实现：域名开着的时候，启动日志里印出来的还是局域网地址。现在两条
    路走的是同一个函数。

    `token` 必须传**现在生效的那把**（`settings.web_token(cfg)`）。以前这里收的是
    `config.json` 里部署时生成的那把，用户在页面上改过口令之后，日志第二行印的就是
    个旧口令 —— 同一个进程的两行日志，两个不同的 token，第二个谁点谁进不去。
    """
    from app import settings as st

    fallback = host
    if fallback in ("", "0.0.0.0", "::", "::0", "*"):
        fallback = "<NAS的局域网IP>" if _in_container() else (_lan_ip() or "127.0.0.1")
    try:
        return st.front_url(cfg, host=fallback, port=port, token=token)
    except Exception as exc:  # noqa: BLE001 —— 地址算不出来也不该拦住启动
        log.debug("算日历地址失败，退回最简单的拼法：%s", exc)
        return f"http://{fallback}:{port}/" + (f"?token={token}" if token else "")


def print_url(label: str, host: str, port: int, token: str = "", cfg: Any = None) -> None:
    print(f"🗓  {label}：{_display_url(host, port, token, cfg)}")
    if _in_container() and not (cfg or {}).get("web", {}).get("public_host"):
        print("   把 <NAS的局域网IP> 换成 NAS 的真实地址（部署脚本结尾也会打印一份）")


def cmd_web(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    from app import web as webmod

    webmod.set_config(cfg)  # 设置面板要拿它显示底值（模型名、base_url）
    httpd = webmod.create_server(cfg.as_dict())
    port = httpd.server_address[1]
    # 口令现读设置（页面「设置」里改过就以库里为准）。config.json 里那把只在
    # 「从没动过设置」时才是对的 —— 见 `_display_url()` 与 README 的踩坑记录。
    token = st.web_token(cfg) or ""
    print_url("日历页面", (cfg.get("web") or {}).get("host") or "", port, token, cfg)
    print("   Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")
    finally:
        httpd.server_close()
    return 0


def _make_bot(cfg: cfgmod.Config, *, require_key: bool = True):
    """装配机器人。

    **没配 key 时不再返回一个「只会报错」的假模型**（早先那个 `_NoKeyLLM`）：
    那样的话用户在页面上填完 key 也还得重启容器才生效。现在一律用真的 `LLM`
    —— 它的配置是每次调用时现读的，页面上存了就立刻能用；缺 key 时它自己抛
    一句能照着做的提示。看日历、扫码登录本来就不需要模型。
    """
    from app.llm import LLM, NO_KEY_HINT
    from app.pipeline import Bot

    llm = LLM(cfg.active_provider)
    if not llm.configured:
        if require_key:
            raise SystemExit(f"❌ {NO_KEY_HINT}")
        log.warning("没配 API key：日历页照常可用，但机器人读不懂消息。")
        print(f"⚠️  没配 API key —— 日历页照常可用，但机器人读不懂消息。\n   {NO_KEY_HINT}")
    return Bot(cfg.as_dict(), llm)


def cmd_chat(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    bot = _make_bot(cfg)
    user = args.user or "cli"
    print("🐑 羊毛机器人（本机对话模式）")
    print(f"   现在时间：{tu.to_minute(tu.now())} {tu.tz_name()}")
    print("   直接说活动 / 直接问几点抢什么；/help 看帮助，quit 退出。\n")
    while True:
        try:
            text = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text.lower() in ("quit", "exit", "q", "退出"):
            break
        reply = bot.handle_text(text, user_id=user)
        print("\n🐑 " + (reply or "（没有回复）") + "\n")
    return 0


def _status_provider(client):
    """给网页用的机器人实况回调（登录状态 + 当前二维码路径）。"""

    def fn() -> dict:
        if client is None:
            return {"bot": "off"}
        qr = client.qr_file
        mtime = 0
        if qr:
            try:
                mtime = int(Path(qr).stat().st_mtime)
            except OSError:
                mtime = 0
        out = {
            "bot": "on",
            "logged_in": client.logged_in,
            "ready": client.ready.is_set(),
            "qr_path": str(qr) if qr else None,
            # 画不出二维码图时，网页上至少能把链接显示出来
            "qr_url": getattr(client, "qr_url", "") or "",
            # 二维码换新时 mtime 会变，页面据此决定要不要重新拉图
            "qr_token": mtime,
        }
        # 「有 token」不等于「收得到消息」，所以要把长轮询的心跳一起告诉页面
        try:
            out.update(client.health())
        except Exception as exc:  # noqa: BLE001 - 页面不该因为探测失败而 500
            out["health_error"] = f"{type(exc).__name__}: {exc}"
        return out

    return fn


def _qr_refresh_provider(client):
    """给网页「刷新二维码」按钮用的动作。

    以前按钮只是把磁盘上同一张旧图再下一次，用户点了看不出任何变化 ——
    现在真的去让登录循环丢掉当前二维码、重新申请一张。
    """

    def fn() -> str:
        if client is None:
            return ""
        if client.logged_in:
            return "微信已经登录了，不需要二维码"
        client.request_qr_refresh()
        return "已请求换一张二维码，几秒后自动出现"

    return fn


def _wechat_logout_provider(client):
    """给网页「退出微信」按钮用的动作。

    **它只清 NAS 本地那份会话**（`data/weixin_state.json` 里的 token 和游标），
    微信那边的授权要用户自己去「解除连接」—— 协议里没有撤权的接口。真正的用处
    是别让人干等那一小时冷却：清掉之后主循环立刻回去申请一张新二维码。
    """

    def fn() -> str:
        if client is None:
            return ""
        if not client.logged_in:
            return "本来就没登录，正在申请二维码"
        client.request_logout()
        return "已退出，几秒后会出现新的二维码"

    return fn


def cmd_bot(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    from app import reminder as reminder_mod
    from app import wechat

    bot = _make_bot(cfg, require_key=False)
    client = wechat.ClawBot(cfg.as_dict(), bot)
    print("📱 登录二维码会存到 "
          f"{client.qr_file or 'data/login_qr.png'}；"
          "想用手机扫码，建议改用 `python main.py all`，日历页上会直接显示。")
    # `bot` 这个命令没有网页，设置只能在 config.json／库里，所以这里按当前设置
    # 直接起停一次。开抢提醒不依赖页面，它只需要一个登录着的 ClawBot。
    reminders = reminder_mod.Reminder(client)
    reminders.sync()
    try:
        client.run()
    finally:
        reminders.stop()
    return 0


def cmd_all(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    from app import reminder as reminder_mod
    from app import web as webmod
    from app import wechat

    bot = _make_bot(cfg, require_key=False)
    client = wechat.ClawBot(cfg.as_dict(), bot)
    reminders = reminder_mod.Reminder(client)
    webmod.set_status_provider(_status_provider(client))
    webmod.set_qr_refresh(_qr_refresh_provider(client))
    webmod.set_wechat_logout(_wechat_logout_provider(client))
    webmod.set_config(cfg)
    webmod.set_reminder(reminders)

    web_raw = cfg.get("web") or {}
    # 同上：日志里印的口令必须是现在生效的那把，不是 config.json 里部署时写下的旧值。
    token = st.web_token(cfg) or ""
    httpd, _thread = webmod.start_background(cfg.as_dict())
    port = httpd.server_address[1]
    print_url("日历页面（也是扫码的地方）", web_raw.get("host") or "", port, token, cfg)

    # 按设置决定起不起 —— 关着的时候提醒线程、它的连接池、它每分钟一次的查询
    # 一样都不存在。`web` 那边保存设置时会再调 `sync()`，所以页面上一关就是当场关。
    reminders.sync()
    if reminders.running:
        print(f"⏰ 开抢提醒已启用：提前 {st.remind_minutes()} 分钟")

    try:
        client.run()
    except KeyboardInterrupt:
        print("\n再见。")
    finally:
        reminders.stop()
        try:
            httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return 0


def cmd_check(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    ok = True
    print("—— 自检 ——")
    print(f"时区      : {tu.tz_name()}")
    print(f"当前时间  : {tu.to_minute(tu.now())}  {tu.weekday_cn(tu.today())}")
    print(f"数据库    : {db.db_path()}")
    s = db.stats()
    print(f"数据库内容: 共 {s['total']} 条（待抢 {s['pending']} / 完成 {s['done']} / 过期 {s['expired']}）")

    provider_name = cfg.active_provider_name
    provider = cfg.active_provider or {}
    print(f"模型      : {provider_name} → {provider.get('model')} @ {provider.get('base_url')}")
    # **key 有两个来源**：config.json／环境变量，以及页面上填的（存在 settings 表里）。
    # 早先这里只看 `provider.get("api_key")`，于是「在网页上配好的」实例跑自检会红着脸说
    # 「没有 api_key，机器人无法调用模型」——而机器人其实一直在正常干活（页面上的
    # `configured` 就是 true）。用户拿着这句假故障去排障只会越查越懵。所以判定一律交给
    # `LLM`：它的 `configured` 每次调用都重读一遍（库里的设置 > config.json，见 app/llm.py）。
    pub = st.public_view(cfg)
    try:
        llm = LLM(provider)
        configured = bool(llm.configured)
    except LLMError as exc:  # base_url / model 都没配 —— 比缺 key 更基本
        print(f"  ❌ {exc}")
        ok = False
    else:
        if not configured:
            print("  ❌ 没有 api_key，机器人无法调用模型（config.json 和页面上都没有）")
            print(f"     {pub['no_key_hint']}")
            ok = False
        else:
            try:
                answer = llm.chat("只回复两个字：可用")
                where = "页面里填的" if pub["api_key_from_page"] else "config.json / 环境变量"
                print(f"  ✅ 模型连通（key 来自{where}），回复：{answer.strip()[:40]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ❌ 模型调用失败：{type(exc).__name__}: {exc}")
                ok = False

    web_cfg = cfg.get("web") or {}
    print("日历页面  : " + _display_url(web_cfg.get("host") or "",
                                        int(web_cfg.get("port") or cfgmod.DEFAULT_WEB_PORT),
                                        st.web_token(cfg) or "",
                                        cfg))
    if _in_container() and not web_cfg.get("public_host"):
        print("            （容器里看不到宿主机 IP，把 <NAS的局域网IP> 换成真实地址）")
    from app.web import WEB_DIR

    if not (WEB_DIR / "index.html").is_file():
        print(f"  ❌ 前端文件缺失：{WEB_DIR / 'index.html'}")
        ok = False
    else:
        print(f"  ✅ 前端文件就绪：{WEB_DIR}")

    print("—— 结论 ——", "一切正常" if ok else "有问题，见上面 ❌")
    return 0 if ok else 1


def cmd_backfill_repeat(cfg: cfgmod.Config, args: argparse.Namespace) -> int:
    """给老活动补「重复规则」和「每周/每月几次」—— 只读原文，不调模型。

    「每天/每周」和「每周限领几次」都是后加的，之前入库的活动那几列是空的；但它们
    的 ``raw_text`` / ``note`` 里往往就写着「每日 09:00:00 起发放」「单用户单周限
    领1张」。不补的话，用户得把老消息重发一遍才能让它们变成重复活动。规则解析用的
    是录入时同一套 `app/repeat.py`，纯 Python，不用花钱、不碰网络。

    ``--all`` 连已经有规则的也按原文重判一遍（解析器修过 bug 之后要用它）。
    但**只写「这次认得出规则」的行**，认不出的一律不动 —— 免得把用户在日历上
    手工填的规则清掉。
    """
    from app import repeat as rp

    redo = bool(getattr(args, "all", False))
    rows = db.all_activities() if redo else db.rows_needing_repeat()
    scope = "全部" if redo else "还没判过规则或配额的"
    if not rows:
        print("✅ 所有活动都已经判过重复规则了，没什么要补的。")
        return 0

    dry = bool(getattr(args, "dry_run", False))
    fixed = 0
    for r in rows:
        text = " ".join(str(r.get(k) or "") for k in ("raw_text", "note"))
        if not text.strip():
            continue
        anchor = tu.parse_minute(r.get("grab_at")) or tu.parse_minute(r.get("start_at"))
        found = rp.fields_from(text, text, text, anchor)
        if not found.get("repeat_kind"):
            continue
        if r.get("repeat_kind") and not redo:
            # 已经有排期的行：**只补配额，绝不重判排期** —— 否则「不带 --all 就不会
            # 动已有规则」这条保证就没了（解析器改过之后老规则确实可能不对，但那要
            # 用户显式 --all 才动）。存着的配额也不清：重读原文只是「补」。
            fields = {k: found[k] for k in rp.QUOTA_FIELDS if found.get(k) or not r.get(k)}
        else:
            # 排期这次认出来了 → 整组照写（它们是一套，缺一个就不成立）；但配额
            # 同样**只补不清**：模型自己填好的「月限1次」经常在原文里找不到能认出来
            # 的写法（#74 的真备注是「单用户单月限选择领取1张」），照写就把人填的
            # 规则抹成空了 —— 而且抹掉是静默的，用户只会看到「领满了那几天还标待抢」。
            fields = {k: found.get(k) for k in rp.SCHEDULE_FIELDS}
            fields.update(
                {k: found[k] for k in rp.QUOTA_FIELDS if found.get(k) or not r.get(k)}
            )
        if not fields:
            continue
        old = rp.describe(r)
        desc = rp.describe(dict(r, **fields))
        if old == desc:
            continue                      # 判出来跟现在一样，不白写一次库
        fixed += 1
        arrow = f"{old} → {desc}" if old else desc
        print(f"  #{r['id']} {r['title'][:28]} → {arrow}")
        if not dry:
            db.update_activity(r["id"], **fields)

    verb = "会补上" if dry else "已补上"
    print(f"\n{verb} {fixed} 条重复规则（共检查 {len(rows)} 条{scope}）。")
    if dry:
        print("（这是 --dry-run，没有写库。去掉它才会真的写。）")
    return 0


COMMANDS = {
    "web": cmd_web,
    "chat": cmd_chat,
    "bot": cmd_bot,
    "all": cmd_all,
    "check": cmd_check,
    "backfill-repeat": cmd_backfill_repeat,
}


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="woolbot",
        description="羊毛机器人：自然语言/截图录入活动，按分钟精确查询，前端日历查看。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("command", choices=sorted(COMMANDS), help="要做什么")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径（默认 config.json）")
    parser.add_argument("--user", default=None, help="chat 模式下模拟的用户 id")
    parser.add_argument("--dry-run", action="store_true",
                        help="backfill-repeat 只看看会改成什么，不写库")
    parser.add_argument("--all", action="store_true",
                        help="backfill-repeat 连已经有规则的也按原文重判（解析器修过之后用）")
    args = parser.parse_args(argv)

    cfg = boot(args.config)
    return COMMANDS[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
