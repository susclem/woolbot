"""用**真实模型**跑一遍常见说法，检查抽取对不对。

    python tools/check_extract.py                 # 跑内置的十几条
    python tools/check_extract.py -v              # 连模型吐的原始 JSON 一起打
    python tools/check_extract.py --pipeline      # 再看「最终会入库什么 / 会怎么答」
    python tools/check_extract.py --text "明天9点京东满99减50"
    python tools/check_extract.py --image shot.png

**只读**：不写 activity 表、不写消息幂等表、不建待确认。录入那几条走的是
`_collect_activities` → `_align_day` → `_repair_end` → `_classify_past`，
也就是「真入库前会经过的那些步骤」，但停在入库前；查询那几条直接调
`_apply_query` 拿最终回复（它只读库）；删除那几条只用关键词去 `db.search`
看看会命中哪些（**绝不会真删**）。

为什么要有这个工具：`tests/test_llm.py` 打的是**假端点**，验的是协议
（请求发得像不像、4xx 要不要重试）；抽取质量只能在真模型上看。改完
prompt 拿它跑一遍，一眼就能看出有没有退化 —— 这是「静默错答」最便宜的防线。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod  # noqa: E402
from app import db, timeutil as tu  # noqa: E402
from app import pipeline  # noqa: E402
from app.llm import LLM, LLMError  # noqa: E402

#: 内置用例。`grab` 是期望的开抢时刻 `(相对今天的天数, "HH:MM")`；
#: 只写确定能对上的，模型换个说法也算对的就不写死。
CASES: list[dict] = [
    {"name": "单条·带时刻", "text": "明天上午9点半京东超市满99减50，记得先领券",
     "intent": "ingest", "grab": (1, "09:30")},
    {"name": "多条·一次两条", "text": "淘宝百亿补贴今晚8点秒杀，另外拼多多整点抢红包是每天10点",
     "intent": "ingest", "count": 2},
    {"name": "有效期·到晚上12点", "text": "明天10点饿了么会员日大额券开抢，到晚上12点结束",
     "intent": "ingest", "grab": (1, "10:00")},
    {"name": "只给时刻·今天已过", "text": "10点开抢，到晚上12点结束",
     "intent": "ingest", "clarify": True},
    {"name": "没给时间·该反问", "text": "京东PLUS会员日有五折券",
     "intent": "ingest", "clarify": True},
    {"name": "补记·已经开抢了", "text": "今天上午9点京东已经开抢了，满199减100",
     "intent": "ingest"},
    {"name": "预约·下周几", "text": "下周三晚上8点天猫预售定金膨胀，别忘了",
     "intent": "ingest"},
    {"name": "查询·今天", "text": "今天抢什么", "intent": "query"},
    {"name": "查询·周二", "text": "周二抢什么", "intent": "query"},
    {"name": "查询·今天有什么活动", "text": "今天有什么活动", "intent": "query"},
    {"name": "查询·精确到分钟", "text": "今天9点半有什么要抢的", "intent": "query"},
    {"name": "查询·一周区间", "text": "这周有哪些活动", "intent": "query"},
    {"name": "删除·一个关键词", "text": "删除梦享", "intent": "delete", "keyword": "梦享"},
    {"name": "删除·把那个删了", "text": "把临沂那个删了", "intent": "delete", "keyword": "临沂"},
    {"name": "闲聊·不是投喂", "text": "你好", "intent": "other"},
]


def _check(case: dict, got: dict, today, text: str = "") -> tuple[list[str], list[str]]:
    """把「模型说的」和「这条用例期望的」对一遍。

    返回 `(problems, notes)`：
      - `problems` 是「用户最终会看到错的」；
      - `notes` 是「模型这步做错了，但 Python 兜底救回来了」—— 结果没事，
        可是这说明模型不可靠，值得知道。
    """
    problems: list[str] = []
    notes: list[str] = []
    if case.get("intent") and got.get("intent") != case["intent"]:
        if case["intent"] == "delete" and pipeline._fallback_keyword(text):
            # 删除识别错了但被动词兜底救回来 —— 用户那边照样能删，只是这步不该靠兜底
            notes.append(
                f"模型把「{text}」判成了 {got.get('intent')!r}，是 Python 的删除动词兜底救回来的。"
            )
        else:
            problems.append(f"intent 期望 {case['intent']}，实得 {got.get('intent')!r}")

    if case.get("keyword") is not None and got.get("intent") == "delete":
        want_kw = str(case["keyword"])
        got_kw = pipeline._clean_keyword(got.get("keyword"))
        # 关键词只要求「能定位」：模型给「梦享券」而期望「梦享」也算对，
        # 因为它是拿去做 LIKE 检索的，不是拿去比对的。
        if not got_kw or not (want_kw in got_kw or got_kw in want_kw):
            problems.append(f"keyword 期望含 {want_kw!r}，实得 {got.get('keyword')!r}")

    acts = pipeline._collect_activities(got, "text")
    if case.get("count") is not None and len(acts) != case["count"]:
        problems.append(f"条数期望 {case['count']}，实得 {len(acts)}")

    if case.get("grab"):
        off, hhmm = case["grab"]
        want = f"{(today + timedelta(days=off)).isoformat()}T{hhmm}"
        raw = (acts[0].get("grab_at") if acts else None) or ""
        if raw != want:
            # 真入库前还会经过 _align_day（用原话里的日子词拨正），所以再看一眼兜底结果。
            # 注意：`_align_day` 是**原地改**、只 return 被改动的那几条 —— 拿它自己的
            # 返回值当结果集是错的（没改动时返回空列表）。
            pipeline._align_day(acts, text, today)
            fixed = (acts[0].get("grab_at") if acts else None) or ""
            if fixed == want:
                notes.append(
                    f"模型把时间算成了 {raw or '(空)'}，是 Python 兜底拨回 {want} 的。"
                    "用户看到的结果是对的，但这一步不该靠兜底 —— 顺手看看提示词。"
                )
            else:
                problems.append(f"grab_at 期望 {want}，实得 {raw or '(空)'}")

    if case.get("clarify") is not None:
        actual_flag = bool(got.get("needs_clarify"))
        if actual_flag != case["clarify"]:
            problems.append(f"needs_clarify 期望 {case['clarify']}，实得 {actual_flag}")

    expects_clarify = bool(case.get("clarify"))
    for act in acts:
        if str(act.get("title") or "").strip() == pipeline.TITLE_FALLBACK:
            if expects_clarify:
                # 这条用例本来就等着机器人反问（原话里压根没有标题可抽），而反问不会
                # 落库，兜底标题也就露不出来 —— 别把「模型没编出一个标题」算成问题。
                # （真踩到：用例「10 点开抢，到晚上 12 点结束」被判失败，而模型给的
                # needs_clarify 完全正确。）
                notes.append("反问前的活动没抽到标题（原话里本来就没有标题，反问不会落库）。")
            else:
                # 模型漏了标题 —— 兜底会让它静默变成日历上一条「未命名活动」
                problems.append("有活动没抽到标题（落库会变成「未命名活动」）")
    return problems, notes


def _ingest_preview(text: str, got: dict) -> str:
    """录入方向：把「真入库前会经过的步骤」走一遍，打印最终形态（不落库）。"""
    acts = pipeline._collect_activities(got, "text")
    if not acts:
        return ""
    # 这两个函数是**原地修改**、只把「被改动的那几条」当作返回值 —— 别拿返回值
    # 覆盖 acts，否则一条都没改的时候会把整个列表清空（这里踩过一次）。
    pipeline._align_day(acts, text)
    pipeline._repair_end(acts)
    wrong_day, stale_today = pipeline._classify_past(acts)
    lines = ["     入库后会是这样："]
    for a in acts:
        lines.append(
            "       · {title}｜平台 {platform}｜🎯 {grab}｜开始 {start}｜至 {end}".format(
                title=a.get("title"),
                platform=a.get("platform") or "-",
                grab=a.get("grab_at") or "未定",
                start=a.get("start_at") or "-",
                end=a.get("end_at") or "-",
            )
        )
    if wrong_day:
        lines.append(f"       ⚠️ {len(wrong_day)} 条日子算到了今天之前 → 会拦下来反问，不直接入库")
    if stale_today:
        lines.append(f"       ⏰ {len(stale_today)} 条今天已经过了 → 照记，回执里不再提（用户 2026-10-10 划掉了那句提醒）")
    if got.get("needs_clarify"):
        lines.append(f"       ❓ 模型要求澄清：{got.get('question') or '(没给问题)'}")
    return "\n".join(lines)


def run_text(llm: LLM, bot: pipeline.Bot, case: dict, *, verbose: bool, show_pipeline: bool) -> tuple[list[str], list[str]]:
    print(f"\n▌{case['name']}")
    print(f"  你说：{case['text']}")
    got = llm.understand(case["text"])
    if verbose:
        print("  模型原始输出：")
        print("   " + json.dumps(got, ensure_ascii=False, indent=2).replace("\n", "\n   "))
    print(f"  意图：{got.get('intent')!r}", end="")
    if got.get("needs_clarify"):
        print(f"　需要澄清：{got.get('question') or '(没给问题)'}", end="")
    print()
    if show_pipeline:
        if got.get("intent") == "ingest":
            preview = _ingest_preview(case["text"], got)
            if preview:
                print(preview)
        elif got.get("intent") == "query":
            reply = bot._apply_query(got, case["text"])
            print("  机器人会回：")
            print("\n".join("   " + ln for ln in (reply or "").splitlines()))
        elif got.get("intent") == "delete":
            # 只查不删：这里**不会**建 pending、更不会动 activity 表
            kw = pipeline._clean_keyword(got.get("keyword")) or pipeline._fallback_keyword(case["text"])
            hits = db.search(kw, limit=5)
            print(f"  会用关键词「{kw}」去库里找，命中 {len(hits)} 条：")
            for r in hits:
                print(f"   · {r.get('title')}  <#{r.get('id')}>")
    problems, notes = _check(case, got, tu.today(), case["text"])
    for note in notes:
        print(f"  ↪ {note}")
    return problems, notes


def run_image(llm: LLM, path: Path, *, verbose: bool, show_pipeline: bool) -> list[str]:
    print(f"\n▌截图：{path.name}")
    data = path.read_bytes()
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    got = llm.understand_image(data, mime)
    if verbose:
        print("  模型原始输出：")
        print("   " + json.dumps(got, ensure_ascii=False, indent=2).replace("\n", "\n   "))
    acts = pipeline._collect_activities(got, "image")
    print(f"  认出 {len(acts)} 条：")
    for a in acts:
        print(
            "   · {title}｜🎯 {grab}｜至 {end}｜有效期线 {line}".format(
                title=a.get("title"),
                grab=a.get("grab_at") or "未定",
                end=a.get("end_at") or "-",
                line=a.get("start_at") or "-",
            )
        )
    ocr = str(got.get("raw_ocr") or "").strip()
    if ocr:
        print(f"  图里抄下来的字：{ocr[:200]}")
    if show_pipeline:
        preview = _ingest_preview("", got)
        if preview:
            print(preview)
    problems = []
    if not acts:
        problems.append("一条都没认出来")
    for a in acts:
        if str(a.get("title") or "").strip() == pipeline.TITLE_FALLBACK:
            problems.append("有活动没认到标题")
        if not a.get("grab_at"):
            problems.append(f"「{a.get('title')}」没抽到开抢时刻")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="用真实模型检查抽取质量（只读，不写库）")
    ap.add_argument("--text", help="只跑这一句")
    ap.add_argument("--image", help="只跑这张图")
    ap.add_argument("--only", help="只跑名字里含这几个字的用例")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印模型原始 JSON")
    ap.add_argument("--pipeline", action="store_true", help="连最终入库形态/最终回复一起看")
    args = ap.parse_args()

    cfg = cfgmod.load()
    tu.configure(cfg.get("timezone"))
    db.init(cfg.path(cfg.get("db_path")))

    llm = LLM(dict(cfg.active_provider))
    if not llm.configured:
        print("❌ 还没配 API key —— 打开日历页面 → 右上角「设置」里填一个（存了立刻生效）")
        return 1
    bot = pipeline.Bot(cfg.as_dict(), llm)
    print(f"模型：{llm.model} @ {llm.base_url}")
    print(f"现在：{tu.human(tu.now())}（{tu.tz_name()}）")

    failures: list[tuple[str, list[str]]] = []
    warnings: list[str] = []
    try:
        if args.image:
            path = Path(args.image)
            if not path.is_file():
                print(f"❌ 找不到图片：{path}")
                return 1
            problems = run_image(llm, path, verbose=args.verbose, show_pipeline=args.pipeline)
            if problems:
                failures.append((path.name, problems))
        else:
            cases = CASES
            if args.text:
                cases = [{"name": "自定义", "text": args.text}]
            elif args.only:
                cases = [c for c in cases if args.only in c["name"]]
                if not cases:
                    print(f"❌ 没有名字含「{args.only}」的用例")
                    return 1
            for case in cases:
                problems, notes = run_text(
                    llm, bot, case, verbose=args.verbose, show_pipeline=args.pipeline
                )
                warnings.extend(f"{case['name']}：{n}" for n in notes)
                if problems:
                    failures.append((case["name"], problems))
    except LLMError as exc:
        print(f"\n❌ 模型调用失败：{exc}")
        return 1

    print("\n" + "─" * 60)
    if warnings:
        print(f"↪ {len(warnings)} 条是兜底救回来的（结果对，但模型那步不可靠）：")
        for w in warnings:
            print(f"  · {w}")
    if not failures:
        print("✅ 用户会看到的结果全部正确" if warnings else "✅ 全部对上了")
        return 0
    print(f"⚠️ {len(failures)} 条对不上：")
    for name, problems in failures:
        print(f"  · {name}")
        for p in problems:
            print(f"      - {p}")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
