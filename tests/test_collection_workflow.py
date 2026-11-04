"""Contracts for the separate collection-to-practice workflow."""

import pytest

from cairn.workflow.contracts import validate_collection_runtime, validate_scan_runtime
from cairn.workflow.execution import RunError


def step(node_id, params=None):
    return {"node_id": node_id, "type": "auto", "params": params or {}}


def test_collection_runtime_accepts_a_pinned_version_and_learning_nodes():
    validate_collection_runtime([
        step("collection_source", {"version_id": 12}),
        step("strategy", {"strategy": "leitner"}),
        step("guided_tutor"),
        step("text_recap"),
    ])


@pytest.mark.parametrize("steps", [
    [],
    [step("collection_source")],
    [step("collection_source", {"version_id": True})],
    [step("collection_source", {"version_id": 0})],
    [step("guided_tutor"), step("collection_source", {"version_id": 12})],
    [step("collection_source", {"version_id": 12}), step("collection_source", {"version_id": 12})],
    [step("collection_source", {"version_id": 12}), step("strategy", {"strategy": "missing"})],
    [step("collection_source", {"version_id": 12}), step("guided_tutor", {"extra": 1})],
    [step("collection_source", {"version_id": 12}), step("cut")],
])
def test_collection_runtime_rejects_unsupported_or_unpinned_graphs(steps):
    with pytest.raises(RunError):
        validate_collection_runtime(steps)


def test_collection_source_is_not_a_scanning_step():
    with pytest.raises(RunError):
        validate_scan_runtime([step("collection_source", {"version_id": 12})])


def test_publish_start_and_deactivate_collection_workflow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from cairn import collection_service
    from cairn.models import CollectionPracticeItem, ErrorItem, Question, Workflow
    from cairn.web import create_app

    monkeypatch.setenv("CAIRN_WORKFLOW_ADMIN_TOKEN", "test-token")
    app = create_app(f"sqlite:///{tmp_path / 'collection-workflow.db'}")
    with TestClient(app) as client:
        with app.state.session_factory() as session:
            learner_id = app.state.learner_id
            question = Question(learner_id=learner_id, subject="数学", question_type="计算",
                                content="Original snapshot", answer="42")
            session.add(question)
            session.flush()
            session.add(ErrorItem(learner_id=learner_id, question_id=question.id, confidence=1.0))
            second = Question(learner_id=learner_id, subject="数学", question_type="计算",
                              content="Second snapshot", answer="21")
            session.add(second)
            session.flush()
            session.add(ErrorItem(learner_id=learner_id, question_id=second.id, confidence=1.0))
            collection = collection_service.create(session, learner_id, name="Test collection", subject="数学",
                                                   tags=[], license="private", author="Parent", source="Home")
            collection_service.replace_items(session, learner_id, collection.id, 0, [
                {"question_id": question.id, "tags": [], "difficulty": None, "source": "Home"},
                {"question_id": second.id, "tags": [], "difficulty": None, "source": "Home"}])
            version = collection_service.publish(session, learner_id, collection.id, 1)
            session.commit()
            version_id, original_id = version.id, question.id
            scan_version = session.get(Workflow, "default").active_version_id
        auth = {"Authorization": "Bearer test-token"}
        graph = {"nodes": [
            {"id": "source", "node_id": "collection_source", "params": {"version_id": version_id}},
            {"id": "strategy", "node_id": "strategy", "params": {"strategy": "leitner"}},
        ], "edges": [{"source": "source", "target": "strategy"}]}
        assert client.post("/api/workflows/default/validate", headers=auth, json={"graph": graph}).status_code == 422
        assert client.post("/api/workflows/collection/validate", headers=auth, json={"graph": graph}).status_code == 200
        saved = client.post("/api/workflows/collection", headers=auth,
                            json={"title": "Collection", "expected_revision": 1, "graph": graph})
        assert saved.status_code == 200
        published = client.post("/api/workflows/collection/publish", headers=auth,
                                json={"expected_revision": saved.json()["revision"]})
        assert published.status_code == 200
        assert "Test collection" in client.get("/collection-practice?lang=en").text
        assert "新加入 2 道" in client.post("/collection-practice").text
        assert "保留已有练习 2 道" in client.post("/collection-practice").text
        with app.state.session_factory() as session:
            assert session.get(Workflow, "default").active_version_id == scan_version
            mappings = list(session.scalars(select(CollectionPracticeItem).order_by(CollectionPracticeItem.id)))
            copy_id, second_copy_id = [item.question_id for item in mappings]
            assert session.get(Question, copy_id).content == "Original snapshot"
        assert client.post(f"/questions/{original_id}/delete").status_code == 409
        assert client.post("/api/workflows/collection/deactivate").status_code == 401
        assert client.post("/api/workflows/collection/deactivate", headers=auth).status_code == 200
        assert client.post(f"/questions/{original_id}/delete").status_code == 409
        assert client.post(f"/questions/{copy_id}/delete", follow_redirects=False).status_code == 303
        assert client.post(f"/questions/{original_id}/delete").status_code == 409
        assert client.post(f"/questions/{second_copy_id}/delete", follow_redirects=False).status_code == 303
        assert client.post(f"/questions/{original_id}/delete", follow_redirects=False).status_code == 303
