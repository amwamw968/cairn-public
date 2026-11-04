"""Paper detection / auto-crop gates — AC2..AC9 of the "裁到纸 · 第一步" card.

One group per acceptance criterion:

- AC2 ``hi-redbg.jpg``: a skewed quad on a red background, paper covering
  44.5% of the frame — below the old 0.55 gate, so this is the case that
  proves the small-page threshold is 0.35. Must crop, then cut 18 blocks.
- AC3 ``user-20261101.jpg``: white paper on a white bed — paper 151 vs edge
  136, so the two cannot be told apart; refuse with
  ``cannot_separate_background`` and hand the image back untouched.
- AC4 ``manual-crop.jpg``: 99.8% of the frame — already full page.
- AC5 ``samsung-scan.jpg``: clean scan at 77%, must still crop, 18 blocks.
- AC6 ``paper-04-original.jpg``: tilted shot, delta 22 — refuse.
- AC7: every refusal returns the *same* array (``np.array_equal``), so no
  silent resize can sneak in.
- AC8: one page detected in well under 400ms.
- AC9: the background and full-page gates are load-bearing — relaxing the
  constant flips the reason, which is the same mutation recorded in
  ``docs/EVIDENCE.md``.

paper-04 lives outside the repo: ``CAIRN_SAMPLE_DIR`` (same convention as
``test_enhance.py``) or the local sample folder; otherwise those cases skip.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from cairn.ocr import crop
from cairn.ocr.crop import detect_paper
from cairn.ocr.cut import cut_questions

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "docs" / "samples"
SAMPLE_DIR = os.environ.get("CAIRN_SAMPLE_DIR")
_ENGINE_INSTALLED = importlib.util.find_spec("rapidocr_onnxruntime") is not None

_HI_REDBG = SAMPLES / "hi-redbg.jpg"
_SAMSUNG = SAMPLES / "samsung-scan.jpg"
_MANUAL = SAMPLES / "manual-crop.jpg"
_USER = SAMPLES / "user-20261101.jpg"


def _find_paper04() -> "Path | None":
    """Locate ``paper-04-original.jpg``; None means the cases skip."""
    candidates = []
    if SAMPLE_DIR:
        candidates.append(Path(SAMPLE_DIR) / "paper-04-original.jpg")
    for path in candidates:
        if path.is_file():
            return path
    return None


PAPER04 = _find_paper04()

needs_paper04 = pytest.mark.skipif(PAPER04 is None, reason="paper-04 需要 CAIRN_SAMPLE_DIR")
needs_engine = pytest.mark.skipif(
    not _ENGINE_INSTALLED, reason="真图切题需要已安装 rapidocr-onnxruntime"
)


def _read(path: Path) -> np.ndarray:
    bgr = cv2.imread(str(path))
    assert bgr is not None, path
    return bgr


def _detect(path: Path):
    bgr = _read(path)
    return bgr, detect_paper(bgr)


def _cut_cropped(path: Path, tmp_path: Path) -> list:
    """Detect → crop → write JPEG → orient/read → cut questions."""
    from cairn.ocr.orient import read_page

    bgr, result = _detect(path)
    target = tmp_path / path.name
    assert cv2.imwrite(str(target), crop.crop_to_paper(bgr, result))
    page = read_page(str(target))
    return cut_questions(page.lines, page.width, page.height)


def _cut_original(path: Path) -> list:
    from cairn.ocr.orient import read_page

    page = read_page(str(path))
    return cut_questions(page.lines, page.width, page.height)


@pytest.fixture()
def restore_engine():
    """Put the cached OCR engine back so ``test_ocr`` stays honest."""
    from cairn.ocr import engine as ocr_engine

    saved = ocr_engine._engine
    yield
    ocr_engine._engine = saved


# ---------------------------------------------------------------- AC2


def test_hi_redbg_detects_skewed_quad_and_permits_crop() -> None:
    """AC2: red background, paper 44.5% — a genuine quad, not "too small"."""
    _bgr, result = _detect(_HI_REDBG)

    assert result.quality == "quad"
    assert result.reason == "ok", result.reason
    assert result.can_auto_crop is True
    assert 0.35 <= result.area_ratio <= 0.85
    assert result.area_ratio == pytest.approx(0.445, abs=0.01)
    assert result.reason != "paper_too_small"


def test_hi_redbg_crop_yields_eighteen_questions(tmp_path, restore_engine) -> None:
    """AC2: after cropping, 18 blocks numbered 1–18 with no gaps."""
    blocks = _cut_cropped(_HI_REDBG, tmp_path)

    assert len(blocks) == 18
    assert [block.no for block in blocks] == list(range(1, 19)), [b.no for b in blocks]


# ---------------------------------------------------------------- AC3


def test_user_photo_paper_blends_into_background() -> None:
    """AC3: paper 151 vs edge 136 — the gate must refuse for that reason."""
    _bgr, result = _detect(_USER)

    assert result.can_auto_crop is False
    assert result.reason == "cannot_separate_background", result.reason
    assert result.paper_level == pytest.approx(151, abs=1)
    assert result.edge_level == pytest.approx(136, abs=1)
    assert result.background_delta == pytest.approx(15, abs=1)
    assert result.area_ratio == pytest.approx(0.575, abs=0.01)


# ---------------------------------------------------------------- AC4


def test_manual_crop_is_already_full_page() -> None:
    """AC4: 99.8% of the frame — cropping would only cost blocks."""
    _bgr, result = _detect(_MANUAL)

    assert result.can_auto_crop is False
    assert result.reason == "already_full_page", result.reason
    assert result.area_ratio == pytest.approx(0.998, abs=0.005)


# ---------------------------------------------------------------- AC5


def test_samsung_scan_detects_quad_and_permits_crop() -> None:
    """AC5: clean scan at 77% still crops."""
    _bgr, result = _detect(_SAMSUNG)

    assert result.quality == "quad"
    assert result.reason == "ok", result.reason
    assert result.can_auto_crop is True
    assert result.area_ratio == pytest.approx(0.771, abs=0.01)


def test_samsung_scan_crop_yields_eighteen_questions(tmp_path, restore_engine) -> None:
    """AC5: after cropping, still 18 blocks numbered 1–18."""
    blocks = _cut_cropped(_SAMSUNG, tmp_path)

    assert len(blocks) == 18
    assert [block.no for block in blocks] == list(range(1, 19)), [b.no for b in blocks]


# ---------------------------------------------------------------- AC6


@needs_paper04
def test_paper_04_cannot_separate_background() -> None:
    """AC6: tilted shot, delta 22 — refuse to crop automatically."""
    _bgr, result = _detect(PAPER04)

    assert result.can_auto_crop is False
    assert result.reason == "cannot_separate_background", result.reason
    assert result.paper_level == pytest.approx(145, abs=2)
    assert result.edge_level == pytest.approx(123, abs=2)
    assert result.background_delta == pytest.approx(22, abs=2)
    assert result.area_ratio == pytest.approx(0.611, abs=0.02)


@needs_paper04
@needs_engine
def test_paper_04_original_cuts_ten_questions_from_nineteen(restore_engine) -> None:
    """AC6: refusing the crop keeps the original page cuttable — 10 blocks 19–28."""
    blocks = _cut_original(PAPER04)

    assert len(blocks) == 10
    assert [block.no for block in blocks] == list(range(19, 29)), [b.no for b in blocks]


# ---------------------------------------------------------------- AC7


def _refused_cases() -> list:
    cases = [_USER, _MANUAL]
    if PAPER04 is not None:
        cases.append(PAPER04)
    return cases


@pytest.mark.parametrize("path", _refused_cases(), ids=lambda p: Path(p).name)
def test_crop_to_paper_returns_input_pixel_identical(path) -> None:
    """AC7: a refused page comes back as the very same pixels — no resize."""
    bgr, result = _detect(path)
    assert result.can_auto_crop is False

    out = crop.crop_to_paper(bgr, result)

    assert np.array_equal(out, bgr)
    assert out.shape == bgr.shape
    assert out.dtype == bgr.dtype


def test_crop_to_paper_returns_input_when_nothing_found() -> None:
    """AC7: ``not_found`` is a refusal too — same pixels back."""
    bgr = np.full((120, 160, 3), 128, np.uint8)
    result = crop.PaperResult(
        corners=None, quality="none", iou=0.0, area_ratio=0.0,
        can_auto_crop=False, reason="not_found",
    )

    assert np.array_equal(crop.crop_to_paper(bgr, result), bgr)


# ---------------------------------------------------------------- AC8


def test_detect_paper_is_fast() -> None:
    """AC8: one page detected in under 400ms (reference: 260–317ms)."""
    bgr = _read(_HI_REDBG)

    timings = []
    for _ in range(3):
        start = time.perf_counter()
        detect_paper(bgr)
        timings.append(time.perf_counter() - start)

    best = min(timings)
    assert best < 0.4, f"detect_paper took {best * 1000:.0f}ms (runs={timings})"


# ---------------------------------------------------------------- AC9


def test_background_gate_is_load_bearing(monkeypatch) -> None:
    """AC9: drop the background gate → AC3's reason changes.

    The same mutation (commenting the gate out in ``crop.py``) is recorded in
    ``docs/EVIDENCE.md``; here it is done with a constant so it stays in-suite.
    """
    _bgr, with_gate = _detect(_USER)
    assert with_gate.reason == "cannot_separate_background"

    monkeypatch.setattr(crop, "_BACKGROUND_DELTA_MIN", -1.0)
    _bgr, without_gate = _detect(_USER)

    assert without_gate.reason != "cannot_separate_background", without_gate.reason


def test_full_page_gate_is_load_bearing(monkeypatch) -> None:
    """AC9: drop the full-page gate → AC4's reason changes."""
    _bgr, with_gate = _detect(_MANUAL)
    assert with_gate.reason == "already_full_page"

    monkeypatch.setattr(crop, "_FULL_PAGE_RATIO", 1.01)
    _bgr, without_gate = _detect(_MANUAL)

    assert without_gate.reason != "already_full_page", without_gate.reason
