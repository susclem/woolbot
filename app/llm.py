"""AI 模型接入层。

设计原则（整个项目的成败点）：

    **LLM 只做「自然语言 ⇄ 结构化」的翻译，绝不决定库里有什么。**

  · 录入方向：自然语言 → 结构化字段。模型只负责抽取，拿不准就标记澄清。
  · 查询方向：自然语言 → 查询条件。检索交给 SQL，模型不参与。
  · 输出方向：查询结果由 Python 确定性排版，不让模型复述，杜绝幻觉。

模型完全可自定义：任何 OpenAI 兼容的 ``/chat/completions`` 端点都行，
``base_url`` / ``model`` / ``vision_model`` / ``api_key`` 都在 config.json 里。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Optional

import requests

from . import timeutil as tu

log = logging.getLogger("woolbot.llm")


class LLMError(RuntimeError):
    """模型层出错。上层只会把它转成一句给用户看的话。"""


class NoKeyError(LLMError):
    """没配 API key。单独一类是为了**不重试** —— 重试 3 次只会白等 7 秒。"""


NO_KEY_HINT = (
    "还没配 API key，所以我读不懂消息。最省事的办法：打开日历页面 → 右上角「设置」→ "
    "填入 API key → 保存（存了立刻生效，不用重启）。"
)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是「羊毛机器人」的理解引擎，服务于一个个人抢购助手。

用户每天会发来各种限时优惠、秒杀、领券、签到活动的信息，也会问「什么时候抢什么」。
你的唯一职责是把用户的自然语言翻译成结构化数据 —— **你不是检索器，不要回答数据库里有什么**。

严格只输出一个 JSON 对象，不要输出任何解释、markdown 代码块或多余文字。"""

