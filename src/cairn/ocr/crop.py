"""Paper detection and cropping — pure functions, no IO.

Step 1 of the "裁到纸" card: decide whether a page can be auto-cropped,
crop the image to the detected paper, and warp the paper to a rectified
page. This module does NOT read files, write files, touch the database,
apply enhancement, or modify the upload path. It only transforms in-memory
numpy arrays.

Detection / candidate / legality / bbox / warp parameters are copied
verbatim from the reference implementation (``/tmp/cairn-ref/scan.py`` and
``/tmp/cairn-ref/probe.py``); they are not retuned here.
"""

from __future__ import annotations

import cv2
import numpy as np
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #


@dataclass
class PaperResult:
    """Outcome of :func:`detect_paper`.

    Attributes
    ----------
    corners:
        Four corner points ``(tl, tr, br, bl)`` in clockwise order, in
        full-resolution image coordinates. ``None`` when no paper is found.
    quality:
        ``"quad"`` for a genuine skewed quadrilateral, ``"bbox"`` when the
        detected region degenerates into its own bounding rectangle, or
        ``"none"`` when nothing is found.
    iou:
        Intersection-over-union between the filled quad and the paper mask.
    area_ratio:
        Area of the quad's outer bounding box divided by the frame area.
    can_auto_crop:
        Whether automatic cropping is safe under the gate rules.
    reason:
        The gate that fired (or ``"ok"``).
    paper_level:
        Median brightness inside the quad inset by 30% towards its centroid.
    edge_level:
        Median brightness of the four 5% border bands of the frame.
    background_delta:
        ``paper_level - edge_level``; small means paper and background blur
        together and the detection cannot be trusted.

    The three levels are ``0.0`` when nothing was found.
    """

    corners: "np.ndarray | None"
    quality: str
    iou: float
    area_ratio: float
    can_auto_crop: bool
    reason: str
    paper_level: float = 0.0
    edge_level: float = 0.0
    background_delta: float = 0.0


# --------------------------------------------------------------------------- #
# Thresholds (copied from reference; do not retune)
# --------------------------------------------------------------------------- #

_DETECT_TARGET_W = 700          # detect_page downscale width
_SCORE_BLUR = 31                # medianBlur kernel for the shading score
_SCORE_SAT_WEIGHT = 0.55        # sat channel subtraction weight
_MORPH_K = 11                   # morphology kernel size
_MORPH_ITERS = 2                # close / open iterations
_APPROX_EPS = 0.02              # approxPolyDP epsilon as fraction of perimeter
_TOP_CONTOURS = 3               # RETR_EXTERNAL, keep largest 3
_MIN_AREA_RATIO = 0.08          # legality: area >= 8% of frame
_MIN_SHORT_SIDE = 0.15          # legality: shortest side >= 0.15 * long side
_MAX_OPP_SIDE_RATIO = 1.6       # legality: opposite side ratio <= 1.6
_ANGLE_MIN = 45.0               # legality: interior angle lower bound
_ANGLE_MAX = 135.0              # legality: interior angle upper bound
_BBOX_DEGENERATE_RATIO = 0.99   # quad area / own bbox area >= this -> bbox
_CONTRAST_THRESHOLD = 12.0      # weak_edge: per-side mean contrast gate
_FRAME_TOUCH_RATIO = 0.45       # paper_cut_by_frame: border coverage gate
_FULL_PAGE_RATIO = 0.85         # already_full_page upper bound
_SMALL_PAGE_RATIO = 0.35        # paper_too_small lower bound
_PAPER_INSET = 0.70             # paper_level: corners kept after 30% inset
_EDGE_BAND = 0.05               # edge_level: border band width, fraction of min side
_BACKGROUND_DELTA_MIN = 25.0    # cannot_separate_background lower bound


# --------------------------------------------------------------------------- #
# Geometry helpers (ported from reference)
# --------------------------------------------------------------------------- #


