"""Exercise durable workflow transitions against an isolated SQLite database."""

import json
import io
import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn.models import init_db, make_engine, make_session_factory
from cairn import export_service, import_service, review_service, scan_service
from cairn.capture.base import DeviceInfo, Frame
from cairn.models.error_item import ErrorItem
from cairn.models.learner import Learner
from cairn.models.question import Question
from cairn.models.scan import Scan, ScanQuestion
from cairn.models.workflow import Workflow, WorkflowVersion
from cairn.models.workflow_run import WorkflowReviewItem, WorkflowRunStep
from cairn.ocr.cut import QuestionBlock
from cairn.workflow import execution
from cairn.workflow.learning import apply_strategy
from cairn.workflow.contracts import validate_scan_runtime
from cairn.workflow.images import select_image
from cairn.workflow.human_actions import crop_confirmed, selection_confirmed
from cairn.workflow.scan_handlers import handlers as scan_handlers
from cairn.workflow import review as workflow_review
from cairn.workflow.service import (WorkflowError, default_executable_graph, ensure_default_workflow,
                                    ensure_runnable_default, validate_for_publish)
from cairn.web import create_app


@pytest.fixture
def factory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
    init_db(engine)
    result = make_session_factory(engine)
    with result() as session:
        session.add(Workflow(id="flow", title="Flow", draft_json="{}", revision=1,
                             active_version_id="flow:v1"))
        session.flush()
        for number in (1, 2):
            version_id = f"flow:v{number}"
            steps = [
                {"node_id": "first", "type": "auto", "params": {"version": number}},
                {"node_id": "confirm", "type": "human", "params": {}},
                {"node_id": "last", "type": "auto", "params": {}},
            ]
            session.add(WorkflowVersion(version_id=version_id, workflow_id="flow", number=number,
                                        graph_json="{}", runtime_json=json.dumps({"version_id": version_id, "steps": steps})))
        session.flush()
        session.add_all([
            Scan(device_id="d", device_name="d", width=1, height=1, path=f"{n}.jpg",
                 workflow_version_id=f"flow:v{n}") for n in (1, 2)
        ])
        session.commit()
    yield result
    engine.dispose()


def test_pinned_versions_pause_and_resume_across_sessions(factory):
    def first(session, scan, params):
        scan.note = str(params["version"])

    def last(session, scan, params):
        scan.note += ":done"

    with factory() as session:
        run = execution.start(session, session.get(Scan, 1))
        execution.advance(session, run, {"first": first, "last": last})
        assert (run.status, run.cursor) == ("waiting", 1)
        session.commit()
        run_id = run.id
    with factory() as session:
        run = session.get(execution.WorkflowRun, run_id)
        assert run.version_id == "flow:v1"
        execution.resume(session, run, "confirm", lambda s, scan, p: None,
                         {"first": first, "last": last})
        session.commit()
        assert (run.status, run.cursor, session.get(Scan, 1).note) == ("completed", 3, "1:done")
        assert [row.status for row in session.scalars(select(WorkflowRunStep).where(
            WorkflowRunStep.run_id == run.id).order_by(WorkflowRunStep.step_index))] == ["completed"] * 3
        with pytest.raises(execution.RunError):
            execution.resume(session, run, "confirm", lambda s, scan, p: None, {})


def test_failed_step_rolls_back_side_effect_and_can_retry(factory):
    def fail(session, scan, params):
        scan.note = "must roll back"
        session.flush()
        raise RuntimeError("private details")

    with factory() as session:
        scan = session.get(Scan, 2)
        run = execution.start(session, scan)
        execution.advance(session, run, {"first": fail})
        assert run.status == "failed"
        assert "private details" not in run.error
        assert scan.note is None
        session.commit()
        with pytest.raises(execution.RunError):
            execution.advance(session, run, {"first": lambda s, scan, p: None})
        execution.advance(session, run, {"first": lambda s, scan, p: setattr(scan, "note", "retried")}, retry=True)
        session.commit()
        assert (run.status, run.cursor, scan.note) == ("waiting", 1, "retried")


def test_stale_session_cannot_repeat_human_action(factory):
    with factory() as session:
        run = execution.start(session, session.get(Scan, 1))
        execution.advance(session, run, {"first": lambda s, scan, p: None})
        session.commit()
        run_id = run.id
    with factory() as first, factory() as second:
        current = first.get(execution.WorkflowRun, run_id)
        stale = second.get(execution.WorkflowRun, run_id)
        execution.resume(first, current, "confirm", lambda s, scan, p: None,
                         {"last": lambda s, scan, p: None})
        first.commit()
        with pytest.raises(execution.RunError, match="其他设备"):
            execution.resume(second, stale, "confirm", lambda s, scan, p: None,
                             {"last": lambda s, scan, p: None})
        second.rollback()


def test_import_interruption_does_not_leave_unlinked_question(factory, monkeypatch):
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        session.add(ScanQuestion(scan_id=1, no=1, bbox_json="{}", stem_text="计算下面的数学问题"))
        session.commit()

        def interrupted():
            raise RuntimeError("simulated interruption")

        monkeypatch.setattr(session, "commit", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            scan_service.import_wrong_questions(session, 1, session.get(Scan, 1), [1],
                                                subject="数学", question_type="填空")
        session.rollback()
        assert session.scalar(select(Question.id)) is None
        assert session.scalar(select(ScanQuestion.question_id)) is None


def test_pinned_strategy_controls_actual_review_schedule(factory):
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        scan = session.get(Scan, 1)
        scan.learner_id = 1
        session.add(ScanQuestion(scan_id=1, no=1, bbox_json="{}", stem_text="计算下面的数学问题"))
        session.commit()
        scan_service.import_wrong_questions(session, 1, scan, [1], subject="数学", question_type="填空")
        apply_strategy(session, scan, {"strategy": "leitner"})
        session.commit()
        item = session.scalar(select(ErrorItem))
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        review = review_service.apply_review(session, item, "good", now=now)
        assert review.next_due_at.date() == (now + timedelta(days=3)).date()
        with pytest.raises(ValueError, match="已复习"):
            apply_strategy(session, scan, {"strategy": "fsrs"})
        assert item.workflow_strategy_id == "leitner"


def test_collection_node_requires_review_and_valid_collection_parameter():
    graph = default_executable_graph()
    graph["nodes"].append({"id": "collection-1", "node_id": "collection_append", "params": {}})
    graph["edges"].append({"source": "review-1", "target": "collection-1"})
    with pytest.raises(WorkflowError, match="题集"):
        validate_for_publish(graph)
    graph["nodes"][-1]["params"] = {"collection_id": 1}
    validate_for_publish(graph)
    graph["edges"][-1] = {"source": "select-1", "target": "collection-1"}
    graph["edges"] = [edge for edge in graph["edges"] if edge != {"source": "select-1", "target": "recognize-1"}]
    graph["edges"].append({"source": "collection-1", "target": "recognize-1"})
    with pytest.raises(WorkflowError, match="人工复核"):
        validate_for_publish(graph)


def test_collection_node_rejects_mismatches_and_does_not_duplicate(factory):
    from cairn import collection_service
    from cairn.models import CollectionItem, CollectionVersion
    from cairn.workflow.learning import append_confirmed_to_collection

    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        scan = session.get(Scan, 1)
        scan.learner_id = 1
        run = execution.start(session, scan)
        q1 = Question(learner_id=1, subject="英语", grade_level="primary", question_type="填空", content="First")
        q2 = Question(learner_id=1, subject="数学", grade_level="primary", question_type="填空", content="Rejected")
        q3 = Question(learner_id=1, subject="数学", grade_level="primary", question_type="填空", content="Existing")
        session.add_all([q1, q2, q3]); session.flush()
        session.add_all([ErrorItem(learner_id=1, question_id=q1.id, confidence=1.0),
                         ErrorItem(learner_id=1, question_id=q2.id, confidence=1.0, workflow_rejected=1),
                         ErrorItem(learner_id=1, question_id=q3.id, confidence=1.0)])
        session.add_all([WorkflowReviewItem(run_id=run.id, question_id=q1.id, status="confirmed"),
                         WorkflowReviewItem(run_id=run.id, question_id=q2.id, status="rejected")])
        target = collection_service.create(session, 1, name="Math", subject="数学", tags=[],
                                           license="private", author="A", source="S")
        collection_service.replace_items(session, 1, target.id, 0,
            [{"question_id": q3.id, "tags": [], "difficulty": None, "source": "S"}])
        version = collection_service.publish(session, 1, target.id, 1)
        frozen = version.snapshot_json
        session.commit()
        with pytest.raises(collection_service.CollectionError, match="学科"):
            append_confirmed_to_collection(session, scan, {"collection_id": target.id})
        session.rollback()
        assert list(session.scalars(select(CollectionItem.question_id))) == [q3.id]
        session.get(Question, q1.id).subject = "数学"
        session.commit()
        append_confirmed_to_collection(session, scan, {"collection_id": target.id})
        session.commit()
        append_confirmed_to_collection(session, scan, {"collection_id": target.id})
        session.commit()
        assert set(session.scalars(select(CollectionItem.question_id))) == {q1.id, q3.id}
        assert session.get(CollectionVersion, version.id).snapshot_json == frozen


def test_reader_can_skip_failed_collection_without_losing_reviewed_question(tmp_path, monkeypatch):
    from cairn import collection_service
    from cairn.models import CollectionItem
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    app = create_app(f"sqlite:///{tmp_path / 'skip-collection.db'}")
    headers = {"Authorization": "Bearer local-only"}
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            collection = collection_service.create(session, app.state.learner_id, name="C", subject="数学", tags=[],
                                                   license="private", author="A", source="S")
            session.commit()
            collection_id = collection.id
        current = client.get("/api/workflows/default").json()
        graph = current["graph"]
        graph["nodes"].append({"id": "collection-1", "node_id": "collection_append", "params": {"collection_id": collection_id}})
        graph["edges"].append({"source": "review-1", "target": "collection-1"})
        graph["nodes"].append({"id": "recap-1", "node_id": "text_recap", "params": {}})
        graph["edges"].append({"source": "collection-1", "target": "recap-1"})
        assert client.post("/api/workflows/default", headers=headers, json={"title": current["title"],
            "expected_revision": current["revision"], "graph": graph}).status_code == 200
        assert client.post("/api/workflows/default/publish", headers=headers).status_code == 200
        with app.state.session_factory() as session:
            learner_id = app.state.learner_id
            scan = Scan(learner_id=learner_id, device_id="d", device_name="d", width=1, height=1,
                        path="test.jpg", workflow_version_id="default:v3")
            question = Question(learner_id=learner_id, subject="数学", grade_level="primary", question_type="计算", content="1 + 2")
            session.add_all([scan, question]); session.flush()
            scan.question_id = question.id
            session.add(ErrorItem(learner_id=learner_id, question_id=question.id, confidence=1.0))
            run = execution.start(session, scan)
            rows = list(session.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run.id).order_by(WorkflowRunStep.step_index)))
            for row in rows[:-2]: row.status = "completed"
            rows[-2].status = "failed"
            run.cursor = len(rows) - 2; run.status = "failed"; run.error = "simulated"
            session.add(WorkflowReviewItem(run_id=run.id, question_id=question.id, status="confirmed"))
            session.commit()
            scan_id, question_id = scan.id, question.id
        page = client.get(f"/scans/{scan_id}/workflow-review?lang=en")
        assert page.status_code == 200 and "Skip collection, keep questions" in page.text
        skipped = client.post(f"/scans/{scan_id}/workflow-skip-collection", data={"lang": "en"}, follow_redirects=False)
        assert skipped.status_code == 303
        progress = client.get(f"/api/scans/{scan_id}/workflow").json()
        assert progress["status"] == "completed" and progress["steps"][-2]["status"] == "skipped"
        assert progress["steps"][-1]["status"] == "completed"
        assert client.post(f"/scans/{scan_id}/workflow-skip-collection").status_code == 409
        with app.state.session_factory() as session:
            from cairn import practice_service
            item = session.scalar(select(ErrorItem).where(ErrorItem.question_id == question_id))
            assert item.workflow_recap_enabled == 1
            assert practice_service.get_practice_item(session, learner_id, item.id) is not None
            assert session.scalar(select(CollectionItem.id)) is None


