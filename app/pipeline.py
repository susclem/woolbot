"""业务流水线：录入、查询、路由、回复排版。

这里有一条贯穿始终的纪律：

    模型负责「翻译」，SQL 负责「检索」，Python 负责「排版」。

查询结果**从不**交给模型复述，因此不可能出现库里没有的活动。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional, Tuple

from . import repeat as rp
from . import timeutil as tu
from .db import (
    activities_on_day,
    clear_pending,
    delete_activity,
    done_days,
    find_duplicate,
    get,
    grab_in_window,
    insert_activity,
    list_range,
    mark_msg,
    pop_pending,
    refresh_expired,
    search,
    seen_msg,
    set_pending,
    stats,
    upcoming,
)
from .llm import LLM, LLMError

log = logging.getLogger("woolbot.pipeline")

#: 用户 2026-10-11 第三次亲手给的整份帮助（原话：「将原来的使用帮助严格替换为以下内容」）。
#: 与他贴过来的原文只有一处不同：开头那行标题「🧰 羊毛机器人」按他要求删掉了（弹窗大标题
#: 已经写着 woolbot，正文再自称一次是重复）。
#:
#: **三个小标题和最后那句外面的 `**` 是他原文里的，是他要的加粗写法，别去动。** 微信只收
#: 纯文本、markdown 不生效，所以星号会原样显示出来 —— 我第一版把星号当成噪声抹掉了，他
#: 回了一句「你理解错了，我要的就是*号加粗」。同一套写法 `app/sayings.py` 早就用过
#: （用户 2026-10-07 也选的「照写 `**…**`」），README 的排障表有那一行。
HELP_TEXT = """**1. 核心操作**
▪️ 加活动：直接发文字或截图（调用AI）。
▪️ 查活动：严格按「日期+时间+查什么」发（如：明天8:30抢什么）不调用AI；多打字（如加「京东」）会走大模型解析（调用AI）。
▪️ 删活动：发「删+关键词」 ，如：删迪士尼（调用AI）。推荐进日历地址手动删除

**2. 一言设置**
去页面「设置」-「一言来源」：
▪️ 留空：默认文学、诗词、哲学、影视
▪️ 填分类：根据页面提示输入对应字母，逗号隔
▪️ 关闭：关掉「bot回复末尾一言」

**3. 快捷指令**
/今天 /即将 /统计 /日历 /help

