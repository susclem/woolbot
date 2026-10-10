"""开抢前的主动提醒 —— 一条微信推出去，**一次模型都不调**。

用户 2026-10-09 要的：快开抢了就主动发一条微信过来。这条路是纯脚本 ——
查库、算时刻、拼字、发送，`self.llm` 从头到尾不参与，所以它不会因为模型限流、
key 失效、接口抖动而失约。

三个零件都是现成的，这里只是把它们接起来：

* **下一场几点**：`pipeline.next_openings()`。和 `/即将` 是**同一个函数**，
  重复活动（每天/每周/每月）的逐天展开由 `repeat.occurrence_on` 负责 ——
  提醒要是自己另算一套时刻，就会出现「机器人说 09:00、提醒却在 08:50 响」，
  而重复活动恰恰是最容易算错的那一类。
* **推给谁**：`ClawBot.state["context_tokens"]` 的键 = 跟机器人说过话的人。
  配了 `allowed_users` 白名单就取交集。从没人说过话就没人可推，静默跳过。
* **发得出去吗**：`ClawBot.send_text()` 只要一个 `to_user_id`，token 自动从
  状态文件兜底，**不需要「正在回复某条消息」这个上下文**。

为什么必须**另起一个线程**：`ClawBot._poll_loop()` 抱着
`/ilink/bot/getupdates` 长轮询不放，服务端最长挂 `longpolling_timeout_ms`
（被夹在 5~120 秒），一次 poll 最多阻塞 130 秒。检查要是塞在轮询循环里，
最多能晚两分钟才发出去 —— 提前 5 分钟的提醒只剩 3 分钟，等于白提醒。

**关掉就不存在**：线程不是「开着空转」，而是 `sync()` 时按设置决定建不建。
关着的时候提醒线程、它那个连接池、它的一分钟一次查询，一样都没有
（用户 2026-10-09 明确要的「不开就别占资源」）。

**一条消息只说最近的那一个时刻**（`soonest()`）：8点20分只提醒 8点半那批，
同一时刻的几场合并成一条，9 点及更晚的不跟着带出来 —— 也是用户 2026-10-09 定的。

**提醒也算一条回复**：末尾照样贴那句「一言」（`sayings_tail()`），用的是机器人
自己那个 `sayings.Sayings` —— 所以设置里那个「一言」开关对它一样管用，
关掉就一句都不取（用户 2026-10-09 要的）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Iterable, Optional

from . import db
from . import pipeline
from . import repeat as rp
from . import settings as st
from . import timeutil as tu
from . import wechat as wx

log = logging.getLogger("woolbot.reminder")

#: 一轮最多睡多久（秒）。压到 60 是有理由的：提醒窗口最短 5 分钟，而新活动是
#: 随手就能录进来的 —— 睡太久，一条「20 分钟后开抢」的活动录进来时窗口可能已经
#: 过去了。睡 60 秒最多晚 1 分钟，代价只是一分钟一次的小查询。
SLEEP_MAX = 60.0

#: 一轮最少睡多久。纯粹是防「算出来个 0 或者负数就变成忙等」。
SLEEP_MIN = 5.0

#: 「这一场已经提醒过了」的台账前缀。台账本身直接用 `inbound_msg` 表
#: （`db.seen_msg` / `db.mark_msg`）—— 那张表就是干「这件事做过没有」的。
LEDGER_PREFIX = "remind"


def ledger_key(user_id: str, row: dict[str, Any], when: datetime) -> str:
    """去重键。**必须带场次时刻**，不能只带活动 id。

    「每天 08:30」的活动今天提醒过、明天还得再提醒一次；只按 id 去重，第二次
    之后一辈子都不会再响。带上时刻，重启容器也不会把同一条重发一遍。
    """
    return f"{LEDGER_PREFIX}:{user_id}:{row.get('id')}:{when:%Y-%m-%dT%H:%M}"


def targets(state: dict[str, Any], allowed: Iterable[str] = ()) -> list[str]:
    """该推给谁：跟机器人说过话的人（就这么简单）。

    `state["context_tokens"]` 是 `handle_inbound` 攒下来的 —— 每收到一条私聊就记下
    发言人和他的 `context_token`。配了白名单就取交集：白名单是用户明确划的圈，
    圈外的人本来连消息都不回，更不该收到主动推送。
    """
    users = [str(user) for user in (state.get("context_tokens") or {}) if str(user).strip()]
    keep = {str(user).strip() for user in allowed if str(user).strip()}
    if keep:
        users = [user for user in users if user in keep]
    return sorted(users)


def soonest(items: list[tuple[datetime, dict[str, Any]]]) -> list[tuple[datetime, dict[str, Any]]]:
    """只留**最近的那一个时刻**的场次。`items` 必须是按时刻升序的。

    用户 2026-10-09 定的规矩：8点20分只提醒 8点半的；8点半那几场合并成一条没问题，
    但 9 点以及更晚的活动**不许跟着带出来** —— 一条消息里混着几个时刻，会让人以为
    9 点也马上要开抢，反而不知道该先盯哪个。

    同一时刻的几场**要合并**（10:00 同时开四个活动很常见），所以这里按时刻取整批，
    不是只取第一条。
    """
    if not items:
        return []
    first = items[0][0]
    return [(when, row) for when, row in items if when == first]


class Reminder:
    """开抢提醒。一个实例管一个线程，`sync()` 让它跟设置保持一致。"""

    def __init__(self, client: Any) -> None:
        #: `wechat.ClawBot`。只用它的 `state` / `allowed` / `send_text` / `logged_in`。
        self.client = client
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # -- 生命周期 -----------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        """起线程。已经跑着就什么都不做，返回 False。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="woolbot-remind", daemon=True
            )
            self._thread.start()
        log.info("开抢提醒已启用（提前 %d 分钟）", st.remind_minutes())
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """停线程并等它真的退出。本来就没跑就什么都不做，返回 False。"""
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return False
        self._stop.set()
        # 从自己的线程里调 stop 会自己 join 自己 —— 那不是等，是死锁。
        if thread is not threading.current_thread():
            thread.join(timeout)
        log.info("开抢提醒已停止")
        return True

    def sync(self) -> bool:
        """按当前设置起停，返回「现在开着吗」。**改完设置必须调它。**

        设置页保存之后调一次，于是「关掉提醒」是当场把线程收掉，不是下次重启
        才生效；「打开提醒」也是当场就起，不用等容器重启。
        """
        if not st.remind_enabled():
            self.stop()
            return False
        self.start()
        return True

    # -- 干活 ---------------------------------------------------------

    def _quota_full(self, row: dict[str, Any], day) -> bool:
        """这一期的配额已经领满了 —— 再提醒就是让人白跑一趟。

        判据和日历卡片、和机器人回执里那句「（本期已领满）」**同源**
        （都是 `repeat.quota_state`）。领满之后那些天本来就显示成已完成，
        提醒却还在催「快去抢」，是同一个项目里最怕的两边各说各话。
        """
        quota = rp.quota_of(row)
        if not quota.week and not quota.month:
            return False
        state = rp.quota_state(row, day, db.done_days(row["id"]))
        return bool(state["week"]["reached"] or state["month"]["reached"])

    def scan(self, now: Optional[datetime] = None) -> tuple[list[tuple[datetime, dict]], float]:
        """一次扫描：返回 (该提醒的场次, 下次该睡多少秒)。

        两个返回值一起给，是因为它们出自**同一次** `next_openings()` 查询 ——
        拆成两个函数就会一分钟查两遍库。
        """
        now = now or tu.now()
        limit = st.remind_minutes() * 60
        due: list[tuple[datetime, dict]] = []
        wait = SLEEP_MAX
        for when, row in pipeline.next_openings(now):
            # `list_range` 默认把 expired 也算进来（日历要它们），但**过期系列
            # 不该再提醒**。`done` 是一次性活动被标掉的，同理。
            if row.get("status") != "pending":
                continue
            left = (when - now).total_seconds()
            if left <= 0:
                continue
            if left > limit:
                # 已按时刻升序，后面只会更远；这一条就是下一个窗口的起点。
                # 算出来的等待时间照样会被 `SLEEP_MAX` 压到一分钟以内 —— 提醒宁可
                # 早看一眼，也不能因为「刚睡下」而漏掉一条一分钟前才录进来的活动。
                wait = left - limit
                break
            if self._quota_full(row, when.date()):
                continue
            due.append((when, row))
        return due, max(SLEEP_MIN, min(wait, SLEEP_MAX))

    def message(self, items: list[tuple[datetime, dict]], now: datetime) -> str:
        """要发出去的那段字。**同一时刻**的一批合并成一条。

        `items` 一定是同一个时刻的（`_send` 用 `soonest()` 保证），所以标题上的
        倒计时取第一场就够了，逐行也就不必再重复印时刻。
        """
        when, first = items[0]
        mins = max(1, int(round((when - now).total_seconds() / 60.0)))
        if len(items) == 1:
            return f"⏰ 还有 {mins} 分钟开抢：\n· {self._line(when, first)}"
        lines = [f"⏰ 还有 {mins} 分钟开抢（{when:%H:%M} 共 {len(items)} 场）："]
        lines.extend(f"· {self._line(when, row, with_time=False)}" for _, row in items)
        return "\n".join(lines)

    def sayings_tail(self) -> str:
        """提醒末尾那句「一言」（用户 2026-10-09 要的：提醒也算一条回复）。

        **用机器人自己那个 `sayings.Sayings`**（同一个池子、同一条连接、同一个开关）
        —— 另起一个实例就是两套实现：两边各自去请求、各自算开关，池子还不共享。
        开关那层不用在这儿再判一遍：`Sayings.tail()` 自己会看
        `settings.sayings_enabled()` 和 `WOOLBOT_NO_SAYINGS`。

        取不到、或者对面根本不是个能取一言的东西，都只是「这条不带」——
        提醒本身照发（和 `wechat.py` 里贴一言那条路同一个态度）。
        """
        sayings = getattr(self.client, "sayings", None)
        if sayings is None:
            return ""
        try:
            return sayings.tail()
        except Exception as exc:  # noqa: BLE001 —— 彩蛋接口出任何意外都不能弄丢提醒
            log.warning("一言没贴上（这条提醒照发）：%s", exc)
            return ""

    @staticmethod
    def _line(when: datetime, row: dict[str, Any], with_time: bool = True) -> str:
        stamp = f"{when:%H:%M} " if with_time else ""
        return f"{stamp}{row.get('title') or '（无标题）'}{pipeline.quota_suffix(row, when.date())}"

    def tick(self, now: Optional[datetime] = None) -> float:
        """跑一轮：该发的发掉，返回下次该睡多少秒。**不睡眠，方便测。**"""
        now = now or tu.now()
        due, wait = self.scan(now)
        if due:
            self._send(due, now)
        return wait

    def _send(self, due: list[tuple[datetime, dict]], now: datetime) -> None:
        if not self.client.logged_in:
            # 还没扫码登录（或者 token 刚失效正在冷却）。没什么可做的，
            # 也别把台账写掉 —— 登录回来时这一场要是还没开抢，还得提醒。
            log.debug("提醒跳过：机器人还没登录")
            return
        users = targets(self.client.state, getattr(self.client, "allowed", ()))
        if not users:
            log.debug("提醒跳过：还没有人跟机器人说过话，不知道推给谁")
            return
        for user in users:
            fresh = [(w, r) for w, r in due if not db.seen_msg(ledger_key(user, r, w))]
            # 一条消息只说**最近的那一个时刻**。注意顺序：先按台账滤掉已经提醒过的，
            # 再取最近。反过来的话，08:30 那批发完之后，08:33 得一直等到 08:30 过去
            # 才轮得上 —— 等于白丢了 08:33 自己的提前量。
            fresh = soonest(fresh)
            if not fresh:
                continue
            body = self.message(fresh, now) + self.sayings_tail()
            try:
                ok = self.client.send_text(user, body, session=self.client.push_session())
            except wx.StaleToken as exc:
                # token 失效这条路由 `_poll_loop` 统一处理（重新登录 + 冷却）。
                # 提醒线程只负责别把异常抛出去，更不能顺手去触发登录。
                log.warning("提醒发不出去（token 失效），等重新登录：%s", exc)
                break
            except Exception as exc:  # noqa: BLE001 —— 一个收件人失败不能连累别人
                log.error("提醒发给 %s 出错：%s", user, exc)
                continue
            if not ok:
                continue
            # **发成功了才记账**：先记后发的话，发送失败的那次就永远不会重试。
            for when, row in fresh:
                db.mark_msg(ledger_key(user, row, when), user, LEDGER_PREFIX)
            log.info("已提醒 %d 场开抢", len(fresh))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.tick()
            except Exception:  # noqa: BLE001 —— 一轮出错不能让提醒线程死掉
                log.exception("提醒这一轮出错")
                wait = SLEEP_MAX
            # `wait()` 返回 True 表示被 `stop()` 叫醒，那就干脆利落地退出，
            # 不用等这一觉睡完（否则关提醒要等最多 60 秒）。
            if self._stop.wait(wait):
                return