def test_scan_pinning_serializes_with_workflow_switch_and_collection_delete(tmp_path, monkeypatch):
    from cairn import collection_service
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    monkeypatch.setenv("CAIRN_MIN_IMAGE_SIDE", "1")
    app = create_app(f"sqlite:///{tmp_path / 'pin-delete-race.db'}")
    headers = {"Authorization": "Bearer local-only"}
    image = io.BytesIO(); Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            collection = collection_service.create(session, app.state.learner_id, name="C", subject="数学", tags=[],
                                                   license="private", author="A", source="S")
            session.commit(); cid = collection.id
        current = client.get("/api/workflows/default").json()
        graph = current["graph"]
        graph["nodes"].append({"id": "collection-1", "node_id": "collection_append", "params": {"collection_id": cid}})
        graph["edges"].append({"source": "review-1", "target": "collection-1"})
        assert client.post("/api/workflows/default", headers=headers, json={"title": current["title"],
            "expected_revision": current["revision"], "graph": graph}).status_code == 200
        assert client.post("/api/workflows/default/publish", headers=headers).status_code == 200
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        original = scan_service.save_upload
        def pause_after_pin(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return original(*args, **kwargs)
        monkeypatch.setattr(scan_service, "save_upload", pause_after_pin)
        def upload():
            return client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False)
        def switch_and_delete():
            response = client.post("/api/workflows/default/rollback/default:v2", headers=headers)
            switched.set()
            return response, client.delete(f"/api/collections/{cid}?expected_revision=0", headers=headers)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_upload = pool.submit(upload)
            assert entered.wait(10)
            pending_switch = pool.submit(switch_and_delete)
            try:
                assert not switched.wait(0.2), "activation must wait until the scan pin is committed"
            finally:
                release.set()
            saved = pending_upload.result(timeout=10)
            switched_response, deleted = pending_switch.result(timeout=10)
        assert saved.status_code == 303 and switched_response.status_code == 200
        assert deleted.status_code == 409
        scan_id = int(saved.headers["location"].split("/")[2])
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["version_id"] == "default:v3"


def test_slow_camera_capture_does_not_hold_database_writer_lock(tmp_path, monkeypatch):
    from cairn import collection_service
    from cairn.capture.fake import FakeCameraBackend
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    app = create_app(f"sqlite:///{tmp_path / 'camera-lock.db'}")
    headers = {"Authorization": "Bearer local-only"}
    with TestClient(app) as client:
        app.state.cameras = FakeCameraBackend()
        with app.state.session_factory() as session:
            collection = collection_service.create(session, app.state.learner_id, name="C", subject="数学", tags=[],
                                                   license="private", author="A", source="S")
            session.commit(); cid = collection.id
        current = client.get("/api/workflows/default").json(); graph = current["graph"]
        graph["nodes"].append({"id": "collection-1", "node_id": "collection_append", "params": {"collection_id": cid}})
        graph["edges"].append({"source": "review-1", "target": "collection-1"})
        assert client.post("/api/workflows/default", headers=headers, json={"title": current["title"],
            "expected_revision": current["revision"], "graph": graph}).status_code == 200
        assert client.post("/api/workflows/default/publish", headers=headers).status_code == 200
        entered, release = threading.Event(), threading.Event()
        original = scan_service.grab_frame
        def slow(*args):
            entered.set()
            assert release.wait(10)
            return original(*args)
        monkeypatch.setattr(scan_service, "grab_frame", slow)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(lambda: client.post("/devices/fake:uvc-pro/snapshot", follow_redirects=False))
            assert entered.wait(10)
            try:
                switched = pool.submit(lambda: client.post("/api/workflows/default/rollback/default:v2", headers=headers)).result(timeout=4)
                assert switched.status_code == 200
                assert client.delete(f"/api/collections/{cid}?expected_revision=0", headers=headers).status_code == 200
            finally:
                release.set()
            response = pending.result(timeout=10)
        assert response.status_code == 303
        scan_id = int(response.headers["location"].split("/")[2])
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["version_id"] == "default:v2"


def test_scan_rereads_active_version_if_it_changes_before_lock(tmp_path, monkeypatch):
    from sqlalchemy import event
    from cairn import collection_service
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    monkeypatch.setenv("CAIRN_MIN_IMAGE_SIDE", "1")
    app = create_app(f"sqlite:///{tmp_path / 'pin-refresh.db'}")
    headers = {"Authorization": "Bearer local-only"}
    image = io.BytesIO(); Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            collection = collection_service.create(session, app.state.learner_id, name="C", subject="数学", tags=[],
                                                   license="private", author="A", source="S")
            session.commit(); cid = collection.id
        current = client.get("/api/workflows/default").json(); graph = current["graph"]
        graph["nodes"].append({"id": "collection-1", "node_id": "collection_append", "params": {"collection_id": cid}})
        graph["edges"].append({"source": "review-1", "target": "collection-1"})
        assert client.post("/api/workflows/default", headers=headers, json={"title": current["title"],
            "expected_revision": current["revision"], "graph": graph}).status_code == 200
        assert client.post("/api/workflows/default/publish", headers=headers).status_code == 200
        entered, release = threading.Event(), threading.Event()
        engine = app.state.session_factory.kw["bind"]
        def before_sql(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE workflow SET revision=workflow.revision"):
                entered.set()
                assert release.wait(10)
        event.listen(engine, "before_cursor_execute", before_sql)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(lambda: client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False))
                assert entered.wait(10)
                try:
                    from cairn.workflow.service import activate
                    with app.state.session_factory() as session:
                        activate(session, session.get(Workflow, "default"), "default:v2", learner_id=app.state.learner_id)
                        session.commit()
                        collection_service.remove(session, app.state.learner_id, cid, 0)
                        session.commit()
                finally:
                    release.set()
                response = pending.result(timeout=10)
        finally:
            event.remove(engine, "before_cursor_execute", before_sql)
        assert response.status_code == 303
        scan_id = int(response.headers["location"].split("/")[2])
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["version_id"] == "default:v2"


def test_upload_processing_does_not_block_other_http_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_MIN_IMAGE_SIDE", "1")
    app = create_app(f"sqlite:///{tmp_path / 'upload-responsive.db'}")
    image = io.BytesIO(); Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    entered, release = threading.Event(), threading.Event()
    original = scan_service.save_upload
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(scan_service, "save_upload", slow)
    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(lambda: client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False))
            assert entered.wait(10)
            try:
                response = pool.submit(lambda: client.get("/strategies")).result(timeout=3)
                assert response.status_code == 200
            finally:
                release.set()
            assert pending.result(timeout=10).status_code == 303


