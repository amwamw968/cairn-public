"""配方层（底座第二步）：一页纸怎么走写在 YAML 里，跑的时候每步留痕。

三件事：

1. 形状与校验（AC2）：`scan-entry` 是三步；未知工具、缺 name/tool 一律
   `RecipeError`，话里带出是哪份配方、第几步、哪个名字。
2. 执行（AC3 / AC4）：全开时三步都 `ran` 且顺序与 YAML 一致；关掉任一步的工具，
   那步 `skipped_disabled`、后面的步骤照跑，页面仍是 200。
3. 接线（AC5 / AC6）：整页识别页与切题页都走 runner（行为与改造前一致：
   OCR 行数、题块数不变；paper-03 仍是 18 题）；`/tools` 能看到当前配方。

真图那一条靠 `CAIRN_SAMPLE_DIR` 打开，没设就跳过——和 `test_ocr_orient.py` 同一套
约定；下面的假引擎用例不依赖任何仓库外文件。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cairn import recipe, tools
from cairn.models import Learner, Scan, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.models.scan import ScanQuestion
from cairn.recipe import RecipeError, spec
from cairn.web import create_app
from sqlalchemy import select

PAGE_W, PAGE_H = 600, 400
SAMPLE_DIR = os.environ.get("CAIRN_SAMPLE_DIR")


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _tools_on_and_recipe_fresh(monkeypatch):
    """每条用例从"六个全开 + 配方缓存干净"起跑，再用一份 tmp 里的配方目录。"""
    for tool in tools.TOOLS:
        monkeypatch.delenv(tool.env, raising=False)
        if tool.legacy_env is not None:
            monkeypatch.delenv(tool.legacy_env, raising=False)
    recipe.clear_cache()
    yield
    recipe.clear_cache()


@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "web.db"


@pytest.fixture()
def client(db_path) -> TestClient:
    app = create_app(f"sqlite:///{db_path}")
    with TestClient(app) as c:
        yield c


class FakeEngine:
    """模仿 RapidOCR 的可调用对象：返回 (结果, 耗时)。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self, path: str):
        self.calls += 1
        return self.result, 0.1


THREE_LINES = [
    (((100, 100), (300, 100), (300, 124), (100, 124)), "1.第一题", 0.95),
    (((100, 140), (300, 140), (300, 164), (100, 164)), "A.3", 0.95),
    (((100, 180), (300, 180), (300, 204), (100, 204)), "2.第二题", 0.95),
]


@pytest.fixture()
def fake_ocr(monkeypatch):
    from cairn.ocr import engine as ocr_engine

    engine = FakeEngine(THREE_LINES)
    monkeypatch.setattr(ocr_engine, "_engine", engine)
    return engine


def _image(tmp_path) -> Path:
    path = tmp_path / "page.png"
    Image.new("RGB", (PAGE_W, PAGE_H), "white").save(path)
    return path


def _scan(tmp_path, db_path) -> int:
    """一张"已经落库、图在盘上"的照片：走 GET 页不用真上传/真预处理。"""
    (tmp_path / "scans").mkdir(exist_ok=True)
    Image.new("RGB", (PAGE_W, PAGE_H), "white").save(tmp_path / "scans" / "fake.png")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner)).first()
            scan = Scan(
                learner_id=learner.id,
                device_id="upload",
                device_name="测试",
                width=PAGE_W,
                height=PAGE_H,
                captured_at=utcnow(),
                path="scans/fake.png",
                source="upload",
            )
            session.add(scan)
            session.commit()
            return scan.id
    finally:
        engine.dispose()


# ------------------------------------------------- 形状与校验（AC2）


def test_scan_entry_recipe_has_four_steps() -> None:
    current = recipe.load("scan-entry")

    assert current.name == "scan-entry"
    assert current.title == "扫一页卷子并切成题"
    assert [(step.name, step.tool, step.on_error) for step in current.steps] == [
        ("摆正", "preprocess", "keep"),
        ("裁切", "crop", "keep"),
        ("切题", "cut", "ask"),
        ("认字", "ocr", "ask"),
    ]


def test_recipe_names_are_the_two_scan_chains() -> None:
    """扫描入库只有两条链：scan-entry（上传默认）与 scan-enhance（多一步增强，留着不删）。

    英语/数学不做成配方（那是下一张卡）。
    """
    assert recipe.names() == ["scan-enhance", "scan-entry"]


