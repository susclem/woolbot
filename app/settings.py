"""运行时可改的设置 —— AI 模型、几个开关，以及「开抢前几分钟提醒」。

**为什么放 SQLite 而不是 config.json**：容器里的 `config.json` 是 bind mount 的，
改它也能改，但配置只在进程启动时读一次 —— 改完得重启容器，而用户正站在页面上
等着用。更要紧的是：用户扫完码、日历也能看了，却发现「没地方填 API key」，
整个机器人等于不可用。所以设置必须能**存了立刻生效**，不需要重启、不需要碰 NAS
上的文件，也不需要 SSH。

**优先级：库里的设置 > 环境变量 > config.json。** 与 `config.py` 的 env 覆盖是
同一套思路，只是又高了一层。空字符串表示「没设置」，会回落到下一层 —— 这样
「清空某个字段」不会把 base_url 变成空。

API key 只往里写，**绝不回吐**：`public_view()` 给页面的永远是掩码。
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from . import config as cfgmod, db, timeutil as tu

log = logging.getLogger("woolbot.settings")

#: 允许被页面改写的 provider 字段。故意不含 api_key 之外的任何敏感项；
#: 也别把 `providers`（多套配置）整个暴露出去 —— 页面只管「当前这套」。
PROVIDER_KEYS = ("base_url", "api_key", "model", "vision_model", "temperature")

#: 非 provider 的开关型设置。
FLAG_KEYS = (
    "web_token_required",
    "chat_enabled",
    "sayings_enabled",
    "remind_enabled",
    "domain_enabled",
    "login_enabled",
)

#: 「提前几分钟提醒」。它不是开关，是个数字，所以**不放进 `FLAG_KEYS`** ——
#: `update()` 会把布尔写成 "1"/"0"，而这个键要的是分钟数，混在一起迟早串味。
REMIND_KEYS = ("remind_minutes",)

#: 文本型设置：自定义口令、外网域名、访问秘钥。同样不能进 `FLAG_KEYS`。
WEB_KEYS = ("web_token", "domain_url", "login_key")

#: 「一言来源」：一串分类字母（如 `d,i,k,h`）或一个完整网址。它不是开关也不是数字，
#: 单独一组 —— `update()` 会把布尔写成 "1"/"0"，混进 `FLAG_KEYS` 迟早串味。
#: **怎么解释这个字符串由 `app/sayings.py::source_url()` 一处说了算**，这里只管存取。
SAYINGS_KEYS = ("sayings_source",)

ALL_KEYS = PROVIDER_KEYS + FLAG_KEYS + REMIND_KEYS + WEB_KEYS + SAYINGS_KEYS

#: **只进不出**的几个键：日志里打码，页面上一律只给掩码。
#: `login_password` 是**上一版的名字**（那一版还要用户名），只为了不改动老部署
#: 已有的秘钥才留着读 —— 见 `login_key()`。
_SECRET_KEYS = ("api_key", "web_token", "login_key", "login_password")

#: 「提前几分钟提醒」的边界。**下限 5 是用户 2026-10-09 定的**：比 5 分钟更短，
#: 提醒还没发出去就该开抢了，等于没有。上限只是防手滑（1440 = 一整天）。
REMIND_MIN_MINUTES = 5
REMIND_MAX_MINUTES = 1440
REMIND_DEFAULT_MINUTES = 10

#: 自定义口令的最短长度。**不设成 0**：`web_token_required` 那个开关就是「干脆不要
#: 口令」，真想要一张白纸可以直接关掉；既然选了要口令，一个字符的口令只是自欺欺人。
WEB_TOKEN_MIN = 4

#: 访问秘钥的最短长度与字符要求。**用户 2026-10-09 定的规矩**：大写、小写、数字、
#: 符号都要有，而且不少于 8 位。这一层只有这么一个秘密了（用户名去掉了），它是
#: 唯一的门闩，所以门槛比自定义口令（`WEB_TOKEN_MIN = 4`）高。
LOGIN_KEY_MIN = 8

#: 秘钥要凑齐的四类字符。名字直接用中文 —— 它会原样出现在报错里给用户看。
_KEY_RULES = (
    ("大写字母", re.compile(r"[A-Z]")),
    ("小写字母", re.compile(r"[a-z]")),
    ("数字", re.compile(r"[0-9]")),
    ("符号", re.compile(r"[^A-Za-z0-9]")),
)

#: 登录一次管多少天。**用户 2026-10-09 从 30 天改成 7 天**。
LOGIN_SESSION_DAYS = 7

#: 会话 cookie 的名字、以及算会话值的盐。会话值 = `sha256(盐|秘钥|版本号)`
#: —— **不用会话表**：改秘钥的那一刻，所有旧 cookie 自动全部作废，
#: 不需要谁去清理，也不怕重启丢内存。
SESSION_COOKIE = "woolbot_login"
SESSION_SALT = "woolbot-login-v1"

#: 设备 cookie：浏览器第一次打开登录页时种下的一个随机串。秘钥错误保护要按
#: 「同一台设备」封人，而浏览器本身不带身份，只能自己发一个。有效期一年 ——
#: 它不是什么凭据，只是个编号，被清了也就是退化成「只按 IP 封」。
DEVICE_COOKIE = "woolbot_device"
DEVICE_MAX_AGE = 365 * 86400

#: 秘钥错误保护：`LOGIN_MAX_FAILS` 次错误之后，**同一个 IP 或同一台设备**
#: 满足其一就整天不许访问。开了外网域名之后这个页面就是挂在公网上的，
#: 没有这一层等于把秘钥交给字典攻击。
#: **用户 2026-10-09 把上限从 5 收到 2**（原话：「连续输入两次秘钥后同 ip 和
#: 或者同设备开始 24 小时保护」）—— 因为口令页现在伪装成秘钥登录页，谁都能
#: 在那张页面上不停试，两次足够证明不是主人。
#: **只记内存**（重启即清空）—— 它防的是脚本爆破，不是防重启；被自己关在门外
#: 时重启一下容器就能进来，比把 24 小时写进库、连主人都进不去要合理。
LOGIN_MAX_FAILS = 2
LOGIN_BAN_SECONDS = 24 * 3600

#: 「最近一次模型调用为什么失败」。这几个键**不是用户设的**，是系统记的 ——
#: 但必须存下来：`configured` 只说明「填了 key」，不说明「key 管用」。打错一个
#: 字符，页面上的黄条就消失了，用户只在微信里收到一句报错，还不知道去哪儿改。
ERROR_KEYS = ("llm_error_kind", "llm_error_message", "llm_error_at")

#: 万一库里只有 kind、没有 message（旧版本写的行、写到一半断电…），也得有话说。
#: 页面上的红条**永远不能只有一个通用句** —— 用户会拿它和旁边「测试连接成功」
#: 对照，得出「这软件自相矛盾」的结论，然后不知道该信谁。
KIND_TEXT = {
    "auth": "API key 不对或已失效，到「设置」里重新填一把。",
    "notfound": "接口地址（base_url）不对，到「设置」里核对一下。",
    "model": "模型名或这次的内容有问题，到「设置」里核对一下。",
    "rate": "模型服务限流了，过一会儿再试。",
    "server": "模型服务自己出错了，不是你配置的问题，过一会儿再试。",
}

_cache: Optional[dict[str, str]] = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            with db.connect() as conn:
                rows = conn.execute("SELECT key, value FROM settings").fetchall()
            _cache = {str(r["key"]): ("" if r["value"] is None else str(r["value"])) for r in rows}
        except Exception as exc:  # noqa: BLE001 — 设置读不出来不该让服务起不来
            log.warning("读设置失败，按空处理：%s", exc)
            _cache = {}
    return _cache


def get(key: str, default: str = "") -> str:
    return _load().get(key, default)


def reset_cache() -> None:
    """丢掉内存缓存，下次读重新查库。测试用；正常运行不需要。"""
    global _cache
    _cache = None


def all_values() -> dict[str, str]:
    return dict(_load())


def _write(clean: dict[str, str]) -> None:
    """写库并让内存缓存失效。空字符串 = 删掉这行（回落到下一层）。"""
    global _cache
    now_s = tu.to_minute(tu.now())
    with db.connect() as conn:
        for key, value in clean.items():
            if value == "":
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, now_s),
                )
    _cache = None  # 下次读重新拉


def update(values: dict[str, Any]) -> dict[str, str]:
    """写入若干设置。只认 ``ALL_KEYS`` 里的键，空值等于删除（回落下一层）。"""
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in ALL_KEYS:
            continue
        if value is None:
            clean[key] = ""
        elif isinstance(value, bool):
            clean[key] = "1" if value else "0"
        else:
            clean[key] = str(value).strip()

    _write(clean)
    if clean.get("api_key"):
        log.info("API key 已更新（%s）", mask_key(clean["api_key"]))
    log.info("设置已更新：%s", {k: ("***" if k in _SECRET_KEYS else v) for k, v in clean.items()})
    return clean


def set_last_error(kind: str, message: str) -> None:
    """记下模型调用的失败原因（供页面报红）。"""
    _write(
        {
            "llm_error_kind": kind,
            "llm_error_message": message,
            "llm_error_at": tu.to_minute(tu.now()),
        }
    )


def clear_last_error() -> None:
    """清掉失败记录。本来就没有就什么都不做（别白写一次库）。"""
    if not any(_load().get(key) for key in ERROR_KEYS):
        return
    _write({key: "" for key in ERROR_KEYS})


def last_error() -> dict[str, Any]:
    """最近一次模型调用失败。没失败过就是空 dict。

    `message` 一定是句能照做的话（见 `KIND_TEXT`）—— 页面全靠它把红条写清楚。
    """
    values = _load()
    kind = values.get("llm_error_kind", "")
    if not kind:
        return {}
    message = values.get("llm_error_message", "")
    if not message:
        message = KIND_TEXT.get(kind, "上次调用模型失败了，到「设置」里检查一下。")
    return {
        "kind": kind,
        "message": message,
        "at": values.get("llm_error_at", ""),
    }


def provider_override() -> dict[str, Any]:
    """要覆盖到当前 provider 上的字段（只含真正设过的）。"""
    values = _load()
    out: dict[str, Any] = {}
    for key in PROVIDER_KEYS:
        value = values.get(key, "")
        if value == "":
            continue
        if key == "temperature":
            try:
                out[key] = float(value)
            except ValueError:
                continue
        else:
            out[key] = value
    return out


def live_model() -> str:
    """当前真正在用的模型名，用于在页面上显示。"""
    return str(provider_override().get("model") or "")


def token_required(default: bool = True) -> bool:
    """网页要不要口令。没设过就按 config.json 里有没有 token 走。"""
    value = _load().get("web_token_required", "")
    if value == "":
        return default
    return value not in ("0", "false", "False", "no")


def chat_enabled() -> bool:
    """机器人要不要闲聊（寒暄、问它是谁、随口聊两句）。**没设过 = 关**。

    用户 2026-10-07 选的这一档：非活动消息一律回一句固定话术（`pipeline.CHAT_OFF_HINT`，
    寒暄是 `CHAT_OFF_GREETING`），回复完全可预期，也不再为一句「你好」多花调用。

    它**只管闲聊**：录入、查询、删除在那条路径的前面就返回了，不受这个开关影响。
    """
    value = _load().get("chat_enabled", "")
    if value == "":
        return False
    return value not in ("0", "false", "False", "no")


def remind_enabled() -> bool:
    """开抢前要不要主动推一条微信。**没设过 = 开**（用户 2026-10-09 点名要的默认）。

    它比 `sayings_enabled()` 管得更多一层：关掉之后不只是「不发提醒」，
    `app/reminder.py` 的那个线程**根本不会被创建**（见 `Reminder.sync`）——
    用户要的是「不开就别占资源」，不是「开着线程什么都不干」。
    """
    value = _load().get("remind_enabled", "")
    if value == "":
        return True
    return value not in ("0", "false", "False", "no")


def remind_minutes() -> int:
    """开抢前几分钟提醒。没设过 = 10；超出上下限一律夹回来。

    **读的时候也要夹**：下限不能只靠接口拦 —— 旧版本写的行、手改过的库、
    写到一半断电，都可能留下一个 1。而 1 分钟的提醒等于没有提醒，还不如按 5 发。
    """
    raw = _load().get("remind_minutes", "")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return REMIND_DEFAULT_MINUTES
    return max(REMIND_MIN_MINUTES, min(REMIND_MAX_MINUTES, value))


def sayings_enabled() -> bool:
    """每条回复末尾那句「一言」要不要带。**没设过 = 开**（用户点名要的）。

    与 `chat_enabled()` 的默认**故意相反**：一言是用户明确要求加的，装完默认就该有；
    闲聊是他明确说不要的，装完默认没有。两者都是「页面上能改、存了立刻生效」。

    `WOOLBOT_NO_SAYINGS=1` 仍然是更硬的一层（部署时不想让它出网就设它），
    判断在 `app/sayings.py`，不在这里。
    """
    value = _load().get("sayings_enabled", "")
    if value == "":
        return True
    return value not in ("0", "false", "False", "no")


def sayings_source() -> str:
    """一言从哪个源取。**空 = 按 `app/sayings.py` 的默认分类走**（`d,i,k,h`）。

    用户 2026-10-09 要的是「在设置里面增加一言的自定义源」：这里存的就是他在
    输入框里填的那串（分类字母或一个完整网址），**怎么解释它不在这里** ——
    `sayings.source_url()` 一个地方说了算，存了立刻生效（写完就清缓存）。
    """
    return (_load().get("sayings_source", "") or "").strip()


def mask_key(key: str) -> str:
    """`sk-1234567890abcdef` → `sk-1…cdef`。给页面看的永远是这个。

    口令和管理员密码也用它（`mask_secret` 只是个说人话的别名）。
    """
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:4]}…{key[-4:]}"


#: 同一件事，换个名字读起来更顺：页面上掩的是口令／密码，不是 API key。
mask_secret = mask_key


def domain_enabled() -> bool:
    """要不要用外网域名。**没设过 = 关**（用户 2026-10-09 点名要的默认）。

    关着的时候地址一律走局域网（`web.public_host:web.public_port`），
    也就是部署脚本写进 config.json 的那个 NAS 地址。
    """
    value = _load().get("domain_enabled", "")
    if value == "":
        return False
    return value not in ("0", "false", "False", "no")


def domain_url() -> str:
    """外网域名，**规范化**成能直接拼的形式。

    用户多半只会填 `wool.example.com`，所以没有协议头就补 `https://`（外网走明文
    http 等于把口令摊在公网上），末尾的斜杠一律削掉 —— 不削的话拼出来就是
    `https://a.com//?token=…`，虽然也能用，但看着像个 bug。
    """
    raw = (_load().get("domain_url", "") or "").strip().rstrip("/")
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def login_enabled() -> bool:
    """页面要不要访问秘钥登录。**没设过 = 关**（用户 2026-10-09 点名要的默认）。"""
    value = _load().get("login_enabled", "")
    if value == "":
        return False
    return value not in ("0", "false", "False", "no")


def login_key() -> str:
    """访问秘钥。**只在进程里用，绝不回吐给页面** —— 与 API key 同一条规矩。

    库里那行**上一版叫 `login_password`**（那一版还要填用户名）。读的时候认老名字，
    是为了让老部署的秘钥不会凭空消失；真写下去的时候只写 `login_key`，
    顺手把老那一行清掉（见 `app/web.py` 的设置接口）。
    """
    got = _load()
    value = (got.get("login_key") or "").strip()
    if value:
        return value
    return (got.get("login_password") or "").strip()


def key_problem(value: str) -> str:
    """秘钥合格吗。合格回空串，不合格回一句能直接给用户看的话。

    **只判断，不动它** —— 用户 2026-10-09 定的规矩：大小写字母、数字、符号都要有，
    而且不少于 `LOGIN_KEY_MIN` 位。不合格就当场报错，绝不偷偷补几个字符再存下去。
    """
    if len(value) < LOGIN_KEY_MIN:
        return f"秘钥至少要 {LOGIN_KEY_MIN} 位"
    missing = [name for name, rule in _KEY_RULES if not rule.search(value)]
    if missing:
        return "秘钥还要有" + "、".join(missing)
    return ""


def login_ready() -> bool:
    """登录这道门**真的能挡住人**吗：开关开着，而且秘钥填了。

    两个条件缺一个都不能放行 —— 只看开关的话，一个没填秘钥的账号会把页面
    **锁死**（谁都不知道秘钥是什么，连自己都进不去）。
    """
    return login_enabled() and bool(login_key())


def login_epoch() -> int:
    """会话版本号。**退出登录时 +1**，让所有已经发出去的 cookie 一起作废。

    这个键**故意不在 `ALL_KEYS` 里** —— 它不是给人填的设置，是内部记账，
    只能由 `bump_login_epoch()` 改。坏值一律当 0。
    """
    raw = (_load().get("login_epoch", "") or "").strip()
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return 0


def bump_login_epoch() -> int:
    """退出登录 —— 无状态 cookie 唯一能「真的作废」的办法。

    会话值只是 `sha256(盐|账号|密码|版本号)`，它**不含时间也不含随机数**：不换版本号
    的话，「退出登录」只能让浏览器删掉自己那份 cookie，谁把 cookie 值抄走了照样能
    一直用（真机上验过：退出之后拿旧 cookie 打 `/api/calendar` 还是 200）。
    退出时把版本号 +1，所有旧会话一起失效 —— 代价是别的设备也要重新登一次，
    这正是「退出登录」本来该有的意思。改密码那条路天生就作废全部会话，不用管这里。
    """
    epoch = login_epoch() + 1
    _write({"login_epoch": str(epoch)})
    log.info("已退出登录：会话版本号 -> %s（旧 cookie 全部失效）", epoch)
    return epoch


def session_value(key: str = "") -> str:
    """这次登录该种下的 cookie 值。秘钥没配就是空串（= 不放行）。

    以前这里还有账号那一项，现在用户名去掉了（用户 2026-10-09 定的「秘钥登录」），
    会话值就是 `sha256(盐|秘钥|版本号)`。
    """
    k = login_key() if key == "" else key
    if not k:
        return ""
    return hashlib.sha256(
        f"{SESSION_SALT}|{k}|{login_epoch()}".encode("utf-8")
    ).hexdigest()[:32]


def web_token(cfg: Optional[Any] = None) -> str:
    """当前生效的页面口令 = **库里的自定义口令 > config.json 的 `web.token`**。

    库里没设过就沿用配置文件里那个（部署时生成的、写进 NAS 上 config.json 的），
    所以「从没碰过设置页」的老部署行为完全不变。
    """
    own = (_load().get("web_token", "") or "").strip()
    if own:
        return own
    try:
        return str(_web_section(cfg).get("token") or "").strip()
    except Exception:  # noqa: BLE001 —— 配置读不出来就当作没口令，别让页面起不来
        return ""


def _web_section(cfg: Optional[Any]) -> dict[str, Any]:
    """把配置里的 `web` 段取成**普通 dict**。

    `app/config.py` 的 `Config.get("web")` 返回的是另一个 `Config` **对象**，不是
    `dict`；`dict(Config(...))` 直接抛 `TypeError: 'Config' object is not iterable`。
    这里以前把这句包在 `except Exception` 里，于是**每一次**都静悄悄退回 `{}` ——
    `web.public_host` 永远读不到，`/日历` 回你一个 `http://127.0.0.1:8080/`，
    启动日志里则是 `<NAS的局域网IP>`。不报错、不崩，只是地址永远是错的。

    单测里传的是普通 dict，所以这条路一直测不到；真机上传的才是 `Config`。
    """
    if not cfg:
        return {}
    try:
        web = cfg.get("web")
    except Exception:  # noqa: BLE001 —— 配置形状不对就当它没有 web 段
        return {}
    if isinstance(web, dict):
        return dict(web)
    try:  # Config 对象：走它自己的 as_dict()
        return dict(web.as_dict())
    except Exception:  # noqa: BLE001
        return {}


def front_url(
    cfg: Optional[Any] = None,
    *,
    host: str = "",
    port: int = 0,
    with_token: bool = True,
    token: Optional[str] = None,
) -> str:
    """前端地址 —— **全项目只在这里算一次**。

    口令提示页、启动日志、自检、还有 `/日历` 指令，四条路都调它。以前 `/日历`
    被删掉就是因为它在 `pipeline.py` 里自己拼了个写死的 `http://<你的NAS地址>:8080/`，
    和真实地址对不上；同一个地址一旦有两个实现，迟早有一个是错的。

    顺序：**外网域名 > 局域网地址**。域名关着或没填就走 `web.public_host` +
    `web.public_port`（部署脚本写进 config.json 的 NAS 真实地址与映射端口）。

    `with_token=False` 给「口令关掉了」的场景用 —— 那时候地址末尾不该挂个空参数。
    `token` 只给**手里已经攥着当前口令**的调用方；不传就现读设置 —— 这样「启动
    日志里的口令」和「真正生效的口令」永远是同一个。`main.py` 那两条打印地址的
    路以前传的是 `config.json` 里部署时生成的 `web.token`，用户在页面上改过口令
    之后就成了废 token（同一个进程的两行日志印出两个不同的口令），现在它们改成
    传 `web_token(cfg)`（= 设置里那个），或者干脆不传。
    """
    web_cfg = _web_section(cfg)

    base = domain_url() if domain_enabled() else ""
    if not base:
        # `public_host` / `public_port` 是**部署脚本写下来的真实地址**，优先级最高：
        # 容器里绑的是 0.0.0.0，容器自己算出来的那个是 docker 网桥的 172.x，
        # 手机连不上它。只有在配置里没写（本机调试）时才用调用方给的地址。
        use_host = str(web_cfg.get("public_host") or "").strip() or host
        use_port = (web_cfg.get("public_port") or port or web_cfg.get("port")
                    or cfgmod.DEFAULT_WEB_PORT)
        if not use_host:
            use_host = "127.0.0.1"
        base = f"http://{use_host}:{int(use_port)}"

    shown = web_token(cfg) if token is None else str(token or "").strip()
    if with_token and shown and token_required(True):
        # **必须转义**：自定义口令里完全可能有个 `&` 或 `#`，直接拼进 URL 会把
        # 参数截断（`#` 之后浏览器根本不会发给服务端），看着像「口令明明没错却进不去」。
        return f"{base}/?token={quote(shown, safe='')}"
    return base + "/"


def _sayings():
    """延迟 import「一言」模块：它 import `requests`，而且它自己要回头用本模块。"""
    from . import sayings as sy

    return sy


def public_view(cfg: Optional[Any] = None) -> dict[str, Any]:
    """给网页看的设置：**不含明文 key**，只告诉它「有没有配」。

    `configured` 是页面上最重要的一格 —— 它就是「机器人现在能不能读懂消息」。
    """
    from . import llm as llm_mod

    override = provider_override()
    base: dict[str, Any] = {}
    if cfg is not None:
        try:
            base = dict(cfg.active_provider)
        except Exception:  # noqa: BLE001
            base = {}

    def pick(key: str, fallback: str = "") -> str:
        return str(override.get(key) or base.get(key) or fallback)

    api_key = str(override.get("api_key") or base.get("api_key") or "")
    return {
        "provider": getattr(cfg, "active_provider_name", "") if cfg is not None else "",
        "base_url": pick("base_url"),
        "model": pick("model"),
        "vision_model": pick("vision_model") or pick("model"),
        "temperature": override.get("temperature", base.get("temperature", 0)),
        "api_key_masked": mask_key(api_key),
        "api_key_from_page": bool(override.get("api_key")),
        "configured": bool(api_key),
        "token_required": token_required(),
        "chat_enabled": chat_enabled(),
        "sayings_enabled": sayings_enabled(),
        # 「一言来源」：把用户填的那串原样回填，外加默认值与可选分类 —— 帮助文字和
        # 输入框的 placeholder 都从这里拿，省得页面再抄一份分类表。
        "sayings_source": sayings_source(),
        "sayings_source_default": _sayings().DEFAULT_SOURCE,
        "sayings_categories": _sayings().CATEGORY_TEXT,
        "remind_enabled": remind_enabled(),
        "remind_minutes": remind_minutes(),
        "remind_min_minutes": REMIND_MIN_MINUTES,
        # 口令与登录：**秘钥只给「有没有」和掩码**。页面把掩码当占位符用来告诉用户
        # 「已经设过了」；用户不在框里打字就原样发回掩码，服务端认得出。
        "web_token_masked": mask_secret(web_token(cfg)),
        "web_token_from_page": bool((_load().get("web_token", "") or "").strip()),
        "web_token_min": WEB_TOKEN_MIN,
        "domain_enabled": domain_enabled(),
        "domain_url": domain_url(),
        "login_enabled": login_enabled(),
        "login_key_set": bool(login_key()),
        "login_key_masked": mask_secret(login_key()) if login_key() else "",
        "login_key_min": LOGIN_KEY_MIN,
        "login_key_hint": "至少 8 位，大写字母、小写字母、数字、符号都要有",
        "login_ready": login_ready(),
        "login_session_days": LOGIN_SESSION_DAYS,
        "login_max_fails": LOGIN_MAX_FAILS,
        "login_ban_hours": LOGIN_BAN_SECONDS // 3600,
        "no_key_hint": llm_mod.NO_KEY_HINT,
        # 填了 key 但 key 不管用 —— 页面据此把黄条换成红条。
        # 没有失败时给 `None`（不是 `{}`）：空对象在 JS 里是**真值**，
        # 前端只要写成 `if (data.last_error)` 就会在模型完全正常时亮红条。
        "last_error": last_error() or None,
    }