def test_legacy_schema_migrates_without_losing_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER, applied_at TEXT)")
        conn.execute("INSERT INTO schema_version VALUES (1, 23, 'old')")
        conn.execute("CREATE TABLE scan (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO scan VALUES (17)")
        conn.execute("CREATE TABLE error_item (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO error_item VALUES (29)")
    engine = make_engine(f"sqlite:///{path}")
    init_db(engine)
    with engine.connect() as conn:
        from cairn.migrations import SCHEMA_VERSION
        assert conn.exec_driver_sql("SELECT version FROM schema_version WHERE id=1").scalar_one() == SCHEMA_VERSION
        assert conn.exec_driver_sql("SELECT id FROM scan").scalar_one() == 17
        assert conn.exec_driver_sql("SELECT workflow_strategy_id FROM error_item WHERE id=29").scalar_one() is None
        assert conn.exec_driver_sql("SELECT workflow_tutor_enabled FROM error_item WHERE id=29").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT workflow_recap_enabled FROM error_item WHERE id=29").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM recap_attempt").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM workflow_run").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM learning_collection_practice_item").scalar_one() == 0
    engine.dispose()


def test_workflow_rejection_migration_backfills_only_rejected_items(tmp_path):
    from cairn.migrations import _add_item_workflow_rejected

    engine = make_engine(f"sqlite:///{tmp_path / 'rejection-migration.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE error_item (id INTEGER PRIMARY KEY, question_id INTEGER)")
        conn.exec_driver_sql("CREATE TABLE workflow_review_item (id INTEGER PRIMARY KEY, question_id INTEGER, status TEXT)")
        conn.exec_driver_sql("INSERT INTO error_item VALUES (1, 11), (2, 12), (3, 13)")
        conn.exec_driver_sql("INSERT INTO workflow_review_item VALUES (1, 11, 'rejected'), (2, 12, 'confirmed')")
        _add_item_workflow_rejected(conn)
        _add_item_workflow_rejected(conn)
        assert conn.exec_driver_sql("SELECT workflow_rejected FROM error_item ORDER BY id").scalars().all() == [1, 0, 0]
    engine.dispose()


def test_start_run_rolls_back_with_callers_transaction(factory):
    with factory() as session:
        run = execution.start(session, session.get(Scan, 1))
        run_id = run.id
        session.rollback()
    with factory() as session:
        assert session.get(execution.WorkflowRun, run_id) is None
        assert list(session.scalars(select(WorkflowRunStep))) == []


def test_item_strategy_survives_learning_data_export(factory, tmp_path):
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        scan = session.get(Scan, 1)
        scan.learner_id = 1
        session.add(ScanQuestion(scan_id=1, no=1, bbox_json="{}", stem_text="计算下面的数学问题"))
        session.commit()
        scan_service.import_wrong_questions(session, 1, scan, [1], subject="数学", question_type="填空")
        apply_strategy(session, scan, {"strategy": "leitner"})
        session.scalar(select(ErrorItem)).workflow_tutor_enabled = 1
        session.commit()
        document = export_service.build_export(session, 1)
    engine = make_engine(f"sqlite:///{tmp_path / 'restore.db'}")
    init_db(engine)
    with make_session_factory(engine)() as session:
        import_service.load_backup(session, document)
        restored = session.scalar(select(ErrorItem))
        assert restored.workflow_strategy_id == "leitner"
        assert restored.workflow_tutor_enabled == 1
    engine.dispose()


def test_export_snapshot_excludes_concurrent_unfinished_selection(factory, monkeypatch):
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        session.get(Scan, 1).learner_id = 1
        session.commit()

    original = export_service._build_export
    def concurrent_selection(session, learner_id, now):
        with factory() as other:
            scan = other.get(Scan, 1)
            question = Question(learner_id=1, subject="数学", grade_level="primary",
                                question_type="填空", content="新的题目")
            other.add(question)
            other.flush()
            other.add(ErrorItem(learner_id=1, question_id=question.id))
            run = execution.start(other, scan)
            other.add(WorkflowReviewItem(run_id=run.id, question_id=question.id, status="pending"))
            other.commit()
        return original(session, learner_id, now)

    monkeypatch.setattr(export_service, "_build_export", concurrent_selection)
    with factory() as session:
        document = export_service.build_export(session, 1)
        assert document["question"] == [] and document["error_item"] == []
    monkeypatch.setattr(export_service, "_build_export", original)
    with factory() as session:
        with pytest.raises(export_service.IncompleteWorkflowExport):
            export_service.build_export(session, 1)


def test_reader_progress_is_read_only_and_scoped_to_learner(tmp_path):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'web.db'}")
    with TestClient(app) as client:
        (tmp_path / "scans").mkdir()
        (tmp_path / "scans" / "mine.jpg").write_bytes(b"image snapshot")
        with app.state.session_factory() as session:
            other = Learner(name="Other")
            session.add(other)
            session.flush()
            mine = Scan(learner_id=app.state.learner_id, device_id="d", device_name="d",
                        width=1, height=1, path="scans/mine.jpg", workflow_version_id="default:v1")
            theirs = Scan(learner_id=other.id, device_id="d", device_name="d",
                          width=1, height=1, path="theirs.jpg", workflow_version_id="default:v1")
            session.add_all([mine, theirs])
            session.commit()
            mine_id, other_id = mine.id, theirs.id
        response = client.get(f"/api/scans/{mine_id}/workflow")
        assert response.status_code == 200
        assert response.json()["status"] == "not_started"
        assert client.get(f"/api/scans/{other_id}/workflow").status_code == 404
        assert client.get(f"/api/scans/{mine_id}/workflow-image").status_code == 404
        assert client.get(f"/api/scans/{other_id}/workflow-image").status_code == 404
        with app.state.session_factory() as session:
            assert session.scalar(select(execution.WorkflowRun.id)) is None
            run = execution.start(session, session.get(Scan, mine_id))
            execution.advance(session, run, {"upright": lambda s, scan, p: None})
            image_path = select_image(session, run, session.get(Scan, mine_id), app.state.db_url, "original")
            session.commit()
        response = client.get(f"/api/scans/{mine_id}/workflow")
        assert response.json()["status"] == "waiting"
        assert [step["status"] for step in response.json()["steps"]] == [
            "completed", "waiting", "pending", "pending"
        ]
        assert client.get(f"/api/scans/{mine_id}/workflow-image").content == b"image snapshot"
        image_path.write_bytes(b"corrupted")
        assert client.get(f"/api/scans/{mine_id}/workflow-image").status_code == 409


def test_deferred_upload_pins_version_before_processing(tmp_path, monkeypatch):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'upload.db'}")
    image = io.BytesIO()
    Image.new("RGB", (1000, 1000), "white").save(image, format="PNG")
    payload = image.getvalue()
    def must_not_process(*args, **kwargs):
        raise AssertionError("processing ran before workflow initialization")
    monkeypatch.setattr(scan_service, "_crop_page", must_not_process)
    monkeypatch.setattr(scan_service, "_write_processed", must_not_process)
    with TestClient(app):
        with app.state.session_factory() as session:
            scan = scan_service.save_upload(session, app.state.learner_id, payload,
                                            db_url=app.state.db_url, defer_processing=True)
            assert (tmp_path / scan.path).read_bytes() == payload
            run = session.scalar(select(execution.WorkflowRun).where(execution.WorkflowRun.scan_id == scan.id))
            assert (run.version_id, run.status, scan.processed_path) == ("default:v2", "pending", None)
            workflow = session.get(Workflow, "default")
            workflow.active_version_id = None
            session.commit()
            with pytest.raises(execution.RunError):
                scan_service.save_upload(session, app.state.learner_id, payload,
                                         db_url=app.state.db_url, defer_processing=True)
            assert len(list(session.scalars(select(Scan)))) == 1
            assert len(list((tmp_path / "scans").rglob("*.jpg"))) == 1


def test_capture_initializes_versioned_run_with_frame(factory, tmp_path):
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        ensure_default_workflow(session)
        session.commit()
        frame = Frame(b"frame", 100, 100, "jpeg", DeviceInfo("camera", "Camera"),
                      datetime.now(timezone.utc))
        scan = scan_service.store_frame(session, frame, 1,
                                        db_url=f"sqlite:///{tmp_path / 'workflow.db'}",
                                        start_workflow=True)
        run = session.scalar(select(execution.WorkflowRun).where(execution.WorkflowRun.scan_id == scan.id))
        assert (scan.workflow_version_id, run.version_id, run.status) == ("default:v1", "default:v1", "pending")
        assert (tmp_path / scan.path).read_bytes() == b"frame"
        session.get(Workflow, "default").active_version_id = None
        session.commit()
        with pytest.raises(execution.RunError):
            scan_service.store_frame(session, frame, 1,
                                     db_url=f"sqlite:///{tmp_path / 'workflow.db'}",
                                     start_workflow=True)
        assert len(list((tmp_path / "scans").rglob("*.jpg"))) == 1


def test_human_resume_applies_pinned_strategy_to_actual_reviews(factory):
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        scan = session.get(Scan, 1)
        scan.learner_id = 1
        session.add(ScanQuestion(scan_id=1, no=1, bbox_json="{}", stem_text="计算下面的数学问题"))
        session.commit()
        scan_service.import_wrong_questions(session, 1, scan, [1], subject="数学", question_type="填空")
        steps = [
            {"node_id": "select", "type": "human", "params": {}},
            {"node_id": "strategy", "type": "auto", "params": {"strategy": "leitner"}},
        ]
        session.add(WorkflowVersion(version_id="flow:v3", workflow_id="flow", number=3,
                                    graph_json="{}", runtime_json=json.dumps({"version_id": "flow:v3", "steps": steps})))
        session.flush()
        scan.workflow_version_id = "flow:v3"
        session.flush()
        run = execution.start(session, scan)
        execution.advance(session, run, {"strategy": apply_strategy})
        assert run.status == "waiting"
        execution.resume(session, run, "select", lambda s, scan, p: None, {"strategy": apply_strategy})
        session.commit()
        assert run.status == "completed"
        item = session.scalar(select(ErrorItem))
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        review = review_service.apply_review(session, item, "good", now=now)
        assert review.next_due_at.date() == (now + timedelta(days=3)).date()


