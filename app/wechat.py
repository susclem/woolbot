"""ClawBot（微信 iLink 机器人）通道。

协议依据：`docs/clawbot-protocol.md`（上游 2.4.6 协议分析全文，已在仓库里）。
本模块只负责「微信怎么收发」；「这条消息是什么意思、库里有什么」全在 `app/pipeline.py`。

上游踩坑记录里有三条纪律，直接决定了下面代码的形状：

1. **HTTP 200 不等于发出去了。** 响应体里的 `ret` / `errcode` 才算业务成功，
   而且 `sendmessage` 必须带上 `msg.from_user_id=""`、`msg.client_id` 和顶层 `base_info`，
   否则微信那头收不到（上游文档里标注为「最关键的坑」）。
2. **服务器返回的 Content-Type 是 `application/octet-stream`。** aiohttp 的 `.json()`
   会因此报错，requests 不会 —— 所以这里用同步 requests，不存在这个坑。
3. **长轮询里的网络超时是正常控制流**，不是故障：保留游标继续下一轮即可，
   绝不能让异常冲出主循环。

请求头 `X-WECHAT-UIN` 每次 POST 都要重新随机生成；
`Authorization` 只在登录拿到 token 之后才有。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests

from . import qr
from . import sayings as sayings_mod

try:  # pycryptodome 的两种发行名都要兼容
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:  # pragma: no cover
    from Cryptodome.Cipher import AES  # type: ignore
    from Cryptodome.Util.Padding import unpad  # type: ignore

log = logging.getLogger("woolbot.wechat")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 首次获取二维码的固定入口；登录后一切业务请求都走响应里的 baseurl。
ENTRY_BASE = "https://ilinkai.weixin.qq.com"
#: 上游默认 CDN 基础地址，只在服务端没给完整 URL 时用作回退拼接。
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

APP_ID = "bot"
#: (major << 16) | (minor << 8) | patch  →  2.4.6
CLIENT_VERSION = "132102"
CHANNEL_VERSION = "2.4.6"
BOT_AGENT = "weixin-ClawBot-API/1.2.0 (python)"
BASE_INFO = {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}

#: 官方入站媒体上限。
MAX_MEDIA_BYTES = 100 * 1024 * 1024
#: 单条文本消息的安全长度，超了要分段发。
TEXT_CHUNK = 1200

# 超时（秒），对齐协议文档 2.7.2
TIMEOUT_POLL = 35.0
TIMEOUT_SEND = 15.0
TIMEOUT_SHORT = 10.0
#: -14 之后官方会暂停该账号 1 小时，我们照做，避免把接口打满。
STALE_TOKEN_COOLDOWN = 3600


class WeChatError(RuntimeError):
    """业务层失败：HTTP 非 2xx、响应不是 JSON、或 ret/errcode 不为 0。"""

    def __init__(
        self,
        message: str,
        ret: Any = None,
        errcode: Any = None,
        errmsg: str = "",
        http: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode
        self.errmsg = errmsg
        self.http = http


class StaleToken(WeChatError):
    """ret/errcode == -14：bot token 已失效，必须停止业务请求并重新登录。"""


class NetworkError(RuntimeError):
    """请求根本没到达：按 dns / tcp / tls / timeout / unknown 分类。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --------------------------------------------------------------------------
# 纯函数工具
# --------------------------------------------------------------------------


def _rand_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再做 base64，每次 POST 重算。"""
    return base64.b64encode(str(random.getrandbits(32)).encode("ascii")).decode("ascii")


def _classify(exc: BaseException) -> str:
    """把 requests 的异常归到协议文档定义的五个类别里。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in text or "timeout" in text:
        return "timeout"
    if any(k in text for k in ("getaddrinfo", "enotfound", "eai_again", "name or service",
                               "nodename nor servname", "name resolution")):
        return "dns"
    if any(k in text for k in ("ssl", "certificate", "tls")):
        return "tls"
    if any(k in text for k in ("refused", "unreachable", "reset by peer", "broken pipe",
                               "connection aborted", "connection reset")):
        return "tcp"
    return "unknown"


def _mask(value: Any) -> str:
    """日志里不打印完整 token / 用户 id。"""
    text = str(value or "")
    if len(text) <= 8:
        return text or "-"
    return f"{text[:4]}…{text[-3:]}"


def _new_client_id() -> str:
    return f"woolbot-{uuid.uuid4().hex[:16]}"


def decode_aes_key(value: str) -> bytes:
    """解出 16 字节 AES key。

    `CDNMedia.aes_key` 在真实数据里有两种编码 —— `base64(16 字节原始 key)` 和
    `base64(32 字符的 hex 字符串)`；`image_item.aeskey` 有时直接放 hex。
    三种都必须认，否则一半的图会解不开。
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("aes_key 为空")
    # 1) 直接就是 32 字符 hex（base64 编码 16 字节只会得到 24 字符，不会撞上）
    if len(value) == 32:
        try:
            return bytes.fromhex(value)
        except ValueError:
            pass
    # 2) base64 之后再判断里面装的是 raw key 还是 hex
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4))
    except Exception:
        raw = b""
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    if len(raw) == 16:
        return raw
    raise ValueError(f"认不出的 aes_key（原长度 {len(value)}，解出 {len(raw)} 字节）")


def decrypt_media(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS#7。"""
    plain = AES.new(key, AES.MODE_ECB).decrypt(data)
    try:
        return unpad(plain, AES.block_size)
    except ValueError:
        # 少数情况下对端没补 padding，直接返回去掉尾部 \x00 的内容
        return plain.rstrip(b"\x00")


