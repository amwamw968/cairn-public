"""Export the question bank as an Anki deck package (``.apkg``).

Why hand-rolled: docs/ROADMAP-0.2.md 决策段 — Cairn 不把 Anki 当底座，只借它的
格式；PyPI 上的 ``anki`` 库是 AGPL，会拖累将来发布。所以这里只用标准库
（``sqlite3`` / ``zipfile`` / ``json`` / ``hashlib``）按 Anki 的包格式自己拼。

Package shape (legacy, widest compatibility):

- ``collection.anki2`` — SQLite database in Anki's **schema v11**:
  ``col`` / ``notes`` / ``cards`` / ``revlog`` / ``graves``.
- ``media`` — JSON map ``{"0": "file.jpg", ...}``; empty object here (no media).

Card model (only one): two fields 提示 / 答案 — front shows the hint, back shows
the answer. One template, one card per note.

Scheduling: **none exported**. Every card goes in as a brand-new card
(``type=0``, ``queue=0``, ``due`` ascending), ``revlog`` stays empty — Anki
plans its own repetition and Cairn keeps its FSRS schedule. The two sides run
in parallel on purpose.

Decks: one top-level deck per ``question.subject`` (语文 / 英语 / 数学), plus
Anki's own ``Default`` deck (id 1) so the collection always has a fallback.

Note type / deck / dconf JSON is the legacy (schema-11) shape Anki expects; the
model/Template/field keys are kept exactly as Anki writes them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime
from io import BytesIO
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.models import Concept, ErrorItem, Question
from cairn.models.concept import question_concept

#: collection.anki2 schema version we emit (legacy; Anki upgrades on import).
SCHEMA_VERSION = 11

#: Stable id for our single note type (epoch-ms style, like Anki's own ids).
MODEL_ID = 1_700_000_000_000
MODEL_NAME = "Cairn 提示答案"
FIELD_HINT = "提示"
FIELD_ANSWER = "答案"

DEFAULT_DECK_NAME = "Default"
FALLBACK_DECK_NAME = "未分类"

#: Legacy subject codes (0.1-era rows) → the Chinese deck names we promise.
_SUBJECT_ALIASES = {
    "math": "数学",
    "mathematics": "数学",
    "english": "英语",
    "chinese": "语文",
}

#: Anki's field separator inside ``notes.flds``.
FIELD_SEPARATOR = "\x1f"

#: Anki's base91 alphabet, used for note guids.
_BASE91 = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "!#$%&()*+,-./:;<=>?@[]^_`{|}~"
)

_SCHEMA = """
CREATE TABLE col (
    id integer primary key,
    crt integer not null,
    mod integer not null,
    scm integer not null,
    ver integer not null,
    dty integer not null,
    usn integer not null,
    ls integer not null,
    conf text not null,
    models text not null,
    decks text not null,
    dconf text not null,
    tags text not null
);
CREATE TABLE notes (
    id integer primary key,
    guid text not null,
    mid integer not null,
    mod integer not null,
    usn integer not null,
    tags text not null,
    flds text not null,
    sfld integer not null,
    csum integer not null,
    flags integer not null,
    data text not null
);
CREATE TABLE cards (
    id integer primary key,
    nid integer not null,
    did integer not null,
    ord integer not null,
    mod integer not null,
    usn integer not null,
    type integer not null,
    queue integer not null,
    due integer not null,
    ivl integer not null,
    factor integer not null,
    reps integer not null,
    lapses integer not null,
    left integer not null,
    odue integer not null,
    odid integer not null,
    flags integer not null,
    data text not null
);
CREATE TABLE revlog (
    id integer primary key,
    cid integer not null,
    usn integer not null,
    ease integer not null,
    ivl integer not null,
    lastIvl integer not null,
    factor integer not null,
    time integer not null,
    type integer not null
);
CREATE TABLE graves (
    usn integer not null,
    oid integer not null,
    type integer not null
);
CREATE INDEX ix_notes_usn on notes (usn);
CREATE INDEX ix_notes_csum on notes (csum);
CREATE INDEX ix_cards_usn on cards (usn);
CREATE INDEX ix_cards_nid on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_usn on revlog (usn);
CREATE INDEX ix_revlog_cid on revlog (cid);
"""

_CSS = """.card {
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  font-size: 20px;
  line-height: 1.5;
  text-align: center;
  color: #222;
  background-color: #fff;
  white-space: pre-wrap;
}
"""

_LATEX_PRE = (
    "\\documentclass[12pt]{article}\n"
    "\\special{papersize=3in,5in}\n"
    "\\usepackage[utf8]{inputenc}\n"
    "\\usepackage{amssymb,amsmath}\n"
    "\\pagestyle{empty}\n"
    "\\setlength{\\parindent}{0in}\n"
    "\\begin{document}\n"
)
_LATEX_POST = "\\end{document}"


def deck_name_for_subject(subject: str | None) -> str:
    """语文/英语/数学 for the usual subjects; legacy codes are mapped too."""
    raw = (subject or "").strip()
    if not raw:
        return FALLBACK_DECK_NAME
    return _SUBJECT_ALIASES.get(raw.casefold(), raw)


def sanitize_tag(raw: str) -> str:
    """Anki tags cannot contain spaces; drop everything that is not a plain
    word character (letters, digits, CJK), underscore or hyphen."""
    return "".join(char for char in raw if char.isalnum() or char in "_-")


def anki_filename(now: datetime) -> str:
    """cairn-anki-YYYYMMDD.apkg (the parent's local date)."""
    return f"cairn-anki-{now:%Y%m%d}.apkg"


def build_anki_package(
    session: Session, learner_id: int, now: datetime | None = None
) -> bytes:
    """The whole ``.apkg`` file as bytes; *now* is injectable so tests are stable.

    Only the questions owned by *learner_id* go into the deck: siblings have
    separate banks and must not see each other's cards in Anki either.
    """
    from cairn.workflow.review import ready_for_practice

    now = now or datetime.now().astimezone()
    questions = list(
        session.scalars(
            select(Question)
            .where(Question.learner_id == learner_id, ready_for_practice(Question.id),
                   (Question.source != "collection-import") | select(ErrorItem.id).where(
                       ErrorItem.question_id == Question.id, ErrorItem.learner_id == learner_id).exists())
            .order_by(Question.id)
        )
    )
    tags_by_question = _tags_by_question(session, [question.id for question in questions])
    collection = _build_collection(questions, tags_by_question, now)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("collection.anki2", collection)
        # Media map: index -> filename. No media in Cairn, so an empty object.
        package.writestr("media", b"{}")
    return buffer.getvalue()


def _tags_by_question(session: Session, question_ids: list[int]) -> dict[int, list[str]]:
    """Canonical concept names per question, sanitized and de-duplicated.

    Restricted to *question_ids* (this learner's questions) so the deck never
    picks up a tag row belonging to a sibling's question.
    """
    if not question_ids:
        return {}
    rows = session.execute(
        select(question_concept.c.question_id, Concept.name)
        .join(Concept, Concept.id == question_concept.c.concept_id)
        .where(question_concept.c.question_id.in_(question_ids))
        .order_by(question_concept.c.question_id, Concept.name)
    )
    tags: dict[int, list[str]] = {}
    for question_id, name in rows:
        tag = sanitize_tag(name)
        bucket = tags.setdefault(question_id, [])
        if tag and tag not in bucket:
            bucket.append(tag)
    return tags


def _build_collection(
    questions: list[Question],
    tags_by_question: dict[int, list[str]],
    now: datetime,
) -> bytes:
    created_seconds = int(now.timestamp())
    modified_ms = int(now.timestamp() * 1000)
    deck_ids = _deck_ids(questions)

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        _insert_col(
            connection,
            created_seconds=created_seconds,
            modified_ms=modified_ms,
            deck_ids=deck_ids,
            question_count=len(questions),
        )
        _insert_notes_and_cards(
            connection,
            questions=questions,
            tags_by_question=tags_by_question,
            deck_ids=deck_ids,
            modified_ms=modified_ms,
        )
        connection.commit()
        return connection.serialize()
    finally:
        connection.close()


def _deck_ids(questions: list[Question]) -> dict[str, int]:
    """Deck name -> id. Id 1 is Default; subject decks start at 2, in first-seen
    order of the questions (stable: questions are id-ascending)."""
    deck_ids = {DEFAULT_DECK_NAME: 1}
    for question in questions:
        name = deck_name_for_subject(question.subject)
        if name not in deck_ids:
            deck_ids[name] = len(deck_ids) + 1
    return deck_ids


def _insert_col(
    connection: sqlite3.Connection,
    *,
    created_seconds: int,
    modified_ms: int,
    deck_ids: dict[str, int],
    question_count: int,
) -> None:
    conf = {
        "activeDecks": [1],
        "curDeck": 1,
        "newSpread": 0,
        "collapseTime": 1200,
        "timeLim": 0,
        "estTimes": True,
        "dueCounts": True,
        "curModel": MODEL_ID,
        "nextPos": question_count + 1,
        "sortType": "noteFld",
        "sortBackwards": False,
        "addToCur": True,
        "dayLearnFirst": False,
        "schedVer": 2,
    }
    connection.execute(
        "insert into col values (1,?,?,?,11,0,0,0,?,?,?,?,?)",
        (
            created_seconds,
            modified_ms,
            modified_ms,
            _dumps(conf),
            _dumps(_models(modified_ms)),
            _dumps(_decks(deck_ids, modified_ms)),
            _dumps(_deck_configs()),
            "{}",  # col.tags: the registry of known tags; optional for import
        ),
    )


def _insert_notes_and_cards(
    connection: sqlite3.Connection,
    *,
    questions: list[Question],
    tags_by_question: dict[int, list[str]],
    deck_ids: dict[str, int],
    modified_ms: int,
) -> None:
    seen_guids: set[str] = set()
    for position, question in enumerate(questions, start=1):
        hint = _clean_field(question.content)
        answer = _clean_field(question.answer or "")
        note_id = modified_ms + position
        card_id = modified_ms + 1_000_000 + position
        deck_id = deck_ids[deck_name_for_subject(question.subject)]
        tags = tags_by_question.get(question.id, [])

        connection.execute(
            "insert into notes values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                note_id,
                _guid(question.id, seen_guids),
                MODEL_ID,
                modified_ms,
                0,
                f" {' '.join(tags)} " if tags else "",
                hint + FIELD_SEPARATOR + answer,
                hint,
                _checksum(hint),
                0,
                "",
            ),
        )
        # Brand-new card: Anki owns the schedule from here on.
        connection.execute(
            "insert into cards values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card_id,
                note_id,
                deck_id,
                0,
                modified_ms,
                0,
                0,  # type: new
                0,  # queue: new
                position,  # due: new-card position, ascending
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                "",
            ),
        )