def test_cut_rows_can_rollback_with_workflow_step(factory):
    with factory() as session:
        scan_service.store_questions(session, 1, [QuestionBlock(1, (0, 0, 10, 10), "题目", [0])],
                                     page_width=100, page_height=100, commit=False)
        assert session.scalar(select(ScanQuestion.id)) is not None
        session.rollback()
    with factory() as session:
        assert session.scalar(select(ScanQuestion.id)) is None


def test_scan_contract_rejects_unresumable_or_unsupported_graphs():
    steps = [
        {"node_id": node, "type": kind, "params": {}}
        for node, kind in [("upright", "auto"), ("crop", "human"), ("cut", "auto"),
                           ("select", "human"), ("recognize", "auto"), ("review", "human")]
    ]
    validate_scan_runtime(steps)
    with pytest.raises(execution.RunError):
        validate_scan_runtime([step for step in steps if step["node_id"] != "select"])
    with pytest.raises(execution.RunError):
        validate_scan_runtime(steps + [{"node_id": "cut", "type": "auto", "params": {}}])
    with pytest.raises(execution.RunError):
        validate_scan_runtime(steps[:2] + [{"node_id": "enhance", "type": "human", "params": {}}] + steps[2:])
    validate_scan_runtime(steps[:-1] + [{"node_id": "strategy", "type": "auto",
                                       "params": {"strategy": "leitner"}}] + steps[-1:])
    with pytest.raises(execution.RunError):
        validate_scan_runtime(steps + [{"node_id": "strategy", "type": "auto",
                                        "params": {"strategy": "not-registered"}}])
    tutor = {"node_id": "guided_tutor", "type": "auto", "params": {}}
    validate_scan_runtime(steps + [tutor])
    with pytest.raises(execution.RunError):
        validate_scan_runtime(steps[:-1] + [tutor] + steps[-1:])


def test_default_upgrade_preserves_v1_and_never_overwrites_edited_draft(factory):
    with factory() as session:
        workflow = ensure_default_workflow(session)
        session.commit()
        old_runtime = session.get(WorkflowVersion, "default:v1").runtime_json
        ensure_runnable_default(session)
        session.commit()
        session.refresh(workflow)
        assert workflow.active_version_id == "default:v2" and workflow.revision == 2
        assert session.get(WorkflowVersion, "default:v1").runtime_json == old_runtime
        assert len(validate_for_publish(json.loads(workflow.draft_json))["steps"]) == 6
        ensure_runnable_default(session)
        session.commit()
        assert len(list(session.scalars(select(WorkflowVersion).where(WorkflowVersion.workflow_id == "default")))) == 2
    with factory() as session:
        workflow = session.get(Workflow, "default")
        workflow.active_version_id = "default:v1"
        workflow.revision = 9
        workflow.draft_json = '{"edited":true}'
        session.commit()
        ensure_runnable_default(session)
        session.commit()
        assert workflow.active_version_id == "default:v1" and workflow.draft_json == '{"edited":true}'


def test_publish_contract_rejects_old_incomplete_default():
    from cairn.workflow.service import default_graph
    with pytest.raises(WorkflowError, match="必须按顺序"):
        validate_for_publish(default_graph())


def test_strategy_node_is_resolved_and_validated_for_publication():
    graph = default_executable_graph()
    graph["nodes"].append({"id": "strategy-1", "node_id": "strategy", "params": {"strategy": "leitner"}})
    graph["edges"].append({"source": "review-1", "target": "strategy-1"})
    assert validate_for_publish(graph)["steps"][-1]["params"] == {"strategy": "leitner"}
    graph["nodes"][-1]["params"] = {}
    assert validate_for_publish(graph)["steps"][-1]["params"] == {"strategy": "fsrs"}
    graph["nodes"][-1]["params"] = {"strategy": "unknown"}
    with pytest.raises(WorkflowError):
        validate_for_publish(graph)


def test_incomplete_draft_can_be_saved_but_not_published(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    app = create_app(db_url=f"sqlite:///{tmp_path / 'draft.db'}")
    with TestClient(app) as client:
        current = client.get("/api/workflows/default").json()
        partial = {"nodes": current["graph"]["nodes"][:1], "edges": []}
        headers = {"Authorization": "Bearer local-only"}
        saved = client.post("/api/workflows/default", headers=headers, json={
            "title": current["title"], "expected_revision": current["revision"], "graph": partial,
        })
        assert saved.status_code == 200 and saved.json()["compiled_preview"] is None
        assert client.post("/api/workflows/default/preview", headers=headers, json={"graph": partial}).status_code == 422
        assert client.post("/api/workflows/default/publish", headers=headers).status_code == 422
        assert client.get("/api/workflows/default").json()["active_version_id"] == "default:v2"


def test_publish_rejects_a_draft_changed_after_editor_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    app = create_app(db_url=f"sqlite:///{tmp_path / 'publish-race.db'}")
    headers = {"Authorization": "Bearer local-only"}
    with TestClient(app) as client:
        current = client.get("/api/workflows/default").json()
        saved = client.post("/api/workflows/default", headers=headers, json={
            "title": "First editor", "expected_revision": current["revision"], "graph": current["graph"],
        }).json()
        other = client.post("/api/workflows/default", headers=headers, json={
            "title": "Second editor", "expected_revision": saved["revision"], "graph": current["graph"],
        })
        assert other.status_code == 200
        rejected = client.post("/api/workflows/default/publish", headers=headers,
                               json={"expected_revision": saved["revision"]})
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "revision_conflict"
        assert client.get("/api/workflows/default").json()["active_version_id"] == "default:v2"
        published = client.post("/api/workflows/default/publish", headers=headers,
                                json={"expected_revision": other.json()["revision"]})
        assert published.status_code == 200 and published.json()["version_id"] == "default:v3"
        versions = client.get("/api/workflows/default/versions").json()["versions"]
        assert all(datetime.fromisoformat(version["published_at"]).utcoffset() == timedelta(0) for version in versions)
        assert client.post("/api/workflows/default/publish", headers=headers,
                           json={"expected_revision": "3"}).status_code == 400


def test_run_image_is_frozen_and_scoped_to_scan(factory, tmp_path):
    folder = tmp_path / "scans"
    folder.mkdir()
    original = folder / "paper.jpg"
    original.write_bytes(b"first image")
    db_url = f"sqlite:///{tmp_path / 'workflow.db'}"
    with factory() as session:
        scan = session.get(Scan, 1)
        scan.path = "scans/paper.jpg"
        session.flush()
        run = execution.start(session, scan)
        selected = select_image(session, run, scan, db_url, "original")
        session.commit()
        assert selected.read_bytes() == b"first image"
        assert run.image_path == selected.relative_to(tmp_path).as_posix()
        original.write_bytes(b"changed image")
        assert selected.read_bytes() == b"first image"
        next_image = select_image(session, run, scan, db_url, "original")
        assert next_image != selected and next_image.read_bytes() == b"changed image"
        with pytest.raises(execution.RunError):
            select_image(session, run, scan, db_url, "../../outside")


def test_real_scan_adapter_uses_frozen_image_and_parks_after_cut(factory, tmp_path, monkeypatch):
    from cairn import ocr

    folder = tmp_path / "scans"
    folder.mkdir()
    (folder / "paper.jpg").write_bytes(b"original image")
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "1")
    monkeypatch.setenv("CAIRN_TOOL_CUT", "1")
    monkeypatch.setenv("CAIRN_TOOL_OCR", "1")
    monkeypatch.setattr(scan_service, "_crop_page", lambda path: (None, None))
    seen = []
    def read_page(path):
        seen.append(path)
        return SimpleNamespace(lines=[], width=100, height=100, angle=0)
    monkeypatch.setattr(ocr, "read_page", read_page)
    monkeypatch.setattr(ocr, "cut_questions", lambda lines, w, h: [QuestionBlock(1, (0, 0, 20, 20), "一道题", [0])])
    steps = [
        {"node_id": "upright", "type": "auto", "params": {}},
        {"node_id": "crop", "type": "human", "params": {}},
        {"node_id": "cut", "type": "auto", "params": {}},
        {"node_id": "select", "type": "human", "params": {}},
    ]
    db_url = f"sqlite:///{tmp_path / 'workflow.db'}"
    with factory() as session:
        session.add(WorkflowVersion(version_id="flow:v3", workflow_id="flow", number=3,
                                    graph_json="{}", runtime_json=json.dumps({"version_id": "flow:v3", "steps": steps})))
        session.flush()
        scan = session.get(Scan, 1)
        scan.path, scan.workflow_version_id = "scans/paper.jpg", "flow:v3"
        session.flush()
        run = execution.start(session, scan)
        execution.advance(session, run, scan_handlers(db_url))
        assert (run.status, run.cursor) == ("waiting", 1)
        execution.resume(session, run, "crop", lambda s, scan, p: None, scan_handlers(db_url))
        assert (run.status, run.cursor) == ("waiting", 3)
        assert seen == [str(tmp_path / run.image_path)]
        assert session.scalar(select(ScanQuestion.stem_text)) == "一道题"
        session.commit()


