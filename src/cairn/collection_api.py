"""Operator-only collection authoring and portable package endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import exists, func, select
from starlette.concurrency import run_in_threadpool

from cairn import collection_package, collection_service
from cairn.models import Collection, CollectionItem, CollectionVersion, ErrorItem, Question
from cairn.workflow.api import _json_body, _operator
from cairn.workflow.review import ready_for_practice

router = APIRouter(prefix="/api", tags=["collection-authoring"])


def _error(exc: collection_service.CollectionError) -> HTTPException:
    return HTTPException(status_code=409 if "其他位置修改" in str(exc) or "正在使用" in str(exc) else 400, detail=str(exc))


def _summary(collection: Collection) -> dict:
    return {"id": collection.id, "name": collection.name, "subject": collection.subject,
            "tags": json.loads(collection.tags_json), "license": collection.license,
            "author": collection.author, "source": collection.source, "revision": collection.revision}


@router.get("/collection-candidates")
def collection_candidates(request: Request, subject: str, offset: int = 0, limit: int = 50):
    _operator(request)
    if not subject.strip() or len(subject) > 32 or offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(status_code=400, detail="筛选条件无效")
    learner_id = request.app.state.learner_id
    with request.app.state.session_factory() as session:
        doubtful = exists(select(ErrorItem.id).where(
            ErrorItem.question_id == Question.id, ErrorItem.learner_id == learner_id,
            ErrorItem.last_reviewed_at.is_(None), ErrorItem.confidence < 1.0,
        ))
        rows = list(session.scalars(select(Question).where(
            Question.learner_id == learner_id, Question.subject == subject.strip(),
            ready_for_practice(Question.id), ~doubtful,
        ).order_by(Question.id).offset(offset).limit(limit + 1)))
        return {"items": [{"id": q.id, "content": q.content, "question_type": q.question_type}
                          for q in rows[:limit]], "has_more": len(rows) > limit}


@router.get("/collections")
def list_collections(request: Request):
    _operator(request)
    with request.app.state.session_factory() as session:
        collections = list(session.scalars(select(Collection).where(
            Collection.learner_id == request.app.state.learner_id).order_by(Collection.id.desc())))
        return {"collections": [dict(_summary(c), item_count=session.scalar(select(func.count()).select_from(
            CollectionItem).where(CollectionItem.collection_id == c.id))) for c in collections]}


@router.get("/collection-versions")
def list_collection_versions(request: Request):
    _operator(request)
    with request.app.state.session_factory() as session:
        rows = list(session.execute(select(CollectionVersion, Collection).join(
            Collection, Collection.id == CollectionVersion.collection_id).where(
            Collection.learner_id == request.app.state.learner_id,
            CollectionVersion.revoked_at.is_(None), CollectionVersion.snapshot_json.is_not(None)
        ).order_by(Collection.id.desc(), CollectionVersion.number.desc())))
        versions = []
        for version, _collection in rows:
            try:
                document, _ = collection_package.validated_version(session, request.app.state.learner_id, version.id)
            except collection_service.CollectionError:
                continue
            manifest = document["manifest"]
            versions.append({"id": version.id, "number": version.number,
                             "name": manifest["name"], "subject": manifest["subject"]})
        return {"versions": versions}


@router.post("/collections")
async def create_collection(request: Request):
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    with request.app.state.session_factory() as session:
        try:
            collection = collection_service.create(session, learner_id,
                name=body.get("name"), subject=body.get("subject"), tags=body.get("tags"),
                license=body.get("license"), author=body.get("author"), source=body.get("source"))
            session.commit()
            return _summary(collection)
        except collection_service.CollectionError as exc:
            session.rollback()
            raise _error(exc) from exc


@router.post("/collections/import")
async def import_collection(request: Request):
    _operator(request)
    learner_id = request.app.state.learner_id
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
        raise HTTPException(status_code=415, detail="请上传 ZIP 题集包")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > collection_package.MAX_PACKAGE:
            raise HTTPException(status_code=413, detail="题集包太大")
        chunks.extend(chunk)
    data = bytes(chunks)
    def save():
        with request.app.state.session_factory() as session:
            try:
                collection = collection_package.import_package(session, learner_id, data)
                session.commit()
                return _summary(collection)
            except Exception:
                session.rollback()
                raise
    try:
        return await run_in_threadpool(save)
    except collection_service.CollectionError as exc:
        raise _error(exc) from exc


@router.get("/collections/{collection_id}")
def get_collection(request: Request, collection_id: int):
    _operator(request)
    with request.app.state.session_factory() as session:
        try:
            collection = collection_service._owned(session, request.app.state.learner_id, collection_id)
            rows = list(session.execute(select(CollectionItem, Question).join(
                Question, Question.id == CollectionItem.question_id).where(
                CollectionItem.collection_id == collection_id, Question.learner_id == collection.learner_id
            ).order_by(CollectionItem.position, CollectionItem.id)))
            versions = list(session.scalars(select(CollectionVersion).where(
                CollectionVersion.collection_id == collection_id).order_by(CollectionVersion.number.desc())))
            return {**_summary(collection), "items": [{"question_id": q.id, "content": q.content,
                "tags": json.loads(item.tags_json), "difficulty": item.difficulty, "source": item.source}
                for item, q in rows], "versions": [{"number": v.number, "revoked": v.revoked_at is not None,
                    "published_at": v.published_at.isoformat()} for v in versions]}
        except collection_service.CollectionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/collections/{collection_id}/items")
async def save_collection_items(request: Request, collection_id: int):
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    with request.app.state.session_factory() as session:
        try:
            collection = collection_service.replace_items(session, learner_id, collection_id,
                                                           body.get("expected_revision"), body.get("items"))
            session.commit()
            return _summary(collection)
        except collection_service.CollectionError as exc:
            session.rollback()
            raise _error(exc) from exc


@router.post("/collections/{collection_id}/metadata")
async def save_collection_metadata(request: Request, collection_id: int):
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    with request.app.state.session_factory() as session:
        try:
            collection = collection_service.update_metadata(session, learner_id, collection_id,
                body.get("expected_revision"), name=body.get("name"), tags=body.get("tags"),
                license=body.get("license"), author=body.get("author"), source=body.get("source"))
            session.commit()
            return _summary(collection)
        except collection_service.CollectionError as exc:
            session.rollback()
            raise _error(exc) from exc


@router.delete("/collections/{collection_id}")
def delete_collection(request: Request, collection_id: int, expected_revision: int):
    _operator(request)
    with request.app.state.session_factory() as session:
        try:
            collection_service.remove(session, request.app.state.learner_id, collection_id, expected_revision)
            session.commit()
            return {"deleted": True}
        except collection_service.CollectionError as exc:
            session.rollback()
            raise _error(exc) from exc


@router.post("/collections/{collection_id}/publish")
async def publish_collection(request: Request, collection_id: int):
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    with request.app.state.session_factory() as session:
        try:
            version = collection_service.publish(session, learner_id, collection_id,
                                                 body.get("expected_revision"))
            session.commit()
            return {"number": version.number, "revision": body["expected_revision"] + 1}
        except collection_service.CollectionError as exc:
            session.rollback()
            raise _error(exc) from exc


@router.post("/collections/{collection_id}/add-to-practice")
async def add_collection_to_practice(request: Request, collection_id: int):
    _operator(request)
    learner_id = request.app.state.learner_id
    body = await _json_body(request)
    with request.app.state.session_factory() as session:
        try:
            added, unchanged = collection_service.add_to_practice(session, learner_id,
                collection_id, body.get("expected_revision"))
            session.commit()
            return {"added": added, "unchanged": unchanged, "revision": body["expected_revision"] + 1}
        except collection_service.CollectionError as exc:
            session.rollback()
            raise _error(exc) from exc


@router.get("/collections/{collection_id}/versions/{number}/package")
def export_collection(request: Request, collection_id: int, number: int):
    _operator(request)
    with request.app.state.session_factory() as session:
        try:
            package = collection_package.export_package(session, request.app.state.learner_id, collection_id, number)
        except collection_service.CollectionError as exc:
            raise _error(exc) from exc
    return Response(package, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="cairn-collection-{collection_id}-v{number}.zip"',
        "Cache-Control": "no-store"})