def _models(modified_ms: int) -> dict[str, Any]:
    return {
        str(MODEL_ID): {
            "id": MODEL_ID,
            "name": MODEL_NAME,
            "type": 0,  # standard (not cloze)
            "mod": modified_ms // 1000,
            "usn": 0,
            "sortf": 0,  # sort on 提示
            "did": 1,
            "tmpls": [
                {
                    "name": "提示→答案",
                    "ord": 0,
                    "qfmt": "{{提示}}",
                    "afmt": "{{FrontSide}}<hr id=answer>{{答案}}",
                    "did": None,
                    "bqfmt": "",
                    "bafmt": "",
                    "bfont": "",
                    "bsize": 0,
                }
            ],
            "flds": [
                _model_field(FIELD_HINT, 0),
                _model_field(FIELD_ANSWER, 1),
            ],
            "css": _CSS,
            "latexPre": _LATEX_PRE,
            "latexPost": _LATEX_POST,
            "latexsvg": False,
            "req": [[0, "any", [0]]],
            "tags": [],
            "vers": [],
        }
    }


def _model_field(name: str, order: int) -> dict[str, Any]:
    return {
        "name": name,
        "ord": order,
        "sticky": False,
        "rtl": False,
        "font": "Arial",
        "size": 20,
        "media": [],
    }