def test_rotated_ocr_freezes_matching_image_and_box_coordinates(factory, tmp_path, monkeypatch):
    from cairn import ocr
    from cairn.workflow.images import selected_image

    folder = tmp_path / "scans"
    folder.mkdir()
    Image.new("RGB", (50, 30), "white").save(folder / "paper.png")
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "1")
    monkeypatch.setenv("CAIRN_TOOL_CUT", "1")
    monkeypatch.setenv("CAIRN_TOOL_OCR", "1")
    monkeypatch.setattr(scan_service, "_crop_page", lambda path: (None, None))
    oriented = Image.new("RGB", (30, 50), "white")
    monkeypatch.setattr(ocr, "read_page", lambda path: SimpleNamespace(
        lines=[], width=30, height=50, angle=90, image=oriented))
    monkeypatch.setattr(ocr, "cut_questions", lambda lines, w, h: [QuestionBlock(1, (3, 4, 8, 9), "题目", [0])])
    steps = [{"node_id": "upright", "type": "auto", "params": {}},
             {"node_id": "cut", "type": "auto", "params": {}},
             {"node_id": "select", "type": "human", "params": {}}]
    db_url = f"sqlite:///{tmp_path / 'workflow.db'}"
    with factory() as session:
        session.add(WorkflowVersion(version_id="flow:v3", workflow_id="flow", number=3,
                                    graph_json="{}", runtime_json=json.dumps({"version_id": "flow:v3", "steps": steps})))
        session.flush()
        scan = session.get(Scan, 1)
        scan.path, scan.workflow_version_id = "scans/paper.png", "flow:v3"
        session.flush()
        run = execution.start(session, scan)
        execution.advance(session, run, scan_handlers(db_url))
        assert run.status == "waiting"
        snapshot = selected_image(session, scan, db_url)
        with Image.open(snapshot) as image:
            assert image.size == (30, 50)
        payload = json.loads(session.scalar(select(ScanQuestion.bbox_json)))
        assert payload == {"bbox": [3, 4, 8, 9], "page": [30, 50], "angle": 0}


def test_crop_resume_requires_durable_human_decision(factory, tmp_path):
    from cairn.workflow.images import selected_image

    folder = tmp_path / "scans"
    folder.mkdir()
    Image.new("RGB", (50, 30), "white").save(folder / "paper.png")
    db_url = f"sqlite:///{tmp_path / 'workflow.db'}"
    steps = [{"node_id": "crop", "type": "human", "params": {}},
             {"node_id": "select", "type": "human", "params": {}}]
    with factory() as session:
        session.add(WorkflowVersion(version_id="flow:v3", workflow_id="flow", number=3,
                                    graph_json="{}", runtime_json=json.dumps({"version_id": "flow:v3", "steps": steps})))
        session.flush()
        scan = session.get(Scan, 1)
        scan.path, scan.workflow_version_id = "scans/paper.png", "flow:v3"
        session.flush()
        run = execution.start(session, scan)
        execution.advance(session, run, {})
        session.commit()
        with pytest.raises(execution.RunError, match="尚未确认"):
            execution.resume(session, run, "crop", crop_confirmed(db_url), {})
        session.rollback()
        assert (run.status, run.cursor) == ("waiting", 0)
        scan_service.mark_crop_skipped(scan, db_url)
        execution.resume(session, run, "crop", crop_confirmed(db_url), {})
        session.commit()
        assert (run.status, run.cursor) == ("waiting", 1)
        assert selected_image(session, scan, db_url).read_bytes() == (folder / "paper.png").read_bytes()


def test_selection_waits_until_auto_and_manual_submissions_succeed(factory, tmp_path, monkeypatch):
    folder = tmp_path / "scans"
    folder.mkdir()
    image = Image.new("RGB", (100, 100), "white")
    image.save(folder / "paper.png")
    db_url = f"sqlite:///{tmp_path / 'workflow.db'}"
    steps = [{"node_id": "select", "type": "human", "params": {}}]
    monkeypatch.setattr(scan_service, "frame_text", lambda path: "人工题目")
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        session.add(WorkflowVersion(version_id="flow:v3", workflow_id="flow", number=3,
                                    graph_json="{}", runtime_json=json.dumps({"version_id": "flow:v3", "steps": steps})))
        session.flush()
        scan = session.get(Scan, 1)
        scan.learner_id, scan.path, scan.workflow_version_id = 1, "scans/paper.png", "flow:v3"
        session.flush()
        run = execution.start(session, scan)
        select_image(session, run, scan, db_url, "original")
        execution.advance(session, run, {})
        session.commit()
        scan_service.store_questions(session, scan.id, [QuestionBlock(1, (0, 0, 30, 30), "自动识别的题目", [0])],
                                     page_width=100, page_height=100)
        scan_service.import_wrong_questions(session, 1, scan, [1], subject="数学", question_type="填空")
        action = selection_confirmed(db_url, [1], [[40, 40, 80, 80]])
        with pytest.raises(execution.RunError, match="手动画框"):
            execution.resume(session, run, "select", action, {})
        session.rollback()
        assert (run.status, run.cursor) == ("waiting", 0)
        result = scan_service.store_manual_frames(session, 1, scan, [(40, 40, 80, 80)],
                                                   page_width=100, page_height=100, angle=0,
                                                   page_image=image, db_url=db_url)
        assert result.created_count == 1
        execution.resume(session, run, "select", action, {})
        session.commit()
        assert run.status == "completed"


def test_scan_review_waits_for_every_question_across_sessions(factory, tmp_path):
    steps = [{"node_id": "recognize", "type": "auto", "params": {}},
             {"node_id": "review", "type": "human", "params": {}},
             {"node_id": "strategy", "type": "auto", "params": {"strategy": "leitner"}}]
    db_url = f"sqlite:///{tmp_path / 'workflow.db'}"
    with factory() as session:
        session.add(Learner(id=1, name="Learner"))
        scan = session.get(Scan, 1)
        scan.learner_id = 1
        session.add(WorkflowVersion(version_id="flow:v3", workflow_id="flow", number=3,
                                    graph_json="{}", runtime_json=json.dumps({"version_id": "flow:v3", "steps": steps})))
        session.flush()
        scan.workflow_version_id = "flow:v3"
        scan_service.store_questions(session, scan.id, [
            QuestionBlock(1, (0, 0, 10, 10), "第一道数学题", [0]),
            QuestionBlock(2, (20, 20, 30, 30), "第二道数学题", [1]),
        ], page_width=100, page_height=100)
        scan_service.import_wrong_questions(session, 1, scan, [1, 2], subject="数学", question_type="填空")
        run = execution.start(session, scan)
        execution.advance(session, run, scan_handlers(db_url))
        assert (run.status, run.cursor) == ("waiting", 1)
        items = list(session.scalars(select(WorkflowReviewItem).order_by(WorkflowReviewItem.question_id)))
        assert len(items) == 2
        first_id, second_id, run_id = items[0].question_id, items[1].question_id, run.id
        workflow_review.decide(session, run, first_id, "confirmed", scan_handlers(db_url))
        session.commit()
        assert run.status == "waiting"
    with factory() as session:
        run = session.get(execution.WorkflowRun, run_id)
        workflow_review.decide(session, run, second_id, "rejected", scan_handlers(db_url))
        session.commit()
        assert run.status == "completed"
        entries = list(session.scalars(select(ErrorItem).order_by(ErrorItem.question_id)))
        assert entries[0].workflow_strategy_id == "leitner"
        assert entries[1].review_status == "archived"
        assert entries[1].workflow_strategy_id is None
        assert workflow_review.decide(session, run, second_id, "rejected", scan_handlers(db_url)).status == "completed"
        with pytest.raises(execution.RunError, match="已经处理"):
            workflow_review.decide(session, run, second_id, "confirmed", scan_handlers(db_url))


