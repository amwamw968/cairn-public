#!/usr/bin/env python3
"""构建本地词典 data/dict/dict.sqlite（ECDICT + CC-CEDICT，均为上游开源数据）。

数据来源（只用上游开源项目，不碰任何 App 打包的词典文件）：
- ECDICT（MIT） https://github.com/skywind3000/ECDICT —— 英汉释义/音标/词性/词形
- CC-CEDICT（CC BY-SA） https://www.mdbg.net/chinese/dictionary?page=cc-cedict

用法：
    python scripts/build_dict.py              # 下载到 data/dict/raw/ 并构建
    python scripts/build_dict.py --offline    # 不下载，直接用 raw/ 里已有文件
    CAIRN_DICT_DB=... 不影响本脚本（本脚本用 --out 指定输出）

产物表结构：
    en(word TEXT PRIMARY KEY, translation TEXT, phonetic TEXT, pos TEXT)
    en_variant(variant TEXT PRIMARY KEY, lemma TEXT)
    zh(term TEXT, pinyin TEXT, PRIMARY KEY(term, pinyin))
外加索引 idx_zh_term(zh.term)、idx_en_variant_lemma(en_variant.lemma)。

词典文件与原始数据都不进 git（见 .gitignore）；本仓库只提交脚本与文档。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import re
import shutil
import sqlite3
import sys
import urllib.request
from pathlib import Path

ECDICT_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
CEDICT_URL = (
    "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"
)

DEFAULT_RAW_DIR = Path("data/dict/raw")
DEFAULT_OUT = Path("data/dict/dict.sqlite")

# ECDICT exchange 字段里的屈折形式键：过去式/过去分词/现在分词/三单/比较级/最高级/复数
_INFLECTION_KEYS = ("p", "d", "i", "3", "r", "t", "s")
_CEDICT_ENTRY = re.compile(r"^\S+\s+(\S+)\s+\[([^\]]+)\]\s+(.*)$")
_CEDICT_PINYIN_SPLIT = re.compile(r"\s+")

# 数字声调 → 标调字母（ü 记作 v:; 轻声原样保留）
_TONE_MAP = {
    "1": "āēīōūǖĀĒĪŌŪǕ",
    "2": "áéíóúǘÁÉÍÓÚǗ",
    "3": "ǎěǐǒǔǚǍĚǏǑǓǙ",
    "4": "àèìòùǜÀÈÌÒÙǛ",
}
_PLAIN = "aeiouüvAEIOÜV"


def numbered_to_marked(pinyin: str) -> str:
    """``pin1 yin1`` → ``pīn yīn``（CC-CEDICT 用数字声调，展示与朗读用标调式）。"""
    out: list[str] = []
    for syllable in pinyin.strip().split():
        match = re.match(r"^(.*?)([1-5])$", syllable)
        if not match:
            out.append(syllable)
            continue
        body, tone = match.group(1), match.group(2)
        if tone == "5" or not any(ch in _PLAIN for ch in body.lower()):
            out.append(body)
            continue
        # 标调位置：有 a 标 a；否则 ou 组合标 o；否则标最后一个元音
        lower = body.lower()
        if "a" in lower:
            pos = lower.index("a")
        elif "ou" in lower:
            pos = lower.index("o")
        else:
            pos = max(lower.index(ch) for ch in lower if ch in _PLAIN)
        vowel = body[pos]
        # 表里每行 6 个字母对应 _PLAIN 前 6 个（a e i o u ü）的小写/大写
        idx = _PLAIN.lower().index(vowel.lower())
        if "AEIOÜV".find(vowel) >= 0:  # 大写元音取表内后半段
            marked = _TONE_MAP[tone][idx + 6]
        else:
            marked = _TONE_MAP[tone][idx]
        out.append(body[:pos] + marked + body[pos + 1 :])
    return " ".join(out)


def download(url: str, dest: Path) -> None:
    """流式下载；失败给清晰提示并退出，不静默。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"下载 {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
    except Exception as exc:  # noqa: BLE001 — 必须把原因原样告诉用户
        tmp.unlink(missing_ok=True)
        print(
            f"下载失败：{url}\n原因：{exc}\n"
            f"请检查网络；或手动下载后放到 {dest}，再用 --offline 重跑。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    tmp.replace(dest)
    print(f"完成 → {dest}（{dest.stat().st_size / 1_048_576:.1f} MB）")


def build_en(conn: sqlite3.Connection, ecdict_csv: Path) -> tuple[int, int]:
    """解析 ECDICT csv → en / en_variant。返回 (词条数, 变体数)。"""
    en_rows: list[tuple[str, str, str, str]] = []
    variants: dict[str, str] = {}
    with ecdict_csv.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            word = (row.get("word") or "").strip()
            if not word:
                continue
            translation = (row.get("translation") or "").strip()
            if not translation:
                translation = (row.get("definition") or "").strip()
            en_rows.append(
                (
                    word,
                    translation,
                    (row.get("phonetic") or "").strip(),
                    (row.get("pos") or "").strip(),
                )
            )
            exchange = row.get("exchange") or ""
            for item in exchange.split("/"):
                item = item.strip()
                if ":" not in item:
                    continue
                key, _, value = item.partition(":")
                value = value.strip()
                if not value or value == word:
                    continue
                if key == "0":  # 该词本身就是屈折形式，0 指向原形
                    variants.setdefault(word, value)
                elif key in _INFLECTION_KEYS:  # 该词是原形，列出各屈折形式
                    variants.setdefault(value, word)
    conn.executemany(
        "INSERT OR REPLACE INTO en(word, translation, phonetic, pos) VALUES (?,?,?,?)",
        en_rows,
    )
    conn.executemany(
        "INSERT OR REPLACE INTO en_variant(variant, lemma) VALUES (?,?)",
        sorted(variants.items()),
    )
    return len(en_rows), len(variants)


def build_zh(conn: sqlite3.Connection, cedict_gz: Path) -> int:
    """解析 CC-CEDICT → zh（简体词, 标调拼音）去重对。"""
    pairs: set[tuple[str, str]] = set()
    with gzip.open(cedict_gz, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#"):
                continue
            match = _CEDICT_ENTRY.match(line.strip())
            if not match:
                continue
            term = match.group(1)
            raw_pinyin = _CEDICT_PINYIN_SPLIT.sub(" ", match.group(2)).strip()
            if term and raw_pinyin:
                pairs.add((term, numbered_to_marked(raw_pinyin)))
    conn.executemany(
        "INSERT OR IGNORE INTO zh(term, pinyin) VALUES (?,?)", sorted(pairs)
    )
    return len(pairs)


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS en;
        DROP TABLE IF EXISTS en_variant;
        DROP TABLE IF EXISTS zh;
        CREATE TABLE en(
            word TEXT PRIMARY KEY,
            translation TEXT,
            phonetic TEXT,
            pos TEXT
        );
        CREATE TABLE en_variant(
            variant TEXT PRIMARY KEY,
            lemma TEXT
        );
        CREATE TABLE zh(
            term TEXT,
            pinyin TEXT,
            PRIMARY KEY(term, pinyin)
        );
        CREATE INDEX idx_zh_term ON zh(term);
        CREATE INDEX idx_en_variant_lemma ON en_variant(lemma);
        """
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_dict.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--offline", action="store_true", help="不下载，直接用 raw/ 里已有文件"
    )
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    ecdict_csv = args.raw_dir / "ecdict.csv"
    cedict_gz = args.raw_dir / "cedict.txt.gz"

    if not args.offline:
        download(ECDICT_URL, ecdict_csv)
        download(CEDICT_URL, cedict_gz)
    else:
        missing = [p for p in (ecdict_csv, cedict_gz) if not p.is_file()]
        if missing:
            print(
                "--offline 但缺少原始文件："
                + "、".join(str(p) for p in missing)
                + "。先不带 --offline 跑一次（联网），或手动放置后重试。",
                file=sys.stderr,
            )
            return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        args.out.unlink()
    conn = sqlite3.connect(args.out)
    try:
        create_schema(conn)
        en_count, variant_count = build_en(conn, ecdict_csv)
        conn.commit()
        zh_count = build_zh(conn, cedict_gz)
        conn.commit()
    finally:
        conn.close()

    size_mb = args.out.stat().st_size / 1_048_576
    print(
        f"构建完成：{args.out}\n"
        f"  en          {en_count:>8} 条\n"
        f"  en_variant  {variant_count:>8} 条\n"
        f"  zh          {zh_count:>8} 条\n"
        f"  文件大小    {size_mb:>8.1f} MB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
