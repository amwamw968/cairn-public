"""Collection ownership, durable snapshots and portable-package boundaries."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from cairn import collection_package, collection_service, export_service, import_service
from cairn.models import (Collection, CollectionItem, CollectionPracticeItem, CollectionVersion, CollectionVersionQuestion,
                          ErrorItem, Learner, Question, RecapAttempt, init_db, make_engine, make_session_factory)
from cairn.web import create_app


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'collections.db'}")
    init_db(engine)
    result = make_session_factory(engine)
    with result() as session:
        session.add_all([Learner(id=1, name="One"), Learner(id=2, name="Two")])
        session.add_all([
            Question(id=1, learner_id=1, subject="数学", grade_level="primary", question_type="计算",
                     content="  1 + 2 = ?  ", answer="3", analysis="把两数相加。"),
            Question(id=2, learner_id=2, subject="数学", grade_level="primary", question_type="计算",
                     content="Private second learner", answer="4"),
        ])
        session.flush()
        session.add(ErrorItem(learner_id=1, question_id=1, confidence=1.0))
        session.commit()
    yield result
    engine.dispose()


def _new(session):
    return collection_service.create(session, 1, name="整数练习", subject="数学", tags=["加法"],
                                     license="CC-BY-4.0", author="Teacher", source="Original")


def _member(question_id=1):
    return {"question_id": question_id, "tags": ["计算"], "difficulty": "基础", "source": "Teacher"}


def test_export_rejects_foreign_collection_member_instead_of_omitting_it(factory):
    with factory() as session:
        collection = _new(session)
        session.add(CollectionItem(collection_id=collection.id, question_id=2, position=0,
                                   tags_json="[]", source="Invalid link"))
        session.commit()
        with pytest.raises(export_service.InconsistentLearnerData):
            export_service.build_export(session, 1)


@pytest.mark.parametrize("attempt_owner,item_owner", [(1, 2), (2, 1)])
def test_export_rejects_recap_with_inconsistent_owner(factory, attempt_owner, item_owner):
    with factory() as session:
        item = session.get(ErrorItem, 1)
        if item_owner == 2:
            item = ErrorItem(learner_id=2, question_id=2, confidence=1.0)
            session.add(item)
            session.flush()
        session.add(RecapAttempt(learner_id=attempt_owner, error_item_id=item.id,
                                 request_key="bad-link", status="pending", assessment="unknown",
                                 reference_hash="0" * 64))
        session.commit()
        with pytest.raises(export_service.InconsistentLearnerData):
            export_service.build_export(session, 1)


def test_version_is_immutable_and_package_contains_only_portable_text(factory):
    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        version = collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        collection_id, snapshot = collection.id, version.snapshot_json
        session.get(Question, 1).content = "Different question"
        session.commit()
        package = collection_package.export_package(session, 1, collection_id, 1)
        assert session.get(CollectionVersion, version.id).snapshot_json == snapshot
        with pytest.raises(collection_service.CollectionError):
            collection_package.export_package(session, 2, collection_id, 1)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "items.jsonl"}
        manifest = json.loads(archive.read("manifest.json"))
        item = json.loads(archive.read("items.jsonl"))
        assert manifest["license"] == "CC-BY-4.0" and manifest["author"] == "Teacher"
        assert item["content"] == "  1 + 2 = ?  "
        assert "learner_id" not in item and "question_id" not in item
        assert "Private second learner" not in archive.read("items.jsonl").decode()


def test_runtime_reads_only_an_owned_valid_immutable_snapshot(factory):
    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        version = collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        session.get(Question, 1).content = "Changed after publication"
        session.flush()
        document, ids = collection_package.validated_version(session, 1, version.id)
        assert document["items"][0]["content"] == "  1 + 2 = ?  " and ids == [1]
        with pytest.raises(collection_service.CollectionError):
            collection_package.validated_version(session, 2, version.id)
        version.snapshot_json += " "
        session.flush()
        with pytest.raises(collection_service.CollectionError, match="校验失败"):
            collection_package.validated_version(session, 1, version.id)


def test_collection_workflow_copies_snapshot_once_and_restores_provenance(factory):
    from datetime import datetime, timezone
    from cairn.workflow.collection_runner import add_version_to_practice

    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        version = collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        steps = [{"node_id": "collection_source", "type": "auto", "params": {"version_id": version.id}},
                 {"node_id": "strategy", "type": "auto", "params": {"strategy": "leitner"}},
                 {"node_id": "guided_tutor", "type": "auto", "params": {}}]
        session.get(Question, 1).content = "Edited after publication"
        assert add_version_to_practice(session, 1, steps) == (1, 0)
        session.commit()
        mapping = session.scalar(select(CollectionPracticeItem))
        copy = session.get(Question, mapping.question_id)
        entry = session.scalar(select(ErrorItem).where(ErrorItem.question_id == copy.id))
        assert copy.content == "  1 + 2 = ?  " and copy.id != 1
        assert entry.wrong_count == 0 and entry.workflow_strategy_id == "leitner" and entry.workflow_tutor_enabled == 1
        entry.last_reviewed_at = datetime(2026, 11, 1, tzinfo=timezone.utc)
        session.commit()
        assert add_version_to_practice(session, 1, steps) == (0, 1)
        session.commit()
        changed = [steps[0], {"node_id": "strategy", "type": "auto", "params": {"strategy": "fsrs"}}]
        with pytest.raises(collection_service.CollectionError, match="策略"):
            add_version_to_practice(session, 1, changed)
        session.rollback()
        with pytest.raises(collection_service.CollectionError):
            add_version_to_practice(session, 2, steps)
        session.rollback()
        with pytest.raises(collection_service.CollectionError, match="练习副本"):
            collection_service.remove(session, 1, collection.id, collection.revision)
        session.rollback()
        document = export_service.build_export(session, 1)

    engine = make_engine("sqlite://")
    init_db(engine)
    try:
        with make_session_factory(engine)() as target:
            import copy as copy_module
            damaged = copy_module.deepcopy(document)
            copied_id = damaged["learning_collection_practice_item"][0]["question_id"]
            damaged["error_item"] = [item for item in damaged["error_item"] if item["question_id"] != copied_id]
            with pytest.raises(import_service.BackupError, match="题集练习来源"):
                import_service.load_backup(target, damaged)
            assert target.scalar(select(Question.id)) is None
            import_service.load_backup(target, document)
            assert add_version_to_practice(target, 1, steps) == (0, 1)
            target.commit()
            assert len(list(target.scalars(select(CollectionPracticeItem)))) == 1
    finally:
        engine.dispose()


def test_concurrent_collection_starts_create_one_copy(factory):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from cairn.workflow.collection_runner import add_version_to_practice

    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        version = collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        steps = [{"node_id": "collection_source", "type": "auto", "params": {"version_id": version.id}}]
    barrier = Barrier(2)
    def start():
        with factory() as session:
            barrier.wait(timeout=5)
            result = add_version_to_practice(session, 1, steps)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start) for _ in range(2)]
        assert sorted(f.result(timeout=10) for f in futures) == [(0, 1), (1, 0)]
    with factory() as session:
        assert len(list(session.scalars(select(CollectionPracticeItem)))) == 1


def test_foreign_doubtful_or_stale_selection_rolls_back(factory):
    with factory() as session:
        collection = _new(session)
        session.commit()
        cid = collection.id
        with pytest.raises(collection_service.CollectionError):
            collection_service.replace_items(session, 1, cid, 0, [_member(2)])
        session.rollback()
        assert session.get(Collection, cid).revision == 0
        session.get(ErrorItem, 1).confidence = 0.5
        session.commit()
        with pytest.raises(collection_service.CollectionError, match="确认"):
            collection_service.replace_items(session, 1, cid, 0, [_member()])
        session.rollback()
        session.get(ErrorItem, 1).confidence = 1.0
        session.commit()
        collection_service.replace_items(session, 1, cid, 0, [_member()])
        session.commit()
        with pytest.raises(collection_service.CollectionError, match="修改"):
            collection_service.replace_items(session, 1, cid, 0, [])
        session.rollback()
        assert len(list(session.scalars(select(CollectionItem)))) == 1


def test_import_is_separate_and_backup_roundtrip_preserves_snapshot(factory, tmp_path):
    import sqlite3
    from cairn.anki_export import build_anki_package
    def anki_count(session):
        with zipfile.ZipFile(io.BytesIO(build_anki_package(session, 2))) as package:
            path = tmp_path / "anki.db"
            path.write_bytes(package.read("collection.anki2"))
        with sqlite3.connect(path) as db:
            return db.execute("SELECT count(*) FROM notes").fetchone()[0]
    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        package = collection_package.export_package(session, 1, collection.id, 1)
        imported = collection_package.import_package(session, 2, package)
        session.commit()
        assert imported.learner_id == 2
        assert session.scalar(select(ErrorItem.id).where(ErrorItem.learner_id == 2)) is None
        assert anki_count(session) == 1
        document = export_service.build_export(session, 2)
        assert len(document["learning_collection"]) == 1
        assert len(document["learning_collection_version_question"]) == 1
    engine = make_engine("sqlite://")
    init_db(engine)
    try:
        with make_session_factory(engine)() as target:
            import_service.load_backup(target, document)
            restored = collection_package.export_package(target, 2, imported.id, 1)
            assert collection_package.read_package(restored) == collection_package.read_package(package)
            from cairn import practice_service
            from datetime import datetime, timezone
            assert collection_service.add_to_practice(target, 2, imported.id, 0) == (1, 0)
            target.commit()
            item = target.scalar(select(ErrorItem).where(ErrorItem.learner_id == 2))
            assert item.wrong_count == 0
            assert item.id in [p.error_item_id for p in practice_service.fetch_practice(target, 2, datetime.now(timezone.utc))]
            assert anki_count(target) == 2
            assert collection_service.add_to_practice(target, 2, imported.id, 1) == (0, 1)
            target.commit()
            assert len(list(target.scalars(select(ErrorItem)))) == 1
    finally:
        engine.dispose()


def _zip(manifest, items, *, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("items.jsonl", items)
        if extra:
            archive.writestr(extra, "bad")
    return buf.getvalue()


def test_package_rejects_malicious_and_incomplete_inputs_before_writes(factory):
    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        package = collection_package.export_package(session, 1, collection.id, 1)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            manifest, items = archive.read("manifest.json"), archive.read("items.jsonl")
        bad_manifest = json.loads(manifest)
        bad_manifest["license"] = "unknown"
        invalid_unicode = json.loads(manifest)
        invalid_unicode["name"] = "\ud800"
        bad = [b"not-a-zip", _zip(manifest, items, extra="../photo.jpg"),
               _zip(json.dumps(bad_manifest).encode(), items), _zip(b'{"a":1,"a":2}', items),
               _zip(manifest, items + b'{}\n'), _zip(manifest, b"\xff\n"),
               _zip(json.dumps(invalid_unicode).encode(), items)]
        for candidate in bad:
            with pytest.raises(collection_service.CollectionError):
                collection_package.import_package(session, 2, candidate)
            assert session.scalar(select(Collection.id).where(Collection.learner_id == 2)) is None


def test_backup_rejects_corrupt_snapshots_or_mismatched_links_before_writes(factory):
    import copy
    import hashlib
    with factory() as session:
        collection = _new(session)
        collection_service.replace_items(session, 1, collection.id, 0, [_member()])
        collection_service.publish(session, 1, collection.id, 1)
        session.commit()
        document = export_service.build_export(session, 1)
    corrupt = copy.deepcopy(document)
    corrupt["learning_collection_version"][0]["snapshot_json"] += " "
    mismatched = copy.deepcopy(document)
    version = mismatched["learning_collection_version"][0]
    snapshot = json.loads(version["snapshot_json"])
    snapshot["question_ids"] = [999]
    version["snapshot_json"] = collection_service.canonical(snapshot)
    version["content_sha256"] = hashlib.sha256(version["snapshot_json"].encode()).hexdigest()
    foreign = copy.deepcopy(document)
    foreign["learning_collection_item"][0]["question_id"] = 999
    foreign_learning = copy.deepcopy(document)
    foreign_learning["error_item"][0]["learner_id"] = 999
    engine = make_engine("sqlite://")
    init_db(engine)
    try:
        with make_session_factory(engine)() as target:
            for candidate in (corrupt, mismatched, foreign, foreign_learning):
                with pytest.raises(import_service.BackupError):
                    import_service.load_backup(target, candidate)
                assert target.scalar(select(Question.id)) is None
                assert target.scalar(select(Collection.id)) is None
    finally:
        engine.dispose()


def test_deleting_question_revokes_versions_and_erases_snapshot(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'delete.db'}")
    with TestClient(app) as client:
        preflight = client.options("/api/collections/1", headers={"Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "DELETE", "Access-Control-Request-Headers": "authorization"})
        assert preflight.status_code == 200 and "DELETE" in preflight.headers["access-control-allow-methods"]
        with app.state.session_factory() as session:
            learner_id = app.state.learner_id
            question = Question(learner_id=learner_id, subject="数学", grade_level="primary", question_type="计算",
                                content="secret", answer="3")
            session.add(question)
            session.flush()
            session.add(ErrorItem(learner_id=learner_id, question_id=question.id, confidence=1.0))
            collection = collection_service.create(session, learner_id, name="C", subject="数学", tags=[],
                                                   license="private", author="A", source="S")
            collection_service.replace_items(session, learner_id, collection.id, 0, [_member(question.id)])
            version = collection_service.publish(session, learner_id, collection.id, 1)
            session.commit()
            question_id, collection_id, version_id = question.id, collection.id, version.id
        assert client.post(f"/questions/{question_id}/delete", follow_redirects=False).status_code == 303
        with app.state.session_factory() as session:
            version = session.get(CollectionVersion, version_id)
            assert version.revoked_at is not None and version.snapshot_json is None
            assert session.scalar(select(CollectionItem.id)) is None
            assert session.scalar(select(CollectionVersionQuestion.version_id)) is None
            with pytest.raises(collection_service.CollectionError):
                collection_package.export_package(session, learner_id, collection_id, 1)


def test_operator_api_create_publish_download_and_import(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "operator-test")
    app = create_app(f"sqlite:///{tmp_path / 'api.db'}")
    headers = {"Authorization": "Bearer operator-test"}
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            question = Question(learner_id=app.state.learner_id, subject="数学", grade_level="primary",
                                question_type="计算", content="2 + 5", answer="7")
            session.add(question)
            session.flush()
            session.add(ErrorItem(learner_id=app.state.learner_id, question_id=question.id, confidence=1.0))
            session.commit()
            qid = question.id
        assert client.get("/api/collections").status_code == 401
        assert client.post("/api/collections/import", content=b"invalid").status_code == 401
        assert client.post("/api/collections/import", headers=headers, content=b"invalid").status_code == 415
        assert client.post("/api/collections/import", headers={**headers, "Content-Type": "application/zip"},
                           content=b"0" * (collection_package.MAX_PACKAGE + 1)).status_code == 413
        assert client.get("/api/collection-candidates?subject=数学", headers=headers).json()["items"][0]["id"] == qid
        created = client.post("/api/collections", headers=headers, json={
            "name": "我的题集", "subject": "数学", "tags": ["计算"], "license": "private",
            "author": "家长", "source": "自编"})
        assert created.status_code == 200
        cid = created.json()["id"]
        selected = client.post(f"/api/collections/{cid}/items", headers=headers,
                               json={"expected_revision": 0, "items": [_member(qid)]})
        assert selected.status_code == 200 and selected.json()["revision"] == 1
        conflict = client.post(f"/api/collections/{cid}/items", headers=headers,
                               json={"expected_revision": 0, "items": []})
        assert conflict.status_code == 409
        published = client.post(f"/api/collections/{cid}/publish", headers=headers,
                                json={"expected_revision": 1})
        assert published.status_code == 200 and published.json()["number"] == 1
        detail = client.get(f"/api/collections/{cid}", headers=headers).json()
        assert detail["items"][0]["content"] == "2 + 5" and detail["versions"][0]["number"] == 1
        package = client.get(f"/api/collections/{cid}/versions/1/package", headers=headers)
        assert package.status_code == 200 and package.headers["content-type"] == "application/zip"
        imported = client.post("/api/collections/import", headers={**headers, "Content-Type": "application/zip"},
                               content=package.content)
        assert imported.status_code == 200 and imported.json()["id"] != cid
        assert len(client.get("/api/collections", headers=headers).json()["collections"]) == 2
        imported_id = imported.json()["id"]
        added = client.post(f"/api/collections/{imported_id}/add-to-practice", headers=headers,
                            json={"expected_revision": 0})
        assert added.json() == {"added": 1, "unchanged": 0, "revision": 1}
        again = client.post(f"/api/collections/{imported_id}/add-to-practice", headers=headers,
                            json={"expected_revision": 1})
        assert again.json() == {"added": 0, "unchanged": 1, "revision": 2}
        renamed = client.post(f"/api/collections/{imported_id}/metadata", headers=headers, json={
            "expected_revision": 2, "name": "已修改", "tags": [], "license": "private", "author": "A", "source": "S"})
        assert renamed.status_code == 200 and renamed.json()["revision"] == 3
        assert collection_package.read_package(package.content)["manifest"]["name"] == "我的题集"
        assert client.delete(f"/api/collections/{imported_id}?expected_revision=3", headers=headers).status_code == 200
        with app.state.session_factory() as session:
            assert len(list(session.scalars(select(Question)))) == 2
            assert len(list(session.scalars(select(ErrorItem)))) == 2


def test_concurrent_collections_cannot_duplicate_learning_records(factory, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with factory() as session:
        session.add(Question(id=3, learner_id=1, subject="数学", grade_level="primary", question_type="计算", content="Asset"))
        first, second = _new(session), _new(session)
        collection_service.replace_items(session, 1, first.id, 0, [_member(3)])
        collection_service.replace_items(session, 1, second.id, 0, [_member(3)])
        session.commit()
        ids = [first.id, second.id]
    barrier = Barrier(2)
    original = collection_service._claim
    def synchronized(*args):
        barrier.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(collection_service, "_claim", synchronized)
    def submit(cid):
        with factory() as session:
            result = collection_service.add_to_practice(session, 1, cid, 1)
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ids))
    assert sorted(results) == [(0, 1), (1, 0)]
    with factory() as session:
        assert len(list(session.scalars(select(ErrorItem).where(ErrorItem.question_id == 3)))) == 1


def test_streamed_import_pins_learner_before_receiving_body(tmp_path, monkeypatch):
    import asyncio
    import httpx
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "operator-test")
    app = create_app(f"sqlite:///{tmp_path / 'streamed.db'}")
    with TestClient(app):
        with app.state.session_factory() as session:
            learner_id = app.state.learner_id
            session.add(Learner(id=999, name="Other"))
            question = Question(learner_id=learner_id, subject="数学", grade_level="primary", question_type="计算", content="Test")
            session.add(question); session.flush()
            collection = collection_service.create(session, learner_id, name="Test", subject="数学", tags=[],
                                                   license="private", author="A", source="S")
            collection_service.replace_items(session, learner_id, collection.id, 0, [_member(question.id)])
            collection_service.publish(session, learner_id, collection.id, 1)
            session.commit()
            package = collection_package.export_package(session, learner_id, collection.id, 1)
        async def send():
            async def data():
                yield package[:10]
                app.state.learner_id = 999
                yield package[10:]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/api/collections/import", content=data(), headers={
                    "Authorization": "Bearer operator-test", "Content-Type": "application/zip"})
        response = asyncio.run(send())
        assert response.status_code == 200
        with app.state.session_factory() as session:
            assert session.get(Collection, response.json()["id"]).learner_id == learner_id