def test_reader_review_page_edits_and_resumes_persisted_scan(tmp_path, monkeypatch):
    from cairn import web

    db_url = f"sqlite:///{tmp_path / 'web-review.db'}"
    app = create_app(db_url=db_url)
    steps = [{"node_id": "recognize", "type": "auto", "params": {}},
             {"node_id": "review", "type": "human", "params": {}},
             {"node_id": "strategy", "type": "auto", "params": {"strategy": "leitner"}}]
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add(WorkflowVersion(version_id="default:v3", workflow_id="default", number=3,
                                        graph_json="{}", runtime_json=json.dumps({"version_id": "default:v3", "steps": steps})))
            session.flush()
            scan = Scan(learner_id=app.state.learner_id, device_id="d", device_name="d",
                        width=100, height=100, path="scans/review.jpg", workflow_version_id="default:v3")
            session.add(scan)
            session.flush()
            scan_service.store_questions(session, scan.id, [
                QuestionBlock(1, (0, 0, 10, 10), "第一道数学题", [0]),
                QuestionBlock(2, (20, 20, 30, 30), "第二道数学题", [1]),
            ], page_width=100, page_height=100)
            scan_service.import_wrong_questions(session, app.state.learner_id, scan, [1, 2], subject="数学", question_type="填空")
            ids = [row.question_id for row in scan_service.list_questions(session, scan.id)]
            run = execution.start(session, scan)
            execution.advance(session, run, scan_handlers(db_url))
            session.commit()
            scan_id = scan.id
        page = client.get(f"/scans/{scan_id}/workflow-review")
        assert page.status_code == 200 and "第一道数学题" in page.text
        blocked_export = client.get("/export")
        assert blocked_export.status_code == 409 and f"/scans/{scan_id}/workflow" in blocked_export.text
        assert "第一道数学题" not in client.get("/questions").text
        assert client.post("/tutor/start", json={"question_id": ids[0]}).status_code == 404
        with zipfile.ZipFile(io.BytesIO(client.get("/export/anki").content)) as package:
            anki_path = tmp_path / "pending.anki2"
            anki_path.write_bytes(package.read("collection.anki2"))
        with sqlite3.connect(anki_path) as anki:
            assert anki.execute("select count(*) from notes").fetchone()[0] == 0
        assert "Check this question" in client.get(f"/scans/{scan_id}/workflow-review?lang=en").text
        english_edit = f"/questions/{ids[0]}/edit?back=workflow-{scan_id}-en"
        english_page = client.get(english_edit).text
        assert "Save changes" in english_page and '<html lang="en">' in english_page
        invalid_edit = client.post(english_edit, data={"content": "", "answer": "", "analysis": ""})
        assert invalid_edit.status_code == 400 and "Question text is required" in invalid_edit.text
        assert '<html lang="en">' in invalid_edit.text
        english_save = client.post(english_edit, data={"content": "第一道数学题", "answer": "", "analysis": ""}, follow_redirects=False)
        assert english_save.headers["location"] == f"/scans/{scan_id}/workflow-review?lang=en"
        edit = client.post(f"/questions/{ids[0]}/edit?back=workflow-{scan_id}",
                           data={"content": "修正后的数学题", "answer": "", "analysis": ""}, follow_redirects=False)
        assert edit.status_code == 303 and edit.headers["location"] == f"/scans/{scan_id}/workflow-review"
        assert "修正后的数学题" in client.get(edit.headers["location"]).text
        first = client.post(f"/scans/{scan_id}/workflow-review/{ids[0]}", data={"decision": "confirmed"}, follow_redirects=False)
        assert first.status_code == 303
        assert client.post("/tutor/start", json={"question_id": ids[0]}).status_code == 404
        assert "第二道数学题" in client.get(first.headers["location"]).text
        fail = {"active": True}
        original_handlers = web.scan_workflow_handlers
        def intermittent_handlers(url):
            mapping = original_handlers(url)
            strategy = mapping["strategy"]
            def intermittent(session, scan, params):
                if fail["active"]:
                    raise RuntimeError("temporary failure")
                strategy(session, scan, params)
            mapping["strategy"] = intermittent
            return mapping
        monkeypatch.setattr(web, "scan_workflow_handlers", intermittent_handlers)
        second = client.post(f"/scans/{scan_id}/workflow-review/{ids[1]}", data={"decision": "rejected"}, follow_redirects=False)
        assert second.status_code == 303
        assert "这张卷子暂时无法继续" in client.get(second.headers["location"]).text
        fail["active"] = False
        retry = client.post(f"/scans/{scan_id}/workflow-retry", data={}, follow_redirects=False)
        assert retry.status_code == 303
        assert "这张卷子核对好了" in client.get(retry.headers["location"]).text
        from cairn.tutor import llm as tutor_llm
        monkeypatch.setattr(tutor_llm, "chat", lambda *args, **kwargs: "你会先看哪个条件？")
        assert client.post("/tutor/start", json={"question_id": ids[0]}).status_code == 200
        assert client.post("/tutor/start", json={"question_id": ids[1]}).status_code == 404
        library = client.get("/questions").text
        assert "修正后的数学题" in library and "第二道数学题" not in library
        with zipfile.ZipFile(io.BytesIO(client.get("/export/anki").content)) as package:
            anki_path = tmp_path / "completed.anki2"
            anki_path.write_bytes(package.read("collection.anki2"))
        with sqlite3.connect(anki_path) as anki:
            assert anki.execute("select count(*) from notes").fetchone()[0] == 1
        backup = client.get("/export")
        assert backup.status_code == 200
        restore_engine = make_engine(f"sqlite:///{tmp_path / 'restore-excluded.db'}")
        init_db(restore_engine)
        with make_session_factory(restore_engine)() as session:
            import_service.load_backup(session, backup.json())
            accepted = session.scalar(select(ErrorItem).where(ErrorItem.question_id == ids[0]))
            excluded = session.scalar(select(ErrorItem).where(ErrorItem.question_id == ids[1]))
            assert accepted.workflow_rejected == 0 and excluded.workflow_rejected == 1
            from cairn import entry_service, graduation_service, practice_service, stats_service
            assert practice_service.get_practice_item(session, accepted.learner_id, accepted.id) is not None
            assert practice_service.get_practice_item(session, excluded.learner_id, excluded.id) is None
            assert stats_service.practice_today(session, accepted.learner_id).remaining == 1
            assert stats_service.practice_today(session, accepted.learner_id).total_items == 1
            with pytest.raises(entry_service.ValidationError):
                graduation_service.restore(session, excluded.id)
        restore_engine.dispose()


def test_workflow_selection_page_uses_snapshot_and_waits_for_both_submissions(tmp_path, monkeypatch):
    from cairn import web

    db_url = f"sqlite:///{tmp_path / 'web-select.db'}"
    app = create_app(db_url=db_url)
    steps = [{"node_id": "select", "type": "human", "params": {"preselect": True}},
             {"node_id": "recognize", "type": "auto", "params": {}},
             {"node_id": "review", "type": "human", "params": {}}]
    folder = tmp_path / "scans"
    folder.mkdir()
    Image.new("RGB", (100, 100), "white").save(folder / "paper.png")
    monkeypatch.setattr(scan_service, "frame_text", lambda path: "人工录入题目")
    monkeypatch.setattr(web, "_red_mark_flags", lambda path, rows, angle: [True] * len(rows))
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add(WorkflowVersion(version_id="default:v3", workflow_id="default", number=3,
                                        graph_json="{}", runtime_json=json.dumps({"version_id": "default:v3", "steps": steps})))
            session.flush()
            scan = Scan(learner_id=app.state.learner_id, device_id="d", device_name="d",
                        width=100, height=100, path="scans/paper.png", workflow_version_id="default:v3")
            session.add(scan)
            session.flush()
            scan_service.store_questions(session, scan.id, [QuestionBlock(1, (0, 0, 30, 30), "自动识别的题目", [0])],
                                         page_width=100, page_height=100)
            run = execution.start(session, scan)
            select_image(session, run, scan, db_url, "original")
            execution.advance(session, run, {})
            session.commit()
            scan_id = scan.id
        page = client.get(f"/scans/{scan_id}/select")
        assert page.status_code == 200 and 'data-workflow="1"' in page.text
        assert f'/api/scans/{scan_id}/workflow-image' in page.text
        assert 'data-recipe="default:v3"' in page.text
        assert 'data-selected="1"' in page.text
        payload = {"auto_numbers": [1], "manual_frames": [[40, 40, 80, 80]],
                   "subject": "英语", "question_type": "阅读"}
        complete_url = f"/api/scans/{scan_id}/workflow-selection"
        assert client.post(f"/scans/{scan_id}/questions", data={"no": "1"}).status_code == 409
        assert client.post(f"/scans/{scan_id}/frame", data={"frame_0": "40,40,80,80"}).status_code == 409
        original_store = scan_service.store_manual_frames
        def fail_manual(*args, **kwargs):
            original_store(*args, **kwargs)
            raise scan_service.FrameError("temporary frame failure")
        monkeypatch.setattr(scan_service, "store_manual_frames", fail_manual)
        assert client.post(complete_url, json=payload).status_code == 409
        with app.state.session_factory() as session:
            assert session.scalar(select(Question.id)) is None
            assert session.scalar(select(execution.WorkflowRun.status)) == "waiting"
        assert list((folder / "crops").glob("*.jpg")) == []
        monkeypatch.setattr(scan_service, "store_manual_frames", original_store)
        original_claim = execution._claim
        barrier = threading.Barrier(2)
        def simultaneous_claim(session, run):
            barrier.wait(timeout=5)
            return original_claim(session, run)
        monkeypatch.setattr(execution, "_claim", simultaneous_claim)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: client.post(complete_url, json=payload), range(2)))
        monkeypatch.setattr(execution, "_claim", original_claim)
        assert all(result.status_code in (200, 409) for result in results)
        assert any(result.status_code == 200 for result in results)
        completed = next(result for result in results if result.status_code == 200)
        assert completed.json()["next_url"] == f"/scans/{scan_id}/workflow-review"
        assert client.post(complete_url, json=payload).status_code == 200
        with app.state.session_factory() as session:
            questions = list(session.scalars(select(Question)))
            assert len(questions) == 2
            assert {(question.subject, question.question_type) for question in questions} == {("英语", "阅读")}
        assert client.post(f"/scans/{scan_id}/questions", data={"no": "1"}).status_code == 409
        assert client.get(completed.json()["next_url"]).status_code == 200


