"""一次性：往本机库里补一条「开抢日已过、有效期到年底」的一次性券。

用来验「日历说这天没有开抢安排，但还能领」这一节。跑完可以
`python tools/seed_demo.py --wipe` 清掉。
"""
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")
from app import config as cf, db, timeutil as tu

db.init(cf.load().db_path)
tu.configure("Asia/Shanghai")

today = tu.today()


def at(d: date, hh: int, mm: int) -> str:
    return f"{d.isoformat()}T{hh:02d}:{mm:02d}"


db.insert_activity({
    "title": "京东超市满199减100（一次性的长效券）",
    "platform": "京东",
    "grab_at": at(today - timedelta(days=3), 10, 0),
    "start_at": at(today - timedelta(days=3), 10, 0),
    "end_at": at(today + timedelta(days=20), 23, 59),
    "note": "有效期 20 天，随时可领",
})
print("已写入", db.stats()["total"], "条；今天 =", today.isoformat())