UNDERSTAND_PROMPT = """{anchor}

【第一步】判断用户这句话的意图 intent，只能取以下值之一：
- "ingest"  用户在投喂活动信息（可能是一条，也可能是多条）
- "query"   用户在问有没有活动、什么时候抢什么
- "confirm" 用户在对上一条确认提问做回复（「对」「嗯」「是」「不对」「错了」）
- "delete"  用户想删掉已经记过的活动，例如「删除梦享」「把临沂那个删了」「港城欢乐购不要了」
- "other"   寒暄、闲聊、问机器人能力、或者其他都不属于的情况

【第二步】按意图填写对应字段。

═══ 当 intent = "ingest" 时 ═══

抽取活动信息。一条消息里有多条活动时，全部放进 activities 数组，不要合并成一条。
每条活动的字段：
- title      字符串。精简、可辨认，保留「平台+商品/品类+优惠力度」这类关键信息。不要照抄整段话。
- platform   字符串或 null。京东 / 淘宝 / 天猫 / 美团 / 拼多多 / 抖音 等。
- category   字符串或 null。数码 / 日用 / 食品 / 美妆 / 服饰 等。
- grab_at    字符串或 null。**开抢时刻**，也就是用户要动手的那一分钟，格式 "YYYY-MM-DDTHH:MM"。
             这是最重要的字段，必须精确到分钟。
- start_at   字符串或 null。活动开始时间。
- end_at     字符串或 null。活动结束时间 / 有效期截止。
- url        字符串或 null。活动链接。
- note       字符串或 null。其他值得记住的补充（门槛、限量、是否会员等）。
- repeat     字符串或 null。**只有**活动明确是「每天/每日/每周X/每月X号」反复发生时才填，
             写成人话，例如 "每天 09:00"、"每天 08:30-22:00"、"每周五 12:00"、
             "每周二、四 10:00"、"每月1日、15日 10:00"。多个日子就一次写全，
             别只挑一个。原文写「每自然月10号」也是「每月10号」，别因为多了两个字就不填。
             可领窗口跨了好几天时（「每自然月10号10:00-20号23:59:59」），
             repeat 只写**开始那天和开始时刻**：repeat="每月10号 10:00"。
             一次性活动、或者你拿不准，一律填 null。**不要**把
             "10月7日 08:30-22:00" 这种某一天的时段写成 repeat，
             也**不要**把「每周限领1次」「每月最多3张」这种**次数限制**写进 repeat ——
             那是配额，不是周期规则。
- confidence 0~1 的小数，你对这条抽取的把握。

时间规则（务必逐条遵守）：
1. 上面已经给你「当前时间」「今天周几」「明天是几号」，**所有相对时间都必须基于它换算**。
   「明天9点」→ 今天+1天 的 09:00；「周二上午9点半」→ 先确定是哪一天的那个周二，再填 09:30。
2. 区分三个时间：grab_at 是「你去抢的那一刻」；start_at/end_at 是「活动本身的有效期」。
   「9点开抢、持续到10点」→ grab_at=09:00、end_at=10:00。
   「活动到月底」→ 只填 end_at，grab_at 填 null。
3. 如果文字里**没有明确的时刻**（比如「今晚」「最近」「这两天」），grab_at 一律填 null，
   并把 needs_clarify 设为 true、clarify_question 写出你想问的话。**绝对不要瞎猜一个时刻。**
4. 如果换算出来的时间**已经早于当前时间**，很可能是用户指的下一周/下个月，请把 needs_clarify 设为 true
   并在 clarify_question 里问清楚，不要默默按过去时间入库。
5. 重复活动：备注里写着「每日 09:00:00 起发放」「领券时间：每天 08:30:00-22:00:00」
   「每周五中午 12:00 开始发放」这类**周期性**规则时，除了照常填 grab_at（填**下一次**
   发生的时刻，用户要靠它知道什么时候动手），还要在 repeat 里写清规则。
   「每日 08:30-22:00 领券」→ repeat="每天 08:30-22:00"、grab_at=明天的 08:30。
   「每周二、周四 10 点」→ repeat="每周二、四 10:00"；「每月 1 号、15 号」→
   repeat="每月1日、15日 10:00"；「领券时间为每自然月10号10:00-20号23:59:59」→
   repeat="每月10号 10:00"（跨天的可领窗口只写开始那天和开始时刻，别写成一天内的时段）。
   规则在原文里怎么写就怎么抄，**不要**自己算成某一天。
   只发生一次的活动 repeat 填 null。
   原文里的「单用户单周限领1张」「每人每月最多3次」是**次数限制**，别往 repeat 里塞，
   也不影响 grab_at —— 它只说明这一期能领几次。

═══ 当 intent = "query" 时 ═══

把问题翻译成查询条件。**只输出条件，不要输出任何活动内容。**
- date        字符串或 null。"YYYY-MM-DD"。用户问某一天时填这里。
- date_from   字符串或 null。范围查询的起始日。
- date_to     字符串或 null。范围查询的结束日。
- time        字符串或 null。"HH:MM"，**精确到分钟**。「9点」→ "09:00"，「9点半」→ "09:30"。
              用户没指定具体时刻就填 null。
- keyword     字符串。从问题里提取的商品/平台/品类关键词，没有就空字符串。
- range_hint  字符串或 null。用户用的原始时间说法（如「今天」「这周」），仅用于回执里复述。
「今天」「明天」「周二」「本周三」等一律换算成具体日期，不要原样保留。

═══ 当 intent = "confirm" 时 ═══
- confirmed   true 表示用户确认无误，false 表示用户否认或要求修改。

═══ 当 intent = "delete" 时 ═══
- keyword     字符串。**最能定位那一条活动的一个词**，从标题、平台、备注里挑。
              用户说「删除梦享」→ "梦享"；「把临沂那个删了」→ "临沂"。
              不要填「活动」「券」「优惠」这种所有条目都有的词；用户没给任何线索就填空字符串。
- ⚠️ 你**没有删除能力**，只负责把关键词抽出来。真正删哪几条由程序列给用户、
              由用户回编号确认。所以：绝不要自己判断「应该删掉第几条」，也不要输出活动列表。

═══ 当 intent = "other" 时 ═══
- reply       字符串。直接用中文简短回答用户。你没有任何活动数据，被问到具体活动时如实说不知道。

═══ 输出格式 ═══
只输出这样一个 JSON 对象（不需要的字段给 null 或省略）：

{
  "intent": "ingest",
  "activities": [
    {
      "title": "京东超市 满99减50 领券",
      "platform": "京东",
      "category": "日用",
      "grab_at": "2026-10-07T09:00",
      "start_at": null,
      "end_at": "2026-10-07T23:59",
      "url": null,
      "note": "限会员",
      "repeat": null,
      "confidence": 0.9
    }
  ],
  "needs_clarify": false,
  "clarify_question": ""
}

query 时改成：
{
  "intent": "query",
  "date": "2026-10-07",
  "date_from": null,
  "date_to": null,
  "time": "09:30",
  "keyword": "",
  "range_hint": "今天"
}

删除时改成：
{
  "intent": "delete",
  "keyword": "梦享"
}

【用户消息】
"""

