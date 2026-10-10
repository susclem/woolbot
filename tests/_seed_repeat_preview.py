"""一次性：往本机库里塞三条带重复规则的真券形状数据，用来看前端日历。

不改任何产品代码；跑完可以 `python tools/seed_demo.py --wipe` 清掉。
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")
from app import config as cf, db, timeutil as tu

db.init(cf.load().db_path)
tu.configure("Asia/Shanghai")

today = tu.today()
tmr = today + timedelta(days=1)
fri = today + timedelta(days=3)          # 2026-10-09 是周五
year_end = date(today.year, 12, 31)


def at(d: date, hh: int, mm: int) -> str:
    return f"{d.isoformat()}T{hh:02d}:{mm:02d}"


db.insert_activity({
    "title": "港城欢乐购·中国银行信用卡满1000元减50元券",
    "platform": "中国银行",
    "grab_at": at(tmr, 8, 30),
    "start_at": at(today - timedelta(days=96), 8, 30),
    "end_at": at(year_end, 23, 59),
    "note": "领券时间：每天 08:30:00-22:00:00，单用户单月限领1张",
    "repeat_kind": "daily", "repeat_at": "08:30", "repeat_until": "22:00",
})
db.insert_activity({
    "title": "云闪付临沂金融助力交通银行满100元减10元券",
    "platform": "云闪付",
    "grab_at": at(tmr, 9, 0),
    "start_at": at(today - timedelta(days=57), 9, 0),
    "end_at": at(year_end, 23, 59),
    "note": "每日 09:00:00 起发放，数量有限先到先得",
    "repeat_kind": "daily", "repeat_at": "09:00",
})
db.insert_activity({
    "title": "云闪付 乐园门票梦享券 满1000减120",
    "platform": "云闪付",
    "grab_at": at(fri, 12, 0),
    "start_at": at(fri, 12, 0),
    "note": "每周五中午12:00开始发放，次周四晚23:59:59截止",
    "repeat_kind": "weekly", "repeat_at": "12:00", "repeat_weekday": 4,
})
db.insert_activity({
    "title": "京东超市满99减50（一次性，对照组）",
    "platform": "京东",
    "grab_at": at(today + timedelta(days=2), 10, 0),
    "end_at": at(today + timedelta(days=2), 22, 0),
    "note": "限PLUS会员",
})
print("已写入", db.stats()["total"], "条；今天 =", today.isoformat())