def order_quad(pts: np.ndarray) -> np.ndarray:
    """Order four points as ``(tl, tr, br, bl)`` clockwise."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def _quad_score(quad: np.ndarray, w: int, h: int):
    """Legality check + area (in small-scale frame units).

    Returns ``(-1, area)`` when the quad is invalid, else ``(area, area)``.
    """
    q = order_quad(quad)
    area = cv2.contourArea(q) / (w * h)
    if area < _MIN_AREA_RATIO:
        return -1, area
    sides = [np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]
    if min(sides) < _MIN_SHORT_SIDE * max(w, h):
        return -1, area
    r1 = max(sides[0], sides[2]) / max(1e-6, min(sides[0], sides[2]))
    r2 = max(sides[1], sides[3]) / max(1e-6, min(sides[1], sides[3]))
    if r1 > _MAX_OPP_SIDE_RATIO or r2 > _MAX_OPP_SIDE_RATIO:
        return -1, area
    angs = []
    for i in range(4):
        a = q[i] - q[(i + 1) % 4]
        b = q[(i + 2) % 4] - q[(i + 1) % 4]
        c = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
        angs.append(np.degrees(np.arccos(np.clip(c, -1, 1))))
    if max(angs) - min(angs) > 45 or not all(_ANGLE_MIN < a < _ANGLE_MAX for a in angs):
        return -1, area
    return area, area


def _detect_page(bgr: np.ndarray):
    """Detect the paper quad and the paper mask.

    Returns ``(quad, mask)`` where ``quad`` is in full-resolution
    coordinates (or ``None``) and ``mask`` is at the detection scale.
    """
    h, w = bgr.shape[:2]
    s = _DETECT_TARGET_W / float(w)
    small = cv2.resize(bgr, (_DETECT_TARGET_W, int(round(h * s))),
                       interpolation=cv2.INTER_AREA)
    hs, ws = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].astype(np.float32)
    base = cv2.medianBlur(gray, _SCORE_BLUR).astype(np.float32)
    score = base - _SCORE_SAT_WEIGHT * cv2.medianBlur(sat.astype(np.uint8), _SCORE_BLUR).astype(np.float32)
    lo, hi = np.percentile(score, 2), np.percentile(score, 98)
    score = np.clip((score - lo) / max(1e-6, hi - lo), 0, 1)
    score = (score * 255).astype(np.uint8)
    _, mask = cv2.threshold(score, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (_MORPH_K, _MORPH_K))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=_MORPH_ITERS)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k, iterations=_MORPH_ITERS)

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, mask
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:_TOP_CONTOURS]
    best = None
    for c in cnts:
        hull = cv2.convexHull(c)
        peri = cv2.arcLength(hull, True)
        approx = cv2.approxPolyDP(hull, _APPROX_EPS * peri, True).reshape(-1, 2)
        cands = []
        if len(approx) == 4:
            cands.append(approx.astype(np.float32))
        p = hull.reshape(-1, 2).astype(np.float32)
        ssum = p.sum(1)
        sdiff = p[:, 0] - p[:, 1]
        quad = np.array(
            [p[np.argmin(ssum)], p[np.argmax(sdiff)],
             p[np.argmax(ssum)], p[np.argmin(sdiff)]],
            dtype=np.float32,
        )
        cands.append(quad)
        rect = cv2.minAreaRect(hull)
        cands.append(cv2.boxPoints(rect).astype(np.float32))
        for q in cands:
            sc, area = _quad_score(q, ws, hs)
            if sc > 0 and (best is None or area > best[1]):
                best = (q, area)
    if best is None:
        return None, mask
    quad_small = best[0] / s
    quad_small = order_quad(np.clip(quad_small, [0, 0], [w - 1, h - 1]))
    return quad_small, mask


def _quad_quality(quad: np.ndarray, img_shape) -> str:
    """Classify the quad as a genuine ``"quad"`` or degenerate ``"bbox"``."""
    if quad is None:
        return "none"
    q = order_quad(quad)
    span_w = float(q[:, 0].max() - q[:, 0].min())
    span_h = float(q[:, 1].max() - q[:, 1].min())
    box_area = max(1.0, span_w * span_h)
    quad_area = abs(float(cv2.contourArea(q)))
    if quad_area / box_area >= _BBOX_DEGENERATE_RATIO:
        return "bbox"
    return "quad"


def _mask_quad_iou(mask: np.ndarray, quad: np.ndarray) -> float:
    """IoU between the filled quad and the paper mask (same scale)."""
    if mask is None or quad is None:
        return 0.0
    mh, mw = mask.shape[:2]
    fill = np.zeros((mh, mw), np.uint8)
    cv2.fillPoly(fill, [order_quad(quad).astype(np.int32)], 255)
    m = (mask > 0).astype(np.uint8) * 255
    inter = np.logical_and(fill > 0, m > 0).sum()
    union = np.logical_or(fill > 0, m > 0).sum()
    return float(inter) / max(1, union)


def _side_contrast(gray: np.ndarray, quad: np.ndarray, n: int = 80):
    """Mean normal-direction gray jump along each of the four edges.

    Returns ``[top, right, bottom, left]``.
    """
    q = order_quad(quad)
    out = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        d = b - a
        l = float(np.linalg.norm(d))
        if l < 8:
            out.append(0.0)
            continue
        nrm = np.array([-d[1], d[0]], np.float32) / l
        vals = []
        for t in np.linspace(0.12, 0.88, n):
            p = a + t * d
            pin, pout = [], []
            for k in range(2, 12):
                for arr, sgn in ((pin, 1), (pout, -1)):
                    x = int(round(p[0] + nrm[0] * k * sgn))
                    y = int(round(p[1] + nrm[1] * k * sgn))
                    if 0 <= x < gray.shape[1] and 0 <= y < gray.shape[0]:
                        arr.append(gray[y, x])
            if pin and pout:
                vals.append(abs(float(np.mean(pin)) - float(np.mean(pout))))
        out.append(float(np.mean(vals)) if vals else 0.0)
    return out


def _frame_touch_ratio(mask: np.ndarray) -> float:
    """Largest border coverage among the four frame edges of the mask.

    Each edge is the fraction of its length that belongs to the mask; the
    maximum is returned.
    """
    if mask is None:
        return 0.0
    mh, mw = mask.shape[:2]
    m = (mask > 0)
    edges = [
        m[0, :].mean(),
        m[mh - 1, :].mean(),
        m[:, 0].mean(),
        m[:, mw - 1].mean(),
    ]
    return float(max(edges))


def _paper_level(gray: np.ndarray, quad: np.ndarray) -> float:
    """Median brightness inside the quad inset 30% towards its centroid."""
    q = order_quad(quad)
    center = q.mean(axis=0)
    inner = ((q - center) * _PAPER_INSET + center).astype(np.int32)
    mask = np.zeros(gray.shape[:2], np.uint8)
    cv2.fillPoly(mask, [inner], 255)
    if not mask.any():
        return 0.0
    return float(np.median(gray[mask > 0]))


def _edge_level(gray: np.ndarray) -> float:
    """Median brightness of the four border bands of the frame.

    Each band is ``_EDGE_BAND`` of the frame's shorter side wide, so a
    landscape and a portrait frame are sampled with the same thickness.
    """
    h, w = gray.shape[:2]
    band = max(1, int(round(_EDGE_BAND * min(h, w))))
    pixels = np.concatenate(
        [
            gray[:band, :].ravel(),
            gray[h - band:, :].ravel(),
            gray[:, :band].ravel(),
            gray[:, w - band:].ravel(),
        ]
    )
    return float(np.median(pixels))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def detect_paper(bgr: np.ndarray) -> PaperResult:
    """Detect the paper in ``bgr`` and decide if it can be auto-cropped.

    Pure function: operates only on the in-memory array.
    """
    h, w = bgr.shape[:2]
    quad, mask = _detect_page(bgr)

    if quad is None:
        return PaperResult(
            corners=None, quality="none", iou=0.0, area_ratio=0.0,
            can_auto_crop=False, reason="not_found",
        )

    quality = _quad_quality(quad, (h, w))
    scale = (mask.shape[1] / float(w)) if mask is not None else 1.0
    iou = _mask_quad_iou(mask, quad * scale) if mask is not None else 0.0

    span_w = float(quad[:, 0].max() - quad[:, 0].min())
    span_h = float(quad[:, 1].max() - quad[:, 1].min())
    area_ratio = (span_w * span_h) / (w * h)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    paper_level = _paper_level(gray, quad)
    edge_level = _edge_level(gray)
    background_delta = paper_level - edge_level

    # Gate order: first hit wins (docs/PLAN.md "裁到纸 · 第一步").
    # Size gates come BEFORE the mask-quality gates: a small but genuine
    # skewed quad (hi-redbg, 44.5%) must stay croppable.
    if area_ratio > _FULL_PAGE_RATIO:
        reason = "already_full_page"
    elif background_delta < _BACKGROUND_DELTA_MIN:
        reason = "cannot_separate_background"
    elif area_ratio < _SMALL_PAGE_RATIO:
        reason = "paper_too_small"
    elif quality == "bbox":
        reason = "degenerate_mask"
    elif mask is not None and _frame_touch_ratio(mask) >= _FRAME_TOUCH_RATIO:
        reason = "paper_cut_by_frame"
    elif _count_strong_edges(gray, quad) < 2:
        reason = "weak_edge"
    else:
        reason = "ok"

    can_auto_crop = reason == "ok"
    return PaperResult(
        corners=quad, quality=quality, iou=iou, area_ratio=area_ratio,
        can_auto_crop=can_auto_crop, reason=reason,
        paper_level=paper_level, edge_level=edge_level,
        background_delta=background_delta,
    )


def _count_strong_edges(gray: np.ndarray, quad: np.ndarray) -> int:
    """Number of quad edges whose mean normal contrast exceeds the gate."""
    sides = _side_contrast(gray, quad)
    return sum(1 for v in sides if v > _CONTRAST_THRESHOLD)


def crop_to_paper(bgr: np.ndarray, result: PaperResult) -> np.ndarray:
    """Crop ``bgr`` to the detected paper.

    Only crops when ``result.can_auto_crop`` is true; otherwise returns the
    input array unchanged (identical pixels).
    """
    if not result.can_auto_crop or result.corners is None:
        return bgr
    return warp_page(bgr, result.corners)


def warp_page(bgr: np.ndarray, corners: np.ndarray, long_side: int = 2000) -> np.ndarray:
    """Warp the quad ``corners`` into a rectified page of max side ``long_side``."""
    q = order_quad(corners)
    w = max(np.linalg.norm(q[0] - q[1]), np.linalg.norm(q[2] - q[3]))
    h = max(np.linalg.norm(q[0] - q[3]), np.linalg.norm(q[1] - q[2]))
    scale = long_side / max(w, h)
    wt, ht = int(round(w * scale)), int(round(h * scale))
    dst = np.array(
        [[0, 0], [wt - 1, 0], [wt - 1, ht - 1], [0, ht - 1]], dtype=np.float32
    )
    m = cv2.getPerspectiveTransform(q, dst)
    out = cv2.warpPerspective(
        bgr, m, (wt, ht), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    return out
