"""批量导入教材词库：``python -m cairn.wordbank_import --dir data/wordbank``。

把目录里每一份 ``*.json``（一份文件 = 一本书，结构见 ``docs/WORDBANK-FORMAT.md``）
交给 ``book_service.import_book`` 幂等导入**教材表**（book/unit/word）。个人题库
（question/error_item）完全不碰——孩子自己的题只能从页面勾选或手工录入进来。

- 幂等：重复跑同一目录，第二次每本都是「新增 0 条、跳过 N 条」，行数不变。
  认书靠书的五件套（学科+出版社+年级+学期+书名），认条目靠 (unit, answer)。
- 溯源：``source_note`` 随书入库；``book.imported_at``（v10）记第一次导入时间。
- 一个文件一个事务：某份不合规（缺 cue/answer…）只回滚那一份，其余照常，
  退出码 1 让调用方知道有失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cairn import book_service
from cairn.book_service import BookDataError, ImportResult
from cairn.models import init_db, make_engine, make_session_factory

__all__ = ["main"]


def _load(path: Path) -> dict:
    """读一份词表 JSON；解析失败按 BookDataError 口径报（不碰数据库）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BookDataError(f"读不懂词表文件 {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise BookDataError(f"词表文件 {path.name} 顶层必须是对象")
    return data


def import_dir(session, directory: Path) -> list[tuple[Path, ImportResult]]:
    """按文件名顺序导入目录里所有 ``*.json``；返回 (文件, 结果) 列表。"""
    results: list[tuple[Path, ImportResult]] = []
    for path in sorted(directory.glob("*.json")):
        results.append((path, book_service.import_book(session, _load(path))))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cairn.wordbank_import",
        description="把一个目录里的教材词表 JSON 全部导入教材库（幂等，不碰个人题库）。",
    )
    parser.add_argument(
        "--dir",
        default="data/wordbank",
        help="词表目录（默认 data/wordbank），里面的 *.json 一份 = 一本书",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="数据库 URL；不给就读 CAIRN_DB_URL，再退到 ./cairn.db",
    )
    args = parser.parse_args(argv)

    directory = Path(args.dir).expanduser()
    if not directory.is_dir():
        print(f"找不到词表目录: {directory}", file=sys.stderr)
        return 2

    db_url = args.db or os.environ.get("CAIRN_DB_URL") or "sqlite:///./cairn.db"
    engine = make_engine(db_url)
    failures: list[str] = []
    try:
        init_db(engine)
        with make_session_factory(engine)() as session:
            for path in sorted(directory.glob("*.json")):
                try:
                    result = book_service.import_book(session, _load(path))
                except BookDataError as exc:
                    failures.append(f"{path.name}: {exc}")
                    print(f"导入失败 {path.name}：{exc}", file=sys.stderr)
                    continue
                print(f"{result.book_name}（book_id={result.book_id}）：{result.summary()}")
    finally:
        engine.dispose()

    if failures:
        print(f"有 {len(failures)} 份文件导入失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
