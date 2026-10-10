"""回复末尾那句「一言」：每条回复都去 https://v1.hitokoto.cn/ 取一句。

用户 2026-10-07 的要求（原文）：「机器人每次回复都脚本请求一次一言的接口
https://v1.hitokoto.cn/，输出一段一言文字，放在每次回复的最后面，与原回复分隔一行，
用**加粗」。微信只收纯文本、markdown 不生效，那两个星号会原样显示出来 —— 这一点问过
他，他选的就是「照写 `**一言文字**`」。

**为什么不是「回复的时候现场去请求一次」**：真机上量过（`probe_sayings3.sh`，NAS 上
连打 5 次）：同一个接口，快的 0.65 秒，慢的 5~9 秒，还有直接超时的 —— 时快时慢是常态。
现场请求的话，每一条回复都要陪着等这几秒，快路问句（本来秒回）会变得明显卡；而收发是
单线程的，等它的时候后面的消息也进不来。所以改成**提前取好、回复时直接拿**：

- `tail()` 从池子里拿一句（拿完立刻在后台补下一句）—— 回复本身不等网络。
- 池子空的那次（比如容器刚起来第一句）才同步取一回，而且只等 `WAIT` 秒；
  等不到就这一条不带，回复照发。
- 后台补池子是**连着补到 `POOL` 句**、并且复用同一条连接（`requests.Session`）：
  真机上每次重建 TLS 就要 1.5 秒，复用之后同一进程里的后续请求快得多。
- 平均仍然是**一次回复一次请求**（只是在它前面那条回复之后就发出去了）。

其余纪律：**任何失败都只是「这次没有这句」**（超时、超时、非 JSON、字段没了），
只写一条 warning，绝不抛给调用方；`app/pipeline.py` 完全不掺和 —— 几百个流水线用例
不该因为「回一条消息」就去连外网（测试一律注入 `NoSayings()` 或假的取句函数）。

开关两层（见 `enabled()`）：`WOOLBOT_NO_SAYINGS=1`（部署时的硬开关）> 设置页上的
「每条回复末尾带一句『一言』」（`settings.sayings_enabled()`，**没设过 = 开**；
用户 2026-10-07 要求放进页面，以前只能 SSH 改环境变量）。

**取哪几类**同样由页面说了算：设置页上的「一言来源」（`settings.sayings_source()`）
存一个字符串，怎么解释它**只有 `source_url()` 一处**说了算 —— 留空走
`DEFAULT_SOURCE`，一串分类字母拼成 `?c=d&c=i&c=k&c=h`（**官方文档那种
`?c=d,i,k,h` 真机回 400**，见 `source_url()` 的说明），`http://`／`https://`
开头的则原样当「自定义源」用。用户 2026-10-09 要的就是这个：
「根据一言官方提供的分类使用方法，在设置里面增加一言的自定义源」。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

#: 用户点名的接口（同一个域名，只是走 http）。回的是 JSON：`{"hitokoto": "…", …}`
#: **为什么不是 https**：真机上同一个进程连打 6 次（`probe_sayings6.sh`）——
#: `https://v1.hitokoto.cn/` 只成 3/6，成功那些平均 5.87 秒，另外三次直接 8 秒读超时；
#: 换成 `http://` 变成 6/6，平均 2.62 秒。这个是公开的玩笑接口，内容不值得为 TLS 付这个代价。
HITOKOTO_URL = "http://v1.hitokoto.cn/"

#: 官方分类（用户 2026-10-09 给的清单）：字母 → 中文名。**顺序就是帮助里的顺序**。
#: 故意没有 `j`（网易云）—— 用户列的清单里就没有它，没他点头不擅自加回来。
CATEGORIES = {
    "a": "动画",
    "b": "漫画",
    "c": "游戏",
    "d": "文学",
    "e": "原创",
    "f": "网络",
    "g": "其他",
    "h": "影视",
    "i": "诗词",
    "k": "哲学",
    "l": "抖机灵",
}

#: 分类清单的人话版（帮助文字、报错都用它，只在这一处拼）。
CATEGORY_TEXT = "、".join(f"{key} {name}" for key, name in CATEGORIES.items())

#: 没设过「一言来源」时取哪几类。**用户 2026-10-09 点名的四类**。
DEFAULT_SOURCE = "d,i,k,h"

#: 自定义源的网址最长多少字符。只是防手滑糊进来一大段，不是安全边界。
MAX_SOURCE_URL = 300


def _letters(raw: str) -> list[str]:
    """从一串里挑出认得的分类字母，**去重且保持出场顺序**。"""
    picked: list[str] = []
    for ch in (raw or "").strip().lower():
        if ch in CATEGORIES and ch not in picked:
            picked.append(ch)
    return picked


def source_url(raw: str = "") -> str:
    """把「一言来源」那串变成一个能 GET 的地址。**这是唯一的解释处。**

    - 空（没传也没设过）→ 默认分类 `DEFAULT_SOURCE`；
    - `http://`／`https://` 开头 → 原样用（这就是「自定义源」）；
    - 其余按分类字母解析（逗号、空格、顿号随便隔，大小写都认，重复只算一次），
      拼成 `?c=d&c=i&c=k&c=h`。

    **为什么不是 `?c=d,i,k,h`**：官方那个逗号写法真机回 HTTP 400
    （`{"status":400,"message":"很抱歉，没有分类有句子符合长度区间。"}`），
    而 `?c=d` 单类正常 —— 同一个参数重复给才是能用的多分类写法，
    真机上连打 10 次，回来的 `type` 就是 d/i/k/h 混着的。
    """
    text = (raw or "").strip()
    if not text:
        from . import settings as st  # 延迟 import：测试里库可能还没 init

        try:
            text = st.sayings_source()
        except Exception as exc:  # noqa: BLE001 — 读不出来就按默认分类走
            log.warning("读「一言来源」失败，按默认分类走：%s", exc)
            text = ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    picked = _letters(text) or _letters(DEFAULT_SOURCE)
    return HITOKOTO_URL + "?" + "&".join(f"c={key}" for key in picked)


def source_problem(raw: str) -> str:
    """存进库之前校验「一言来源」。合格回空串，不合格回一句给人看的话。

    **只判断不改数**（和 `settings.key_problem()` 一个态度）：不认的字母当场报错、
    绝不悄悄丢掉 —— 用户以为设的是 `i,k`，实际只生效了 `i`，这种安静的错误
    比报错难查一百倍。
    """
    text = (raw or "").strip()
    if not text:
        return ""       # 留空是合法的：回到默认分类
    if text.startswith("http://") or text.startswith("https://"):
        if any(ch.isspace() for ch in text):
            return "一言来源的网址里不能有空格"
        if len(text) > MAX_SOURCE_URL:
            return f"一言来源的网址太长了（最多 {MAX_SOURCE_URL} 个字符）"
        return ""
    for ch in text:
        if ch.isalpha() and ch.lower() not in CATEGORIES:
            return f"分类里没有「{ch}」——只有 {CATEGORY_TEXT}"
    if not _letters(text):
        return f"一言来源要填分类字母（{CATEGORY_TEXT}），或者一个 http:// 开头的网址"
    return ""

#: 后台补池子时最多等它多久。真机上它 0.65~9 秒都出现过，所以给得宽一点 ——
#: 反正在后台等，不占回复的时间。
TIMEOUT = 8.0

#: 池子空、必须在回复里现场等一回了，最多等这么久。宁可不带这一句，也不让回复卡住。
WAIT = 1.5

#: 池子里最多囤几句（囤多了没意义，还都是老句子）
POOL = 2

#: 标记「这是一句一言」。按用户原话照写 —— 微信不支持加粗，星号会露出来。
MARK = "**"


def enabled() -> bool:
    """总开关。两层，**环境变量更硬**（每次调用现读）：

    1. `WOOLBOT_NO_SAYINGS=1` —— 部署时的硬开关，设了就彻底不取，页面上也开不回来；
    2. 页面上的「每条回复末尾带一句一言」（`settings.sayings_enabled()`，**没设过 = 开**）。

    第二层是用户 2026-10-07 要的：以前只能 SSH 改环境变量，现在在设置里点一下就行，
    存了立刻生效（`settings` 只在写的时候清缓存）。
    """
    raw = os.environ.get("WOOLBOT_NO_SAYINGS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return False
    from . import settings as st  # 延迟 import：settings 在测试里可能还没 init 库

    return st.sayings_enabled()


class Sayings:
    """一言。`tail()` 永远回字符串 —— 取不到就是空串，不抛异常，也不等网络。"""

    def __init__(
        self,
        url: Optional[str] = None,
        timeout: float = TIMEOUT,
        fetcher: Optional[Callable[..., str]] = None,
    ) -> None:
        #: 空 = **取的时候现读**设置页上的「一言来源」（`source_url()`）；
        #: 传了就用传的（测试、探针）。
        self.url = url
        self.timeout = timeout
        #: 换掉取句子的实现（测试用；默认走下面那个 HTTP 版本）
        self._fetch_impl = fetcher or self.fetch
        self._pool: list[str] = []
        self._lock = threading.Lock()
        self._busy = False
        #: 复用同一条连接：真机上量过，每次都新建 TLS 要多花 1.5 秒左右
        #: （`probe_sayings2.sh`：TCP 0.27s、TLS 1.47s、之后 GET 只要 0.88s）
        self._session = requests.Session()

    # -- 取一句 ---------------------------------------------------------

    def fetch(self, timeout: Optional[float] = None) -> str:
        """同步去取一句。**任何失败都返回空串**，调用方不用 try。

        地址**每次现算**（`source_url()`）：设置页上换分类或换源之后，下一次
        取句子就跟着变，不用重启（和「一言」那个开关同一个脾气）。
        """
        data = None
        url = self.url or source_url()
        try:
            resp = self._session.get(
                url,
                timeout=timeout or self.timeout,
                headers={"User-Agent": "woolbot/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            text = str((data or {}).get("hitokoto") or "").strip()
        except Exception as exc:  # 网络不通、超时、非 JSON、字段没了 —— 都算「这次没有」
            log.warning("取一言失败，这次就不带了：%s", exc)
            return ""
        if not text:
            log.warning("一言接口这回没给句子：%r", data)
            return ""
        # 一言里偶尔带换行，塞进回复会把「分隔一行」的排版搞乱
        return " ".join(text.split())

    def prefetch(self) -> None:
        """后台补一句进池子（同时只会有一个在跑）。可以随时调，重复调是安全的。

        开关关着时**一句都不取** —— 光在 `tail()` 里挡是不行的：`wechat.ClawBot`
        一启动就会调这里（`:251`），关掉开关却还在后台偷请求那个接口，
        等于「关了还在发请求」。
        """
        if not enabled():
            return
        with self._lock:
            if self._busy or len(self._pool) >= POOL:
                return
            self._busy = True
        threading.Thread(target=self._fill, name="sayings-prefetch", daemon=True).start()

    def _fill(self) -> None:
        """后台把池子补满（`POOL` 句）。取不到就停手，不对着打不通的接口连着打。"""
        try:
            while True:
                with self._lock:
                    if len(self._pool) >= POOL:
                        return
                text = self._fetch_impl()
                if not text:
                    return
                with self._lock:
                    # 补的过程中只有 take() 会让池子变短，所以这里直接收下不会超
                    self._pool.append(text)
        finally:
            with self._lock:
                self._busy = False

    def take(self, wait: Optional[float] = None) -> str:
        """拿一句：池子里有就直接拿；没有就只等 `WAIT` 秒（不是 `timeout`）。"""
        with self._lock:
            if self._pool:
                return self._pool.pop(0)
        return self._fetch_impl(timeout=WAIT if wait is None else wait)

    # -- 给回复用的那一小段 ---------------------------------------------

    def tail(self) -> str:
        """要贴在回复末尾的那一段（**自己带前面那个空行**）；没有就是空串。"""
        if not enabled():
            return ""
        text = self.take()
        if not text:
            # 这次没拿到，也别等下次回复才想起来 —— 挂个后台去补
            self.prefetch()
            return ""
        self.prefetch()
        return f"\n\n{MARK}{text}{MARK}"


def with_sentence(text: str) -> str:
    """测试与探针用：把一句话包成 tail 的样子。"""
    return f"\n\n{MARK}{text}{MARK}"


class NoSayings:
    """测试用：永远没有一言，也绝不连网。"""

    def tail(self) -> str:
        return ""

    def prefetch(self) -> None:
        return None