def split_text(text: str, limit: int = TEXT_CHUNK) -> list[str]:
    """按行切成不超过 limit 的段落；单行超长就硬切。"""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks


def image_mime(data: bytes) -> str:
    """从魔数判断图片类型 —— 微信不保证给 Content-Type。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class ClawBot:
    """扫码登录 + 长轮询收发 + 媒体下载解密。

    典型用法：

        client = ClawBot(cfg.as_dict(), bot)
        client.run()          # 阻塞，Ctrl+C 优雅退出
    """

    def __init__(self, raw_config: dict[str, Any], bot: Any, sayings: Any = None) -> None:
        self.cfg = raw_config or {}
        wcfg = dict(self.cfg.get("wechat") or {})
        self.wcfg = wcfg
        #: pipeline.Bot —— 只暴露 handle_text / handle_image 两个方法
        self.bot = bot
        #: 每条回复末尾那句一言（用户 2026-10-07 要的）。默认真的去请求
        #: `https://v1.hitokoto.cn/`；测试一律传 `sayings.NoSayings()`，
        #: 否则几十个收发用例会各自去连一次外网。取不到只是没有那一句。
        self.sayings = sayings if sayings is not None else sayings_mod.Sayings()
        #: 先把第一句取好（后台线程，不挡启动）：那个接口时快时慢，
        #: 等到第一条消息来了才现场去取，第一句回复就得陪它等。
        try:
            self.sayings.prefetch()
        except Exception as exc:
            log.warning("预热一言失败（不影响收发）：%s", exc)

        # 兜底名字跟 `app/config.py` / `config.example.json` 的默认值保持一致：三处写法不同
        # 的时候，注释里写「data/wechat_state.json」、真文件却叫 `weixin_state.json`，
        # 照着文档去容器里找会找不到（这个不一致真出现过）。
        self.state_path = self._resolve(wcfg.get("state_file") or "data/weixin_state.json")
        self.media_dir = self._resolve(wcfg.get("media_dir") or "data/media")
        self.cdn_base = (wcfg.get("cdn_base") or DEFAULT_CDN_BASE).rstrip("/")
        #: 取二维码的入口。协议规定是固定域名；留成可配置是为了能指向测试/自建端点。
        self.entry_base = (wcfg.get("entry_base") or ENTRY_BASE).rstrip("/")

        self.allowed = {str(u).strip() for u in (wcfg.get("allowed_users") or []) if str(u).strip()}
        self.save_media = bool(wcfg.get("save_media", True))

        self.session = requests.Session()
        self.session.headers["User-Agent"] = BOT_AGENT
        #: 主动提醒线程专用的连接池，**懒创建**（见 `push_session()`）。
        self._push_session: Optional[requests.Session] = None

        self.state: dict[str, Any] = self._load_state()
        self._tickets: dict[str, str] = {}
        self._stop = threading.Event()
        self._consecutive_failures = 0
        self.timeout_poll = TIMEOUT_POLL
        self.qr_base = self.entry_base
        #: 最近一次登录二维码图片的落地位置，网页端会来读
        self.qr_file: Optional[Path] = None
        #: 二维码的原始载荷（那个 https 链接），画不出图时网页上显示它
        self.qr_url: str = ""
        #: 网页上点「刷新二维码」时置位，登录循环看到就重新申请一张。
        #: 没有它的话，页面上那个按钮只是把磁盘上同一张旧图再下一次 —— 用户
        #: 看到的是「点了没反应」。
        self._qr_refresh = threading.Event()
        #: 网页上点「退出微信」时置位。和 `_qr_refresh` 分开是有原因的：
        #: 那个是「换一张码」，已经登录时会被主循环主动吃掉（二维码根本不需要）；
        #: 这个是「本机这份会话不要了，立刻回到扫码状态」，必须穿透到长轮询那一层
        #: —— 否则 token 已经被清掉、循环还抱着它一直打接口。
        self._logout = threading.Event()
        #: 叫醒 `_sleep()` 用的条件变量。**只置位还不够**：主循环失败退避时
        #: 躺着不动（token 失效那条路要睡 STALE_TOKEN_COOLDOWN 秒），
        #: 这时候点刷新就真的没反应，得有人把它提前捅醒。
        self._wake = threading.Condition()
        self.ready = threading.Event()
        #: 最近一次长轮询的时刻（time.time()）。「有 token」不等于「收得到消息」：
        #: token 失效后有 1 小时冷却，这段时间页面上写着「已连接」而消息全在队列里
        #: 躺着 —— 所以页面需要看的是这个心跳，不是 logged_in。
        self.last_poll_at: float = 0.0
        #: 最近一次长轮询失败的原因（成功或超时都会清空）
        self.last_poll_error: str = ""

    def health(self) -> dict[str, Any]:
        """给页面看的实况：到底还收不收得到消息。"""
        age: Optional[float] = None
        if self.last_poll_at:
            age = max(0.0, time.time() - self.last_poll_at)
        # 长轮询本身会挂住 timeout_poll 秒，所以「还在转」的判定要留够余量
        limit = max(90.0, self.timeout_poll * 3)
        return {
            "logged_in": self.logged_in,
            "seconds_since_poll": None if age is None else int(age),
            "polling": age is not None and age <= limit,
            "last_error": self.last_poll_error,
        }

    def request_qr_refresh(self) -> None:
        """让登录循环丢掉当前二维码、重新申请一张。线程安全，随便谁调。"""
        log.info("收到手动刷新二维码的请求")
        self._qr_refresh.set()
        # 把可能正在退避长睡的循环戳醒：否则页面上点了按钮要等到
        # 退避结束才有动静（token 失效那条路是整整一小时）。
        with self._wake:
            self._wake.notify_all()

    def request_logout(self) -> None:
        """网页上点「退出微信」：把 NAS 本地这份会话立刻丢掉，回到扫码状态。

        **它不会在微信那边撤销授权。** 协议里没有那个接口，能发出去的只有
        `notifystop`（一句「我停了」的通知），真正撤权只有用户在微信里
        「解除连接」那一步。

        那为什么还要这个按钮：只解除连接的话，NAS 这边要等下一次长轮询撞上
        `-14` 才发现，而那条路按协议要先冷却 `STALE_TOKEN_COOLDOWN` 秒（一小时）
        才重新申请二维码 —— 用户这一小时里看到的是一张怎么点都刷不出来的页面。
        点了这个按钮，token 和游标当场清掉，新二维码几秒内就出来。

        线程安全，随便谁调（网页线程调它，主循环自己会看到标志）。
        """
        log.info("收到退出微信的请求：丢掉本机会话，准备重新扫码")
        try:
            # 趁 token 还在，先跟服务端说一声我停了（最佳努力，失败不影响退出）
            self.notify("stop")
        except Exception as exc:  # noqa: BLE001
            log.debug("退出前通知服务端失败（不影响退出）：%s", exc)
        self.logout()  # keep_cursor=False：游标也一起丢，新会话不该接着旧队列
        self._logout.set()
        self.ready.clear()
        # 把可能正在退避长睡的主循环戳醒（和刷新二维码同一个道理）
        with self._wake:
            self._wake.notify_all()

    # -- 状态持久化 ---------------------------------------------------

    @staticmethod
    def _resolve(value: str | Path) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def _load_state(self) -> dict[str, Any]:
        blank: dict[str, Any] = {
            "bot_token": "",
            "baseurl": "",
            "ilink_bot_id": "",
            "ilink_user_id": "",
            "get_updates_buf": "",
            "context_tokens": {},
            "updated_at": "",
        }
        if not self.state_path.is_file():
            return blank
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("登录状态文件读不了（%s），当没登录处理：%s", self.state_path, exc)
            return blank
        if not isinstance(data, dict):
            return blank
        for k, v in blank.items():
            data.setdefault(k, v)
        if not isinstance(data.get("context_tokens"), dict):
            data["context_tokens"] = {}
        return data

    def _save_state(self) -> None:
        """临时文件写完后原子替换，避免进程退出时留下半截 JSON。"""
        self.state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.state_path)
            try:
                os.chmod(self.state_path, 0o600)  # token 不该给同机其他用户看
            except OSError:
                pass
        except OSError as exc:
            log.error("登录状态写不进去（%s）：%s", self.state_path, exc)

    @property
    def logged_in(self) -> bool:
        return bool(self.state.get("bot_token"))

    # -- HTTP 底座 ----------------------------------------------------

    def _url(self, path: str, base: Optional[str] = None) -> str:
        root = (base or self.state.get("baseurl") or self.entry_base).rstrip("/")
        return f"{root}{path}"

    def _post_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _rand_uin(),
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": CLIENT_VERSION,
        }
        token = self.state.get("bot_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _qr_headers() -> dict[str, str]:
        # 官方对二维码状态 GET 只发这两个公共应用头
        return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": CLIENT_VERSION}

    def _check(self, path: str, resp: requests.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise WeChatError(
                f"{path} 返回 HTTP {resp.status_code}", http=resp.status_code,
                errmsg=(resp.text or "")[:200],
            )
        try:
            # requests 不像 aiohttp 那样校验 Content-Type，服务器给
            # application/octet-stream 也能正常解析（上游踩坑 2 在这里不成立）
            data = resp.json()
        except ValueError as exc:
            raise WeChatError(f"{path} 的响应不是 JSON：{(resp.text or '')[:200]}") from exc
        if not isinstance(data, dict):
            raise WeChatError(f"{path} 返回了非对象 JSON")

        ret, errcode = data.get("ret"), data.get("errcode")
        errmsg = data.get("errmsg") or ""
        bad = (ret not in (None, 0)) or (errcode not in (None, 0))
        if bad:
            message = f"{path} 业务失败 ret={ret} errcode={errcode} errmsg={errmsg}"
            if ret == -14 or errcode == -14:
                raise StaleToken(message, ret=ret, errcode=errcode, errmsg=errmsg)
            raise WeChatError(message, ret=ret, errcode=errcode, errmsg=errmsg)
        return data

    def push_session(self) -> requests.Session:
        """给**主动提醒线程**用的那个连接池，用到才建。

        为什么要分开：`requests.Session` 官方没承诺线程安全，而提醒线程会在
        长轮询正抱着 `self.session` 等 `/ilink/bot/getupdates` 的时候插进来发一条
        `sendmessage` —— 两个线程共用一个 Session 是拿运气办事。

        为什么懒创建而不是在 `__init__` 里就建好：提醒功能**关着的时候连这个池子
        都不该存在**（用户 2026-10-09 的「不开就别占资源」）。这个函数只有
        `app/reminder.py` 会调，所以判断「开没开」这件事不用在这里再做一遍。
        """
        if self._push_session is None:
            session = requests.Session()
            session.headers["User-Agent"] = BOT_AGENT
            self._push_session = session
        return self._push_session

    def _post(
        self,
        path: str,
        body: Optional[dict[str, Any]] = None,
        timeout: float = TIMEOUT_SEND,
        base: Optional[str] = None,
        auth: bool = True,
        session: Optional[requests.Session] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = dict(body or {})
        payload.setdefault("base_info", dict(BASE_INFO))
        if auth:
            headers = self._post_headers()
        else:
            # 登录前的请求没有 token；POST 仍要声明 JSON
            headers = self._qr_headers()
            headers["Content-Type"] = "application/json"
        url = self._url(path, base)
        #: 不传就还是老样子走 `self.session` —— 只有主动提醒那条路会传自己的池子。
        sess = session or self.session
        try:
            resp = sess.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise NetworkError(_classify(exc), f"POST {path} 没连上：{exc}") from exc
        return self._check(path, resp)

    def _get(
        self, path: str, params: dict[str, Any], timeout: float = TIMEOUT_POLL,
        base: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            resp = self.session.get(
                self._url(path, base), params=params, headers=self._qr_headers(), timeout=timeout
            )
        except requests.RequestException as exc:
            raise NetworkError(_classify(exc), f"GET {path} 没连上：{exc}") from exc
        return self._check(path, resp)

    # -- 登录 ---------------------------------------------------------

    def _token_list(self) -> list[str]:
        token = self.state.get("bot_token")
        return [token] if token else []

    def _present_qr(self, content: str) -> None:
        """把二维码给到人手上。

        `qrcode_img_content` 是 HTTPS 链接而不是 base64 图片（上游踩坑 1），
        而且那个链接指向一个 JS 渲染的网页，直接下载只会拿到 HTML。
        所以顺序是：① 链接本身就是扫码内容，先本地画成二维码 PNG；
        ② 万一服务端某天真的下发图片，才走下载；③ 画不出来就打印链接兜底。
        """
        content = (content or "").strip()
        if not content:
            log.error("服务端没给二维码内容")
            return

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        out = self.state_path.parent / "login_qr.png"

        if content.startswith("http"):
            # 这个链接就是二维码的载荷：微信扫出来会打开它，页面自己完成登录。
            self.qr_url = content
            png = qr.render_png(content)
            if png:
                out.write_bytes(png)
                self.qr_file = out
                log.info("二维码已生成：%s（日历页面顶部就能扫）", out)
            else:
                # 退一步：也许服务端给的真是图片
                try:
                    resp = self.session.get(content, timeout=TIMEOUT_SEND)
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if resp.status_code == 200 and "image" in ctype and resp.content:
                        out.write_bytes(resp.content)
                        self.qr_file = out
                        log.info("二维码图片已下载：%s", out)
                except requests.RequestException as exc:
                    log.debug("下载二维码图片失败：%s", exc)
            log.info("也可以直接在手机上打开：%s", content)
        else:
            try:
                out.write_bytes(base64.b64decode(content))
                self.qr_file = out
                log.info("二维码图片已存到 %s", out)
            except Exception as exc:  # noqa: BLE001
                log.info("二维码内容：%s（解析失败：%s）", content[:80], exc)

    def _ask_verify_code(self) -> Optional[str]:
        """数字配对码。

        先看文件再看终端：容器里没有可交互终端，「写文件」是唯一的通路；
        而终端 `input()` 在非交互环境下一旦被调到就会阻塞整条登录线程。
        """
        path = self.state_path.parent / "verify_code.txt"
        if path.is_file():
            try:
                code = path.read_text(encoding="utf-8").strip()
                path.unlink()
            except OSError:
                code = ""
            if code:
                log.info("从 %s 读到配对码", path)
                return code
        try:
            if os.isatty(0):
                code = input("手机提示要数字配对码，请输入：").strip()
                return code or None
        except (EOFError, OSError):
            pass
        log.warning(
            "服务端要求数字配对码，但没有可交互终端。"
            "把验证码写进 %s，下一轮会自动读取。", path
        )
        return None

    def login(self, timeout_total: float = 480.0, refresh_limit: int = 3) -> bool:
        """完整登录状态机。成功返回 True 并持久化 token。"""
        deadline = time.time() + timeout_total
        refreshes = 0
        self._qr_refresh.clear()  # 上一轮遗留的刷新请求不该立刻打断这一轮
        while time.time() < deadline and not self._stop.is_set():
            data = self._post(
                "/ilink/bot/get_bot_qrcode?bot_type=3",
                {"local_token_list": self._token_list()},
                timeout=TIMEOUT_SEND,
                base=self.entry_base,
                auth=False,
            )
            qrcode = data.get("qrcode") or ""
            if not qrcode:
                raise WeChatError("获取二维码成功但没带 qrcode 字段")
            log.info("二维码标识 %s", _mask(qrcode))
            self._present_qr(data.get("qrcode_img_content") or "")

            self.qr_base = self.entry_base
            verify_code: Optional[str] = None
            manual = False

            while time.time() < deadline and not self._stop.is_set():
                if self._qr_refresh.is_set():
                    self._qr_refresh.clear()
                    manual = True
                    # 手动刷新是「我愿意继续等」，所以重新计时；
                    # 而且不算进 refresh_limit —— 那是防上游作废循环的，不是防用户的
                    deadline = time.time() + timeout_total
                    log.info("按页面请求换一张二维码，重新等待 %.0f 秒", timeout_total)
                    break
                params: dict[str, Any] = {"qrcode": qrcode}
                if verify_code:
                    params["verify_code"] = verify_code
                try:
                    st = self._get(
                        "/ilink/bot/get_qrcode_status", params, timeout=TIMEOUT_POLL,
                        base=self.qr_base,
                    )
                except NetworkError as exc:
                    # 网络超时、网关 524 都按 wait 处理，继续轮询
                    log.debug("扫码状态轮询失败（%s）：%s", exc.kind, exc)
                    if exc.kind != "timeout":
                        time.sleep(2)
                    continue

                status = str(st.get("status") or "").lower()
                if status == "wait":
                    continue
                if status == "scaned":
                    verify_code = None
                    log.info("已扫码，请在手机上确认")
                    continue
                if status == "need_verifycode":
                    code = self._ask_verify_code()
                    if code:
                        verify_code = code
                    else:
                        time.sleep(3)
                    continue
                if status == "verify_code_blocked":
                    log.warning("配对码连错太多次，换一张二维码")
                    verify_code = None
                    break
                if status == "scaned_but_redirect":
                    host = st.get("redirect_host") or ""
                    if host:
                        self.qr_base = f"https://{host}"
                        log.info("切换到节点 %s 继续轮询", host)
                    continue
                if status == "binded_redirect":
                    # 只有本地确实还留着 token 才算成功，不能凭这个状态凭空造 token
                    if self.state.get("bot_token"):
                        log.info("账号已绑定过本客户端，复用本地 token")
                        return True
                    log.info("账号已绑到别的客户端，重新申请二维码")
                    break
                if status == "expired":
                    log.info("二维码过期，重新申请")
                    break
                if status == "confirmed":
                    token = st.get("bot_token") or ""
                    if not token or not st.get("ilink_bot_id"):
                        raise WeChatError("登录确认了但没拿到 bot_token / ilink_bot_id")
                    self.state.update({
                        "bot_token": token,
                        "baseurl": st.get("baseurl") or self.entry_base,
                        "ilink_bot_id": st.get("ilink_bot_id") or "",
                        "ilink_user_id": st.get("ilink_user_id") or "",
                    })
                    self._tickets.clear()
                    self._save_state()
                    log.info(
                        "登录成功：bot=%s user=%s baseurl=%s",
                        _mask(self.state["ilink_bot_id"]), _mask(self.state["ilink_user_id"]),
                        self.state["baseurl"],
                    )
                    return True
                log.debug("没见过的扫码状态 %s，继续", status)

            # 内层循环是 break 出来的（二维码作废/换节点/已绑别的客户端），换一张
            if manual:
                continue
            refreshes += 1
            if refreshes > refresh_limit:
                raise WeChatError(f"二维码连续 {refresh_limit} 次作废，登录放弃")

        if self._stop.is_set():
            return False
        raise WeChatError("登录超时（等太久没人扫码）")

    def logout(self, keep_cursor: bool = False) -> None:
        self.state["bot_token"] = ""
        if not keep_cursor:
            self.state["get_updates_buf"] = ""
        self._tickets.clear()
        self._save_state()
        # 旧二维码已经随着这个会话一起作废了：留着它，页面就会继续把那张扫不动的
        # 图摆在那儿（用户扫了没反应，还以为是网络问题）。清掉之后页面会显示
        # 「正在向微信申请二维码」，等 login() 换了新的再出现。
        self.qr_file = None
        self.qr_url = ""

    # -- 发送 ---------------------------------------------------------

    def get_typing_ticket(self, user_id: str, context_token: str = "") -> Optional[str]:
        if user_id in self._tickets:
            return self._tickets[user_id]
        try:
            data = self._post(
                "/ilink/bot/getconfig",
                {"ilink_user_id": user_id, "context_token": context_token},
                timeout=TIMEOUT_SHORT,
            )
        except (WeChatError, NetworkError) as exc:
            log.debug("getconfig 失败（不影响发正文）：%s", exc)
            return None
        ticket = data.get("typing_ticket") or ""
        if ticket:
            self._tickets[user_id] = ticket
        return ticket or None

    def send_typing(self, user_id: str, status: int, context_token: str = "") -> None:
        """输入状态。纯体验功能，失败绝不能影响正文。"""
        ticket = self.get_typing_ticket(user_id, context_token)
        if not ticket:
            return
        try:
            self._post(
                "/ilink/bot/sendtyping",
                {"ilink_user_id": user_id, "typing_ticket": ticket, "status": status},
                timeout=TIMEOUT_SHORT,
            )
        except (WeChatError, NetworkError) as exc:
            log.debug("sendtyping(%s) 失败：%s", status, exc)

    def send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str = "",
        session: Optional[requests.Session] = None,
    ) -> bool:
        to_user_id = str(to_user_id or "")
        if not to_user_id or not text:
            return False
        token = context_token or (self.state.get("context_tokens") or {}).get(to_user_id, "")
        ok = True
        for chunk in split_text(text):
            msg: dict[str, Any] = {
                # from_user_id 必须是空字符串，client_id 必须每次唯一 ——
                # 少了这两个字段，HTTP 200 但微信收不到（上游最关键的坑）
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": _new_client_id(),
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": chunk}}],
            }
            if token:
                msg["context_token"] = token
            try:
                self._post("/ilink/bot/sendmessage", {"msg": msg}, timeout=TIMEOUT_SEND, session=session)
            except StaleToken:
                raise
            except (WeChatError, NetworkError) as exc:
                log.error("发给 %s 失败：%s", _mask(to_user_id), exc)
                ok = False
                break
        return ok

    def notify(self, kind: str, timeout: float = TIMEOUT_SHORT) -> None:
        """notifystart / notifystop，最佳努力。"""
        path = "/ilink/bot/msg/notifystart" if kind == "start" else "/ilink/bot/msg/notifystop"
        if not self.logged_in:
            return
        try:
            self._post(path, {}, timeout=timeout)
            log.info("已通知服务端 %s", kind)
        except (WeChatError, NetworkError) as exc:
            log.warning("%s 通知失败（不影响收发）：%s", kind, exc)

    # -- 媒体 ---------------------------------------------------------

    def _fetch_cdn(self, cdn: dict[str, Any]) -> bytes:
        full = cdn.get("full_url")
        if full:
            url = full
        else:
            param = cdn.get("encrypt_query_param")
            if not param:
                raise WeChatError("这条媒体消息里没有可用的下载地址")
            url = f"{self.cdn_base}/download?encrypted_query_param={quote(str(param), safe='')}"
        try:
            resp = self.session.get(url, timeout=60)
        except requests.RequestException as exc:
            raise NetworkError(_classify(exc), f"下载媒体失败：{exc}") from exc
        if resp.status_code >= 400:
            raise WeChatError(f"下载媒体返回 HTTP {resp.status_code}")
        body = resp.content
        if len(body) > MAX_MEDIA_BYTES:
            raise WeChatError(f"媒体 {len(body)} 字节，超过 100 MiB 上限")
        return body

    def download_image(self, item: dict[str, Any]) -> tuple[bytes, str]:
        """下载并解密一张图片，返回 (明文, mime)。"""
        container = item.get("image_item") or {}
        cdn = container.get("media") or {}
        if not cdn:
            raise WeChatError("图片消息里没有 media 字段")
        raw = self._fetch_cdn(cdn)

        key_b64 = None
        for candidate in (container.get("aeskey"), cdn.get("aes_key")):
            if candidate:
                key_b64 = candidate
                break
        if not key_b64:
            log.warning("图片没有 aes_key，按明文处理")
            return raw, image_mime(raw)
        try:
            key = decode_aes_key(str(key_b64))
            plain = decrypt_media(raw, key)
        except Exception as exc:  # noqa: BLE001
            log.warning("图片解密失败（%s），退回原始字节", exc)
            return raw, image_mime(raw)
        if not plain:
            raise WeChatError("图片解密后是空的")
        return plain, image_mime(plain)

    # -- 入站处理 -----------------------------------------------------

    @staticmethod
    def extract_text(items: list[dict[str, Any]]) -> str:
        parts = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if it.get("type") == 1:
                text_item = it.get("text_item")
                if isinstance(text_item, dict) and text_item.get("text"):
                    parts.append(str(text_item["text"]))
            elif it.get("type") not in (0, 2, 3, 4, 5, 11, 12):
                # 未知类型不能当成文本读
                log.info("跳过未知 item 类型：%r", it.get("type"))
        return "\n".join(parts).strip()

    def handle_inbound(self, msg: dict[str, Any]) -> None:
        user_id = str(msg.get("from_user_id") or "")
        if not user_id:
            return
        if msg.get("group_id"):
            # 协议没有群聊发送分支，私聊回复发言人是错的，直接跳过
            log.info("跳过群聊消息 group_id=%s", _mask(msg.get("group_id")))
            return

        context_token = str(msg.get("context_token") or "")
        if context_token:
            tokens = self.state.setdefault("context_tokens", {})
            if tokens.get(user_id) != context_token:
                tokens[user_id] = context_token
                self._save_state()

        if self.allowed and user_id not in self.allowed:
            log.warning("用户 %s 不在 allowed_users 白名单里，忽略", _mask(user_id))
            return

        items = [it for it in (msg.get("item_list") or []) if isinstance(it, dict)]
        msg_id = str(msg.get("message_id") or msg.get("client_id") or "")
        images = [it for it in items if it.get("type") == 2]
        text = self.extract_text(items)

        if not text and not images:
            log.info("这条消息里没有文本也没有图片，跳过（msg_id=%s）", _mask(msg_id))
            return

        reply_parts: list[str] = []
        self.send_typing(user_id, 1, context_token)
        try:
            if text:
                log.info("收到文字（%s）：%s", _mask(user_id), text[:60].replace("\n", " "))
                reply = self.bot.handle_text(text, user_id=user_id, msg_id=msg_id)
                if reply:
                    reply_parts.append(reply)
            for idx, item in enumerate(images):
                try:
                    data, mime = self.download_image(item)
                except (WeChatError, NetworkError) as exc:
                    log.error("取图失败：%s", exc)
                    reply_parts.append(f"⚠️ 这张图我没取到：{exc}")
                    continue
                log.info("收到图片 %d 字节（%s）", len(data), mime)
                if self.save_media:
                    self._persist_media(msg_id, idx, data, mime)
                # 同一条消息里多张图共用 msg_id 会互相顶掉，加后缀区分
                img_msg_id = f"{msg_id}#{idx}" if msg_id else ""
                reply = self.bot.handle_image(data, mime=mime, user_id=user_id, msg_id=img_msg_id)
                if reply:
                    reply_parts.append(reply)
        finally:
            self.send_typing(user_id, 2, context_token)

        if reply_parts:
            body = "\n\n".join(reply_parts)
            # 一言贴在最后，隔一个空行（用户 2026-10-07 要的：每条回复都带）。
            # 这里再兜一层 try：彩蛋接口出任何意外都不能把回复本身弄丢 ——
            # `sayings.Sayings` 自己已经不抛了，但那是它的承诺，不是这条路的保证。
            try:
                body += self.sayings.tail()
            except Exception as exc:
                log.warning("一言没贴上（这条回复照发）：%s", exc)
            self.send_text(user_id, body, context_token)

    def _persist_media(self, msg_id: str, idx: int, data: bytes, mime: str) -> None:
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/bmp": ".bmp"}.get(mime, ".bin")
        safe = "".join(c for c in str(msg_id) if c.isalnum() or c in "-_")[-16:] or "msg"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{safe}-{idx}{ext}"
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
            (self.media_dir / name).write_bytes(data)
        except OSError as exc:
            log.debug("媒体存盘失败：%s", exc)

    # -- 主循环 -------------------------------------------------------

    def _sleep(self, seconds: float, *, wake_on_refresh: bool = True) -> bool:
        """等一会儿。返回 True 表示该退出了（stop）。

        两个「提前醒」的来源：stop，以及网页上点了「刷新二维码」。退避期间
        （尤其是 token 失效那条 `STALE_TOKEN_COOLDOWN` 秒的冷却）如果只置位
        不叫醒，用户点了按钮要等退避结束才有动静 —— 看起来就是「点了没反应」。

        被刷新请求叫醒时返回 False：调用处会回到循环顶部，那时 `logged_in`
        已经是假，于是重新走 `login()` 申请一张新二维码。

        `wake_on_refresh=False` 给长轮询的退避用：那时候人已经登录着、
        二维码根本不需要，被这个请求提前叫醒只会让退避失效、白打接口。
        """
        deadline = time.monotonic() + max(0.0, seconds)
        with self._wake:
            while True:
                if self._stop.is_set():
                    return True
                if wake_on_refresh and self._qr_refresh.is_set():
                    log.info("刷新二维码的请求打断了等待，这就去换一张")
                    return False
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                # 最多等 1 秒再回头看一眼：信号处理器只会置 `_stop`（它不能去抢
                # 这把锁 —— 主线程正抱着它等的时候去抢就是死锁），所以这里自己
                # 勤看着点，SIGTERM 才不会被一个 3600 秒的冷却拖住。
                self._wake.wait(min(left, 1.0))

    def poll_once(self) -> int:
        """跑一轮长轮询，返回收到的消息条数。异常按协议语义向上抛。"""
        body = {"get_updates_buf": self.state.get("get_updates_buf") or ""}
        data = self._post("/ilink/bot/getupdates", body, timeout=self.timeout_poll + 10)

        ms = data.get("longpolling_timeout_ms")
        if isinstance(ms, (int, float)) and ms > 0:
            self.timeout_poll = max(5.0, min(120.0, float(ms) / 1000.0))

        new_buf = data.get("get_updates_buf")
        if isinstance(new_buf, str) and new_buf:
            self.state["get_updates_buf"] = new_buf
            self._save_state()

        msgs = [m for m in (data.get("msgs") or []) if isinstance(m, dict)]
        for msg in msgs:
            if self._stop.is_set():
                break
            try:
                self.handle_inbound(msg)
            except StaleToken:
                raise
            except Exception as exc:  # noqa: BLE001 - 单条消息出错不能拖垮循环
                log.exception("处理消息失败：%s", exc)
        return len(msgs)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            if self._logout.is_set():
                # 网页上点了「退出微信」，token 已经被清掉了：再打接口就是拿一个
                # 空 token 去撞 -14，然后白白吃一小时冷却。这里收工，让 `run()`
                # 回到顶部重新走登录、申请新二维码。
                log.info("收到退出微信的请求，长轮询收工")
                return
            try:
                self.poll_once()
                self._consecutive_failures = 0
                self.last_poll_at = time.time()
                self.last_poll_error = ""
            except StaleToken:
                self.last_poll_error = "登录已失效，正在等待重新登录"
                raise
            except NetworkError as exc:
                if exc.kind == "timeout":
                    # 客户端超时是长轮询的正常控制流，保留游标继续
                    log.debug("长轮询超时，继续")
                    self.last_poll_at = time.time()
                    continue
                self._consecutive_failures += 1
                self.last_poll_error = f"{exc.kind}: {exc}"
                wait = 30.0 if self._consecutive_failures >= 3 else 2.0
                log.warning(
                    "长轮询失败（%s，连续第 %d 次）：%s —— %.0f 秒后重试",
                    exc.kind, self._consecutive_failures, exc, wait,
                )
                if self._consecutive_failures >= 3:
                    self._consecutive_failures = 0
                if self._sleep(wait, wake_on_refresh=False):
                    return
            except WeChatError as exc:
                # ret/errcode 非 0：同样退避，不能空转打接口
                self._consecutive_failures += 1
                self.last_poll_error = str(exc)
                wait = 30.0 if self._consecutive_failures >= 3 else 2.0
                log.error("长轮询业务错误：%s —— %.0f 秒后重试", exc, wait)
                if self._consecutive_failures >= 3:
                    self._consecutive_failures = 0
                if self._sleep(wait, wake_on_refresh=False):
                    return

    def _install_signals(self) -> None:
        def handler(signum, _frame):  # noqa: ANN001
            log.info("收到信号 %s，准备退出", signum)
            self._stop.set()

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # 不在主线程（比如被测试拉起）时忽略
                pass

    def stop(self) -> None:
        self._stop.set()
        # 顺手把正在退避的循环叫醒：否则关容器要等满一整个冷却周期。
        with self._wake:
            self._wake.notify_all()

    def run(self) -> None:
        """阻塞运行：没登录就先扫码登录，然后一直长轮询。"""
        self._install_signals()
        log.info("ClawBot 启动，状态文件 %s", self.state_path)

        while not self._stop.is_set():
            try:
                if not self.logged_in:
                    # 退出微信留下的标志只管到下一次登录：新会话一开始它就作废，
                    # 否则长轮询一进去就退出（新二维码扫完了却一条消息都不收）。
                    self._logout.clear()
                    self.login()
                    continue
                # 已经登录着就不需要二维码：把可能残留的刷新请求吃掉。
                # 留着它会让下面那些退避等待全部立刻返回 —— 退避就失效了。
                if self._qr_refresh.is_set():
                    log.info("已经登录着，忽略刷新二维码的请求")
                    self._qr_refresh.clear()
                self.ready.set()
                self.notify("start")
                self._poll_loop()
            except StaleToken as exc:
                if self._logout.is_set():
                    # 这一发 -14 是**我们自己**刚把 token 清掉的结果（用户在网页上
                    # 点了退出微信），不是账号被暂停 —— 别拿一小时冷却把新二维码
                    # 挡在外面，直接回去重新登录。
                    log.info("退出微信后撞上 token 失效，跳过冷却直接重新扫码")
                    continue
                # 协议：-14 表示 token 失效；官方会暂停该账号 1 小时，别快速重试
                log.error("token 已失效（%s）。%.0f 秒后再尝试重新登录。", exc, STALE_TOKEN_COOLDOWN)
                self.logout(keep_cursor=True)
                if self._sleep(STALE_TOKEN_COOLDOWN):
                    break
            except NetworkError as exc:
                log.warning("连接失败（%s）：%s —— 10 秒后重试", exc.kind, exc)
                if self._sleep(10):
                    break
            except WeChatError as exc:
                log.error("业务错误：%s —— 10 秒后重试", exc)
                if self._sleep(10):
                    break
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001 - 任何意外都不该让进程默默死掉
                log.exception("主循环意外异常：%s", exc)
                if self._sleep(10):
                    break

        self.ready.clear()
        self.notify("stop", timeout=5)
        log.info("ClawBot 已停止")