def _decks(deck_ids: dict[str, int], modified_ms: int) -> dict[str, Any]:
    return {
        str(deck_id): {
            "id": deck_id,
            "name": name,
            "desc": "",
            "mod": modified_ms // 1000,
            "usn": 0,
            "collapsed": False,
            "browserCollapsed": False,
            "newToday": [0, 0],
            "revToday": [0, 0],
            "lrnToday": [0, 0],
            "timeToday": [0, 0],
            "conf": 1,
            "dyn": 0,
            "extendNew": 10,
            "extendRev": 50,
        }
        for name, deck_id in deck_ids.items()
    }


def _deck_configs() -> dict[str, Any]:
    return {
        "1": {
            "id": 1,
            "name": "Default",
            "mod": 0,
            "usn": 0,
            "maxTaken": 60,
            "autoplay": True,
            "timer": 0,
            "replayq": True,
            "dyn": False,
            "new": {
                "bury": False,
                "delays": [1, 10],
                "initialFactor": 2500,
                "ints": [1, 4, 7],
                "order": 1,
                "perDay": 20,
            },
            "rev": {
                "bury": False,
                "ease4": 1.3,
                "fuzz": 0.05,
                "ivlFct": 1,
                "maxIvl": 36500,
                "minSpace": 1,
                "perDay": 200,
            },
            "lapse": {
                "delays": [10],
                "leechAction": 0,
                "leechFails": 8,
                "minInt": 1,
                "mult": 0,
            },
        }
    }


def _clean_field(value: str) -> str:
    """Strip the two characters that would corrupt the flds encoding."""
    return value.replace(FIELD_SEPARATOR, "").replace("\x00", "")


def _checksum(sort_field: str) -> int:
    """Anki's note checksum: first 8 hex digits of sha1(sort field)."""
    return int(hashlib.sha1(sort_field.encode("utf-8")).hexdigest()[:8], 16)


def _guid(question_id: int, seen: set[str]) -> str:
    """Deterministic per question (re-export = update, not duplicate), but
    guaranteed unique inside one package."""
    salt = 0
    while True:
        seed = f"cairn:question:{question_id}:{salt}"
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        guid = "".join(_BASE91[byte % len(_BASE91)] for byte in digest[:10])
        if guid not in seen:
            seen.add(guid)
            return guid
        salt += 1


def _dumps(payload: Any) -> str:
    """UTF-8 JSON with Chinese written as-is (never ``\\uXXXX``)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