CONFIRM_PROMPT = """{anchor}

用户在回复你上一条确认提问：「{question}」

你当时准备记录的内容是：
{payload}

请判断用户是确认（confirmed=true）还是否认/要改（confirmed=false）。
用户说「对」「嗯」「是」「可以」「没错」即为确认；说「不对」「错」「不是」「改一下」即为否认。
如果用户在确认的同时补充或修正了信息（例如「对，但是10点」），也请把修正后的完整活动信息重新填进 activities。
只输出 JSON：
{"confirmed": true, "activities": [], "needs_clarify": false, "clarify_question": ""}
"""

IMAGE_PROMPT = """{anchor}

用户发来了一张活动截图。请识别图中的活动信息，抽取成结构化字段。

你要重点看三样东西：
1. **抢的时间** —— 开抢时刻（grab_at）、活动开始（start_at）、活动结束/有效期止（end_at）
2. **活动标题** —— 用「平台 + 商品/品类 + 优惠」概括，别把图里所有字都抄下来
3. **活动有效期** —— 什么时候结束

字段与规则：
- title      字符串。精简可辨认的标题。
- platform   字符串或 null。从截图的水印、logo、文案判断（京东/淘宝/天猫/美团/拼多多/抖音等）。
- category   字符串或 null。
- grab_at    "YYYY-MM-DDTHH:MM" 或 null。**图上没有日期时，按上面的「当前时间」推断**，
             并降低 confidence；完全推不出来就填 null 且 needs_clarify=true。
- start_at   "YYYY-MM-DDTHH:MM" 或 null。
- end_at     "YYYY-MM-DDTHH:MM" 或 null。
- url        字符串或 null。图上有链接或二维码旁的文字才填。
- note       字符串或 null。门槛、限量、会员要求等。
- repeat     字符串或 null。图上明确写着「每天/每日/每周X/每月X号」这类周期规则时才填，
             例如 "每天 09:00"、"每天 08:30-22:00"、"每周五 12:00"、"每周二、四 10:00"、
             "每月1日、15日 10:00"；图上写「每自然月10号」也是「每月10号」。
             可领窗口跨了好几天时（「每自然月10号10:00-20号23:59:59」），
             repeat 只写**开始那天和开始时刻**："每月10号 10:00"。
             否则 null。「每周限领1次」这类次数限制**不要**填这里。
- confidence 0~1。
- raw_ocr    字符串。把图中与活动相关的原始文字按顺序抄一遍，方便用户核对。

一张图里有多场活动时，全部放进 activities 数组。

只输出 JSON：
{
  "intent": "ingest",
  "activities": [
    {
      "title": "...", "platform": null, "category": null,
      "grab_at": null, "start_at": null, "end_at": null,
      "url": null, "note": null, "repeat": null,
      "confidence": 0.6, "raw_ocr": "..."
    }
  ],
  "needs_clarify": false,
  "clarify_question": ""
}
"""


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


_FIELD_RE = re.compile(r"\{([a-z_]+)\}")


