"""题号切题 + 方向校正卡：题号正则、跨行合并、排除项、空页、deskew、CLI/web 展示。

全部用**合成 OcrLine / 假引擎**，不依赖真模型与仓库外真图；
真图（paper-03 / paper-04）实跑的验收在 docs/EVIDENCE.md。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import importlib

from cairn.ocr import __main__ as ocr_main
from cairn.ocr.cut import QuestionBlock, cut_questions
from cairn.ocr.deskew import estimate_angle
from cairn.ocr.engine import OcrError, OcrLine
from cairn.web import create_app

PAGE_W, PAGE_H = 1600, 1200


def _line(text: str, x: int, y: int, w: int = 200, h: int = 24, conf: float = 0.9) -> OcrLine:
    box = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    return OcrLine(text=text, box=box, confidence=conf, y_center=y + h // 2)


def _question_lines(*rows: str, y_step: int = 40) -> list[OcrLine]:
    """一栏内自上而下的若干行，行距固定。"""
    return [_line(text, 100, 100 + i * y_step) for i, text in enumerate(rows)]


# ---------- AC2：cut_questions 基本结构 ----------

def test_cut_single_question_block_fields():
    lines = _question_lines("1.计算 5+3=")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, QuestionBlock)
    assert block.no == 1
    assert block.bbox == (100, 100, 300, 124)
    assert block.stem_text == "1.计算 5+3="
    assert block.line_indexes == [0]


def test_cut_line_indexes_are_indices_into_input():
    # 输入乱序（recognize 已按 y 排序，这里验证索引忠实于传入列表）
    lines = [_line("2.第二题", 100, 140), _line("1.第一题", 100, 100)]
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    first = [b for b in blocks if b.no == 1][0]
    assert first.line_indexes == [1]
    assert first.bbox == (100, 100, 300, 124)


def test_cut_bbox_is_union_of_all_member_lines():
    lines = [
        _line("3.先看图", 100, 100, w=200),
        _line("再填空", 500, 140, w=150, h=30),
    ]
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert len(blocks) == 1
    assert blocks[0].bbox == (100, 100, 650, 170)


# ---------- AC4：题号正则与排除项 ----------

@pytest.mark.parametrize(
    "text,expected_no",
    [
        ("1.计算下列各题", 1),
        ("2、解方程", 2),
        ("3．看图填空", 3),
        ("（4）按要求画一画", 4),
        ("(5)量一量", 5),
        ("６、直接写得数", 6),
        ("12.计算 25×4", 12),
    ],
)
def test_question_number_variants(text, expected_no):
    blocks = cut_questions(_question_lines(text), PAGE_W, PAGE_H)
    assert len(blocks) == 1
    assert blocks[0].no == expected_no


def test_option_lines_are_not_question_numbers():
    lines = _question_lines("1.下面哪个对？", "A.3", "B.5")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1]
    assert "A.3" in blocks[0].stem_text and "B.5" in blocks[0].stem_text


def test_page_number_and_footer_rows_are_dropped():
    lines = _question_lines("1.第一题内容", "12", "第 2 页", "- 3 -", "2.第二题内容")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1, 2]
    assert "12" not in blocks[0].stem_text
    assert "第 2 页" not in blocks[0].stem_text
    assert blocks[0].line_indexes == [0]  # 页码行不进任何题
    assert blocks[1].line_indexes == [4]


def test_inline_sequence_is_not_question_number():
    # "4个百"（分隔符不紧跟）与 "2.5米"（小数）都不是题号
    lines = _question_lines("1.数一数，4个百是多少", "2.5米等于多少厘米")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1]
    assert "2.5米" in blocks[0].stem_text


def test_content_before_first_question_is_dropped():
    # 页眉/大标题在首个题号之前，不进任何题块
    lines = _question_lines("一、选择题。（每题2分，共16分）", "1.第一题")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1]
    assert "选择题" not in blocks[0].stem_text


# ---------- AC5：跨行合并 ----------

def test_multiline_merge_chinese_no_space():
    lines = _question_lines("9.在横线上填", "上合适的数", "10.下一题内容")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [9, 10]
    assert blocks[0].stem_text == "9.在横线上填上合适的数"  # 中文相接不加空格
    assert "下一题" not in blocks[0].stem_text  # 不许把下一题内容并进来


def test_multiline_merge_ascii_gets_space():
    lines = _question_lines("1.tom has 3", "2 apples left")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert blocks[0].stem_text == "1.tom has 3 2 apples left"


def test_later_question_never_absorbs_next_question():
    lines = _question_lines("1.第一题", "2.第二题", "3.第三题")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.stem_text for b in blocks] == ["1.第一题", "2.第二题", "3.第三题"]


# ---------- 题号丢失修复：行内嵌题号 / 断头行（真图 paper-03 第 14 题） ----------

def test_inline_question_number_starts_new_question():
    # 下一题的行首被并进上一题的文本行：必须在行内断开
    lines = _question_lines(
        "13.每本日记本x元，买了7本，付了30元，应找回30元14.用2倍的放大镜看一个三角形",
        "角的度数是另一个锐角度数的4倍",
    )
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [13, 14]
    assert blocks[0].stem_text == "13.每本日记本x元，买了7本，付了30元，应找回30元"
    assert blocks[1].stem_text.startswith("14.用2倍的放大镜")
    assert "应找回" not in blocks[1].stem_text


def test_inline_decimal_is_not_question_number():
    lines = _question_lines("13.每本日记本x元，付了30元，应找回14.5元")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [13]


def test_broken_head_row_becomes_the_missing_question():
    # 题号行被横切成两截：左半（含 "14."）整截丢失，只剩以 "）" 开头的右半；
    # 下一个认出的题号是 15 —— 卷面编号证明这里缺的是 14
    lines = [
        _line("13.每本日记本x元，买了7本，付了30元，应找回30", 100, 100, w=600),
        _line("）一个直角三角形，其中一个锐", 900, 140, w=300),
        _line("角的度数是另一个锐角度数的4倍，这两个锐角分别是", 100, 180, w=600),
        _line("15.在里填上“>”或“<”。", 100, 220, w=300),
    ]
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [13, 14, 15]
    assert blocks[0].stem_text.endswith("应找回30")  # 断头行不许并进上一题
    assert blocks[1].stem_text.startswith("）一个直角三角形"), blocks[1].stem_text
    assert "角的度数" in blocks[1].stem_text


def test_broken_head_without_missing_number_merges_back():
    # 题号没缺（17 后面就是 18）：断头行只是上一题被吞了行首的续行，不许凭空造题
    lines = _question_lines(
        "17.小星有两根木条，第三根最长是（", "）厘米，最长为（", "18.摆一个独立的正方形",
    )
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [17, 18]
    assert "最长为" in blocks[0].stem_text


def test_two_broken_heads_do_not_create_phantom_question():
    # 连续两个断头行（同一行被切碎）：定不了号，全部并回上一题
    lines = _question_lines(
        "17.小星有两根木条，第三根最长是（", "）厘米，最长为（", "）厘米，（填整数）", "18.摆一个正方形",
    )
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [17, 18]
    assert "最长为" in blocks[0].stem_text and "填整数" in blocks[0].stem_text


def test_broken_head_before_first_question_is_dropped():
    lines = _question_lines("）残留的尾巴", "1.第一题")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1]
    assert "残留" not in blocks[0].stem_text


# ---------- 栏内切题：跨栏不合并、重复/跳号不抛异常 ----------

def test_two_columns_never_merge_into_one_question():
    # 左右两栏同一 y 各有题号 1：必须各成一块
    lines = [_line("1.左栏题", 100, 100), _line("1.右栏题", 1200, 100)]
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.stem_text for b in blocks] == ["1.左栏题", "1.右栏题"]
    assert blocks[0].bbox[0] < blocks[1].bbox[0]


def test_duplicate_and_skipped_numbers_kept_in_order():
    blocks = cut_questions(_question_lines("1.甲", "1.乙", "5.丙"), PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1, 1, 5]  # 按出现顺序，保留原始 no，不抛异常


# ---------- 栏内顺序：跨页照片的右页不许排到左页前面 ----------

def _two_page_spread() -> list[OcrLine]:
    """左右两页并排、左右同一 y 各有一题；左页最后一行越缝（框伸进栏缝）。

    越缝行让 `lines.py` 找不到"零块横穿"的栏缝，整页被当成一栏——正是
    paper-04 的形状：题号按 y 全局排序会把右页 24 排到左页 19 前面。
    """
    lines: list[OcrLine] = []
    for i in range(4):
        # 左页最后一条续行越缝：右边缘 880，栏缝 880~900 只剩 20px（< 行高 24）
        w = 780 if i == 3 else 200
        lines.append(_line(f"{i + 1}.左{i + 1}", 100, 100 + i * 60, w=200))
        lines.append(_line(f"左{i + 1}续", 100, 130 + i * 60, w=w))
        lines.append(_line(f"{i + 5}.右{i + 5}", 900, 100 + i * 60, w=200))
        lines.append(_line(f"右{i + 5}续", 900, 130 + i * 60, w=200))
    return lines


def test_cross_page_order_keeps_left_page_first():
    blocks = cut_questions(_two_page_spread(), PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1, 2, 3, 4, 5, 6, 7, 8]
    # 左页四题在前、右页四题在后；越缝那块也按行首归栏，右页续行不许被左页吞掉
    assert all(b.bbox[2] <= 900 for b in blocks[:4]), [b.bbox for b in blocks]
    assert all(b.bbox[0] >= 900 for b in blocks[4:]), [b.bbox for b in blocks]
    assert blocks[7].stem_text == "8.右8右8续"


def test_single_column_page_is_not_split():
    # 单栏页：缩进续行只在文字中间，右侧那截没有题号 → 不许补切
    lines = [
        _line("1.甲", 100, 100),
        _line("缩进的续行", 160, 140),
        _line("2.乙", 100, 180),
    ]
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1, 2]
    assert blocks[0].stem_text == "1.甲缩进的续行"


# ---------- 跳号补号：题号没认出来时不静默丢题 ----------

def test_gap_of_one_is_filled_with_inferred_question():
    # 20 → 22：中间那段正文的题号没认出来，卷面编号证明缺的是 21
    lines = _question_lines("20.用竖式计算", "3.8x2.1=7.98", "22.再添一条线段")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [20, 21, 22]
    assert [b.inferred_number for b in blocks] == [False, True, False]
    assert blocks[0].stem_text == "20.用竖式计算"  # 前题只留题号行
    assert blocks[1].stem_text == "3.8x2.1=7.98"  # 中间那段正文成为候选题题干
    assert blocks[2].stem_text == "22.再添一条线段"


def test_gap_of_two_is_not_guessed():
    # 20 → 23 连跳两个号：缺 21 还是 22 说不准，保持缺号
    lines = _question_lines("20.甲", "中间正文", "23.丁")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [20, 23]
    assert not any(b.inferred_number for b in blocks)
    assert "中间正文" in blocks[0].stem_text


def test_section_header_is_never_question_nor_stem():
    # 大题标题既不是题号，也不许当候选题的题干
    lines = _question_lines(
        "1.甲", "续行", "三、计算题。（共26分）", "3.丙",
    )
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1, 2, 3]
    assert blocks[1].inferred_number is True
    assert blocks[1].stem_text == "续行"
    assert all("计算题" not in b.stem_text for b in blocks)


def test_section_header_only_between_numbers_adds_nothing():
    # 跳号中间只有大题标题、没有正文：不补
    lines = _question_lines("1.甲", "四、操作与分析。（共3分）", "3.丙")
    blocks = cut_questions(lines, PAGE_W, PAGE_H)
    assert [b.no for b in blocks] == [1, 3]
    assert not any(b.inferred_number for b in blocks)


def test_question_block_inferred_number_defaults_false():
    block = cut_questions(_question_lines("1.甲"), PAGE_W, PAGE_H)[0]
    assert block.inferred_number is False



# ---------- AC7：空页 / 无题号 / 禁用开关 ----------

def test_empty_lines_return_empty_list():
    assert cut_questions([], PAGE_W, PAGE_H) == []


def test_page_without_question_numbers_returns_empty_list():
    lines = _question_lines("今天天气真好", "适合去公园散步")
    assert cut_questions(lines, PAGE_W, PAGE_H) == []


def test_cli_cut_raises_ocr_error_when_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    image = tmp_path / "any.png"
    image.write_bytes(b"fake")
    assert ocr_main.main(["cut", str(image)]) == 1
    assert "CAIRN_OCR_DISABLE" in capsys.readouterr().err


# ---------- CLI cut 子命令（假引擎） ----------

class FakeEngine:
    def __init__(self, result):
        self.result = result

    def __call__(self, path):
        return self.result, 0.1


def _write_real_png(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (PAGE_W, PAGE_H), "white").save(path)
    return str(path)


def test_cli_cut_prints_question_blocks(tmp_path, monkeypatch, capsys):
    from cairn.ocr import engine as ocr_engine

    path = _write_real_png(tmp_path)
    result = [
        (((100, 100), (300, 100), (300, 124), (100, 124)), "1.第一题", 0.95),
        (((100, 140), (300, 140), (300, 164), (100, 164)), "2.第二题", 0.95),
    ]
    monkeypatch.setattr(ocr_engine, "_engine", FakeEngine(result))
    assert ocr_main.main(["cut", path]) == 0
    out = capsys.readouterr().out
    assert "共 2 题" in out
    assert "[ 1] bbox=(100, 100, 300, 124) 1.第一题" in out
    assert "[ 2] bbox=(100, 140, 300, 164) 2.第二题" in out


def test_cli_cut_marks_inferred_number_with_question_mark(tmp_path, monkeypatch, capsys):
    from cairn.ocr import engine as ocr_engine

    path = _write_real_png(tmp_path)
    result = [
        (((100, 100), (300, 100), (300, 124), (100, 124)), "1.第一题", 0.95),
        (((100, 140), (300, 140), (300, 164), (100, 164)), "第一题的续行", 0.95),
        (((100, 180), (300, 180), (300, 204), (100, 204)), "3.第三题", 0.95),
    ]
    monkeypatch.setattr(ocr_engine, "_engine", FakeEngine(result))
    assert ocr_main.main(["cut", path]) == 0
    out = capsys.readouterr().out
    assert "共 3 题" in out
    assert "[ 1] bbox=(100, 100, 300, 124) 1.第一题" in out  # 确认题号不加标记
    assert "[? 2] bbox=(100, 140, 300, 164) 第一题的续行" in out  # 推测题号带 ?
    assert "候选题" in out  # 输出里说明 ? 的含义，不许静默当成确定题号


# ---------- AC6：deskew 角度估计与旋转 ----------

def _tilted_line(text: str, dy: int, x: int = 100, y: int = 100, w: int = 200) -> OcrLine:
    # 底边从 (x, y) 到 (x+w, y+dy)：dy>0 = 顺时针歪（右边低）
    box = ((x, y), (x + w, y + dy), (x + w, y + dy + 24), (x, y + 24))
    return OcrLine(text=text, box=box, confidence=0.9, y_center=y + 12 + dy // 2)


def test_estimate_angle_horizontal_is_zero():
    assert estimate_angle(_question_lines("1.平的", "2.也是平的")) == 0.0


def test_estimate_angle_no_lines_is_zero():
    assert estimate_angle([]) == 0.0


def test_estimate_angle_takes_median_of_baselines():
    lines = [_tilted_line("甲", dy=9), _tilted_line("乙", dy=9, y=300), _line("丙", 100, 600)]
    # 两个歪 9/200（≈2.58°）、一个水平 → 中位数 = 2.58°
    import math

    expected = math.degrees(math.atan2(9, 200))
    assert abs(estimate_angle(lines) - expected) < 1e-9


def test_deskew_small_angle_returns_original_image(monkeypatch, tmp_path):
    deskew_mod = importlib.import_module("cairn.ocr.deskew")

    path = _write_real_png(tmp_path)
    monkeypatch.setattr(deskew_mod, "recognize", lambda p: _question_lines("1.平的"))
    image, angle = deskew_mod.deskew(path)
    assert angle == 0.0
    assert image.size == (PAGE_W, PAGE_H)  # 未重采样：尺寸不变


def test_deskew_rotates_tilted_page(monkeypatch, tmp_path):
    import math

    deskew_mod = importlib.import_module("cairn.ocr.deskew")

    path = _write_real_png(tmp_path)
    monkeypatch.setattr(deskew_mod, "recognize", lambda p: [_tilted_line("1.歪的", dy=9)])
    image, angle = deskew_mod.deskew(path)
    assert abs(angle - math.degrees(math.atan2(9, 200))) < 1e-9
    assert image.size != (PAGE_W, PAGE_H)  # expand=True：摆正后尺寸变了


def test_deskew_no_lines_returns_original(monkeypatch, tmp_path):
    deskew_mod = importlib.import_module("cairn.ocr.deskew")

    path = _write_real_png(tmp_path)
    monkeypatch.setattr(deskew_mod, "recognize", lambda p: [])
    image, angle = deskew_mod.deskew(path)
    assert angle == 0.0
    assert image.size == (PAGE_W, PAGE_H)


def test_deskew_disabled_raises_ocr_error(monkeypatch, tmp_path):
    deskew_mod = importlib.import_module("cairn.ocr.deskew")

    monkeypatch.setenv("CAIRN_OCR_DISABLE", "1")
    path = _write_real_png(tmp_path)
    with pytest.raises(OcrError):
        deskew_mod.deskew(path)


def test_deskew_missing_file_raises_ocr_error(tmp_path):
    deskew_mod = importlib.import_module("cairn.ocr.deskew")

    with pytest.raises(OcrError, match="图片不存在"):
        deskew_mod.deskew(str(tmp_path / "no-such.png"))


# ---------- AC8：web 展示页题目块区块 ----------

def _make_scan(tmp_path):
    from cairn.models import Scan, make_engine, make_session_factory
    from cairn.models.learner import utcnow

    db_path = tmp_path / "web.db"
    app = create_app(f"sqlite:///{db_path}")
    (tmp_path / "scans").mkdir()
    Image.new("RGB", (PAGE_W, PAGE_H), "white").save(tmp_path / "scans" / "fake.png")
    with TestClient(app) as client:
        engine = make_engine(f"sqlite:///{db_path}")
        factory = make_session_factory(engine)
        with factory() as session:
            scan = Scan(
                learner_id=client.app.state.learner_id,
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
            scan_id = scan.id
    return app, scan_id


def test_scan_ocr_page_shows_question_blocks(tmp_path, monkeypatch):
    from cairn.ocr import engine as ocr_engine

    app, scan_id = _make_scan(tmp_path)
    with TestClient(app) as client:
        result = [
            (((100, 100), (300, 100), (300, 124), (100, 124)), "1.第一题", 0.95),
            (((100, 140), (300, 140), (300, 164), (100, 164)), "A.3", 0.95),
            (((100, 180), (300, 180), (300, 204), (100, 204)), "2.第二题", 0.95),
        ]
        monkeypatch.setattr(ocr_engine, "_engine", FakeEngine(result))
        resp = client.get(f"/scans/{scan_id}/ocr")
    assert resp.status_code == 200
    body = resp.text
    assert "读出了 2 道题" in body
    assert "1.第一题A.3" in body  # 选项行并入题干（中文相接不加空格）
    assert "2.第二题" in body
    assert "(100, 100, 300, 164)" in body  # 并集外接框


def test_scan_ocr_page_without_questions_hides_block_section(tmp_path, monkeypatch):
    from cairn.ocr import engine as ocr_engine

    app, scan_id = _make_scan(tmp_path)
    with TestClient(app) as client:
        result = [
            (((100, 100), (300, 100), (300, 124), (100, 124)), "没有题号的页面", 0.95),
        ]
        monkeypatch.setattr(ocr_engine, "_engine", FakeEngine(result))
        resp = client.get(f"/scans/{scan_id}/ocr")
    assert resp.status_code == 200
    assert "读出了" not in resp.text  # 无题号时不渲染题目块区块
    assert "没有题号的页面" in resp.text  # 行明细仍展示
