"""整页 OCR 卡（通道卡 B 前半）：行聚类、中英拼接、禁用开关、错误路径、惰性初始化。

全部用**假引擎**（monkeypatch 掉 engine 模块的 `_engine`），不依赖真模型；
真图实跑的验收在 docs/EVIDENCE.md，不进测试套件。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.ocr import engine as ocr_engine
from cairn.ocr.engine import OcrError, OcrLine, recognize
from cairn.ocr.lines import group_rows, to_text
from cairn.web import create_app


def _line(text: str, x: int, y: int, w: int = 100, h: int = 24, conf: float = 0.9) -> OcrLine:
    box = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    return OcrLine(text=text, box=box, confidence=conf, y_center=y + h // 2)


class FakeEngine:
    """模仿 RapidOCR 的可调用对象：返回 (结果, 耗时)，结果可为 None。"""

    def __init__(self, result, exc: Exception | None = None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def __call__(self, path: str):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result, 0.1


@pytest.fixture
def install_engine(monkeypatch):
    """把假引擎塞进模块缓存，绕开真模型；测完由 monkeypatch 还原为 None。"""

    def _install(fake: FakeEngine) -> FakeEngine:
        monkeypatch.setattr(ocr_engine, "_engine", fake)
        return fake

    return _install


# ---------- group_rows：先切栏、栏内聚行 ----------

def test_group_rows_same_row_y_diff_5px():
    # 单栏同一视觉行两块：y 差 5px，框高度 24px（中位数×0.6 = 14.4 容差）→ 一栏一行；
    # 块间距 20px 远小于栏缝阈值（页宽 220 → max(13.2, 72)=72），不会误切栏
    rows = group_rows([_line("甲", 0, 100), _line("乙", 120, 105)])
    assert len(rows) == 1  # 一栏
    assert [line.text for line in rows[0][0]] == ["甲", "乙"]


def test_group_rows_different_rows_y_diff_60px():
    # 不同行：y 差 60px > 容差 → 一栏两行，且自上而下
    rows = group_rows([_line("下", 0, 160), _line("上", 0, 100)])
    assert len(rows) == 1
    assert rows[0][0][0].text == "上"
    assert rows[0][1][0].text == "下"


def test_two_columns_same_y_must_not_merge():
    # 并排两页（左页 x≈100-300，右页 x≈1200-1400），同一 y → 必须分成两栏两行，
    # 绝不允许缝成一句。整页宽 1400 → 阈值 max(84, 72)=84，栏缝 900 远超。
    rows = group_rows([_line("左行", 100, 100), _line("右行", 1200, 100)])
    assert len(rows) == 2
    assert rows[0][0][0].text == "左行"
    assert rows[1][0][0].text == "右行"
    assert to_text(rows) == "左行\n\n右行"


def test_single_column_same_y_nearby_blocks_still_merge():
    # 单栏：同一 y 的紧邻两块（缝宽 10px < 行高中位数 24px）仍然聚成一行
    rows = group_rows([_line("解下列", 0, 100, w=150), _line("方程", 160, 100, w=150)])
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert to_text(rows) == "解下列方程"


def test_single_column_full_page_no_split():
    # 单栏整页：各行块边界错开，不存在贯穿整页的垂直空白带 → 一栏多行
    lines = [
        _line("甲", 0, 100), _line("乙", 120, 105),
        _line("丙", 0, 160, w=220),
        _line("丁", 0, 220), _line("戊", 120, 225),
        _line("己", 0, 280, w=220),
    ]
    rows = group_rows(lines)
    assert len(rows) == 1
    assert len(rows[0]) == 4


def test_three_columns_middle_narrow_split():
    # 三栏（中间窄栏）：栏内行距 30px / 块高 24px 的密排文字，栏缝贯穿整页
    def col(text: str, x: int, n: int) -> list:
        return [_line(f"{text}{i}", x, 100 + i * 30, w=150) for i in range(n)]

    rows = group_rows(col("左", 0, 7) + col("中", 600, 6) + col("右", 1200, 7))
    assert len(rows) == 3
    assert [len(c) for c in rows] == [7, 6, 7]
    assert rows[0][0][0].text == "左0"
    assert rows[1][0][0].text == "中0"
    assert rows[2][0][0].text == "右0"
    assert to_text(rows).count("\n\n") == 2  # 栏间两个空行分隔


def test_to_text_column_order_left_to_right():
    # 栏输出顺序 = 左→右，栏间空行
    rows = group_rows([_line("右", 1200, 100), _line("左", 100, 100)])
    assert to_text(rows) == "左\n\n右"


def test_group_rows_empty():
    assert group_rows([]) == []


# ---------- to_text ----------

def test_to_text_chinese_blocks_no_space():
    rows = group_rows([_line("解下列", 0, 100), _line("方程", 120, 100)])
    assert to_text(rows) == "解下列方程"


def test_to_text_english_blocks_get_space():
    rows = group_rows([_line("cm", 0, 100), _line("2", 120, 100)])
    assert to_text(rows) == "cm 2"


# ---------- recognize：禁用 / 错误路径 / 惰性 ----------

def test_recognize_raises_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    image = tmp_path / "any.png"
    image.write_bytes(b"fake")  # 文件存在也会被禁用开关拦下
    with pytest.raises(OcrError, match="CAIRN_OCR_DISABLE"):
        recognize(str(image))


def test_recognize_missing_file_raises_ocr_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr_engine, "_engine", FakeEngine([]))  # 不许碰到真模型
    with pytest.raises(OcrError, match="图片不存在"):
        recognize(str(tmp_path / "no-such-image.png"))


def test_recognize_wraps_engine_failure(monkeypatch, tmp_path):
    image = tmp_path / "garbage.png"
    image.write_bytes(b"not an image at all")
    fake = FakeEngine(None, exc=RuntimeError("cannot identify image file"))
    monkeypatch.setattr(ocr_engine, "_engine", fake)
    with pytest.raises(OcrError, match="解码失败"):
        recognize(str(image))


def test_recognize_none_result_means_no_text(monkeypatch, tmp_path):
    image = tmp_path / "blank.png"
    image.write_bytes(b"fake")
    monkeypatch.setattr(ocr_engine, "_engine", FakeEngine(None))
    assert recognize(str(image)) == []


def test_engine_is_lazy_on_import():
    # import 不得加载模型：模块级缓存保持 None
    assert ocr_engine._engine is None


# ---------- web 路由（假引擎，不落库断言） ----------

def test_scan_ocr_page_renders_lines(tmp_path, monkeypatch):
    from cairn.models import Scan, make_engine, make_session_factory
    from cairn.models.learner import utcnow

    db_path = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db_path}")
    with TestClient(app) as client:
        engine = make_engine(f"sqlite:///{db_path}")
        factory = make_session_factory(engine)
        with factory() as session:
            scan = Scan(
                learner_id=client.app.state.learner_id,
                device_id="upload",
                device_name="测试",
                width=400,
                height=100,
                captured_at=utcnow(),
                path="scans/fake.png",
                source="upload",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
        image = tmp_path / "fake.png"
        image.write_bytes(b"fake")
        (tmp_path / "scans").mkdir()
        image.rename(tmp_path / "scans" / "fake.png")
        result = [((10, 20), (60, 20), (60, 40), (10, 40)), "答案 42", 0.95]
        monkeypatch.setattr(ocr_engine, "_engine", FakeEngine([result]))

        resp = client.get(f"/scans/{scan_id}/ocr")
        assert resp.status_code == 200
        body = resp.text
        assert "答案 42" in body
        assert "95%" in body
        assert "(10, 20)" in body  # 原始尺寸下的框坐标


def test_scan_ocr_missing_scan_returns_404(tmp_path):
    db_path = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db_path}")
    with TestClient(app) as client:
        resp = client.get("/scans/999999/ocr")
        assert resp.status_code == 404
        assert "没有这张照片" in resp.text


def test_scan_ocr_engine_failure_shows_readable_error_not_500(tmp_path, monkeypatch):
    from cairn.models import Scan, make_engine, make_session_factory
    from cairn.models.learner import utcnow

    db_path = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db_path}")
    with TestClient(app) as client:
        engine = make_engine(f"sqlite:///{db_path}")
        factory = make_session_factory(engine)
        with factory() as session:
            scan = Scan(
                learner_id=client.app.state.learner_id,
                device_id="upload",
                device_name="测试",
                width=400,
                height=100,
                captured_at=utcnow(),
                path="scans/fake.png",
                source="upload",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
        image = tmp_path / "fake.png"
        image.write_bytes(b"fake")
        (tmp_path / "scans").mkdir()
        image.rename(tmp_path / "scans" / "fake.png")
        fake = FakeEngine(None, exc=RuntimeError("cannot identify image file"))
        monkeypatch.setattr(ocr_engine, "_engine", fake)

        resp = client.get(f"/scans/{scan_id}/ocr")
        assert resp.status_code == 200  # 可读提示，不是 500
        # G2-4 文案：「识别失败」→「没读出来」（识别/OCR 红线，§4.1 一律说读题）
        assert "没读出来" in resp.text
        assert "解码失败" in resp.text
