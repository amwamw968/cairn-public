#!/usr/bin/env python3
"""幻觉对账：今天「说了几次答对了」vs「库里真的有几条判分记录」。

生产事故（2026-10-31）：Rui 第二轮起不再调工具，自己编题、自己宣布「答对了」，
库里只有 1 条 review。服务端听不到 TTS，只能拿客户端日志和库里的 review 账本对账——
这就是本脚本做的事。

用法：
    python scripts/audit_claims.py --log rui.log                      # 今天
    python scripts/audit_claims.py --log rui.log --day 2026-10-31
    python scripts/audit_claims.py --log rui.log --json
    python scripts/audit_claims.py --log rui.log --strict             # 有幻觉就 exit 1

日志格式假设（与 docs/RUI-DEPLOY.md 里 docker logs 的输出一致）：
    - 带 HH:MM:SS 的行按当天处理；
    - 含 `tool`/`cairn_` 的行算工具调用，不算「说出口的话」；
    - 其余匹配 --claim-regex 的行算一次「答对了」。
格式不同的日志用 --claim-regex / --tool-regex / --submit-regex 覆盖。

库里的真相（也可单独当 SQL 用，见 docs/HALLUCINATION-AUDIT.md）：
    SELECT rating, COUNT(*) FROM review
    WHERE date(reviewed_at, 'localtime') = :day GROUP BY rating;
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from cairn.models import make_engine, make_session_factory
from cairn.models.review import Review

DEFAULT_DB_URL = "sqlite:///./cairn.db"
DEFAULT_CLAIM_REGEX = r"答对了|答对啦|你答对了"
DEFAULT_TOOL_REGEX = r"\btool\b|cairn_"
DEFAULT_SUBMIT_REGEX = r"cairn_submit_answer"


def _to_tz(value: datetime, tz) -> datetime:
    """库里取出来可能是 naive（SQLite 丢时区），统一按 UTC 解释再转本地。"""
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(tz)


def scan_log(path: Path, pattern: re.Pattern[str], tool: re.Pattern[str], submit: re.Pattern[str]):
    """返回（声称答对次数, submit_answer 调用次数, 命中行样例）。"""
    claims = 0
    submit_calls = 0
    samples: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        if submit.search(line):
            submit_calls += 1
        if pattern.search(line):
            if tool.search(line):
                continue  # 工具名/工具返回文本，不算模型自己说出口的话
            claims += 1
            if len(samples) < 3:
                samples.append(line.strip()[:120])
    return claims, submit_calls, samples


def review_counts(db_url: str, day: date) -> Counter[str]:
    """当天（本地时区）的 review 条数，按 rating 分桶。"""
    engine = make_engine(db_url)
    try:
        factory = make_session_factory(engine)
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        with factory() as session:
            # 家庭量级只有几千条 review，取回来按本地日切，避免 SQLite 丢时区的坑。
            rows = session.execute(select(Review.rating, Review.reviewed_at)).all()
            return Counter(
                rating
                for rating, reviewed_at in rows
                if start <= _to_tz(reviewed_at, tz) < end
            )
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对账：声称答对 vs review 记录")
    parser.add_argument("--log", required=True, help="Rui 的日志文件（一天一份）")
    parser.add_argument("--day", default=None, help="YYYY-MM-DD，缺省今天")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL, help=f"缺省 {DEFAULT_DB_URL}")
    parser.add_argument("--claim-regex", default=DEFAULT_CLAIM_REGEX)
    parser.add_argument("--tool-regex", default=DEFAULT_TOOL_REGEX)
    parser.add_argument("--submit-regex", default=DEFAULT_SUBMIT_REGEX)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="发现幻觉时 exit 1")
    args = parser.parse_args(argv)

    day = date.fromisoformat(args.day) if args.day else datetime.now().date()
    log = Path(args.log)
    if not log.exists():
        parser.error(f"日志不存在：{log}")

    claims, submit_calls, samples = scan_log(
        log,
        re.compile(args.claim_regex),
        re.compile(args.tool_regex),
        re.compile(args.submit_regex),
    )
    ratings = review_counts(args.db_url, day)
    reviews = sum(ratings.values())
    good = ratings.get("good", 0)
    unmatched = max(0, claims - good)

    report = {
        "day": day.isoformat(),
        "log": str(log),
        "claims_correct": claims,
        "submit_answer_calls": submit_calls,
        "reviews": reviews,
        "reviews_good": good,
        "reviews_by_rating": dict(ratings),
        "claims_without_review": unmatched,
        "samples": samples,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"日期            {report['day']}")
        print(f"日志            {report['log']}")
        print(f"声称答对        {claims} 次")
        print(f"submit_answer   {submit_calls} 次")
        print(f"review 记录     {reviews} 条（判对 {good} 条）")
        if unmatched:
            print(f"疑似幻觉        {unmatched} 次「答对了」没有对应的判分记录")
            print("库里的分档      " + "、".join(f"{k}={v}" for k, v in ratings.items()))
            for line in samples:
                print(f"  样例：{line}")
        else:
            print("对账一致        没有发现「没说就判」的记录")
    return 1 if (args.strict and unmatched) else 0


if __name__ == "__main__":
    raise SystemExit(main())