def _fill(template: str, **values: str) -> str:
    """把模板里的 ``{anchor}`` 之类换成实际值。

    **刻意不用 ``str.format()``**：这些提示词里嵌着 JSON 示例，而 format 会把
    示例里的大括号当成格式字段。``UNDERSTAND_PROMPT`` 就这么炸过一次 ——
    ``KeyError: '\\n  "intent"'``，`understand()` 每次调用都抛异常，整条
    录入/查询链路全废，而桩掉 LLM 的单元测试完全看不见。

    所以这里：单趟正则替换、只认 ``[a-z_]+`` 这种字段名、认不出的原样留着
    （JSON 的 ``{`` 后面跟着引号，不会命中）。
    """

    def sub(match: "re.Match[str]") -> str:
        return values.get(match.group(1), match.group(0))

    return _FIELD_RE.sub(sub, template)


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。容忍 ```json 围栏和前后废话。"""
    if not text:
        raise LLMError("模型返回空内容")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # 退一步：取第一个 { 到最后一个 } 之间
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end > start:
        candidate = s[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # 常见毛病：尾随逗号
            cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise LLMError(f"模型输出不是合法 JSON: {exc}\n原文片段: {s[:400]}") from exc
    raise LLMError(f"模型输出里找不到 JSON：{s[:400]}")


#: 页面上会报红的失败类型 —— 都是用户自己能修的。
#: 限流和 5xx 是对方的问题，报了只会让人白担心，所以只在微信里说一句。
PAGE_ALERT_KINDS = ("auth", "notfound", "model")


def _explain_http(status: int, body: str, *, base_url: str, model: str) -> tuple[str, str]:
    """把模型接口的 HTTP 错误码翻译成人话。

    返回 ``(kind, message)``：kind 给机器判断（要不要在页面上报红），message 给人看。
    原来这里是 ``f"模型接口 HTTP {status}: {resp.text[:400]}"`` 直接甩给用户 ——
    微信里收到一坨 DeepSeek 的英文报错，谁也不知道该干什么。
    """
    detail = (body or "").strip().replace("\n", " ")[:160]
    tail = f" 服务端说：{detail}" if detail else ""
    if status in (401, 403):
        return "auth", (
            f"API key 不对或已失效（HTTP {status}）。打开日历页面 → 右上角「设置」→ "
            f"重新填一把（注意别把结尾的空格一起粘进去）。{tail}"
        )
    if status == 404:
        return "notfound", (
            f"接口地址不对（HTTP 404）：{base_url} 上找不到 /chat/completions。"
            "到「设置」里检查 base_url —— 多数服务填到域名就行（如 https://api.deepseek.com），"
            f"有的要带 /v1。{tail}"
        )
    if status == 400:
        return "model", (
            f"请求被拒（HTTP {status}）。最常见的两个原因：模型名写错了（现在用的是"
            f"「{model}」），或者这次的内容超出了它的限制。到「设置」里核对一下。{tail}"
        )
    if status == 429:
        return "rate", f"模型服务限流了（HTTP 429），过一会儿再发一次就行。{tail}"
    if status >= 500:
        return "server", f"模型服务自己出错了（HTTP {status}），不是你配置的问题，过一会儿再试。{tail}"
    return "other", f"模型接口返回 HTTP {status}。{tail}"


def _record_problem(kind: str, message: str) -> None:
    """记下「最近一次模型调用为什么失败」，好让页面把话说明白。

    `configured` 只代表「填了 key」，不代表「key 管用」—— 打错一个字符，页面上的
    黄条就消失了，用户之后只会在微信里收到一句报错，还不知道去哪儿改。

    记不上就算了：记录失败绝不该把一次调用拖垮。
    """
    if kind not in PAGE_ALERT_KINDS:
        return
    try:
        from . import settings as st

        st.set_last_error(kind, message)
    except Exception as exc:  # noqa: BLE001
        log.debug("记录模型失败原因时出错（忽略）：%s", exc)


def _clear_problem() -> None:
    """模型调通了 —— 把之前记下的失败清掉。"""
    try:
        from . import settings as st

        st.clear_last_error()
    except Exception as exc:  # noqa: BLE001
        log.debug("清除模型失败记录时出错（忽略）：%s", exc)


def _settings_override() -> dict[str, Any]:
    """读页面上存过的模型设置。惰性 import，免得 settings ↔ llm 循环依赖。"""
    try:
        from . import settings as st
        return st.provider_override()
    except Exception as exc:  # noqa: BLE001 — 设置读不出来不该让模型调用挂掉
        log.debug("读页面设置失败，忽略：%s", exc)
        return {}


class LLM:
    """OpenAI 兼容的模型客户端。

    **配置是每次调用时现算的**（`_refresh()`）：页面上改了模型/API key，写进
    SQLite 后立刻生效，不需要重启容器。优先级：库里的设置 > config.json /
    环境变量。这一点很要紧 —— 用户扫完码、日历也能看，唯一缺的就是一个 key，
    总不能让他在 NAS 上 SSH 改文件才能用。
    """

    def __init__(self, provider: dict[str, Any]):
        self._base = dict(provider)
        self._refresh()

    def _refresh(self) -> None:
        provider = dict(self._base)
        provider.update(_settings_override())
        self.base_url = (provider.get("base_url") or "").rstrip("/")
        self.api_key = provider.get("api_key") or ""
        self.model = provider.get("model") or ""
        self.vision_model = provider.get("vision_model") or self.model
        self.temperature = provider.get("temperature", 0)
        self.timeout = int(provider.get("timeout", 120))
        if not self.base_url:
            raise LLMError("config.json 里没有配置 base_url")
        if not self.model:
            raise LLMError("config.json 里没有配置 model")

    @property
    def configured(self) -> bool:
        self._refresh()
        return bool(self.api_key)

    # -- 底层 ---------------------------------------------------------

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def _post_once(self, payload: dict[str, Any]) -> str:
        if not self.api_key:
            # 不重试：没 key 重试多少次都一样，还会白等 7 秒
            raise NoKeyError(NO_KEY_HINT)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = requests.post(
            self._endpoint(), headers=headers, json=payload, timeout=self.timeout
        )
        if resp.status_code >= 400:
            kind, message = _explain_http(
                resp.status_code, resp.text, base_url=self.base_url, model=self.model
            )
            # 只记用户自己能修的那几类（见 PAGE_ALERT_KINDS）
            _record_problem(kind, message)
            raise LLMError(message)
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"模型接口返回的不是 JSON: {resp.text[:300]}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise LLMError(f"模型接口报错: {data['error']}")
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型返回结构异常: {str(data)[:400]}") from exc
        # 成功一次就把页面上的红条清掉：用户把 key 改好了，提示就该自己消失。
        # 没有记录时 `clear_last_error()` 只查一次内存缓存，等于白调。
        _clear_problem()
        return content

    def _post(self, payload: dict[str, Any], retries: int = 3) -> str:
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return self._post_once(payload)
            except LLMError as exc:
                msg = str(exc)
                if isinstance(exc, NoKeyError):
                    raise
                # HTTP 4xx 里除了限流，重试没意义
                if "HTTP 4" in msg and "429" not in msg:
                    raise
                last = exc
            except requests.RequestException as exc:
                last = LLMError(f"网络错误: {exc}")
            wait = 2 ** attempt
            log.warning("模型调用失败（第 %d 次），%d 秒后重试: %s", attempt + 1, wait, last)
            time.sleep(wait)
        raise last or LLMError("模型调用失败")

    def _messages_json(
        self, prompt: str, model: Optional[str] = None, max_tokens: int = 2048
    ) -> dict[str, Any]:
        self._refresh()  # 页面上刚改的模型/温度，这一步就读到了
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        text = self._post(payload)
        return _extract_json(text)

    # -- 文字理解 -----------------------------------------------------

    def understand(self, text: str) -> dict[str, Any]:
        """一次调用同时完成意图判定与字段抽取。"""
        prompt = _fill(UNDERSTAND_PROMPT, anchor=tu.anchor_block()) + (text or "")
        data = self._messages_json(prompt)
        data.setdefault("intent", "other")
        return data

    def confirm(self, question: str, payload: dict[str, Any], user_text: str) -> dict[str, Any]:
        prompt = (
            _fill(
                CONFIRM_PROMPT,
                anchor=tu.anchor_block(),
                question=question,
                payload=json.dumps(payload, ensure_ascii=False, indent=2),
            )
            + "\n【用户的回复】\n"
            + user_text
        )
        return self._messages_json(prompt, max_tokens=1024)

    # -- 图片理解 -----------------------------------------------------

    def understand_image(self, image_bytes: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
        self._refresh()
        b64 = base64.b64encode(image_bytes).decode()
        prompt = _fill(IMAGE_PROMPT, anchor=tu.anchor_block())
        payload: dict[str, Any] = {
            "model": self.vision_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                },
            ],
            "temperature": self.temperature,
            "max_tokens": 2048,
        }
        text = self._post(payload)
        return _extract_json(text)

    # -- 闲聊兜底 -----------------------------------------------------

    def chat(self, text: str) -> str:
        self._refresh()
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是「羊毛机器人」，一个个人抢购活动助手。你负责把用户发来的活动信息"
                        "记进日历，并回答什么时候抢什么。你没有任何活动数据，被问到具体活动时"
                        "不要编造，如实说明并提示用户可以问「今天抢什么」。回答简短，中文。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0.6,
            "max_tokens": 512,
        }
        return self._post(payload).strip()