def test_load_caches_until_cleared() -> None:
    first = recipe.load("scan-entry")
    assert recipe.load("scan-entry") is first

    recipe.clear_cache()
    assert recipe.load("scan-entry") is not first


def test_unknown_recipe_name_is_loud() -> None:
    with pytest.raises(RecipeError, match="没有这份配方：scan-entryy"):
        recipe.load("scan-entryy")


def test_path_like_recipe_name_is_rejected() -> None:
    with pytest.raises(RecipeError, match="不合法"):
        recipe.load("../scan-entry")


def _write(monkeypatch, tmp_path, name: str, text: str) -> None:
    monkeypatch.setattr("cairn.recipe.loader.RECIPE_DIR", tmp_path)
    (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_unknown_tool_names_the_step_and_the_tool(monkeypatch, tmp_path) -> None:
    _write(
        monkeypatch,
        tmp_path,
        "typo",
        "name: typo\ntitle: 写错工具\nsteps:\n"
        "  - {name: 摆正, tool: preprocess, on_error: keep}\n"
        "  - {name: 切题, tool: cutt, on_error: ask}\n",
    )

    with pytest.raises(RecipeError) as excinfo:
        recipe.load("typo")
    message = str(excinfo.value)
    assert "第 2 步（切题）" in message
    assert "cutt" in message


def test_missing_name_field_names_the_field(monkeypatch, tmp_path) -> None:
    _write(monkeypatch, tmp_path, "noname", "title: 没名字\nsteps: []\n")

    with pytest.raises(RecipeError, match="缺字段 name"):
        recipe.load("noname")


def test_missing_tool_field_names_the_step(monkeypatch, tmp_path) -> None:
    _write(
        monkeypatch,
        tmp_path,
        "notool",
        "name: notool\ntitle: 缺工具\nsteps:\n  - {name: 认字, on_error: ask}\n",
    )

    with pytest.raises(RecipeError) as excinfo:
        recipe.load("notool")
    assert "第 1 步（认字）" in str(excinfo.value)
    assert "缺字段 tool" in str(excinfo.value)


def test_unknown_on_error_is_loud(monkeypatch, tmp_path) -> None:
    _write(
        monkeypatch,
        tmp_path,
        "badmode",
        "name: badmode\ntitle: 兜底写错\nsteps:\n"
        "  - {name: 认字, tool: ocr, on_error: ignore}\n",
    )

    with pytest.raises(RecipeError, match="on_error"):
        recipe.load("badmode")


def test_yaml_name_must_match_file_name(monkeypatch, tmp_path) -> None:
    _write(
        monkeypatch,
        tmp_path,
        "file-one",
        "name: other\ntitle: 名字对不上\nsteps:\n"
        "  - {name: 认字, tool: ocr, on_error: ask}\n",
    )

    with pytest.raises(RecipeError, match="必须一致"):
        recipe.load("file-one")


# ------------------------------------------------- 执行（AC3 / AC4）


def _scan_entry():
    return recipe.load("scan-entry")


def test_all_steps_run_in_yaml_order(tmp_path, fake_ocr) -> None:
    current = _scan_entry()
    result = recipe.run(current, {"path": _image(tmp_path), "scan": None})

    assert result.statuses == [recipe.RAN, recipe.RAN, recipe.RAN, recipe.RAN]
    assert [step.name for step in result.steps] == ["摆正", "裁切", "切题", "认字"]
    assert [step.name for step in result.steps] == [step.name for step in current.steps]
    assert result.data["blocks"]  # 切题结果
    assert result.data["rows"]  # 认字结果（按栏按行整理好）
    assert all(step.ms >= 0 for step in result.steps)
    assert result.trace().endswith("s")


def test_cut_off_does_not_stop_the_rest(tmp_path, monkeypatch, fake_ocr) -> None:
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")

    result = recipe.run(_scan_entry(), {"path": _image(tmp_path), "scan": None})

    assert result.statuses == [
        recipe.RAN,
        recipe.RAN,
        recipe.SKIPPED_DISABLED,
        recipe.RAN,
    ]
    assert result.missing_tools == {"cut": tools.disabled_reason("cut")}
    assert "切题 已关闭" in result.trace()


def test_ocr_off_never_touches_the_page(tmp_path, monkeypatch) -> None:
    """认字关掉时连 read_page 都不该调：切题靠认字，只能待在那儿。"""
    from cairn import ocr as ocr_pkg

    def boom(path):
        raise AssertionError("认字关掉了还在识别")

    monkeypatch.setattr(ocr_pkg, "read_page", boom)
    monkeypatch.setenv("CAIRN_TOOL_OCR", "0")

    result = recipe.run(_scan_entry(), {"path": _image(tmp_path), "scan": None})

    assert result.statuses == [
        recipe.RAN,
        recipe.RAN,
        recipe.SKIPPED_UNAVAILABLE,
        recipe.SKIPPED_DISABLED,
    ]
    assert "认字 已关闭" in result.trace()


def test_preprocess_off_keeps_going_with_the_original(tmp_path, monkeypatch, fake_ocr) -> None:
    monkeypatch.setenv("CAIRN_TOOL_PREPROCESS", "0")
    image = _image(tmp_path)

    result = recipe.run(_scan_entry(), {"path": image, "scan": None})

    assert result.statuses[0] == recipe.SKIPPED_DISABLED
    assert result.statuses[1:] == [recipe.RAN, recipe.RAN, recipe.RAN]
    assert result.data["image_path"] == image  # 没摆正就用原图
    assert result.data["blocks"]


@pytest.mark.parametrize(
    ("on_error", "tail"),
    [("keep", "（保持原样继续）"), ("skip", "（这步先不做）"), ("ask", "")],
)
def test_on_error_decides_how_a_failure_is_swallowed(
    tmp_path, monkeypatch, on_error: str, tail: str
) -> None:
    """三种兜底都继续往下跑，区别只写在原因里（页面据此说话）。"""

    def boom(path):
        raise RuntimeError("引擎没装")

    monkeypatch.setattr("cairn.ocr.read_page", boom)
    broken = spec.Recipe(
        name="broken",
        title="故意坏一步",
        steps=(
            spec.Step(name="摆正", tool="preprocess", on_error="keep"),
            spec.Step(name="切题", tool="cut", on_error=on_error),
        ),
    )

    result = recipe.run(broken, {"path": _image(tmp_path), "scan": None})

    assert result.statuses == [recipe.RAN, recipe.FAILED_FALLBACK]
    assert result.failure_note == f"引擎没装{tail}"


def test_no_fallback_means_raise(tmp_path, monkeypatch) -> None:
    """on_error 写成 keep/skip/ask 以外 = 没兜底，直接往上抛（AC7 的变异口子）。"""

    def boom(path):
        raise RuntimeError("引擎没装")

    monkeypatch.setattr("cairn.ocr.read_page", boom)
    broken = spec.Recipe(
        name="strict",
        title="没兜底",
        steps=(
            spec.Step(name="摆正", tool="preprocess", on_error="keep"),
            spec.Step(name="切题", tool="cut", on_error="raise"),
        ),
    )

    with pytest.raises(RuntimeError, match="引擎没装"):
        recipe.run(broken, {"path": _image(tmp_path), "scan": None})


def test_tool_without_a_handler_is_unavailable_not_a_crash(tmp_path, fake_ocr) -> None:
    """清单里有、但还没有执行器的工具：记一笔"做不了"，不当失败。"""
    odd = spec.Recipe(
        name="odd",
        title="用了还没接线的工具",
        steps=(
            spec.Step(name="摆正", tool="preprocess", on_error="keep"),
            spec.Step(name="判题", tool="grading", on_error="keep"),
            spec.Step(name="认字", tool="ocr", on_error="ask"),
        ),
    )

    result = recipe.run(odd, {"path": _image(tmp_path), "scan": None})

    assert result.statuses == [recipe.RAN, recipe.SKIPPED_UNAVAILABLE, recipe.RAN]


# ------------------------------------------------- 接线：整页识别页（AC3 / AC4）


def test_ocr_page_traces_four_steps(client, tmp_path, db_path, fake_ocr) -> None:
    scan_id = _scan(tmp_path, db_path)

    response = client.get(f"/scans/{scan_id}/ocr")

    assert response.status_code == 200
    # 这份夹具直接塞了一行 scan（没走上传、磁盘上没有增强图、也没有裁切结论），
    # 四步照跑：摆正说"用原图"，裁切现判一次（纯白图分不出纸），切题/认字正常。
    assert (
        "配方：摆正 用原图 · 裁切 跳过（纸和背景分不开，请换深色背景或自己框） · 切题 切出 2 题"
        in response.text
    )
    assert "读出了 2 道题" in response.text  # 行数与题块数与改造前一致


def test_ocr_page_says_which_step_was_off(client, tmp_path, db_path, fake_ocr, monkeypatch) -> None:
    scan_id = _scan(tmp_path, db_path)
    monkeypatch.setenv("CAIRN_TOOL_OCR", "0")

    response = client.get(f"/scans/{scan_id}/ocr")

    assert response.status_code == 200
    assert "认字 已关闭" in response.text
    assert "文字识别已关闭" in response.text  # 与改造前同一句提示
    assert "摆正 用原图" in response.text  # 前面的步骤照跑


def test_ocr_page_with_cut_off_shows_the_trace(client, tmp_path, db_path, fake_ocr, monkeypatch) -> None:
    scan_id = _scan(tmp_path, db_path)
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")

    response = client.get(f"/scans/{scan_id}/ocr")

    assert response.status_code == 200
    assert "切题 已关闭" in response.text
    assert "认字 已关闭" not in response.text  # 后面那步照跑
    assert "读出了" not in response.text  # 没切就没有题块


# ------------------------------------------------- 接线：切题页（AC4 / AC5）


def test_questions_page_uses_the_recipe(client, tmp_path, db_path, fake_ocr) -> None:
    scan_id = _scan(tmp_path, db_path)

    response = client.get(f"/scans/{scan_id}/questions")

    assert response.status_code == 200
    assert "共 2 题" in response.text

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            rows = session.scalars(select(ScanQuestion)).all()
    finally:
        engine.dispose()
    assert [row.no for row in rows] == [1, 2]


def test_questions_page_with_ocr_off_says_so(client, tmp_path, db_path, monkeypatch) -> None:
    scan_id = _scan(tmp_path, db_path)
    monkeypatch.setenv("CAIRN_TOOL_OCR", "0")

    response = client.get(f"/scans/{scan_id}/questions")

    assert response.status_code == 200
    assert "文字识别已关闭" in response.text


# ------------------------------------------------- /tools 页（AC6）


def test_tools_page_shows_the_current_recipe(client) -> None:
    response = client.get("/tools")

    assert response.status_code == 200
    body = response.text
    assert "当前配方" in body
    assert "扫一页卷子并切成题" in body
    assert "scan-entry" in body
    for step_name in ("摆正", "裁切", "切题", "认字"):
        assert step_name in body
    # 一页纸的顺序就是它：不许再写内联样式
    assert "<style" not in body


def test_tools_page_recipe_section_marks_the_disabled_step(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_TOOL_CUT", "0")

    response = client.get("/tools")

    assert response.status_code == 200
    # 「已关闭」只出现在工具表的"关掉的原因"里，配方那一节用「关了」说事
    assert response.text.count("已关闭") == 1
    assert "关了" in response.text


def test_tools_page_survives_a_broken_recipe(client, monkeypatch, tmp_path) -> None:
    """配方坏了不该把排查页也弄崩：那一节写原因，页面照旧 200。"""
    monkeypatch.setattr("cairn.recipe.loader.RECIPE_DIR", tmp_path)
    recipe.clear_cache()

    response = client.get("/tools")

    assert response.status_code == 200
    assert "配方读不出来" in response.text


# ------------------------------------------------- 真图回归（AC5）


@pytest.mark.skipif(
    not SAMPLE_DIR or importlib.util.find_spec("rapidocr_onnxruntime") is None,
    reason="真图回归需要 CAIRN_SAMPLE_DIR + 已安装 rapidocr-onnxruntime",
)
def test_real_paper03_still_cuts_18_through_the_recipe() -> None:
    """改造前后同一份结果：paper-03 仍然是 18 题（这一条走的是配方，不是裸调用）。"""
    path = Path(SAMPLE_DIR) / "paper-03-original.jpg"

    result = recipe.run(recipe.load("scan-entry"), {"path": path, "scan": None})

    assert result.statuses == [recipe.RAN, recipe.RAN, recipe.RAN]
    assert [block.no for block in result.data["blocks"]] == list(range(1, 19))
