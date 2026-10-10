"""塞一批演示数据，用来验证日历页面和查询。跑法：

    python tools/seed_demo.py            # 写入（默认不重复插）
    python tools/seed_demo.py --clear    # 先清空再写
    python tools/seed_demo.py --wipe     # 只清空，不写（玩完演示数据用这个）
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod  # noqa: E402
from app import db, timeutil as tu  # noqa: E402

DEMO = [
    # (相对天数, 开抢时刻, 标题, 平台, 品类, 有效期末, 备注)
    (0, "09:00", "京东超市 满99减50 领券", "京东", "日用", "23:59", "限会员，需先领券"),
    (0, "09:30", "淘宝百亿补贴 秒杀", "淘宝", "数码", "12:00", "限时 2 小时"),
    (0, "10:00", "美团外卖 满30减15", "美团", "外卖", "22:00", None),
    (0, "20:00", "拼多多百亿补贴 整点抢", "拼多多", "家电", "23:59", "整点场"),
    (1, "09:30", "京东 PLUS 会员日 5折券", "京东", "全品类", "23:59", "PLUS 专享"),
    (1, "20:00", "天猫双11 预售定金", "天猫", "全品类", None, "定金膨胀"),
    (2, "00:00", "银行 App 抽奖 每日一次", "招商银行", "金融", "23:59", None),
    (3, "10:00", "饿了么 会员日 大额券", "饿了么", "外卖", "23:59", None),
    (-2, "09:00", "已过期的活动（演示状态）", "京东", "日用", "23:59", None),
]


def _wipe() -> int:
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        conn.execute("DELETE FROM activity")
    print(f"已清空 activity 表（删掉 {n} 条）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true", help="先清空 activity 表，再写演示数据")
    ap.add_argument("--wipe", action="store_true", help="只清空 activity 表，不写演示数据")
    args = ap.parse_args()

    cfg = cfgmod.load()
    tu.configure(cfg.get("timezone"))
    db.init(cfg.path(cfg.get("db_path")))

    if args.wipe:
        return _wipe()

    if args.clear:
        _wipe()

    today = tu.today()
    n = 0
    for days, hhmm, title, platform, category, end, note in DEMO:
        d = today + timedelta(days=days)
        grab = f"{d.isoformat()}T{hhmm}"
        end_at = f"{d.isoformat()}T{end}" if end else None
        if db.find_duplicate(title, grab):
            continue
        db.insert_activity(
            {
                "title": title,
                "platform": platform,
                "category": category,
                "grab_at": grab,
                "end_at": end_at,
                "note": note,
                "status": "pending",
                "source": "manual",
            }
        )
        n += 1

    db.refresh_expired()
    s = db.stats()
    print(f"新增 {n} 条；库里现在共 {s['total']} 条（待抢 {s['pending']} / 完成 {s['done']} / 过期 {s['expired']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