def test_scan_workflow_routes_resume_crop_select_and_review_on_pinned_version(tmp_path, monkeypatch):
    from cairn import ocr

    db_url = f"sqlite:///{tmp_path / 'full-web.db'}"
    app = create_app(db_url=db_url)
    folder = tmp_path / "scans"
    folder.mkdir()
    Image.new("RGB", (100, 100), "white").save(folder / "paper.png")
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "1")
    monkeypatch.setenv("CAIRN_TOOL_CUT", "1")
    monkeypatch.setenv("CAIRN_TOOL_OCR", "1")
    monkeypatch.setattr(scan_service, "_crop_page", lambda path: (None, None))
    monkeypatch.setattr(ocr, "read_page", lambda path: SimpleNamespace(lines=[], width=100, height=100, angle=0))
    monkeypatch.setattr(ocr, "cut_questions", lambda lines, w, h: [QuestionBlock(1, (0, 0, 30, 30), "计算这道数学题", [0])])
    steps = [{"node_id": "upright", "type": "auto", "params": {}},
             {"node_id": "crop", "type": "human", "params": {}},
             {"node_id": "cut", "type": "auto", "params": {}},
             {"node_id": "select", "type": "human", "params": {}},
             {"node_id": "recognize", "type": "auto", "params": {}},
             {"node_id": "review", "type": "human", "params": {}}]
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            session.add(WorkflowVersion(version_id="default:v3", workflow_id="default", number=3,
                                        graph_json="{}", runtime_json=json.dumps({"version_id": "default:v3", "steps": steps})))
            session.flush()
            scan = Scan(learner_id=app.state.learner_id, device_id="d", device_name="d", width=100, height=100,
                        path="scans/paper.png", workflow_version_id="default:v3")
            session.add(scan)
            session.flush()
            execution.start(session, scan)
            session.commit()
            scan_id = scan.id
        started = client.post(f"/scans/{scan_id}/workflow-continue", data={}, follow_redirects=False)
        assert started.status_code == 303
        assert client.get(started.headers["location"], follow_redirects=False).headers["location"] == f"/scans/{scan_id}/crop"
        assert client.get(f"/scans/{scan_id}/crop").status_code == 200
        cropped = client.post(f"/scans/{scan_id}/crop", json={"skip": True})
        assert cropped.status_code == 200
        assert client.get(cropped.json()["next_url"], follow_redirects=False).headers["location"] == f"/scans/{scan_id}/select"
        done = client.post(f"/api/scans/{scan_id}/workflow-selection", json={"auto_numbers": [1], "manual_frames": []})
        assert done.status_code == 200 and done.json()["next_url"] == f"/scans/{scan_id}/workflow-review"
        with app.state.session_factory() as session:
            question_id = session.scalar(select(ScanQuestion.question_id).where(ScanQuestion.scan_id == scan_id))
        decision = client.post(f"/scans/{scan_id}/workflow-review/{question_id}", data={"decision": "confirmed"}, follow_redirects=False)
        assert decision.status_code == 303
        progress = client.get(f"/api/scans/{scan_id}/workflow").json()
        assert progress["version_id"] == "default:v3" and progress["status"] == "completed"


def test_new_upload_runs_default_workflow_without_legacy_bypass(tmp_path, monkeypatch):
    from cairn import ocr

    app = create_app(db_url=f"sqlite:///{tmp_path / 'upload-workflow.db'}")
    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "local-only")
    monkeypatch.setenv("CAIRN_MIN_IMAGE_SIDE", "1")
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "1")
    monkeypatch.setenv("CAIRN_TOOL_CUT", "1")
    monkeypatch.setenv("CAIRN_TOOL_OCR", "1")
    monkeypatch.setattr(scan_service, "_crop_page", lambda path: (None, None))
    monkeypatch.setattr(ocr, "read_page", lambda path: SimpleNamespace(lines=[], width=100, height=100, angle=0))
    monkeypatch.setattr(ocr, "cut_questions", lambda lines, w, h: [QuestionBlock(1, (0, 0, 30, 30), "计算这道数学题", [0])])
    image = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    with TestClient(app) as client:
        upload = client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False)
        assert upload.status_code == 303
        scan_id = int(upload.headers["location"].split("/")[2])
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["version_id"] == "default:v2"
        original_scan_id = scan_id
        from cairn import collection_service
        from cairn.models import CollectionItem
        with app.state.session_factory() as session:
            target = collection_service.create(session, app.state.learner_id, name="卷面练习", subject="数学", tags=[],
                                               license="private", author="家长", source="卷面")
            session.add(Learner(id=999, name="Another learner"))
            session.flush()
            foreign = collection_service.create(session, 999, name="Foreign", subject="数学", tags=[],
                                                license="private", author="A", source="S")
            session.commit()
            collection_id, foreign_id = target.id, foreign.id
        current = client.get("/api/workflows/default").json()
        graph = current["graph"]
        graph["nodes"].append({"id": "strategy-1", "node_id": "strategy", "params": {"strategy": "leitner"}})
        graph["edges"].append({"source": "review-1", "target": "strategy-1"})
        graph["nodes"].append({"id": "tutor-1", "node_id": "guided_tutor", "params": {}})
        graph["edges"].append({"source": "strategy-1", "target": "tutor-1"})
        graph["nodes"].append({"id": "recap-1", "node_id": "text_recap", "params": {}})
        graph["edges"].append({"source": "tutor-1", "target": "recap-1"})
        graph["nodes"].append({"id": "collection-1", "node_id": "collection_append", "params": {"collection_id": collection_id}})
        graph["edges"].append({"source": "recap-1", "target": "collection-1"})
        headers = {"Authorization": "Bearer local-only"}
        import copy
        foreign_graph = copy.deepcopy(graph)
        foreign_graph["nodes"][-1]["params"]["collection_id"] = foreign_id
        assert client.post("/api/workflows/default/preview", headers=headers, json={"graph": foreign_graph}).status_code == 422
        saved = client.post("/api/workflows/default", headers=headers, json={
            "title": current["title"], "expected_revision": current["revision"], "graph": graph,
        })
        assert saved.status_code == 200
        published = client.post("/api/workflows/default/publish", headers=headers)
        assert published.status_code == 200 and published.json()["version_id"] == "default:v3"
        current_learner = app.state.learner_id
        app.state.learner_id = 999
        try:
            refused = client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")})
            assert refused.status_code == 409 and "不属于当前学习者" in refused.text
        finally:
            app.state.learner_id = current_learner
        second = client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False)
        assert second.status_code == 303
        scan_id = int(second.headers["location"].split("/")[2])
        assert client.get(f"/api/scans/{original_scan_id}/workflow").json()["version_id"] == "default:v2"
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["version_id"] == "default:v3"
        assert client.delete(f"/api/collections/{collection_id}?expected_revision=0", headers=headers).status_code == 409
        assert client.post("/api/workflows/default/rollback/default:v2", headers=headers).status_code == 200
        assert client.delete(f"/api/collections/{collection_id}?expected_revision=0", headers=headers).status_code == 409
        assert client.get("/api/workflows/default/runs").status_code == 401
        runs = client.get("/api/workflows/default/runs", headers=headers).json()["runs"]
        assert {run["version_id"] for run in runs} == {"default:v2", "default:v3"}
        assert all(set(run) == {"scan_id", "version_id", "status", "updated_at"} for run in runs)
        assert all(datetime.fromisoformat(run["updated_at"]).utcoffset() == timedelta(0) for run in runs)
        assert client.post(f"/scans/{scan_id}/confirm", data={}).status_code == 409
        assert client.get(f"/scans/{scan_id}/ocr", follow_redirects=False).headers["location"] == f"/scans/{scan_id}/workflow"
        started = client.post(f"/scans/{scan_id}/workflow-continue", data={"lang": "en"}, follow_redirects=False)
        assert started.status_code == 303 and started.headers["location"].endswith("?lang=en")
        next_page = client.get(started.headers["location"], follow_redirects=False)
        assert next_page.headers["location"] == f"/scans/{scan_id}/crop?lang=en"
        crop_page = client.get(next_page.headers["location"])
        assert '<html lang="en">' in crop_page.text and "Outline the page" in crop_page.text
        assert "Brightness difference" in crop_page.text and "Detection details" in crop_page.text
        assert client.post(f"/scans/{scan_id}/crop", json={"skip": True}).status_code == 200
        selected_page = client.get(f"/scans/{scan_id}/workflow?lang=en", follow_redirects=False)
        assert selected_page.headers["location"] == f"/scans/{scan_id}/select?lang=en"
        selected_page = client.get(selected_page.headers["location"])
        assert '<html lang="en">' in selected_page.text and "Select questions to practice" in selected_page.text
        assert 'data-lang="en"' in selected_page.text and 'value="Math"' in selected_page.text
        assert "destination collection uses Math" in selected_page.text
        mismatch = client.post(f"/api/scans/{scan_id}/workflow-selection", json={"auto_numbers": [1], "manual_frames": [], "subject": "英语"})
        assert mismatch.status_code == 409 and "科目与目标题集不一致" in mismatch.json()["detail"]
        with app.state.session_factory() as session:
            assert session.scalar(select(ScanQuestion.question_id).where(ScanQuestion.scan_id == scan_id)) is None
        selection = client.post(f"/api/scans/{scan_id}/workflow-selection", json={"auto_numbers": [1], "manual_frames": []})
        assert selection.status_code == 200
        review_page = client.get(f"/scans/{scan_id}/workflow?lang=en", follow_redirects=False)
        assert review_page.headers["location"] == f"/scans/{scan_id}/workflow-review?lang=en"
        assert "Check this question" in client.get(review_page.headers["location"]).text
        with app.state.session_factory() as session:
            from cairn import entry_service, practice_service, stats_service
            question_id = session.scalar(select(ScanQuestion.question_id).where(ScanQuestion.scan_id == scan_id))
            item = session.scalar(select(ErrorItem).where(ErrorItem.question_id == question_id))
            item_id = item.id
            assert item_id not in [item.error_item_id for item in practice_service.fetch_practice(session, app.state.learner_id, datetime.now(timezone.utc))]
            assert practice_service.get_practice_item(session, app.state.learner_id, item_id) is None
            assert stats_service.practice_today(session, app.state.learner_id).remaining == 0
            assert not stats_service.build_stats(session, app.state.learner_id).completion.has_data
            with pytest.raises(entry_service.ValidationError):
                practice_service.submit_answer(session, app.state.learner_id, item_id, "answer")
            with pytest.raises(entry_service.ValidationError):
                review_service.grade(session, item_id, "good")
        assert client.post("/review-queue/reject", data={"item_id": str(item_id)}).status_code == 409
        assert client.post(f"/questions/{question_id}/delete").status_code == 409
        from cairn import web
        original_handlers = web.scan_workflow_handlers
        interrupted = {"active": True}
        def intermittent_collection(url):
            mapping = original_handlers(url)
            original = mapping["collection_append"]
            def handler(session, scan, params):
                if interrupted["active"]:
                    raise RuntimeError("simulated failure")
                original(session, scan, params)
            mapping["collection_append"] = handler
            return mapping
        monkeypatch.setattr(web, "scan_workflow_handlers", intermittent_collection)
        failed = client.post(f"/scans/{scan_id}/workflow-review/{question_id}", data={"decision": "confirmed"})
        assert failed.status_code == 200 and "跳过收录，保留题目" in failed.text
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["status"] == "failed"
        interrupted["active"] = False
        assert client.post(f"/scans/{scan_id}/workflow-retry", data={}).status_code == 200
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["status"] == "completed"
        assert f'/scans/{scan_id}/confirm' not in client.get(f"/scans/{scan_id}").text
        with app.state.session_factory() as session:
            assert session.scalar(select(ErrorItem.workflow_strategy_id).where(ErrorItem.question_id == question_id)) == "leitner"
            assert item_id in [item.error_item_id for item in practice_service.fetch_practice(session, app.state.learner_id, datetime.now(timezone.utc))]
            assert session.get(ErrorItem, item_id).workflow_tutor_enabled == 1
            assert session.get(ErrorItem, item_id).workflow_recap_enabled == 1
            assert stats_service.practice_today(session, app.state.learner_id).remaining == 1
            assert session.scalar(select(CollectionItem.question_id).where(CollectionItem.collection_id == collection_id)) == question_id
            question = session.get(Question, question_id)
            question.answer, question.analysis = "42", "计算得到结果。"
            session.commit()
        assert f'/tutor?question_id={question_id}' in client.get("/practice").text
        assert f'/practice/recap/{item_id}' in client.get("/practice").text
        from cairn.tutor import llm
        monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: '{"assessment":"needs_more","focus":"method"}')
        page = client.get(f"/practice/recap/{item_id}?lang=en")
        assert page.status_code == 200 and "Explain your thinking" in page.text
        import re
        key = re.search(r'name="request_key" value="([a-f0-9]{32})"', page.text).group(1)
        started = client.post(f"/practice/recap/{item_id}", data={"request_key": key, "text": "I add them", "lang": "en"}, follow_redirects=False)
        assert started.status_code == 303
        assert "What method did you use?" in client.get(started.headers["location"]).text
        assert "Continue your previous response" in client.get(f"/practice/recap/{item_id}?lang=en").text
        monkeypatch.setattr(llm, "chat", lambda *args, **kwargs: '{"assessment":"adequate","focus":"none"}')
        result_url = started.headers["location"].split("?")[0]
        followed = client.post(result_url + "/followup", data={"text": "I add the quantities", "lang": "en"}, follow_redirects=False)
        assert followed.status_code == 303 and "Your explanation has been saved" in client.get(followed.headers["location"]).text
        unsure_form = {"request_key": "9" * 32, "text": "", "lang": "en", "action": "unsure"}
        first_unsure = client.post(f"/practice/recap/{item_id}", data=unsure_form, follow_redirects=False)
        second_unsure = client.post(f"/practice/recap/{item_id}", data=unsure_form, follow_redirects=False)
        assert first_unsure.status_code == 303 and first_unsure.headers["location"] == second_unsure.headers["location"]
        assert "Saved for another review" in client.get(first_unsure.headers["location"]).text
        with app.state.session_factory() as session:
            from cairn.models import Review
            assert len(list(session.scalars(select(Review).where(Review.error_item_id == item_id)))) == 1
        from cairn.tutor import service as tutor_service
        monkeypatch.setattr(tutor_service, "_generate", lambda stage, question: ("先说说你的思路。", False))
        tutor = client.post("/tutor/start", json={"question_id": question_id})
        assert tutor.status_code == 200 and tutor.json()["stage"] == "ask_thought"
        deleted = client.post(f"/questions/{question_id}/delete", follow_redirects=False)
        assert deleted.status_code == 303
        with app.state.session_factory() as session:
            from cairn.models.tutor import TutorMessage, TutorSession
            assert session.get(Question, question_id) is None
            assert session.scalar(select(WorkflowReviewItem.id).where(WorkflowReviewItem.question_id == question_id)) is None
            assert session.scalar(select(TutorSession.id)) is None
            assert session.scalar(select(TutorMessage.id)) is None
            from cairn.models.recap import RecapAttempt
            assert session.scalar(select(RecapAttempt.id)) is None
            assert session.get(Scan, scan_id) is not None
            assert session.get(Scan, original_scan_id) is not None
            assert session.scalar(select(CollectionItem.id).where(CollectionItem.collection_id == collection_id)) is None
        revision = client.get(f"/api/collections/{collection_id}", headers=headers).json()["revision"]
        assert client.delete(f"/api/collections/{collection_id}?expected_revision={revision}", headers=headers).status_code == 200
        assert client.post("/api/workflows/default/rollback/default:v3", headers=headers).status_code == 422


