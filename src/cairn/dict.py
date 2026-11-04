"""本地开源词典（ECDICT + CC-CEDICT）只读查询层。

词典是独立于业务库的 sqlite 文件（默认 ``data/dict/dict.sqlite``，可用环境变量
``CAIRN_DICT_DB`` 覆盖），由 ``scripts/build_dict.py`` 从上游开源数据构建：

- ECDICT（MIT）：英汉释义 / 音标 / 词性，以及 exchange 字段导出的词形还原表。
- CC-CEDICT（CC BY-SA）：中文词的拼音。

设计约束（词典卡）：

- **词典缺失时系统照常工作**：文件不存在 ``open_dict`` 返回 ``None``，所有查询
  返回 ``None`` / 空列表，绝不抛异常、绝不影响录入主流程。
- **线程安全**：不持有共享连接——每次查询自开一个只读连接（``mode=ro``），
  用完即关。sqlite 打开连接的开销对词典查询量级可忽略，换来的是零共享状态、
  多线程（web 服务多 worker）无需加锁。
- 惰性打开：``open_dict`` 只做一次 ``is_file()`` 检查，连接推迟到第一次查询；
  已打开的 store 按路径缓存，文件后来才出现（测试先建后查）也能被发现。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

__all__ = [
    "DictStore",
    "default_dict_path",
    "lookup_en",
    "lookup_zh_pinyin",
    "open_dict",
    "reset_cache",
]

ENV_DICT_DB = "CAIRN_DICT_DB"
DEFAULT_DICT_PATH = Path("data") / "dict" / "dict.sqlite"

# 按解析后的绝对路径缓存已打开的 store（连接在 store 内每次查询自开）。
_STORES: dict[str, "DictStore"] = {}


class DictStore:
    """一个已存在的词典 sqlite 文件的只读句柄。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        # 每次查询独立只读连接：线程安全靠"无共享"而不是锁。
        uri = f"file:{self.path.resolve()}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def lookup_en(self, word: str) -> dict[str, str] | None:
        """英文词 → {"translation","phonetic","pos"}；查不到返回 None。

        大小写无关（``COLLATE NOCASE``，对英文够用）；先查 ``en`` 主表，
        查不到再经 ``en_variant`` 归一到原形词再查一次。
        """
        text = (word or "").strip()
        if not text:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT translation, phonetic, pos FROM en "
                "WHERE word = ? COLLATE NOCASE LIMIT 1",
                (text,),
            ).fetchone()
            if row is None:
                lemma = conn.execute(
                    "SELECT lemma FROM en_variant "
                    "WHERE variant = ? COLLATE NOCASE LIMIT 1",
                    (text,),
                ).fetchone()
                if lemma is not None and lemma[0]:
                    row = conn.execute(
                        "SELECT translation, phonetic, pos FROM en "
                        "WHERE word = ? COLLATE NOCASE LIMIT 1",
                        (lemma[0],),
                    ).fetchone()
        if row is None:
            return None
        return {"translation": row[0], "phonetic": row[1], "pos": row[2]}

    def lookup_zh_pinyin(self, term: str) -> list[str]:
        """中文词 → 拼音列表（同词多音返回多个，按拼音排序保证稳定）。"""
        text = (term or "").strip()
        if not text:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT pinyin FROM zh WHERE term = ?", (text,)
            ).fetchall()
        return sorted(row[0] for row in rows if row[0])


def default_dict_path() -> Path:
    """查询路径优先级：CAIRN_DICT_DB 环境变量 > data/dict/dict.sqlite。"""
    return Path(os.environ.get(ENV_DICT_DB) or DEFAULT_DICT_PATH)


def open_dict(path: str | Path | None = None) -> DictStore | None:
    """惰性打开词典；文件不存在返回 None（不抛）。"""
    resolved = Path(path) if path is not None else default_dict_path()
    if not resolved.is_file():
        return None
    key = str(resolved.resolve())
    store = _STORES.get(key)
    if store is None:
        store = DictStore(resolved)
        _STORES[key] = store
    return store


def reset_cache() -> None:
    """丢弃已缓存的 store（测试与重建词典后用）。"""
    _STORES.clear()


def lookup_en(word: str) -> dict[str, str] | None:
    """模块级便捷入口：用默认路径（env 可覆盖）的词典查英文词。"""
    store = open_dict()
    return store.lookup_en(word) if store is not None else None


def lookup_zh_pinyin(term: str) -> list[str]:
    """模块级便捷入口：用默认路径（env 可覆盖）的词典查中文词拼音。"""
    store = open_dict()
    return store.lookup_zh_pinyin(term) if store is not None else []