💡 
**标准句式或快捷指令 = 不调用AI；截图、大白话、非标句式 = 调用AI。**"""


#: 模型没给标题时的兜底。单独抽成常量是因为「模型漏了标题」是一种**要被抓出来的错**
#: —— 兜底会让它静默变成日历上一条叫「未命名活动」的东西（`tools/check_extract.py`
#: 就拿这个值当警报）。
TITLE_FALLBACK = "未命名活动"

#: 陪聊关着的时候（设置页的「允许机器人闲聊」，**默认就是关**），整句寒暄就地回这一句。
#: 用户 2026-10-07 选的：「非活动消息一律固定话术」—— 回复完全可预期，一次模型都不花。
CHAT_OFF_GREETING = (
    "你好～我只管抢券的事：发活动信息我来记，问「今天抢什么」我查库。"
    "要用法发「使用帮助」。"
)

#: 陪聊关着的时候，认不出来是活动的消息（「今天天气怎么样」这种）统一回这一句。
CHAT_OFF_HINT = (
    "我只管抢券的事：发活动信息我来记，问「今天抢什么」我查库。要用法发「使用帮助」。"
)

#: 常见寒暄。陪聊关着时**整句**命中就本地回掉，省掉一次模型调用。
#: 只做「整句相等」匹配（先 strip + lower），所以「你好，明天9点京东抢券」不会被吞掉
#: —— 那句话是录入，得走模型。
_SMALL_TALK = frozenset({
    "你好", "您好", "哈喽", "嗨", "hello", "hi", "在吗", "在么", "在不在",
    "早上好", "中午好", "下午好", "晚上好", "晚安", "再见", "拜拜", "byebye",
    "谢谢", "多谢", "感谢", "辛苦了", "你是谁", "你叫什么", "你叫什么名字", "你是什么",
    "好的", "好", "嗯", "嗯嗯", "哦", "收到", "知道了", "ok",
})


def _is_small_talk(text: str) -> bool:
    """整句就是一句寒暄吗。只给「陪聊关着」那条路用。"""
    return (text or "").strip().lower() in _SMALL_TALK


def _chat_enabled() -> bool:
    """设置页上的「允许机器人闲聊」开着吗。没设过 = 关。

    单独包一层是为了两件事：设置运行中能改（`settings` 自己有缓存，写完就失效），
    以及**读设置失败不能让整条消息处理崩掉** —— 那种情况下按「关」处理，最多是少陪
    一句聊，不会丢一条活动。
    """
    try:
        from . import settings as st

        return st.chat_enabled()
    except Exception as exc:  # noqa: BLE001
        log.warning("读「允许闲聊」开关失败，按关闭处理：%s", exc)
        return False


def _front_url(cfg: Any = None) -> str:
    """日历页面的地址。**只调 `settings.front_url()`，不在这里拼。**

    以前这里自己拼过 `http://<你的NAS地址>:8080/`，发出去的是个占位符 ——
    同一个地址有两个实现，就一定会有一个是错的。读设置失败时给一句人话，
    不能让 `/日历` 因为一个地址把整条消息处理搞崩。
    """
    try:
        from . import settings as st

        return st.front_url(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("算日历地址失败：%s", exc)
        return "地址算不出来，去 NAS 上看容器日志里的那行「日历页面已启动」。"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _days_between(d_from: date, d_to: date) -> list[date]:
    """闭区间里的每一天。调用方已保证 `d_from <= d_to`，但这里也防一手死循环。"""
    if d_to < d_from:
        return [d_from]
    return [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]


# ---------------------------------------------------------------------------
# 自然语言删除
# ---------------------------------------------------------------------------
#
# 这一块的纪律：**模型只说「删哪个」，Python 决定「删不删」**。
# 模型从头到尾拿不到删除能力，它抽出的关键词只用来检索；真正落刀前必须由用户
# 回一个编号。删除是不可逆的，所以每一步都宁可多问一句。

#: 句子里出现这些词就算「用户想删东西」。有动词才认 —— 光有「梦享」两个字
#: 可能是查询、也可能是录入，抢过来就错了。
_DELETE_VERBS = ("删除", "删掉", "删了", "去掉", "移除", "清除", "清掉", "删", "不要了")

#: 句尾的语气词/废话。「的活动」必须在「活动」前面，否则「梦享的活动」会剩个「的」。
_TAIL_NOISE = ("的活动", "活动", "这一条", "那一条", "这个", "那个", "吧", "呗", "啊", "呀",
               "。", "！", "!", "~", "，", ",", " ")

#: 中文数字。只认单字，够用了 —— 用户不会说「第二十三条」。
#: 两个用处：`_pick_indices` 认「回第二条」里的「二」，`_cn_int` 认「十五点」里的字
#: （用户点过名的说法）。只此一份 —— 别在时刻那节再定义一遍，那样这份就没人用了。
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_CANCEL_WORDS = ("取消", "算了", "不用", "不删", "别删", "不要", "放弃", "先不", "不动")

_ALL_WORDS = ("都删", "全删", "全都", "全部", "所有", "都清", "一起删", "统统")


def _clean_keyword(raw: Any) -> str:
    """把关键词上的动词残渣和语气词摘干净。"""
    kw = str(raw or "").strip()
    kw = re.sub(r"^(一下|把|帮我|请|给我)\s*", "", kw)
    kw = re.sub(r"^(那个|这个|那条|这条)\s*", "", kw)
    changed = True
    while changed and kw:
        changed = False
        for noise in _TAIL_NOISE:
            if kw.endswith(noise):
                kw = kw[: -len(noise)].strip()
                changed = True
    return kw


def _fallback_keyword(text: str) -> str:
    """模型没认出「删除」意图时，从句子本身把关键词抽出来。

    为什么要有这条兜底：模型答一句 `other` 或者把「删除梦享」当成查询，对用户来说
    就是**对着机器人说了要删，它却没反应**。而猜错的代价只是多问一句「你要删哪条」。
    """
    t = (text or "").strip()
    if not t or len(t) > 40:
        return ""  # 长句子多半是正经的活动信息，别乱抢
    alt = "|".join(re.escape(v) for v in _DELETE_VERBS)
    for pattern in (
        rf"把(.+?)(?:{alt})",       # 把临沂那个删了
        rf"^(.*?)(?:{alt})$",       # 梦享券删掉
        rf"(?:{alt})\s*(.*)$",      # 删除梦享
    ):
        m = re.search(pattern, t)
        if m:
            kw = _clean_keyword(m.group(1))
            if kw:
                return kw
    return ""


def _pick_indices(text: str, n: int) -> list[int]:
    """从用户的回复里挑出他指的是第几条（1 起）。挑不出来就是空列表。"""
    t = text or ""
    picked = [int(m.group()) for m in re.finditer(r"\d+", t)]
    if not picked:
        for ch in t:
            if ch in _CN_DIGITS:
                picked.append(_CN_DIGITS[ch])
                break  # 中文数字只认第一个：「第一条」已经够表达了
    out: list[int] = []
    for i in picked:
        if 1 <= i <= n and i not in out:
            out.append(i)
    return sorted(out)


def _looks_like_cancel(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _CANCEL_WORDS)


def _looks_like_all(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _ALL_WORDS)



def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "未知", "未定", "n/a", "na", "-"):
        return None
    return s


def _parse_date(value: Any) -> Optional[date]:
    s = _clean(value)
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _parse_time(value: Any) -> Optional[tuple[int, int]]:
    s = _clean(value)
    if not s:
        return None
    s = s.replace("：", ":")
    m = re.match(r"^(\d{1,2}):(\d{2})", s)
    if not m:
        m = re.match(r"^(\d{1,2})点(\d{1,2})?分?$", s)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2) or 0)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh, mm
    return None


def _mins(value: Optional[str]) -> Optional[datetime]:
    return tu.parse_minute(value)


# 相对日期的**兜底**解析。
#
# 为什么需要它：模型是「翻译器」，但它算日期会算错——而算错是**静默**的。
# 用户问「周三抢什么」，模型若没给出 date，老代码会默默退回「今天」，
# 于是用户拿到一份今天清单、还以为那是周三的。这种错最伤人。
# 「今天/明天/周三」这类词是纯确定性的东西，Python 自己解比模型可靠得多，
# 所以这里补一层：只在模型**一个日期都没给出**时才启用，绝不推翻模型的结论。
_REL_DAY_OFFSET = {
    "今天": 0, "今日": 0, "本日": 0,
    "明天": 1, "明日": 1, "第二天": 1,
    "后天": 2, "後天": 2,
    "大后天": 3,
    "昨天": -1, "昨日": -1,
    "前天": -2,
}
_REL_DAY_RE = re.compile("|".join(sorted(_REL_DAY_OFFSET, key=len, reverse=True)))

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
# 「这周三 / 下周二 / 上周一 / 周三 / 星期三 / 礼拜天」都认
_WEEKDAY_RE = re.compile(r"(上|下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])")


def _explicit_day(text: str, ref: Optional[date] = None) -> Optional[date]:
    """只认**无歧义**的日子词（今天/明天/后天/昨天/前天/大后天）。

    「周三」不算 —— 它到底指本周那个还是下一个，得看语气，交给模型判断。
    """
    if not text:
        return None
    m = _REL_DAY_RE.search(text)
    if not m:
        return None
    return (ref or tu.today()) + timedelta(days=_REL_DAY_OFFSET[m.group(0)])


def _weekday_day(ref: date, prefix: str, ch: str) -> date:
    target = _WEEKDAY_INDEX[ch]
    this_monday = ref - timedelta(days=ref.weekday())

    if prefix == "上":
        return this_monday - timedelta(days=7) + timedelta(days=target)
    if prefix == "下":
        return this_monday + timedelta(days=7) + timedelta(days=target)
    if prefix in ("这", "本"):
        # 「本周三」就是本周那个周三，哪怕它已经过去了
        return this_monday + timedelta(days=target)

    ahead = (target - ref.weekday()) % 7
    return ref + timedelta(days=ahead)


def _fallback_day(text: str, ref: Optional[date] = None) -> Optional[date]:
    """从「今天 / 周三 / 下周二」这类词里解出一个日期；解不出返回 None。

    规则刻意做得可预测：
      · 今天/明天/后天/昨天/前天/大后天 → 相对今天偏移
      · 上周X / 上星期X            → 上周那一天的 X
      · 下X / 下周X                → 下周那一天的 X
      · 周X / 本周X / 这周X        → **最近的**那个 X（今天算）
    最后一条是照人话来的：周三下午问「周二抢什么」，人指的是下一个周二，
    而不是已经过去的那一个。
    """
    if not text:
        return None
    ref = ref or tu.today()

    explicit = _explicit_day(text, ref)
    if explicit:
        return explicit

    m = _WEEKDAY_RE.search(text)
    if not m:
        return None
    return _weekday_day(ref, m.group(1) or "", m.group(2))


def _sole_day(text: str, ref: Optional[date] = None) -> Optional[date]:
    """原话里**所有**日子词都指向同一天时返回那天；出现两种说法就返回 None。

    比 `_fallback_day` 严格得多，因为它要拿去**推翻模型的结论**，所以只在原话
    毫无歧义时才配开口。「明天9点开抢，有效期到后天」里有两天的说法，究竟哪个词
    管哪个字段 Python 猜不出来（先出现的那个未必就是开抢日），一律交回给模型。
    """
    if not text:
        return None
    ref = ref or tu.today()

    days = {ref + timedelta(days=_REL_DAY_OFFSET[w]) for w in _REL_DAY_RE.findall(text)}
    for prefix, ch in _WEEKDAY_RE.findall(text):
        days.add(_weekday_day(ref, prefix or "", ch))
    if len(days) == 1:
        return days.pop()
    return None


# ---------------------------------------------------------------------------
# 固定句式的查询（不调模型的快路）
# ---------------------------------------------------------------------------
#
# 用户 2026-10-07 定下的规矩：查询用固定句式说 —— 「本周六抢什么」「明天8:30抢什么」
# 「本周五8:30抢什么」。这几样东西（日子、时刻、「抢什么」）**全是确定性的**，
# Python 自己解比模型可靠（模型在日期上错过，`_fallback_day` 就是为那个写的），
# 而且快：省掉一次模型往返，微信里几乎秒回。
#
# 唯一的纪律：**只有整句话正好是「日子 + 时刻 + 查询词」时才走这条路**。多一个字
# 就交回模型 —— 否则「明天9点京东超市满99减50抢券」这种录入会被抢过来当查询，
# 用户就会看到「那天没有活动」而不是「已记下」。

#: 句子里的查询词。有它才可能是查询；光有日子不算。
_QUERY_WORDS = (
    "抢什么", "抢啥", "有什么活动", "有哪些活动", "有什么", "有哪些", "有啥",
    "都有什么", "都有啥", "查什么", "查一下", "买什么",
)

#: 中文数字的字符集（转成整数由 `_cn_int` 负责，字典在文件上方只定义一份）。
_CN_NUM = r"[零〇一二两三四五六七八九十]{1,3}"

#: 时刻的说法：「8:30」「8点半」「晚上8点」「上午9点」「中午12:00」「十五点」。
#: 必须带 `:` 或「点」才认 —— 不带的话「10月9号」里的数字会被当成时刻。
_TIME_TEXT_RE = re.compile(
    r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜|晚)?"
    r"(\d{1,2}|" + _CN_NUM + r")"
    r"(?:[:：](\d{1,2})|点(半|(\d{1,2}|" + _CN_NUM + r")分?)?)"
)

#: 绝对日子：「10月9号」「10月9日」。
_ABS_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

#: 把这些语气词摘干净之后还剩东西，就说明这不是一句「纯查询」。
_QUERY_FILLER = " \t，,。.！!？?~、:：的了啊呀吧呢嘛哦"


def _cn_int(text: str) -> Optional[int]:
    """把「十五」「二十」「二十三」「八」这类中文数字转成整数（只管 0–99）。

    用户点名过「今天十五点抢什么」这种说法 —— 数字写成汉字也得认，否则整句话
    会被当成「多写了别的字」丢回给模型，白等一次往返。
    """
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s.count("十") == 1:
        head, _, tail = s.partition("十")
        tens = 1 if not head else _CN_DIGITS.get(head)
        ones = 0 if not tail else _CN_DIGITS.get(tail)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def _fallback_time(text: str) -> Optional[Tuple[int, int]]:
    """从原话里抠一个时刻；抠不到返回 None。

    只管钟点，不管「早上/晚上」之外的语义（「晚8点」＝20:00 是常识，Python 直接算）。
    数字可以是阿拉伯数字，也可以是汉字（「十五点」「九点二十」）。
    """
    m = _TIME_TEXT_RE.search(text or "")
    if not m:
        return None
    part, hh_s, mm_colon, half_or_num, mm_fen = m.groups()
    hh = _cn_int(hh_s)
    if hh is None:
        return None
    if mm_colon:
        mm = int(mm_colon)
    elif half_or_num == "半":
        # 注意：第 4 组是「半」**或**数字那个外层组，只有字面是「半」才是半点，
        # 否则「9点45」会被当成 9:30（这个坑真踩过）。
        mm = 30
    elif mm_fen:
        mm = _cn_int(mm_fen)
        if mm is None:
            return None
    else:
        mm = 0
    if part in ("下午", "傍晚", "晚上", "夜里", "晚", "半夜") and hh < 12:
        hh += 12
    elif part == "中午" and hh < 6:
        # 「中午1点」＝13:00；「中午11点」还是 11:00
        hh += 12
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _abs_day(text: str, ref: date) -> Optional[date]:
    """「10月9号」→ 那一年的 10 月 9 日。

    没写年份时就近取：候选日若比参照日早了半年以上，说明用户说的是**明年**那个
    （12 月说「1月5号」，指的显然是明年）。半年这个界很宽，宽到不会把「上个月的
    3 号」误判成明年。
    """
    m = _ABS_DATE_RE.search(text or "")
    if not m:
        return None
    try:
        cand = date(ref.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    if cand < ref - timedelta(days=180):
        try:
            cand = date(ref.year + 1, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return cand


def parse_fixed_query(text: str, ref: Optional[date] = None) -> Optional[dict[str, Any]]:
    """整句话正好是「日子 + 时刻 + 查询词」时，解成查询条件；否则 None。

    返回 `{"intent": "query", "date": "YYYY-MM-DD", "time": "HH:MM"|None}`，
    形状和模型给的 JSON 一样 —— 这样 `_apply_query` 那套（精确到分钟、重复规则、
    开抢/还能领的分区）一个字都不用改，两条路也**不可能**给出不同答案。
    """
    s = (text or "").strip()
    if not s or not any(w in s for w in _QUERY_WORDS):
        return None
    ref = ref or tu.today()

    day = _fallback_day(s, ref) or _abs_day(s, ref)
    clock = _fallback_time(s)

    # 把认得的部分全摘掉，还剩东西就不是纯查询 —— 交回模型。
    rest = s
    for w in sorted(_QUERY_WORDS, key=len, reverse=True):
        rest = rest.replace(w, "")
    rest = _REL_DAY_RE.sub("", rest)
    rest = _WEEKDAY_RE.sub("", rest)
    rest = _ABS_DATE_RE.sub("", rest)
    rest = _TIME_TEXT_RE.sub("", rest)
    rest = rest.strip(_QUERY_FILLER)
    if rest:
        return None
    if day is None:
        # 「9点半抢什么」这种只说时刻的老句式，日子就是今天 —— 和模型那条路
        # （`_apply_query` 缺 date 时按今天算）保持一致，两条路不许给出不同答案。
        if clock is None:
            return None
        day = ref
    out: dict[str, Any] = {"intent": "query", "date": day.isoformat()}
    if clock:
        out["time"] = f"{clock[0]:02d}:{clock[1]:02d}"
    return out


# ---------------------------------------------------------------------------
# 录入
# ---------------------------------------------------------------------------


def _align_day(
    acts: list[dict[str, Any]], text: str, ref: Optional[date] = None
) -> list[dict[str, Any]]:
    """原话说的是「明天」，模型却记成了今天 —— 以原话为准把日子拨正。

    这是查询侧 `_fallback_day` 的镜像，但录入侧的同类错误更隐蔽：模型把「明天
    上午9点半」记成今天 09:30，只要现在还没到 09:30，`_classify_past` 就拦不住
    （它只拦「落在今天之前」），活动会静默躺到错日子上，到点也不提醒。

    两道护栏，免得它帮倒忙：
      · 原话必须**只有一个**日子（`_sole_day`），含糊就交回给模型；
      · 模型给的日期若**早于今天**，一律不碰 —— 那是「补记一条已经开抢的」，
        该由 `_classify_past` 去问，不该被 Python 悄悄挪到下周。
    只拨 grab_at，以及**原本跟 grab_at 同一天**的 start_at / end_at。
    """
    ref = ref or tu.today()
    target = _sole_day(text, ref)
    if target is None:
        return []

    moved: list[dict[str, Any]] = []
    for a in acts:
        g = _mins(a.get("grab_at"))
        if g is None or g.date() == target or g.date() < ref:
            continue
        delta = target - g.date()
        for field in ("start_at", "end_at"):
            v = _mins(a.get(field))
            if v is not None and v.date() == g.date():
                a[field] = tu.to_minute(v + delta)
        a["grab_at"] = tu.to_minute(g + delta)
        moved.append(a)
    return moved


def _repair_end(acts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """有效期早于开抢时刻 —— 这条活动一入库就会被判成「已过期」。

    `db.refresh_expired()` 是拿 end_at 判过期的，所以一个算错的 end_at 会让刚记下
    的活动在日历上直接灰掉，用户还以为白记了。多半是模型把 end_at 的**日子**搞错
    （开抢 10-07 09:00，有效期却写 10-06 23:59），按天推到开抢当天即可。

    还有一种是日子看着没错、**时刻**却在开抢之前：用户说「到晚上12点」，
    口语里那是当天 24:00（即第二天 00:00），模型很容易直接写成当天 00:00。
    这种情况按天补齐是补不动的（days=0），得再往后推一天。
    """
    fixed: list[dict[str, Any]] = []
    for a in acts:
        end = _mins(a.get("end_at"))
        if end is None:
            continue
        anchors = [v for v in (_mins(a.get("grab_at")), _mins(a.get("start_at"))) if v is not None]
        if not anchors:
            continue
        anchor = max(anchors)
        if end >= anchor:
            continue
        shifted = end + timedelta(days=max(0, (anchor.date() - end.date()).days))
        if shifted <= anchor:  # 同一天但时刻早于开抢，见 docstring 第二段
            shifted += timedelta(days=1)
        a["end_at"] = tu.to_minute(shifted)
        fixed.append(a)
    return fixed


class IngestResult:
    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []
        self.question: Optional[str] = None
        self.pending_payload: Optional[dict[str, Any]] = None


def _normalize_activity(
    raw: dict[str, Any], source: str, text: str = ""
) -> dict[str, Any]:
    grab = tu.parse_minute(_clean(raw.get("grab_at")))
    start = tu.parse_minute(_clean(raw.get("start_at")))
    end = tu.parse_minute(_clean(raw.get("end_at")))
    conf = raw.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None
    note = _clean(raw.get("note"))
    # 重复规则：模型的 repeat 字段 / 它写的备注 / 用户原话 / 截图 OCR 原文，四处任一
    # 说得出「每天 9 点」就认。**这是 Python 兜底**，不是让模型自己算展开 —— 模型只
    # 负责认出「每天」，把「哪几天、几点」交给 repeat.py，免得它算错日子还是静默的。
    # OCR 也喂一路，是因为截图那条路没有用户原话，而模型有时只在 OCR 原文里留下
    # 「每自然月10号10:00-20号23:59:59」——#74 那条工行券就是这么丢成一次性活动的。
    rep = rp.fields_from(
        _clean(raw.get("repeat")), note, text, grab or start, _clean(raw.get("raw_ocr"))
    )
    return {
        "title": _clean(raw.get("title")) or TITLE_FALLBACK,
        "platform": _clean(raw.get("platform")),
        "category": _clean(raw.get("category")),
        "grab_at": tu.to_minute(grab) if grab else None,
        "start_at": tu.to_minute(start) if start else None,
        "end_at": tu.to_minute(end) if end else None,
        "url": _clean(raw.get("url")),
        "note": note,
        "confidence": conf,
        "source": source,
        "status": "pending",
        # 截图路径没有「用户原话」，就把图里认出的原文存下来（schema 里
        # raw_text 的注释写着「回溯纠错用」，此前一直没人写它）。
        "raw_text": text or _clean(raw.get("raw_ocr")),
        **rep,
    }


def _collect_activities(
    data: dict[str, Any], source: str, text: str = ""
) -> list[dict[str, Any]]:
    """兼容模型把活动放在 activities / multiple / 顶层 三种写法。"""
    raw_list: list[dict[str, Any]] = []
    for key in ("activities", "multiple", "items"):
        value = data.get(key)
        if isinstance(value, list) and value:
            raw_list = [v for v in value if isinstance(v, dict)]
            break
    if not raw_list:
        if any(k in data for k in ("title", "grab_at", "end_at")):
            raw_list = [data]
    return [_normalize_activity(r, source, text) for r in raw_list]


def _classify_past(acts: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """找出时间可疑的活动，分成两类 —— 两类的处置完全不同。

    · `wrong_day`：开抢时刻落在**今天之前**。这几乎必然是模型把「下周三」算成了
      「本周三」这类跨周错误，会让活动静悄悄掉到错误的日子上，必须拦下来问清楚。
    · `stale_today`：就在今天、但时刻已经过了。用户很可能只是在补记一条已经开抢的
      活动，拦下来问就太烦人了 —— 照记，**回执里也不再多说一句**（2026-10-10 用户
      把原来那句提醒划掉了：「就算时间已经过了也没关系，不用提醒，就正常录入就行了」）。
      挑出来只是为了让它们**不**落进 `wrong_day`；`tools/check_extract.py` 拿它做统计。
    """
    now = tu.now()
    today = tu.today()
    wrong_day: list[dict] = []
    stale_today: list[dict] = []
    for a in acts:
        g = _mins(a.get("grab_at"))
        stale = g is not None and g < now - timedelta(minutes=1)
        a["_stale"] = stale
        if not stale:
            continue
        if g.date() < today:
            wrong_day.append(a)
            a["_wrong_day"] = True
        else:
            stale_today.append(a)
            a["_wrong_day"] = False
    return wrong_day, stale_today


#: 微信只收纯文本（`wechat.py` 发的是 `item_list` 里的 `text_item`），markdown 的
#: `**08:30**` 在手机上是**原样显示**两个星号的。要让时刻真变粗，唯一的办法是换
#: 字形 —— 数学粗体数字 U+1D7CE..U+1D7D7。
#:
#: 万一哪台手机的字库缺这个区段（会显示成方框），把它换成 `"0123456789"` 就退回
#: 普通数字，其余代码一行都不用动。
_BOLD_DIGITS = "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"


def bold_time(label: str) -> str:
    """把标签**结尾的时刻**换成粗体字形，其余原样。

    用户 2026-10-07 要的「时间加粗」。只动结尾那一段 `数字:数字`，所以
    「3天后（周六）09:00」只有时刻变粗，前面的「3天后」不会被搞成怪样子，
    「时间未定」这类没有时刻的标签则一字不改。
    """
    m = re.search(r"(\d{1,2}:\d{2})$", label)
    if not m:
        return label
    clock = "".join(
        _BOLD_DIGITS[int(c)] if c.isdigit() else c for c in m.group(1)
    )
    return label[: m.start()] + clock


def _time_group_lines(pairs: list[tuple[str, str]]) -> list[str]:
    """同一个时刻只说一遍：先一行（加粗的）时刻，下面跟着它底下的标题。

    用户 2026-10-07 定的格式 —— 三张 08:30 的券以前要把 08:30 写三遍：

        𝟬𝟴:𝟯𝟬
        港城欢乐购·中国银行信用卡满1000元减50元券
        港城欢乐购 工商银行信用卡满50元减10元券

    调用方必须先按时刻排好序，否则同一个时刻会被拆成好几组。
    """
    lines: list[str] = []
    shown: Optional[str] = None
    for when, title in pairs:
        if when != shown:
            lines.append(f"   {bold_time(when)}")
            shown = when
        lines.append(f"   {title}")
    return lines


#: 往后找多久。每天/每周的重复型最多 7 天就能撞上下一场，剩下的余量留给
#: 「预售券」这种一次性的远期活动。
_NEXT_LOOKAHEAD_DAYS = 60


def _next_openings(after: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    """每一条活动**下一次开抢**的时刻，按时刻排序；同一条只出现一次。

    `db.upcoming` 只拿库里那串 `grab_at` 跟目标比大小，而重复型活动的 `grab_at`
    还停在最初录入的那一天 —— 于是「每天 08:30」的港城券在 `/即将` 里整个漏掉
    （真机上那张列表躺着「后天 12:00」「3天后 09:00」，却没有明天早上 08:30 那
    三条）。重复型的下一场只能靠 `repeat.occurrence_on` 逐天展开算（只看接下来
    这些天，不写库）。

    `grab_at` 是空的（没有固定开抢时刻、只能领的）不进这个列表 —— 返回类型里
    没有 `None` 这种东西，`/即将` 里那类行走它自己的「时间未定」分支。
    """
    d_from = after.date()
    out: list[tuple[datetime, dict[str, Any]]] = []
    for r in list_range(d_from, d_from + timedelta(days=_NEXT_LOOKAHEAD_DAYS)):
        when: Optional[datetime]
        if rp.rule_of(r) is not None:
            when = None
            for i in range(_NEXT_LOOKAHEAD_DAYS + 1):
                hit = rp.occurrence_on(r, d_from + timedelta(days=i))
                if hit is not None and hit > after:
                    when = hit
                    break
        else:
            when = _mins(r.get("grab_at"))
            if when is not None and when <= after:
                when = None
        if when is not None:
            out.append((when, r))
    out.sort(key=lambda p: (p[0], p[1].get("id") or 0))
    return out


def quota_suffix(r: dict[str, Any], d: Optional[date]) -> str:
    """重复活动的配额尾巴：``（本周 1/2）`` / ``（本期已领满）``；没有配额就是空串。

    「每周限领 1 次」的券这周已经领过了，还照旧列在「今天抢什么」里就是在骗人
    —— 用户跑一趟什么都领不到。而且这个数必须和日历上那张卡片**同源**
    （`repeat.quota_state`）：日历标「已完成」、机器人说「今天抢」，正是这个
    项目里最怕的那种两边各说各话。

    **主动提醒也调它**（`app/reminder.py`），所以留在模块级：提醒线程手上没有
    `Bot` 实例，而它推的那句话要是和 `/即将`、日历卡片口径不一致，用户会以为
    自己看错了行。
    """
    if d is None:
        return ""
    quota = rp.quota_of(r)
    if not quota.week and not quota.month:
        return ""
    state = rp.quota_state(r, d, done_days(r["id"]))
    if state["week"]["reached"] or state["month"]["reached"]:
        return "（本期已领满）"
    bits = [
        f"{label} {state[unit]['done']}/{state[unit]['limit']}"
        for unit, label in (("week", "本周"), ("month", "本月"))
        if state[unit]["limit"]
    ]
    return "（" + "·".join(bits) + "）" if bits else ""


class Bot:
    def __init__(self, raw_config: dict[str, Any], llm: LLM):
        self.cfg = raw_config
        self.llm = llm
        self.qcfg = dict(raw_config.get("query") or {})

    # -- 对外入口 -----------------------------------------------------

    def handle_text(self, text: str, user_id: str = "", msg_id: str = "") -> str:
        text = (text or "").strip()
        if msg_id and seen_msg(msg_id):
            log.info("重复消息，跳过: %s", msg_id)
            return ""
        try:
            reply = self._handle_text_inner(text, user_id)
        except LLMError as exc:
            log.error("模型调用失败: %s", exc)
            reply = f"⚠️ AI 调用失败：{exc}\n稍后再试，或者检查 config.json 里的模型配置。"
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让异常打断长轮询
            log.exception("处理消息出错")
            reply = f"⚠️ 处理这条消息时出错了：{type(exc).__name__}: {exc}"
        if msg_id:
            mark_msg(msg_id, user_id, "text")
        return reply

    def handle_image(
        self, image_bytes: bytes, mime: str = "image/jpeg", user_id: str = "", msg_id: str = ""
    ) -> str:
        if msg_id and seen_msg(msg_id):
            return ""
        try:
            data = self.llm.understand_image(image_bytes, mime)
            reply = self._apply_ingest(data, user_id, source="image")
        except LLMError as exc:
            log.error("图片识别失败: %s", exc)
            reply = f"⚠️ 图片识别失败：{exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("处理图片出错")
            reply = f"⚠️ 处理这张图时出错了：{type(exc).__name__}: {exc}"
        if msg_id:
            mark_msg(msg_id, user_id, "image")
        return reply

    # -- 文字处理 -----------------------------------------------------

    def _handle_text_inner(self, text: str, user_id: str) -> str:
        if not text:
            return HELP_TEXT

        cmd = text.lower().strip()
        # 「使用帮助」也要秒回：它就是发给自己的一份说明书，没有半点需要模型理解的东西。
        if cmd in ("/help", "/帮助", "help", "帮助", "使用帮助", "使用说明",
                   "说明书", "怎么用", "用法", "帮助文档"):
            return HELP_TEXT
        if cmd in ("/今天", "/today"):
            return self._format_day(tu.today())
        if cmd in ("/即将", "/upcoming"):
            return self._format_upcoming()
        if cmd in ("/统计", "/stats"):
            s = stats()
            return (
                f"📊 库里共 {s['total']} 条活动：\n"
                f"  · 待抢 {s['pending']}\n"
                f"  · 已完成 {s['done']}\n"
                f"  · 已过期 {s['expired']}"
            )
        if cmd in ("/日历", "/calendar"):
            # 2026-10-07 这条指令被删过一次：当时它在**这里自己拼**了个写死的
            # `http://<你的NAS地址>:8080/`，和真实地址对不上，留着只会骗人。
            # 2026-10-09 用户点名要它回来 —— 现在地址统一由 `settings.front_url()`
            # 算（外网域名 > 局域网地址），改口令、换域名都会自动跟着变，
            # 所以这里再拼一次就又有两个实现了，那正是当初出事的根因。
            return f"🗓 日历页面：{_front_url(self.cfg)}"

        # 有未决的确认提问，优先走确认流程
        pending = pop_pending(user_id) if user_id else None
        if pending:
            return self._resolve_pending(pending, text, user_id)

        # 陪聊关着的时候，整句寒暄就地回掉：常见寒暄用不着模型，一次调用都不花。
        # 位置在 pending 之后 —— 「好的」在确认流程里是「对，就这么记」，不能被
        # 当成寒暄吞掉。
        if not _chat_enabled() and _is_small_talk(text):
            log.info("闲聊已关，寒暄就地回掉（不调模型）：%r", text[:40])
            return CHAT_OFF_GREETING

        # 固定句式（日子 + 时刻 + 查询词）走确定性快路：不调模型，秒回。
        # 只有整句话正好是那个形状才认，多一个字就落到下面的模型路径去。
        fixed = parse_fixed_query(text)
        if fixed:
            log.info("固定句式直接查库（不调模型）：%r", text[:40])
            return self._apply_query(fixed, text)

        data = self.llm.understand(text)
        intent = str(data.get("intent") or "other").lower()
        if intent in ("other", "query"):
            # 模型没往「删除」上想时的兜底。只抢这两类意图：`ingest`/`confirm` 是建设性
            # 动作，句子里顺带一个「删」字（比如「明天9点京东抢券，把旧那条删了」）不该
            # 让整句话改道去删东西。
            kw = _fallback_keyword(text)
            if kw:
                data = {"intent": "delete", "keyword": kw}
                intent = "delete"

        if intent == "ingest":
            return self._apply_ingest(data, user_id, source="text", text=text)
        if intent == "query":
            return self._apply_query(data, text)
        if intent == "delete":
            return self._handle_delete(data, text, user_id)
        if intent == "confirm":
            return "我这边没有待确认的记录。你可以直接发活动信息，或者问我「今天抢什么」。"
        # 陪聊关着：模型自己回的那句寒暄也丢掉，统一回固定话术。
        # 用户 2026-10-07 选的这一档 ——「非活动消息一律固定话术」，回复完全可预期。
        # 走到这里说明录入/查询/删除都已经在上面返回了，这个开关碰不到它们。
        if not _chat_enabled():
            log.info("闲聊已关，回固定话术：%r", text[:40])
            return CHAT_OFF_HINT
        reply = _clean(data.get("reply"))
        if reply:
            return reply
        return self.llm.chat(text)

    # -- 录入实现 -----------------------------------------------------

    def _apply_ingest(
        self, data: dict[str, Any], user_id: str, source: str, text: str = ""
    ) -> str:
        acts = _collect_activities(data, source, text)
        if not acts:
            q = _clean(data.get("clarify_question")) or (
                "这条信息里我没读出活动内容，能再说具体点吗？"
                "比如「京东明天上午9点抢满99减50的券，到晚上12点」。"
            )
            return "🤔 " + q

        # 先按原话把日子拨正、把倒挂的有效期推顺，再去判「是不是过去了」——
        # 否则一个被模型记错日子的活动会顶着错误的时间通过检查。
        for a in _align_day(acts, text):
            log.info("开抢日子按原话修正为 %s：%s", a.get("grab_at"), a.get("title"))
        for a in _repair_end(acts):
            log.warning(
                "有效期原本早于开抢，已推到开抢当天：%s 开抢 %s / 有效期至 %s",
                a.get("title"), a.get("grab_at"), a.get("end_at"),
            )

        wrong_day, _stale_today = _classify_past(acts)
        needs_clarify = bool(data.get("needs_clarify"))
        question = _clean(data.get("clarify_question"))

        if wrong_day and not needs_clarify:
            needs_clarify = True
            first = wrong_day[0]
            question = (
                f"这条活动的开抢时刻是 {tu.human_relative(_mins(first.get('grab_at')))}，"
                f"已经过去了。你是想说下周的同一时间吗？还是我算错了？"
            )

        if needs_clarify:
            question = question or "这条活动的时间我没把握，能确认一下开抢的确切时间吗？"
            if user_id:
                set_pending(
                    user_id,
                    "confirm_ingest",
                    {"activities": acts, "source": source},
                    question,
                )
            return "🤔 " + question + "\n\n（回一句「对」我就按这个记；不对的话直接把时间再说一遍）"

        ids: list[int] = []
        pairs: list[tuple[dict[str, Any], int]] = []
        dupes: list[dict[str, Any]] = []
        for a in acts:
            clean = {k: v for k, v in a.items() if not k.startswith("_")}
            existing = find_duplicate(clean.get("title"), clean.get("grab_at"))
            if existing:
                dupes.append(existing)
                continue
            new_id = insert_activity(clean)
            if new_id:
                ids.append(new_id)
                pairs.append((clean, new_id))
        return self._receipt(pairs, dupes)

    def _receipt(
        self,
        pairs: list[tuple[dict[str, Any], int]],
        dupes: list[dict[str, Any]],
    ) -> str:
        if not pairs:
            if dupes:
                titles = "、".join(str(d.get("title")) for d in dupes[:3])
                return f"ℹ️ 这条已经记过了（{titles}），没有重复录入。"
            return "ℹ️ 没读到新的活动内容。"
        lines = [f"✅ 已记下 {len(pairs)} 条活动：", ""]
        for i, (a, aid) in enumerate(pairs, start=1):
            lines.append(f"{i}. {a['title']}  <#{aid}>")
            lines.append("   " + self._time_line(a))
            if a.get("note"):
                lines.append(f"   📝 {a['note']}")
            if a.get("url"):
                lines.append(f"   🔗 {a['url']}")
        if dupes:
            lines.append("")
            lines.append(f"（另有 {len(dupes)} 条与库里已有的重复，已跳过）")
        # 2026-10-10 用户把原来那句「⏰ 提醒一句：今天 10:00 的开抢时间已经过了。／
        # 如果你其实是想记下一次，把时间再说一遍我就改。」一并划掉了，原话是
        # 「这个红框部分的内容不要，就算时间已经过了也没关系，不用提醒，就正常录入就行了」。
        # 所以回执里**不再**有任何「已经过去」的提示 —— `_classify_past()` 照样把这类
        # 活动挑出来（它们必须**不**落进 `wrong_day` 那一路，否则又要被问「是不是下周」），
        # 只是挑出来之后没人在回执里吭声了。
        lines.append("")
        lines.append("问一句「今天抢什么」就能查。")
        # 2026-10-10 用户要求：「每次通过 clawbot 录入活动以后（**只有这个情况下**），
        # 回复内容里应该附带日历地址，这样每次录完都能方便去前端检查」。位置照他给的
        # 样例：就在「问一句」这句之后 —— 末尾那句一言是 `wechat.py` 贴的，自然排后面。
        # 地址**只走 `settings.front_url()`**（跟 `/日历` 指令、启动日志同一个实现），
        # 在这里另拼一个就又多了一个迟早会过期的地址。
        # 「没读到活动」和「已经记过了」两条路**不带** —— 那两条没录进去东西，
        # 用户要的是「录完去核对」。
        lines.append("")
        lines.append(f"📅 日历页面：{_front_url(self.cfg)}")
        return "\n".join(lines)

    def _time_line(self, a: dict[str, Any]) -> str:
        bits: list[str] = []
        if a.get("platform"):
            bits.append(str(a["platform"]))
        if a.get("category"):
            bits.append(str(a["category"]))
        g = _mins(a.get("grab_at"))
        rep = rp.describe(a)
        if rep:
            # 重复型活动：说「每天 09:00」比说「下一次 10月7日 09:00」有用得多，
            # 但下一次也得给出来 —— 用户要靠它知道什么时候动手。
            bits.append(rep + (f"（下一次 {tu.human_relative(g)}）" if g else ""))
        elif g:
            bits.append(f"🎯 {tu.human_relative(g)} 开抢")
        start = _mins(a.get("start_at"))
        if start:
            bits.append(f"开始 {tu.human(start)}")
        end = _mins(a.get("end_at"))
        if end:
            bits.append(f"有效期至 {tu.human(end)}")
        if not g and not start and not end and not rep:
            bits.append("⏰ 时间未定")
        return " · ".join(bits)

    def _resolve_pending(self, pending: dict[str, Any], text: str, user_id: str) -> str:
        payload = pending.get("payload") or {}
        if (pending.get("kind") or payload.get("kind")) == "delete":
            return self._resolve_delete(pending, text, user_id)
        question = pending.get("question") or ""
        try:
            verdict = self.llm.confirm(question, payload, text)
        except LLMError as exc:
            log.error("确认判定失败: %s", exc)
            return "🤔 我没听清，能再说一次「对」或者把时间重新说一遍吗？"

        confirmed = bool(verdict.get("confirmed"))
        acts = _collect_activities(verdict, "text", text) if verdict.get("activities") else []

        if not confirmed and not acts:
            return (
                "👌 那这条先不记了。你把时间重新说一遍就行，比如"
                "「今天上午9点半，京东超市满99减50」"
            )

        if not acts:
            # 还原用户最初那句话，让重复规则能重新从原话里认出来 —— 不然
            # 「每天9点」这条信息在确认这一步就丢了。
            acts = [
                _normalize_activity(
                    a, payload.get("source") or "text", _clean(a.get("raw_text")) or ""
                )
                for a in (payload.get("activities") or [])
            ]
        else:
            # 模型被问的只是「对不对」，很可能把「每天」吞掉不回声。规则是用户说
            # 的，不该在这一步凭空消失 —— 按标题把原来记下的规则补回来。
            old = {a.get("title"): a for a in (payload.get("activities") or [])}
            for a in acts:
                if a.get("repeat_kind"):
                    continue
                src = old.get(a.get("title")) or {}
                for key in rp.FIELDS:
                    if src.get(key):
                        a[key] = src[key]

        if not acts:
            return "👌 已取消。"

        # 用户确认了「过去」的时间，那就照他说的记。
        # 但倒挂的有效期仍然要推顺 —— 不管从哪条路进来，end_at 早于 grab_at
        # 的活动一入库就会被 refresh_expired() 判成「已过期」。
        for a in _repair_end(acts):
            log.warning(
                "有效期原本早于开抢，已推到开抢当天：%s 开抢 %s / 有效期至 %s",
                a.get("title"), a.get("grab_at"), a.get("end_at"),
            )

        pairs: list[tuple[dict[str, Any], int]] = []
        dupes: list[dict[str, Any]] = []
        for a in acts:
            clean = {k: v for k, v in a.items() if not k.startswith("_")}
            existing = find_duplicate(clean.get("title"), clean.get("grab_at"))
            if existing:
                dupes.append(existing)
                continue
            new_id = insert_activity(clean)
            if new_id:
                pairs.append((clean, new_id))
        clear_pending(user_id)
        return self._receipt(pairs, dupes)

    # -- 删除实现 -----------------------------------------------------

    def _handle_delete(self, data: dict[str, Any], text: str, user_id: str) -> str:
        """用户说要删东西：先列出来，等他回编号。**这一步绝不删任何东西。**"""
        kw = _clean_keyword(data.get("keyword")) or _fallback_keyword(text)
        hits = search(kw, limit=10)
        if not hits:
            tip = f"带「{kw}」的" if kw else "任何"
            return (
                f"🔍 没找到{tip}活动。\n"
                "换个词试试（标题、平台、备注里任一处匹配都算），"
                "或者问我「今天抢什么」看看库里有什么。"
            )

        total = stats().get("total", len(hits))
        lines = [f"🗑 找到 {len(hits)} 条{'带「' + kw + '」的' if kw else ''}活动：", ""]
        for i, r in enumerate(hits, start=1):
            lines.append(f"{i}. {r.get('title')}  <#{r.get('id')}>")
            lines.append("   " + self._time_line(r))
        lines.append("")
        lines.append("回复编号就删（比如「1」，或「1 2」一起删）；回复「都删」全删，回复「取消」不删。")
        if total > len(hits):
            lines.append(f"（库里还有 {total - len(hits)} 条没列出来，说个更准的词再删）")

        if not user_id:
            # 没有 user_id 就没地方存这次待确认（比如直接被别的程序调用）。
            # 这时候宁可什么也不做：**猜一个「大概是要全删」是不可接受的**。
            return "\n".join(lines) + "\n\n（这次没法往下走：没有会话标识，我记不住你的选择。）"

        set_pending(
            user_id,
            "delete",
            {
                "kind": "delete",
                "keyword": kw,
                "ids": [r.get("id") for r in hits],
                "titles": [str(r.get("title")) for r in hits],
            },
            f"删掉哪几条（带「{kw}」的那些）？",
        )
        return "\n".join(lines)

    def _resolve_delete(self, pending: dict[str, Any], text: str, user_id: str) -> str:
        """处理「回编号确认删除」这一步 —— 这是唯一真正落刀的地方。"""
        payload = pending.get("payload") or {}
        ids = [int(i) for i in (payload.get("ids") or []) if i is not None]
        titles = [str(t) for t in (payload.get("titles") or [])]
        if not ids:
            return "👌 那几条已经不在了，不用删。"

        t = (text or "").strip()
        if _looks_like_cancel(t):
            return "👌 那就不删了，一条都没动。"
        if _looks_like_all(t):
            picked = list(range(1, len(ids) + 1))
        else:
            picked = _pick_indices(t, len(ids))

        if not picked:
            # 认不出来就把选择权还给用户，而不是自己猜。pending 也得存回去 ——
            # 它刚被 `pop_pending` 拿走了，不还回去用户下一句「1」就没人接了。
            if user_id:
                set_pending(user_id, "delete", payload, pending.get("question") or "")
            return (
                "🤔 我没听清你要删哪条。回复编号（比如「1」或「1 2」）就删；"
                "「都删」全删；「取消」不删。"
            )

        done: list[str] = []
        for i in picked:
            aid = ids[i - 1]
            row = get(aid) or {}
            if delete_activity(aid):
                done.append(str(row.get("title") or (titles[i - 1] if i <= len(titles) else aid)))
        if not done:
            return "ℹ️ 这几条已经不在库里了（可能刚被删过）。"

        lines = [f"🗑 已删掉 {len(done)} 条："]
        lines.extend(f"   {title}" for title in done)
        lines.append(f"库里还剩 {stats().get('total', 0)} 条。问一句「今天抢什么」就能查。")
        return "\n".join(lines)

    # -- 查询实现 -----------------------------------------------------

    def _apply_query(self, data: dict[str, Any], text: str = "") -> str:
        refresh_expired()

        d_from = _parse_date(data.get("date_from"))
        d_to = _parse_date(data.get("date_to"))
        single = _parse_date(data.get("date"))
        time_parts = _parse_time(data.get("time"))
        keyword = _clean(data.get("keyword")) or ""

        if single and not d_from:
            d_from = d_to = single
        if d_from and not d_to:
            d_to = d_from
        if d_to and not d_from:
            d_from = d_to
        if not d_from and not d_to:
            # 模型一个日期都没给 —— 别默默退回今天（那是在给错答案），
            # 先用确定性规则从原话里抠一次，抠不到才认命落到今天。
            guess = _fallback_day(text)
            if guess:
                log.info("模型没给日期，兜底解出 %s（原话：%r）", guess, text[:40])
            d_from = d_to = guess or tu.today()
        if d_from and d_to and d_from == d_to:
            # 模型给了日期，但原话里有个无歧义的日子词，两者对不上 —— 以原话为准。
            # 只在模型给的是**单独一天**时生效，「从明天到周五」这种区间不碰。
            explicit = _explicit_day(text)
            if explicit and explicit != d_from:
                log.warning(
                    "模型解出的日期 %s 与原话不符，按原话改用 %s（原话：%r）",
                    d_from, explicit, text[:40],
                )
                d_from = d_to = explicit
        if d_from and d_to and d_from > d_to:
            d_from, d_to = d_to, d_from

        assert d_from is not None and d_to is not None

        if time_parts:
            return self._report_minute(d_from, d_to, time_parts, keyword, data)
        return self._report_range(d_from, d_to, keyword, data)

    def _filter_keyword(self, rows: list[dict], keyword: str) -> list[dict]:
        if not keyword:
            return rows
        k = keyword.lower()
        out = []
        for r in rows:
            hay = " ".join(
                str(r.get(f) or "") for f in ("title", "platform", "category", "note")
            ).lower()
            if k in hay:
                out.append(r)
        return out

    # 精确到分钟的报告
    def _report_minute(
        self,
        d_from: date,
        d_to: date,
        time_parts: tuple[int, int],
        keyword: str,
        data: dict[str, Any],
    ) -> str:
        hh, mm = time_parts
        clock = f"{hh:02d}:{mm:02d}"

        if d_from != d_to:
            # 「这周9点半」这类跨天查询：逐天严格匹配
            hits: list[dict] = []
            day = d_from
            while day <= d_to:
                hits.extend(self._rows_opening_at(day, hh, mm))
                day += timedelta(days=1)
            hits = self._filter_keyword(hits, keyword)
            if not hits:
                return (
                    f"📭 {tu.human(datetime(d_from.year, d_from.month, d_from.day))}"
                    f" ~ {tu.human(datetime(d_to.year, d_to.month, d_to.day))} 之间，"
                    f"没有正好 {bold_time(clock)} 开抢的活动。"
                )
            return self._render_hits(hits, f"{bold_time(clock)} 开抢", keyword)

        hits = self._filter_keyword(self._rows_opening_at(d_from, hh, mm), keyword)

        blocks: list[str] = []

        if hits:
            blocks.append(self._render_hits(hits, f"{bold_time(clock)} 开抢", keyword))
        else:
            head = f"📭 {tu.human_day(d_from)} {bold_time(clock)} 没有开抢安排。"
            if keyword:
                head += f"（筛选词：{keyword}）"
            blocks.append(head)

        # 附加分区（唯一保留的一节）：用户 2026-10-07 点名要的那一种补充 ——
        # 「今天还有哪些**没开始**的」。只列晚于他问的那一刻、也晚于此刻的：
        # 「抢券」抢的是第一分钟，问 11:00 却把 9 点、10 点那些抖出来对他没用，
        # 已经开抢过去的、只能领不能抢的，一律不带（他原话：「不要带出今天不能抢的
        # 活动」）。真机截图上这一节以前是「还能领的」8 条长标题 + 当天全部开抢安排，
        # 整屏都是它，而用户只是问「9 点半有什么」。
        floor = hh * 60 + mm
        if d_from == tu.today():
            now = tu.now()
            floor = max(floor, now.hour * 60 + now.minute)
        later = [
            r
            for r in self._filter_keyword(list_range(d_from, d_from), keyword)
            if self._opens_at(r, d_from) is not None and self._opens_at(r, d_from) > floor
        ]
        later.sort(key=lambda r: self._opens_at(r, d_from) or 0)
        if later:
            limit = int(self.qcfg.get("later_limit", self.qcfg.get("ongoing_limit", 5)) or 5)
            lines = [f"📌 {tu.human_day(d_from)}还没开始的 {len(later)} 条："]
            lines.extend(self._list_lines(later[:limit], d_from))
            if len(later) > limit:
                lines.append(f"   还有 {len(later) - limit} 条，去日历页面看。")
            blocks.append("\n".join(lines))

        # 这一天连还没开始的都没有 —— **就到这里**。
        #
        # 以前这里还会补一句「🔎 供参照，接下来最近的」把后面几天的活动抖出来。
        # 用户 2026-10-07 的原话：「我问今天的活动，为什么要把后面的给带出来，
        # 没有就结束就行了啊」。问哪一天就答哪一天，别拿别的日子凑数。
        return "\n\n".join(blocks)

    # 按天/范围报告
    def _report_range(
        self, d_from: date, d_to: date, keyword: str, data: dict[str, Any]
    ) -> str:
        rows = self._filter_keyword(list_range(d_from, d_to), keyword)
        if not rows:
            hint = _clean(data.get("range_hint"))
            label = hint or (
                tu.human_day(d_from)
                if d_from == d_to
                else f"{d_from.month}月{d_from.day}日 ~ {d_to.month}月{d_to.day}日"
            )
            # 同上：没有就是没有，后面几天的活动不在这里兜底（`/即将` 才管往后看）。
            return f"📭 {label}没有活动记录。"

        if d_from == d_to:
            return self._render_day(d_from, rows)

        # 多天：按天分组。每天都「还能领」的长效券不按天重复列（问一周会看到同一条
        # 刷七遍），改成末尾统一说一次。
        opening_rows = [r for r in rows if any(self._opens_on(r, day) for day in _days_between(d_from, d_to))]
        long_running = [r for r in rows if r not in opening_rows]

        lines: list[str] = []
        label = f"{d_from.month}月{d_from.day}日 ~ {d_to.month}月{d_to.day}日"
        lines.append(f"🗓 {label} 共 {len(rows)} 条：")
        for day in _days_between(d_from, d_to):
            day_rows = [r for r in opening_rows if self._opens_on(r, day)]
            if day_rows:
                lines.append(f"── {tu.human_day(day)} ──")
                lines.extend(self._list_lines(day_rows, day))
        if long_running and self.qcfg.get("show_ongoing", True):
            lines.append("")
            lines.append(f"📌 另有 {len(long_running)} 条整个区间内都在有效期内：")
            for r in long_running:
                lines.append(self._ongoing_line(r))
        return "\n".join(lines)

    def _opens_at(self, r: dict[str, Any], d: date) -> Optional[int]:
        """这一天它开抢的**那一刻**（分钟数）；不是这天开抢就 None。

        重复型取 `repeat.occurrence_on` 展开出来的那一次，其余取 `grab_at`（再退
        `start_at`）。`_row_when` 只关心时刻，这里连「是不是这一天」一起答，
        精确到分钟的查询要靠它 —— 见 `_rows_opening_at` 的说明。
        """
        once = rp.occurrence_on(r, d)
        if once is not None:
            return once.hour * 60 + once.minute
        if not self._opens_on(r, d):
            return None
        g = _mins(r.get("grab_at")) or _mins(r.get("start_at"))
        return (g.hour * 60 + g.minute) if g else None

    def _rows_opening_at(self, d: date, hh: int, mm: int) -> list[dict]:
        """这一天正好 ``hh:mm`` 开抢的，**含重复型活动展开出来的那一次**。

        `grab_in_window()` 是在 SQL 里拿 `grab_at` / `start_at` 跟窗口比的，而
        「每天 08:30」这种在库里只有一行、日期还停在最初录入的那天 —— 于是
        「明天 8:30 抢什么」说不出来，可同一句「明天抢什么」却明明白白列着
        08:30（真机上就是这么自相矛盾的）。这里按天取一遍、拿展开后的时刻对齐。
        """
        target = hh * 60 + mm
        hits = list(grab_in_window(*tu.minute_window(d, hh, mm, 1)))
        seen = {r["id"] for r in hits}
        for r in list_range(d, d):
            if r["id"] in seen:
                continue
            if self._opens_at(r, d) == target:
                hits.append(r)
        return hits

    def _render_day(self, d: date, rows: list[dict[str, Any]]) -> str:
        """一天的清单。开抢的和「还能领的」必须分开说。

        混在一起的话，「今天抢什么」会拿明天的活动来回答今天 —— 行首的日期就是
        证据，可用户第一眼只看到「共 N 条」。
        """
        label = tu.human_day(d)
        opening = [r for r in rows if self._opens_on(r, d)]
        claimable = [r for r in rows if self._claimable_on(r, d)]

        lines: list[str] = []
        if opening:
            lines.append(f"🗓 {label} 共 {len(opening)} 条开抢：")
            lines.extend(self._list_lines(opening, d))
        else:
            lines.append(f"📭 {label} 没有开抢安排。")
        if claimable and self.qcfg.get("show_ongoing", True):
            lines.append("")
            lines.append(f"📌 还能领的 {len(claimable)} 条（不是这天开抢）：")
            for r in claimable:
                lines.append(self._ongoing_line(r))
        return "\n".join(lines)

    def _render_hits(self, rows: list[dict], label: str, keyword: str) -> str:
        """精确到分钟的命中清单 —— 同样只给标题。

        时刻已经写在标题行（`label`）里了，每条再重复一遍只是噪音。
        """
        suffix = f"（筛选：{keyword}）" if keyword else ""
        lines = [f"🎯 {label} 共 {len(rows)} 条{suffix}"]
        for r in rows:
            lines.append(f"   {r['title']}")
        return "\n".join(lines)

    def _ongoing_line(self, r: dict[str, Any]) -> str:
        """「还能领的」一条：只给标题和**什么时候截止**。

        截止日是这一节唯一的干货 —— 去掉它就只剩一个标题，反而没用。
        """
        end = _mins(r.get("end_at"))
        tail = f"（{tu.human(end, with_weekday=False)} 截止）" if end else ""
        return f"   {r['title']}{tail}"

    def _row_when(self, r: dict[str, Any], day: Optional[date] = None) -> str:
        """清单里那一刻的 `HH:MM`。

        ``day`` 给出来时，重复型活动显示的是**那一天**的时刻；没有 ``day`` 就退回
        ``grab_at``（「接下来要抢的」那种列表要它）。
        """
        g = _mins(r.get("grab_at"))
        if day is not None:
            once = rp.occurrence_on(r, day)
            if once is not None:
                g = once
        return g.strftime("%H:%M") if g else "??:??"

    def _quota_suffix(self, r: dict[str, Any], d: Optional[date]) -> str:
        """重复活动的配额尾巴。实现搬到模块级的 `quota_suffix()` 了 —— 主动提醒
        也要用它，而那时候手上没有 `Bot` 实例。这里留一层，是为了不动老调用点。"""
        return quota_suffix(r, d)

    def _list_lines(self, rows: list[dict], day: Optional[date] = None) -> list[str]:
        """清单主体：按时刻分组，同一个时刻只写一遍（加粗），下面摆它的标题。

        排序也在这里做 —— `list_range` 是按 `grab_at` 排的，而重复型那一行的
        `grab_at` 还停在最初录入那天，照它排会把不同时刻交错在一起，同一个 08:30
        就会分成两组各写一遍。没时刻的（`??:??`）排在最后。

        排序**只按时刻**、且是稳定排序：同一个时刻里的几条保持库里的先后
        （用户录入的顺序），不按标题重排 —— 标题排序看着像随机。
        """
        pairs = [(self._row_when(r, day), r["title"] + self._quota_suffix(r, day)) for r in rows]
        pairs.sort(key=lambda p: (1, "") if p[0] == "??:??" else (0, p[0]))
        return _time_group_lines(pairs)

    @staticmethod
    def _opens_on(r: dict[str, Any], d: date) -> bool:
        """这一天是它的「开抢/开始」日吗？

        只有这个才算「今天抢什么」的答案。**光「还在有效期内」不算** —— 一张
        有效期到年底的券在每一天都「还能领」，把它列进「今天抢什么」会让人以为
        今天有得抢，然后看见行首写着明天的日期。（用户库里真有两张这样的券。）

        重复型（每天/每周）走 `repeat.occurrence_on`：一张「每天 09:00」的券在
        每一天都算开抢日，这正是它和「一张长效一次性券」的区别所在。

        写成 staticmethod 是给日历用的：`web` 里那条 `/api/activities` 也要摆
        「这一天还能领的」，它必须和机器人用**同一份**判断（见文件末尾的
        `opens_on` / `claimable_on`）。
        """
        if rp.rule_of(r) is not None:
            return rp.occurrence_on(r, d) is not None
        g = _mins(r.get("grab_at"))
        if g and g.date() == d:
            return True
        start = _mins(r.get("start_at"))
        return bool(start and start.date() == d)

    @staticmethod
    def _claimable_on(r: dict[str, Any], d: date) -> bool:
        """这一天它还在有效期内吗（不含「就是这天开始」的那些）。"""
        if rp.rule_of(r) is not None:
            # 重复型只在自己那几天能领：一张「每周五」的券，周六不该说「还能领」。
            # 该它上的日子已经进主清单了，这里一律不重复。
            return False
        if Bot._opens_on(r, d):
            return False
        start = _mins(r.get("start_at"))
        end = _mins(r.get("end_at"))
        day_start = datetime(d.year, d.month, d.day)
        day_end = day_start + timedelta(days=1)
        if start and end:
            return start < day_end and end >= day_start
        if end and not start:
            # 没有开始时间，唯一能证明「它已经开始」的就是开抢时刻已经过去。
            # 少了这一句，「后天 10:00 开抢、后天 22:00 截止」的券会在今天被说成
            # 「还能领的」—— 用户今天根本领不了（真踩到：日历那一天多摆了一张卡）。
            g = _mins(r.get("grab_at"))
            if g and g >= day_end:
                return False
            return end >= day_start
        return False

    def _belongs_to_day(self, r: dict[str, Any], d: date) -> bool:
        """这一天的分组里该不该有它（开抢的 + 还能领的）。"""
        return self._opens_on(r, d) or self._claimable_on(r, d)

    # -- 指令用 -------------------------------------------------------

    def _format_day(self, d: date) -> str:
        refresh_expired()
        rows = list_range(d, d)
        if not rows:
            return f"📭 {tu.human_day(d)}没有活动记录。"
        return self._render_day(d, rows)

    def _format_upcoming(self) -> str:
        refresh_expired()
        openings = _next_openings(tu.now())[:10]
        pairs: list[tuple[str, str]] = [
            (tu.human_relative(when), r["title"]) for when, r in openings
        ]
        # 剩下那些没能算出「下一场」的行（没有开抢时刻、只是还在有效期内；或者
        # 远得超出向前找的窗口）照旧接在后头 —— 以前它们和准点的混在
        # `upcoming()` 里，现在准点的走上面那条按**真实时刻**算的路，但这类行
        # 不能因此消失：丢下它们，用户会以为这些券没记上。
        listed = {r.get("id") for _when, r in openings}
        for r in upcoming(50):
            if r.get("id") in listed:
                continue
            when = _mins(r.get("grab_at"))
            pairs.append((tu.human_relative(when) if when else "时间未定", r["title"]))
        if not pairs:
            return "📭 接下来没有待抢的活动。发一条活动信息给我就能记上。"
        lines = ["⏭ 接下来要抢的："]
        lines.extend(_time_group_lines(pairs))
        return "\n".join(lines)


# -- 给日历用的同一对判据 ------------------------------------------------
#
# `web` 里摆日历要回答同一个问题：「这一天它算开抢，还是只是还能领？」这两个函数
# 就是 `Bot` 用的那两个，**不是复制品** —— 日历和机器人各写一套日子判据，正是之前
# 「机器人说明天、日历标今天」那个 bug 的成因。
opens_on = Bot._opens_on
claimable_on = Bot._claimable_on

#: 「这一条下一次几点开抢」。`/即将` 和 `app/reminder.py`（开抢前主动提醒）都用它 ——
#: **同一个函数，不是复制品**。提醒要是自己另算一套时刻，就会出现「机器人说 09:00、
#: 提醒在 08:50 响、可开抢其实是 09:30」这种自相矛盾，而重复活动（每天/每周/每月）
#: 恰恰是最需要逐天展开、最容易算错的那一类。
next_openings = _next_openings