def test_camera_capture_starts_pinned_default_workflow(tmp_path):
    from cairn.capture.fake import FakeCameraBackend

    app = create_app(db_url=f"sqlite:///{tmp_path / 'camera-workflow.db'}")
    with TestClient(app) as client:
        app.state.cameras = FakeCameraBackend()
        response = client.post("/devices/fake:uvc-pro/snapshot", follow_redirects=False)
        assert response.status_code == 303
        scan_id = int(response.headers["location"].split("/")[2])
        progress = client.get(f"/api/scans/{scan_id}/workflow").json()
        assert (progress["version_id"], progress["status"]) == ("default:v2", "pending")


def test_failed_cut_can_be_explicitly_skipped_for_manual_selection(tmp_path, monkeypatch):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'manual-workflow.db'}")
    monkeypatch.setenv("CAIRN_MIN_IMAGE_SIDE", "1")
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "1")
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")
    monkeypatch.setattr(scan_service, "_crop_page", lambda path: (None, None))
    monkeypatch.setattr(scan_service, "frame_text", lambda path: "手动圈选的题目")
    image = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    with TestClient(app) as client:
        upload = client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False)
        scan_id = int(upload.headers["location"].split("/")[2])
        assert client.post(f"/scans/{scan_id}/workflow-manual-cut").status_code == 409
        client.post(f"/scans/{scan_id}/workflow-continue")
        assert client.post(f"/scans/{scan_id}/crop", json={"skip": True}).status_code == 200
        progress = client.get(f"/api/scans/{scan_id}/workflow").json()
        assert progress["status"] == "failed" and progress["steps"][2]["status"] == "failed"
        assert "跳过自动切题，自己圈题" in client.get(f"/scans/{scan_id}/workflow").text
        assert client.post(f"/scans/{scan_id}/workflow-manual-cut", follow_redirects=False).status_code == 303
        progress = client.get(f"/api/scans/{scan_id}/workflow").json()
        assert progress["status"] == "waiting" and progress["steps"][2]["status"] == "skipped"
        selection = client.post(f"/api/scans/{scan_id}/workflow-selection", json={
            "auto_numbers": [], "manual_frames": [[10, 10, 80, 80]], "subject": "语文", "question_type": "阅读",
        })
        assert selection.status_code == 200
        with app.state.session_factory() as session:
            question = session.scalar(select(Question))
            assert (question.subject, question.question_type) == ("语文", "阅读")
            question_id = question.id
        assert client.post(f"/scans/{scan_id}/workflow-review/{question_id}", data={"decision": "confirmed"}).status_code == 200
        assert client.get(f"/api/scans/{scan_id}/workflow").json()["status"] == "completed"


def test_disabled_preprocessing_keeps_original_and_waits_for_crop(tmp_path, monkeypatch):
    app = create_app(db_url=f"sqlite:///{tmp_path / 'no-preprocess.db'}")
    monkeypatch.setenv("CAIRN_MIN_IMAGE_SIDE", "1")
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "0")
    def must_not_process(path):
        raise AssertionError("disabled preprocessing must not run")
    monkeypatch.setattr(scan_service, "_crop_page", must_not_process)
    image = io.BytesIO()
    Image.new("RGB", (100, 100), "white").save(image, format="PNG")
    with TestClient(app) as client:
        upload = client.post("/scans/upload", files={"file": ("paper.png", image.getvalue(), "image/png")}, follow_redirects=False)
        scan_id = int(upload.headers["location"].split("/")[2])
        client.post(f"/scans/{scan_id}/workflow-continue", follow_redirects=False)
        progress = client.get(f"/api/scans/{scan_id}/workflow").json()
        assert progress["status"] == "waiting" and progress["steps"][1]["node_id"] == "crop"
        assert client.get(f"/api/scans/{scan_id}/workflow-image").content == image.getvalue()
