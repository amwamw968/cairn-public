"""词典卡 — 本地开源词典（ECDICT/CC-CEDICT）查询层与录入自动补。

全部用 tmp_path 里现建的小型假词典 sqlite（不依赖网络、不依赖真实词典文件）；
通过 CAIRN_DICT_DB 环境变量指过去。词典缺失的用例确保主流程零影响。
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from cairn import dict as dict_service
from cairn.book_service import import_book
from cairn.entry_service import DuplicateEntryError, EntryPayload, create_error_item
from cairn.models import Question, Word


def _build_fake_dict(path) -> None:
    """小型假词典：结构同 scripts/build_dict.py 的产物。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE en(
            word TEXT PRIMARY KEY, translation TEXT, phonetic TEXT, pos TEXT
        );
        CREATE TABLE en_variant(variant TEXT PRIMARY KEY, lemma TEXT);
        CREATE TABLE zh(term TEXT, pinyin TEXT, PRIMARY KEY(term, pinyin));
        CREATE INDEX idx_zh_term ON zh(term);
        CREATE INDEX idx_en_variant_lemma ON en_variant(lemma);
        """
    )
    conn.execute(
        "INSERT INTO en VALUES (?,?,?,?)",
        ("apple", "n. 苹果", "ˈæp(ə)l", "n:"),
    )
    conn.execute("INSERT INTO en_variant VALUES (?,?)", ("apples", "apple"))
    # 同词多音（多音字「行」）
    conn.execute("INSERT INTO zh VALUES (?,?)", ("行", "xíng"))
    conn.execute("INSERT INTO zh VALUES (?,?)", ("行", "háng"))
    conn.execute("INSERT INTO zh VALUES (?,?)", ("苹果", "píngguǒ"))
    conn.commit()
    conn.close()


@pytest.fixture()
def dict_db(tmp_path, monkeypatch):
    path = tmp_path / "dict.sqlite"
    _build_fake_dict(path)
    monkeypatch.setenv("CAIRN_DICT_DB", str(path))
    dict_service.reset_cache()
    yield path
    dict_service.reset_cache()


@pytest.fixture()
def no_dict(tmp_path, monkeypatch):
    """环境变量指向一个不存在的文件：词典缺失的常态。"""
    monkeypatch.setenv("CAIRN_DICT_DB", str(tmp_path / "absent.sqlite"))
    dict_service.reset_cache()
    yield
    dict_service.reset_cache()


# ---------------------------------------------------------------- 查询层


def test_open_dict_missing_file_returns_none(no_dict):
    assert dict_service.open_dict() is None
    assert dict_service.lookup_en("apple") is None
    assert dict_service.lookup_zh_pinyin("苹果") == []


def test_lookup_en_hit_and_case_insensitive(dict_db):
    hit = dict_service.lookup_en("apple")
    assert hit == {"translation": "n. 苹果", "phonetic": "ˈæp(ə)l", "pos": "n:"}
    # 大小写无关
    assert dict_service.lookup_en("Apple")["translation"] == "n. 苹果"


def test_lookup_en_variant_resolves_to_lemma(dict_db):
    hit = dict_service.lookup_en("apples")
    assert hit is not None and hit["translation"] == "n. 苹果"
    assert dict_service.lookup_en("不存在的词") is None


def test_lookup_zh_pinyin_returns_sorted_list(dict_db):
    assert dict_service.lookup_zh_pinyin("行") == ["háng", "xíng"]
    assert dict_service.lookup_zh_pinyin("苹果") == ["píngguǒ"]
    assert dict_service.lookup_zh_pinyin("没有的词") == []


# ---------------------------------------------------------------- 导入路径


def _zh_book() -> dict:
    return {
        "subject": "语文",
        "publisher": "人民教育出版社",
        "grade": 5,
        "semester": "上",
        "name": "人教版 五年级上册 语文（词典测试）",
        "units": [
            {
                "unit_no": 1,
                "words": [
                    {"answer": "苹果"},  # 缺 cue → 词典补拼音
                    {"answer": "行"},  # 缺 cue → 词典补第一个拼音
                    {"answer": "词典里没有的词"},  # 补不到 → needs_manual
                ],
            }
        ],
    }


def test_import_fills_missing_cue_from_dict(session, dict_db):
    result = import_book(session, _zh_book())
    assert result.created == 2  # 补到拼音的两条入库
    assert result.needs_manual == ("词典里没有的词",)
    assert "待人工补全 1 条" in result.summary()
    cues = {
        w.answer: w.cue
        for w in session.scalars(select(Word)).all()
    }
    assert cues["苹果"] == "píngguǒ"
    assert cues["行"] == "háng"  # 多音取排序后的第一个
    assert "词典里没有的词" not in cues  # 补不到的不落库


def test_import_missing_cue_without_dict_still_records_needs_manual(session, no_dict):
    result = import_book(session, _zh_book())
    assert result.created == 0
    assert result.needs_manual == ("苹果", "行", "词典里没有的词")


# ---------------------------------------------------------------- 录入路径


def test_entry_autofills_en_meaning_from_dict(session, learner, dict_db):
    item = create_error_item(
        session,
        learner.id,
        EntryPayload(
            subject="英语",
            question_type="单词",
            content="apple",  # 家长只敲了单词，没给中文释义
            answer="apple",
        ),
    )
    question = session.get(Question, item.question_id)
    assert question.content == "n. 苹果"  # 词典补的中文释义
    assert question.audio_text == "apple"


def test_entry_user_filled_meaning_wins(session, learner, dict_db):
    item = create_error_item(
        session,
        learner.id,
        EntryPayload(
            subject="英语",
            question_type="单词",
            content="一种红色的水果",  # 家长手填的中文释义
            answer="apple",
        ),
    )
    question = session.get(Question, item.question_id)
    assert question.content == "一种红色的水果"  # 用户手填优先，不被覆盖


def test_entry_autofills_zh_pinyin_from_dict(session, learner, dict_db):
    item = create_error_item(
        session,
        learner.id,
        EntryPayload(
            subject="语文",
            question_type="词语",
            content="苹果",  # 只有词，没有拼音
            answer="苹果",
        ),
    )
    question = session.get(Question, item.question_id)
    assert question.audio_text == "píngguǒ"


def test_entry_user_filled_audio_wins(session, learner, dict_db):
    item = create_error_item(
        session,
        learner.id,
        EntryPayload(
            subject="语文",
            question_type="词语",
            content="苹果",
            answer="苹果",
            audio_text="ping2 guo3",  # 家长手填的朗读 cue
        ),
    )
    question = session.get(Question, item.question_id)
    assert question.audio_text == "ping2 guo3"


def test_entry_without_dict_unaffected(session, learner, no_dict):
    """词典缺失：录入照常，不自动补也不报错。"""
    item = create_error_item(
        session,
        learner.id,
        EntryPayload(subject="英语", question_type="单词", content="apple", answer="apple"),
    )
    question = session.get(Question, item.question_id)
    assert question.content == "apple"  # 补不到就保持原样


def test_entry_math_never_autofills_from_dict(session, learner, dict_db):
    """数学条目：词典是语言数据，数学一律不补——audio_text 必须为空。

    故意用假词典里有的多音字「行」：变异（退回按 lang 补全）时这条也会红。
    """
    item = create_error_item(
        session,
        learner.id,
        EntryPayload(subject="数学", question_type="计算", content="一行有几人？", answer="行"),
    )
    question = session.get(Question, item.question_id)
    assert question.audio_text is None  # 汉字答案也不补拼音
    assert question.content == "一行有几人？"  # 题面原样，不被词典动过


def test_entry_reentry_after_autofill_still_duplicates(session, learner, dict_db):
    """补全重写了 en 的 content（future→释义），重录同一个词仍要被查重拦住。"""
    payload = EntryPayload(subject="英语", question_type="单词", content="apple", answer="apple")
    create_error_item(session, learner.id, payload, reject_duplicates=True)
    with pytest.raises(DuplicateEntryError):
        create_error_item(session, learner.id, payload, reject_duplicates=True)
