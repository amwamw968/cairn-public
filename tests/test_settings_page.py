"""卡 G2-7：设置页（GET/POST /settings）。

- 孩子年级（选填）+ 每天练几题（默认 5，1-50 夹紧）；主按钮「保存」，303 回带成功提示；
- 数据导出/备份三条能力链接 + 关于（版本号只住 pyproject.toml，页面读它）；
- 主流程不依赖设置项（蓝图 E.12）；迁移 22 给 learner.daily_target 加列（默认 5）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cairn.models import Learner, make_engine, make_session_factory
from cairn.web import create_app


def _daily_target(db) -> int:
    engine = make_engine(f"sqlite:///{db}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            return session.query(Learner).first().daily_target
    finally:
        engine.dispose()


def test_settings_page_renders_and_saves(tmp_path):
    db = tmp_path / "s.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        html = c.get("/settings").text
        assert "设置" in html
        assert "孩子年级（选填）" in html
        assert "每天练几题" in html
        assert 'value="5"' in html, "默认 5 题"
        assert "数据导出 / 备份" in html
        for link in ("/export", "/export/anki", "/import"):
            assert f'href="{link}"' in html
        assert "Cairn 凯恩 · 您的学习小伙伴" in html
        # 关于里显示的版本号必须与 pyproject.toml 单源一致
        import re
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        want = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M).group(1)
        assert want in html, f"设置页应显示版本 {want}"
        # 唯一主按钮是「保存」
        assert html.count('class="btn btn--primary"') == 1
        assert "保存" in html

        resp = c.post(
            "/settings",
            data={"grade_level": "primary-3", "daily_target": "7"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings?saved=1"

        html = c.get("/settings?saved=1").text
        assert "保存好了" in html
        assert 'value="7"' in html
        assert "primary-3" in html

    assert _daily_target(db) == 7


def test_settings_clamps_daily_target(tmp_path):
    """每天练几题夹在 1-50；瞎填回落默认 5。"""
    db = tmp_path / "c.db"
    app = create_app(f"sqlite:///{db}")
    with TestClient(app) as c:
        c.post("/settings", data={"daily_target": "999"})
        assert _daily_target(db) == 50
        c.post("/settings", data={"daily_target": "-3"})
        assert _daily_target(db) == 1
        c.post("/settings", data={"daily_target": "abc"})
        assert _daily_target(db) == 5
