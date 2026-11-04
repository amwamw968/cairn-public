"""Bounded text-only collection packages; never extract archive paths."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
import zlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from cairn.collection_service import CollectionError, LICENSES, MAX_ITEMS, _string, _tags, canonical, create
from cairn.models import Collection, CollectionItem, CollectionVersion, CollectionVersionQuestion, Question

MAX_PACKAGE = 8 * 1024 * 1024
MAX_MANIFEST = 32 * 1024
MAX_CONTENT = 16 * 1024 * 1024
MAX_LINE = 32 * 1024
_MANIFEST_KEYS = {"format_version", "name", "subject", "tags", "license", "author", "source", "version", "item_count", "generated_at"}
_ITEM_KEYS = {"subject", "grade_level", "question_type", "content", "answer", "analysis", "tags", "difficulty", "source"}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CollectionError("JSON 含重复字段")
        result[key] = value
    return result


def _load_json(data: bytes):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(CollectionError("JSON 数值无效")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CollectionError("题集包不是有效的 UTF-8 JSON") from exc


def _optional(value, name: str, limit: int):
    return None if value is None else _string(value, name, limit, required=False)


def _text(value, name: str, limit: int, *, required: bool = False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise CollectionError(f"{name} 长度或格式无效")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CollectionError(f"{name} 包含无效字符") from exc
    return value


def validate(manifest: object, items: object) -> dict:
    """Validate the complete portable document before any database writes."""
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise CollectionError("题集清单字段无效")
    if type(manifest["format_version"]) is not int or manifest["format_version"] != 1:
        raise CollectionError("题集格式版本不受支持")
    if type(manifest["version"]) is not int or manifest["version"] < 1 or manifest["version"] > 1_000_000:
        raise CollectionError("题集版本无效")
    if not isinstance(items, list) or not 0 < len(items) <= MAX_ITEMS:
        raise CollectionError("题集条目数量无效")
    if type(manifest["item_count"]) is not int or manifest["item_count"] != len(items):
        raise CollectionError("题目数量与清单不符")
    if not isinstance(manifest["license"], str) or manifest["license"] not in LICENSES:
        raise CollectionError("题集许可无效")
    try:
        created = datetime.fromisoformat(manifest["generated_at"])
        if created.utcoffset() is None:
            raise ValueError("timezone required")
    except (TypeError, ValueError, OverflowError) as exc:
        raise CollectionError("题集生成时间无效") from exc
    normalized_manifest = {
        "format_version": 1, "name": _string(manifest["name"], "题集名称", 120),
        "subject": _string(manifest["subject"], "题集学科", 32), "tags": _tags(manifest["tags"]),
        "license": manifest["license"], "author": _string(manifest["author"], "署名", 120),
        "source": _string(manifest["source"], "来源", 300), "version": manifest["version"],
        "item_count": len(items), "generated_at": manifest["generated_at"],
    }
    normalized_items = []
    for item in items:
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            raise CollectionError("题目字段无效")
        subject = _string(item["subject"], "题目学科", 32)
        if subject != normalized_manifest["subject"]:
            raise CollectionError("题目学科与题集不一致")
        normalized_items.append({
            "subject": subject, "grade_level": _string(item["grade_level"], "年级", 16),
            "question_type": _string(item["question_type"], "题型", 32),
            "content": _text(item["content"], "题干", 10000, required=True),
            "answer": _text(item["answer"], "答案", 10000),
            "analysis": _text(item["analysis"], "解析", 10000),
            "tags": _tags(item["tags"]), "difficulty": _optional(item["difficulty"], "难度", 64),
            "source": _string(item["source"], "题目来源", 300),
        })
    return {"manifest": normalized_manifest, "items": normalized_items}


def read_package(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_PACKAGE:
        raise CollectionError("题集包太大或格式无效")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != 2 or {info.filename for info in infos} != {"manifest.json", "items.jsonl"}:
                raise CollectionError("题集包只能包含清单和题目文本")
            if any(info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16) for info in infos):
                raise CollectionError("题集包不能包含加密文件或链接")
            if any(info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for info in infos):
                raise CollectionError("题集包的压缩格式不受支持")
            if any(info.file_size > (MAX_MANIFEST if info.filename == "manifest.json" else MAX_CONTENT) for info in infos):
                raise CollectionError("题集内容太大")
            manifest = _load_json(archive.read("manifest.json"))
            raw_lines = archive.read("items.jsonl").splitlines()
            if len(raw_lines) > MAX_ITEMS or any(not line or len(line) > MAX_LINE for line in raw_lines):
                raise CollectionError("题目行数或长度无效")
            return validate(manifest, [_load_json(line) for line in raw_lines])
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError, NotImplementedError, OSError, zlib.error) as exc:
        raise CollectionError("题集包无法读取") from exc


def _serialize(document: dict) -> bytes:
    manifest = canonical(document["manifest"]).encode("utf-8")
    lines = b"\n".join(canonical(item).encode("utf-8") for item in document["items"]) + b"\n"
    if len(manifest) > MAX_MANIFEST or len(lines) > MAX_CONTENT or any(len(line) > MAX_LINE for line in lines.splitlines()):
        raise CollectionError("题集内容太大")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("items.jsonl", lines)
    package = out.getvalue()
    if len(package) > MAX_PACKAGE:
        raise CollectionError("题集包太大")
    return package


def export_package(session: Session, learner_id: int, collection_id: int, version_number: int) -> bytes:
    version = session.scalar(select(CollectionVersion).join(Collection).where(
        Collection.id == collection_id, Collection.learner_id == learner_id,
        CollectionVersion.number == version_number))
    document, _ = validated_version(session, learner_id, version.id if version else None)
    return _serialize(document)


def validated_version(session: Session, learner_id: int, version_id: int | None) -> tuple[dict, list[int]]:
    """Read an owned immutable version only after checking its content and links."""
    version = session.scalar(select(CollectionVersion).join(Collection).where(
        CollectionVersion.id == version_id, Collection.learner_id == learner_id))
    if version is None or version.revoked_at is not None or version.snapshot_json is None:
        raise CollectionError("题集版本不存在或已撤销")
    raw = version.snapshot_json.encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != version.content_sha256:
        raise CollectionError("题集版本校验失败")
    snapshot = _load_json(raw)
    if not isinstance(snapshot, dict) or set(snapshot) != {"manifest", "items", "question_ids"}:
        raise CollectionError("题集版本格式无效")
    document = validate(snapshot["manifest"], snapshot["items"])
    ids = snapshot["question_ids"]
    if (document["manifest"]["version"] != version.number
            or not isinstance(ids, list) or len(ids) != len(document["items"])
            or any(type(qid) is not int or qid <= 0 for qid in ids) or len(set(ids)) != len(ids)):
        raise CollectionError("题集版本引用无效")
    linked = set(session.scalars(select(CollectionVersionQuestion.question_id).where(
        CollectionVersionQuestion.version_id == version.id)))
    owned = set(session.scalars(select(Question.id).where(Question.id.in_(ids), Question.learner_id == learner_id)))
    if linked != set(ids) or owned != set(ids):
        raise CollectionError("题集版本引用无效")
    return document, ids


def import_package(session: Session, learner_id: int, data: bytes) -> Collection:
    """Import a separate local collection, never change existing questions."""
    document = read_package(data)
    manifest, items = document["manifest"], document["items"]
    collection = create(session, learner_id, name=manifest["name"], subject=manifest["subject"],
                        tags=manifest["tags"], license=manifest["license"],
                        author=manifest["author"], source=manifest["source"])
    questions = []
    for index, item in enumerate(items):
        question = Question(learner_id=learner_id, subject=item["subject"], grade_level=item["grade_level"],
                            question_type=item["question_type"], content=item["content"], answer=item["answer"],
                            analysis=item["analysis"], source="collection-import")
        session.add(question)
        session.flush()
        questions.append(question)
        session.add(CollectionItem(collection_id=collection.id, question_id=question.id, position=index,
                                   tags_json=canonical(item["tags"]), difficulty=item["difficulty"],
                                   source=item["source"]))
    payload = canonical({**document, "question_ids": [q.id for q in questions]})
    version = CollectionVersion(collection_id=collection.id, number=manifest["version"], snapshot_json=payload,
                                content_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest())
    session.add(version)
    session.flush()
    session.add_all([CollectionVersionQuestion(version_id=version.id, question_id=q.id) for q in questions])
    session.flush()
    return collection
