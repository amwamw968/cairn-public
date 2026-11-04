"""前端 Phase 2-c：裁边确认页（家长拖四个角）——只看页面这一层。

这张卡钉五件事：

1. `GET /scans/{id}/crop` 对三张真图各 200，页面上出现**这条 reason 对应的那句中文**
   （八条话术逐字照卡；改一个字就红）。
2. 页面零内联 `<style>`、零内联 `<script>`（唯一那个 `<script>` 是外链），
   四支角的 DOM 容器与 canvas 容器都在，`crop-confirm.js` 真的被引到。
3. 「跳过」打的是 `POST /scans/{id}/crop {"skip": true}`（TestClient 直接调这条接口，
   不需要真浏览器）；前端那三个按钮确实各接在这三条路上。
4. 不存在的 scan → 404（不是 500）；老卷子没有 sidecar 也照样 200。
5. `components.css` 里 `.crop-stage` 是 `max-width: 100%`、图是整幅宽（手机上不横滚）。

后端语义一行不动：`test_crop_apply.py` / `test_crop_info_api.py` 管接口本身。

卡 D1（视觉第一刀）在文件尾部追加七条，钉的是"看得见的东西"：纸有没有被框起来
（圆角/描边/阴影/留白）、遮罩是不是先铺满再挖空、框线两层、四角 20px 白圆落在
44px 热区上、主按钮整行 + 次按钮半行、字号节奏（24/32 · 14/22 · 14/20 · 12/18）。
色值字面量在 `components.css` 与 `crop-confirm.js` 里各是一条断言。

真图只用仓库里的 `docs/samples/*.jpg`，不依赖 `CAIRN_SAMPLE_DIR`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from cairn import recipe, scan_service, tools
from cairn import web as web_module
from cairn.models import Learner, Scan, make_engine, make_session_factory
from cairn.models.learner import utcnow
from cairn.web import create_app

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "docs" / "samples"
_SRC = Path(web_module.__file__).parent
TEMPLATES = _SRC / "templates"
STATIC = _SRC / "static"

HI_REDBG = SAMPLES / "hi-redbg.jpg"
USER_PHOTO = SAMPLES / "user-20261101.jpg"
MANUAL_CROP = SAMPLES / "manual-crop.jpg"

# 卡里那八条，逐字抄一遍：这是"页面说人话"的唯一判据。
REASON_TEXT = {
    "ok": "自动裁切完成，看一眼四个角对不对",
    "not_found": "没找到纸张，请把整张卷子拍进来",
    "already_full_page": "这张已经是整页了，不用裁",
    "cannot_separate_background": "纸和背景分不开，建议换深色桌面重拍",
    "paper_too_small": "卷子在画面里太小，靠近一点重拍",
    "degenerate_mask": "边缘没看清，请把四个角拖到纸上",
    "paper_cut_by_frame": "卷子被画面切掉了边，请重拍一张完整的",
    "weak_edge": "有一条边不太确定，请核对",
}

# 三张真图实测的 reason（与 test_crop_info_api.py 的 EXPECTED_REASON 同源）
SAMPLE_REASON = {
    HI_REDBG: "ok",
    USER_PHOTO: "cannot_separate_background",
    MANUAL_CROP: "already_full_page",
}

# 卡 D2b 的三档结论（产品口径，逐字）：正常 / 可疑两档是固定短句，
# 其余闸门沿用 `REASON_TEXT` 里原来那句。
VERDICT_TEXT = {
    "ok": "边缘对齐 4/4",
    "weak_edge": "有一条边不确定",
    "degenerate_mask": "有一条边不确定",
}


def verdict_line(reason: str) -> str:
    return VERDICT_TEXT.get(reason, REASON_TEXT[reason])


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def _tools_on_and_recipe_fresh(monkeypatch):
    """每条用例从"八全开 + 配方缓存干净"起跑（与 P2-a/P2-b 的用法一致）。"""
    for spec in tools.TOOLS:
        monkeypatch.delenv(spec.env, raising=False)
        if spec.legacy_env is not None:
            monkeypatch.delenv(spec.legacy_env, raising=False)
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
        # Exercise the historical upload and scan contract.
        from cairn.models.workflow import Workflow
        with app.state.session_factory() as session:
            session.get(Workflow, "default").active_version_id = "default:v1"
            session.commit()
        yield c


def upload(client: TestClient, path: Path) -> int:
    response = client.post(
        "/scans/upload",
        files={"file": (path.name, path.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    return int(response.url.path.rsplit("/", 1)[-1])


def page(client: TestClient, scan_id: int) -> str:
    response = client.get(f"/scans/{scan_id}/crop")
    assert response.status_code == 200, response.text
    return response.text


def declarations(css: str, selector: str) -> str:
    """取 *selector* 那条规则的声明块（支持 `.a, .b { … }` 并列写法）。"""
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", cleaned):
        if selector in [part.strip() for part in match.group(1).split(",")]:
            return match.group(2)
    raise AssertionError(f"components.css 里没有这条规则：{selector}")


# ------------------------------------------------- AC2：三张真图各说各的那句话


@pytest.mark.parametrize("sample", [HI_REDBG, USER_PHOTO, MANUAL_CROP], ids=lambda p: p.stem)
def test_page_says_the_line_for_each_reason(client, sample):
    """AC2 / AC5：页面 200，且**只**出现这张图那一档的结论短句。"""
    scan_id = upload(client, sample)
    reason = SAMPLE_REASON[sample]
    html = page(client, scan_id)

    expected = verdict_line(reason)
    assert expected in html, f"{sample.name} 少了「{expected}」"
    assert f'data-reason="{reason}"' in html

    leaked = [
        verdict_line(code)
        for code in REASON_TEXT
        if code != reason and verdict_line(code) in html
    ]
    assert leaked == [], f"{sample.name} 上出现了别的档的话：{leaked}"


def test_the_eight_lines_are_the_ones_from_the_card():
    """AC2 的另一半：web.py 那张表就是卡里八条，一条不多一条不少。"""
    assert web_module._CROP_REASON_TEXT == REASON_TEXT
    assert len(web_module._CROP_REASON_TEXT) == 8


def test_an_old_scan_without_a_sidecar_still_opens(client, db_path):
    """老卷子没有 `<uuid>.crop.json`：不套用八条里的任何一句，但页面照样 200。"""
    shot = db_path.parent / "scans" / "old.jpg"
    shot.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (600, 800), "white").save(shot, format="JPEG", quality=90)

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            learner = session.scalars(select(Learner).order_by(Learner.id.asc())).first()
            scan = Scan(
                learner_id=learner.id,
                device_id="upload",
                device_name="浏览器上传",
                width=600,
                height=800,
                captured_at=utcnow(),
                path="scans/old.jpg",
                source="upload",
            )
            session.add(scan)
            session.commit()
            scan_id = scan.id
    finally:
        engine.dispose()

    html = page(client, scan_id)
    assert web_module._CROP_REASON_UNKNOWN in html
    for text in REASON_TEXT.values():
        assert text not in html


# --------------------------------------------- AC3：没有内联样式，四角与 canvas 在


def test_page_has_no_inline_style_and_no_inline_script(client):
    """AC3：渲染结果里零 `<style>`；唯一的 `<script>` 是那个外链。"""
    html = page(client, upload(client, HI_REDBG))

    assert "<style" not in html
    assert "<script>" not in html
    assert html.count("<script") == 1
    assert '<script src="/static/crop-confirm.js" defer></script>' in html


def test_template_itself_extends_the_shell():
    """G2-3 升级：模板挂 v3 壳（不再自带骨架），没有内联样式块、没有内联脚本。"""
    source = (TEMPLATES / "crop.html").read_text(encoding="utf-8")

    assert source.startswith('{% extends "_v3.html" %}')
    assert "<style" not in source
    assert "<script>" not in source
    assert '<script src="/static/crop-confirm.js" defer></script>' in source
    assert "tokens.css" not in source and "components.css" not in source


def test_four_corners_and_the_canvas_are_in_the_dom(client):
    """AC3：四支角各是一个 DOM 容器（data-corner 0..3），外加一层 canvas。"""
    html = page(client, upload(client, HI_REDBG))

    assert 'class="crop-stage"' in html
    assert 'id="crop-canvas"' in html
    assert html.count('class="crop-handle"') == 4
    for corner in range(4):
        assert f'data-corner="{corner}"' in html
        assert f'id="crop-handle-{corner}"' in html


# --------------------------------------------- AC4：跳过 = 那条 skip 接口


def test_skip_hits_the_skip_branch_of_the_same_endpoint(client, db_path):
    """AC4：「跳过」走 `POST /scans/{id}/crop {"skip": true}`，只标 skipped。"""
    scan_id = upload(client, HI_REDBG)

    response = client.post(f"/scans/{scan_id}/crop", json={"skip": True})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["human"] == "skipped"

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            stored = db_path.parent / session.get(Scan, scan_id).path
    finally:
        engine.dispose()
    sidecar = json.loads(
        stored.with_suffix(scan_service.CROP_RECORD_SUFFIX).read_text(encoding="utf-8")
    )
    assert sidecar["human"] == "skipped"


def test_the_three_buttons_are_wired_to_those_two_endpoints(client):
    """AC4 的前端一半：按钮 id 与它要打的那两条路，都在 crop-confirm.js 里。"""
    html = page(client, upload(client, HI_REDBG))
    for button in ("crop-redetect", "crop-skip", "crop-apply"):
        assert f'id="{button}"' in html

    script = (STATIC / "crop-confirm.js").read_text(encoding="utf-8")
    assert "'/api/scans/' + scanId + '/crop-info'" in script
    assert "'/scans/' + scanId + '/crop'" in script
    assert "'/scans/' + scanId + '/questions'" in script
    assert "skip: true" in script
    assert "getElementById('crop-skip')" in script
    assert "setPointerCapture" in script
    # 400 要在页面上说话，不许静默
    assert "notice--" in script and "detail" in script


# ------------------------------------------------------------ AC5：不存在的照片


def test_a_missing_scan_is_404_not_500(client):
    """AC5：scan 不存在 → 404。"""
    response = client.get("/scans/987654/crop")

    assert response.status_code == 404
    assert response.status_code != 500


# ------------------------------------------------- AC7：手机上不横向滚动


def test_the_stage_never_wider_than_the_screen():
    """G2-3 升级：机制不变、锁到活的 v3 样式——`.crop-stage` 不宽过屏、图片整幅铺。"""
    css = (STATIC / "v3.css").read_text(encoding="utf-8")

    stage = declarations(css, ".crop-stage")
    assert "max-width: 100%" in stage
    assert "overflow: hidden" in stage
    assert "position: relative" in stage

    image = declarations(css, ".crop-stage__image")
    assert "width: 100%" in image
    assert "max-width: 100%" in image


# ============================================ 卡 D1：把纸框起来（视觉第一刀）
#
# 这一组断的是"看得见的东西"：框外要暗、框内要亮、框线两层、四角有角标、
# 按钮分主次、字号有节奏。色值一律不能出现在 components.css 和 crop-confirm.js
# 里——两个文件各自被守卫和下面这条钉住。


def test_the_stage_holds_the_paper_like_a_sheet():
    """G2-3 升级：v3 卡片语言——圆角卡 + 1px 描边 + 卡阴影；上一张卡的 AC7 不许掉。"""
    css = (STATIC / "v3.css").read_text(encoding="utf-8")
    stage = declarations(css, ".crop-stage")

    assert "border-radius: var(--v3-radius-card)" in stage
    assert "border: 1px solid var(--v3-border)" in stage
    assert "box-shadow: var(--v3-shadow-card)" in stage
    assert "max-width: 100%" in stage
    assert "overflow: hidden" in stage


def test_draw_paints_a_scrim_and_punches_the_paper_out():
    """A.3：先铺 --crop-scrim，再 destination-out 挖空四边形，然后两层框线。

    去掉铺遮罩那两行（fillStyle + fillRect），这条立刻红——这是"框外变暗"的
    唯一来源；不靠截图的平均亮度，靠驱动它的那两行代码在不在。
    """
    script = (STATIC / "crop-confirm.js").read_text(encoding="utf-8")

    assert "token('--crop-scrim')" in script
    assert "fillRect(0, 0, width, height)" in script
    assert "globalCompositeOperation = 'destination-out'" in script
    # 先铺整幅，再挖空：顺序反了等于白画
    assert script.index("fillRect(0, 0, width, height)") < script.index(
        "globalCompositeOperation = 'destination-out'"
    )

    # 框线两层：3px 深底压 2px 白线
    assert "token('--crop-edge-halo')" in script
    assert "token('--crop-edge-inner')" in script
    assert "context.lineWidth = 3" in script
    assert "context.lineWidth = 2" in script

    # 四角 L 形角标：臂长走令牌，颜色走强调色
    assert "token('--crop-bracket-size')" in script
    assert "token('--color-accent')" in script


def test_the_script_has_no_colour_literal():
    """A.3：JS 里 0 个 rgba(/rgb(/#字面量，颜色全部走 token()。"""
    script = (STATIC / "crop-confirm.js").read_text(encoding="utf-8")
    cleaned = re.sub(r"/\*.*?\*/", "", script, flags=re.S)
    cleaned = re.sub(r"^\s*//.*$", "", cleaned, flags=re.M)

    assert re.search(r"rgba?\(", cleaned) is None, "JS 里出现了 rgb()/rgba()"
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", cleaned) is None, "JS 里出现了 #色值"


def test_the_handles_are_44px_touch_targets():
    """G2-3 升级：手柄视觉交给 canvas 画线（JS 不动），DOM 只保 44px 热区 + 抓手光标。"""
    css = (STATIC / "v3.css").read_text(encoding="utf-8")

    target = declarations(css, ".crop-handle")
    assert "width: 44px" in target, "44px 触摸热区被改小了"
    assert "height: 44px" in target, "44px 触摸热区被改小了"
    assert "touch-action: none" in target, "拖角不能被浏览器手势抢走"
    assert "cursor: grab" in target


# ------------------------------------------------------ B：按钮分主次


def test_the_primary_button_comes_first_and_owns_the_row(client):
    """G2-3 升级：唯一主按钮「好了，下一步」整行在前；重检测/跳过降为文字链（钩子 id 不变）。"""
    html = page(client, upload(client, HI_REDBG))

    assert "<h1" in html and "把卷子框出来</h1>" in html
    # DOM 顺序：主按钮在前
    assert html.index('id="crop-apply"') < html.index('id="crop-redetect"')
    assert html.index('id="crop-apply"') < html.index('id="crop-skip"')
    assert 'id="crop-apply"' in html and "好了，下一步" in html
    assert 'class="btn btn--primary" id="crop-apply"' in html
    # 全页唯一主按钮
    assert html.count('class="btn btn--primary"') == 1
    # 文字链三件：重拍（回拍卷子）/ 重新检测 / 跳过裁边
    assert "重拍" in html and 'href="/upload"' in html
    assert 'id="crop-redetect"' in html and "重新检测" in html
    assert 'id="crop-skip"' in html and "跳过裁边" in html

    css = (STATIC / "v3.css").read_text(encoding="utf-8")
    row = declarations(css, ".select-actions .btn")
    assert "width: 100%" in row, "主按钮整行宽"
    base_btn = declarations(css, ".btn")
    assert "height: var(--v3-btn-h)" in base_btn, "主按钮 48 高红线"
    primary = declarations(css, ".btn--primary")
    assert "background: var(--v3-primary)" in primary
    linklike = declarations(css, ".linklike")
    # V3.1 交互件原则：裸文字链取消，linklike = 幽灵胶囊按钮（有边框有底色）
    assert "border: 1px solid var(--v3-border)" in linklike
    assert "border-radius: var(--v3-radius-pill)" in linklike
    assert "height: 38px" in linklike


def test_the_actions_block_is_a_table_not_a_row_of_blocks():
    """G2-3 升级：动作区整列堆叠、整行按钮（v3 红线：主按钮 48 高、整行宽）。"""
    css = (STATIC / "v3.css").read_text(encoding="utf-8")
    actions = declarations(css, ".select-actions")

    assert "flex-direction: column" in actions
    assert "width: 100%" in declarations(css, ".select-actions .btn")


# ---------------------------------------------------- C：字号与留白节奏


def test_the_type_scale_has_one_loud_heading_and_a_quiet_foot(client):
    """G2-3 升级：字号走 v3 令牌档——标题 18/600、结论 16/600、数字等宽、小字安静。"""
    html = page(client, upload(client, HI_REDBG))
    assert "把卷子框出来</h1>" in html
    assert 'class="hint"' in html, "v3 的提示行是 .hint（蓝图 E.3 拖角引导）"
    assert "跳过裁边，直接用这张" in html

    css = (STATIC / "v3.css").read_text(encoding="utf-8")

    heading = declarations(css, ".page-title")
    assert "font-size: var(--v3-fs-title)" in heading
    assert "font-weight: 600" in heading

    label = declarations(css, ".verdict__label")
    assert "font-size: var(--v3-fs-body)" in label
    assert "font-weight: 600" in label

    value = declarations(css, ".facts__value")
    assert "font-size: var(--v3-fs-body)" in value
    assert "font-variant-numeric: tabular-nums" in value

    meta = declarations(css, ".facts__meta")
    assert "font-size: var(--v3-fs-caption)" in meta
    assert "color: var(--v3-hint)" in meta

    notice = declarations(css, ".notice")
    assert "font-size: var(--v3-fs-body-s)" in notice
    assert "line-height: 1.5" in notice



# ==================================== 卡 D2b：印刷表单（结论条 + 数据条 + 按钮当行）
#
# 这一组钉三件事：页面块的顺序（h1 → .verdict → .facts → .crop-stage → .crop-actions）、
# 结论条 / 数据条的"两线夹一行 + 三列带竖线"、以及三档结论的映射（含 weak_edge /
# degenerate_mask 这两个没有真图样本的闸门——直接对 web 层的映射函数下断言）。


def test_the_page_reads_in_order(client):
    """G2-3 升级：块序 h1 → 结论 → 数据 → 裁边台 → 动作区；蓝图引导句在场。"""
    html = page(client, upload(client, HI_REDBG))

    order = [
        'class="verdict',
        'class="facts"',
        'class="crop-stage"',
        'class="select-actions"',
    ]
    positions = [html.index(token) for token in order]
    assert positions == sorted(positions), f"页面块的顺序不对：{positions}"
    assert html.index("把卷子框出来</h1>") < positions[0]

    assert "拖四个角把整张卷子框住" in html, "缺拖角引导"
    assert "好了，下一步" in html, "缺蓝图 E.3 主按钮"


def test_the_verdict_stands_on_a_quiet_card(client):
    """G2-3 升级：结论条 = v3 卡片（1px 描边 + 卡圆角），正常档无警告色。"""
    html = page(client, upload(client, HI_REDBG))
    assert 'class="verdict"' in html, "ok 档是正常档，不该带警告修饰"
    assert "verdict__label" in html and "verdict__meta" in html

    css = (STATIC / "v3.css").read_text(encoding="utf-8")
    verdict = declarations(css, ".verdict")

    assert "border: 1px solid var(--v3-border)" in verdict
    assert "border-radius: var(--v3-radius-card)" in verdict
    assert "align-items: baseline" in verdict
    # 三档修饰：warn 描边变黄、fail 描边变红（红只给错误/破坏性）
    warn = declarations(css, ".verdict--warn")
    assert "border-color: var(--v3-warn)" in warn
    fail = declarations(css, ".verdict--fail")
    assert "border-color: var(--v3-danger)" in fail


def test_the_facts_are_three_columns(client):
    """G2-3 升级：数据条三列网格，三格的标题就是卡面那三个词。"""
    html = page(client, upload(client, HI_REDBG))
    assert html.count('class="facts__cell"') == 3
    for caption in ("纸占画面", "纸面比周围亮", "四边贴合度"):
        assert caption in html, f"数据条少了「{caption}」"

    css = (STATIC / "v3.css").read_text(encoding="utf-8")
    facts = declarations(css, ".facts")
    assert "grid-template-columns: repeat(3, 1fr)" in facts


@pytest.mark.parametrize("reason", sorted(REASON_TEXT))
def test_the_three_verdict_tiers_follow_the_card(reason):
    """AC5：ok → 边缘对齐 4/4；weak_edge / degenerate_mask → 有一条边不确定；其余原句。"""
    text, modifier, edges = web_module._crop_verdict(reason)

    assert text == verdict_line(reason)
    assert modifier == {
        "ok": "",
        "weak_edge": "verdict--warn",
        "degenerate_mask": "verdict--warn",
    }.get(reason, "verdict--fail")
    assert edges == {"ok": "4/4", "weak_edge": "3/4", "degenerate_mask": "3/4"}.get(
        reason, "—"
    )


def test_an_old_scan_gets_the_no_record_line_not_a_number():
    """没有 sidecar：结论用"没有裁切记录"那句，数据条三格显示「—」，不拿 0 冒充读数。"""
    text, modifier, edges = web_module._crop_verdict(None)
    assert text == web_module._CROP_REASON_UNKNOWN
    assert modifier == "verdict--fail"
    assert edges == "—"

    facts = web_module._crop_facts(None, _StubScan(width=None, height=None))
    assert facts == {
        "area_pct": "—",
        "delta_text": "—",
        "edges_text": "—",
        "size_text": "原图尺寸未知",
    }


class _StubScan:
    """只给 `_crop_facts` 用的两列（它只读 width / height）。"""

    def __init__(self, width, height):
        self.width = width
        self.height = height
