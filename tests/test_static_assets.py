"""设计地基（卡：tokens.css / base.css / /static 挂载 / upload 页试点）。

只测这一层：静态资源能不能取到、令牌齐不齐、组件类名在不在、上传页有没有
退回内联 <style>、以及"全离线"这条硬约束。业务行为由既有用例覆盖，这里不碰。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cairn import web as web_module
from cairn.web import create_app

_SRC = Path(web_module.__file__).parent
STATIC_DIR = _SRC / "static"
TOKENS_CSS = STATIC_DIR / "tokens.css"
BASE_CSS = STATIC_DIR / "base.css"
UPLOAD_HTML = _SRC / "templates" / "capture.html"

# 组件契约（卡上写死的类名，后面的页面照着用）
COMPONENT_SELECTORS = [
    ".btn",
    ".btn--primary",
    ".btn--ghost",
    ".btn--danger",
    ".card",
    ".card__title",
    ".field",
    ".input",
    ".textarea",
    ".notice",
    ".notice--info",
    ".notice--warn",
    ".notice--danger",
    ".empty",
    ".badge",
    ".badge--ok",
    ".badge--warn",
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(f"sqlite:///{tmp_path}/static.db")
    with TestClient(app) as c:
        yield c


def _style_rules(css: str):
    """逐条产出 (选择器列表, 声明块)，注释先剥掉（注释里会写中文标点，别混进选择器）。"""
    cleaned = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for match in re.finditer(r"([^{}]+)\{([^}]*)\}", cleaned):
        selectors = [part.strip() for part in match.group(1).split(",")]
        yield selectors, match.group(2)


def _declarations(css: str, selector: str) -> str:
    """取 *selector* 所在规则的声明块（支持 `.a, .b { … }` 这种并列选择器）。"""
    for selectors, declarations in _style_rules(css):
        if selector in selectors:
            return declarations
    raise AssertionError(f"没有找到规则：{selector}")


def _var(css: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", css)
    assert match, f"没有定义变量：{name}"
    return match.group(1).strip()


# ------------------------------------------------------------------ 能被取到


def test_tokens_css_is_served_as_css(client):
    """/static/tokens.css 200 且 Content-Type 是 text/css，强调色令牌必须在。"""
    response = client.get("/static/tokens.css")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/css"
    # 换皮后不再钉死具体色值（那是设计决定，会变），只钉"令牌真的在、且两种主题都定义了"
    assert "--accent:" in response.text
    assert response.text.count("--accent:") >= 2, "暗色主题也要定义 --accent"


def test_base_css_is_served_as_css(client):
    """AC2：/static/base.css 200 且 Content-Type 是 text/css。"""
    response = client.get("/static/base.css")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "text/css"
    assert ".btn--primary" in response.text


def test_static_mount_is_a_real_mount_not_a_catch_all(client):
    """挂的是 StaticFiles 而不是兜底路由：不存在的文件必须 404，不能回 HTML。"""
    response = client.get("/static/does-not-exist.css")
    assert response.status_code == 404
    assert "text/html" not in response.headers.get("content-type", "")


# -------------------------------------------------------------- 令牌是不是齐


def test_tokens_cover_every_scale_group():
    """AC：灰阶 6 档 + 强调色 + 三状态色 + 字号 6 + 行高 2 + 间距 6 + 圆角 5 + 阴影 2。

    字号自 D3 起是六档唯一真值；v1 的五个旧名（--text-xs…--text-xl）保留为
    指向它们的别名，名字由 test_design_system 的 LEGACY 名单单独钉住。
    """
    css = TOKENS_CSS.read_text(encoding="utf-8")

    # 换皮后强调色不再是 teal；钉"是合法 hex 且暗色主题另有一套"，不钉具体值
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", _var(css, "--accent"))
    assert css.count("--accent:") >= 2, "暗色主题也要定义 --accent"
    assert len(re.findall(r"--gray-\d+\s*:", css)) == 6
    for name in ("--ok", "--warn", "--danger"):
        assert _var(css, name).startswith("#")

    sizes = {
        "--text-meta": "11px",
        "--text-caption": "12px",
        "--text-label": "14px",
        "--text-body": "16px",
        "--text-value": "20px",
        "--text-display": "28px",
    }
    for name, want in sizes.items():
        assert _var(css, name) == want

    assert _var(css, "--leading-tight") == "1.25"
    assert _var(css, "--leading-normal") == "1.6"

    spaces = {
        "--space-1": "4px",
        "--space-2": "8px",
        "--space-3": "12px",
        "--space-4": "16px",
        "--space-5": "24px",
        "--space-6": "32px",
    }
    for name, want in spaces.items():
        assert _var(css, name) == want

    # 按名断言，不再数个数：计数只保证"有 3 个 --radius-*"，改名或换值都能溜过去。
    radii = {
        "--radius-xs": "4px",
        "--radius-sm": "4px",
        "--radius-md": "8px",
        "--radius-lg": "14px",
        "--radius-full": "999px",
    }
    for name, want in radii.items():
        assert _var(css, name) == want
    for name in ("--shadow-1", "--shadow-2"):
        assert _var(css, name), f"{name} 是空的"


def test_tokens_are_the_only_file_with_raw_colors():
    """AC3：色值只允许出现在 tokens.css，base.css 与模板里不许写死 #rrggbb。"""
    hex_color = re.compile(r"#[0-9a-fA-F]{3,8}\b")

    assert hex_color.search(TOKENS_CSS.read_text(encoding="utf-8")) is not None
    assert hex_color.search(BASE_CSS.read_text(encoding="utf-8")) is None
    assert hex_color.search(UPLOAD_HTML.read_text(encoding="utf-8")) is None


# ------------------------------------------------------------ 组件契约在不在


def test_base_css_defines_the_component_contract():
    """AC7：卡上列出的类名一个都不能少。"""
    css = BASE_CSS.read_text(encoding="utf-8")
    missing = [sel for sel in COMPONENT_SELECTORS if not _has_rule(css, sel)]
    assert missing == []


def _has_rule(css: str, selector: str) -> bool:
    return any(selector in selectors for selectors, _ in _style_rules(css))


def test_tap_targets_and_body_font_are_mobile_first():
    """AC5：可点元素 44px 起、字号 16px 起（避免 iOS 聚焦放大），正文也是 16px。"""
    css = BASE_CSS.read_text(encoding="utf-8")
    tokens = TOKENS_CSS.read_text(encoding="utf-8")

    assert _var(tokens, "--tap-min") == "44px"
    assert _var(tokens, "--text-body") == "16px"
    assert _var(tokens, "--text-base") == "var(--text-body)", "v1 名要指向 16px 档"

    assert "font-size: var(--text-base)" in _declarations(css, "body")

    # .textarea 在并列规则里拿 44px 下限，随后单独一条把它抬到三行高。
    for selector in (".btn", ".input", ".textarea"):
        declarations = _declarations(css, selector)
        assert "min-height: var(--tap-min)" in declarations
        assert "font-size: var(--text-base)" in declarations


def test_input_has_focus_and_error_states():
    """AC：表单控件有焦点态（可见描边）与错误态 .is-error。"""
    css = BASE_CSS.read_text(encoding="utf-8")

    focus = _declarations(css, ".input:focus")
    assert "outline" in focus
    assert "border-color" in focus

    error = _declarations(css, ".input.is-error")
    assert "var(--danger)" in error

    assert "var(--danger)" in _declarations(css, ".textarea.is-error")


def test_notice_variants_use_state_tokens():
    """三种提示条各自走自己的状态色变量，不在 base.css 里另起炉灶。"""
    css = BASE_CSS.read_text(encoding="utf-8")
    for variant, token in (
        (".notice--info", "--accent-weak"),
        (".notice--warn", "--warn-weak"),
        (".notice--danger", "--danger-weak"),
    ):
        assert f"var({token})" in _declarations(css, variant)


# -------------------------------------------------------------- 上传页试点


def test_upload_page_links_v3_stylesheet_and_drops_inline_style(client):
    """G2-3 升级：拍卷子页挂 v3 样式（不再引旧两份），依旧零内联 <style>。"""
    html = client.get("/upload").text

    assert re.search(r'<link rel="stylesheet" href="/static/v3\.css(?:\?[^" ]+)?">', html)
    assert "tokens.css" not in html
    assert "base.css" not in html and "components.css" not in html
    assert "<style" not in html.lower()
    assert "font-family: sans-serif" not in html


def test_upload_page_keeps_its_visible_text(client):
    """The page explains capture, offers alternatives, and describes upload timing."""
    html = client.get("/upload").text

    assert "拍一张卷子" in html
    assert "页面铺平，光线均匀，四角都拍进来" in html
    assert "拍照" in html
    assert "从相册选" in html
    # 「已拍的卷子」这个出口还在（换了外观，不改路）
    assert 'href="/scans">已拍的卷子' in html
    assert "手动输入一道题" in html
    # 页脚那句"离线算"的承诺还在（换了说法）
    assert "照片质量先在这台设备上检查；确认后照片会上传到 Cairn。" in html


def test_upload_page_uses_component_classes(client):
    """G2-3 升级：拍卷子页真的用 v3 组件类，而不是换了个地方继续写死样式。

    v3 组件：标题 .page-title、主按钮 .btn.btn--primary、续步 .btn.btn--secondary、
    结论条 .verdict、数据条 .facts、预览 .figure、动作区 .select-actions；
    老类一个都不许回来。
    """
    html = client.get("/upload").text

    assert 'class="page-title"' in html
    assert 'class="btn btn--primary"' in html
    assert 'class="btn btn--secondary"' in html
    assert 'class="select-actions"' in html
    assert 'class="figure"' in html
    assert 'class="hint--foot"' in html
    # 结论条与数据条由脚本拼出：断言拼的就是这两个类，而不是别处又写一套样式
    assert 'class="verdict' in html
    assert 'class="facts"' in html
    assert "verdictBlock(" in html and "factsBlock(" in html

    # 反向：v1 的整页类名（详情条 / 卡片 / 拒绝块）不许回到这一页
    for legacy in ('class="detail"', 'class="card"', 'class="reject"'):
        assert legacy not in html, f"拍卷子页又出现了老类名：{legacy}"

    # 服务端拒绝原因走提示条（不是老的 reject 块），模板里逐字保留
    assert '<p class="notice notice--danger">{{ error }}</p>' in UPLOAD_HTML.read_text(
        encoding="utf-8"
    )


def test_every_var_used_in_base_css_is_defined_in_tokens():
    """拼错变量名不会报错、只会静默丢失样式——这条把 41 个引用逐个对回 tokens.css。"""
    base = BASE_CSS.read_text(encoding="utf-8")
    tokens = TOKENS_CSS.read_text(encoding="utf-8")

    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", base))
    defined = set(re.findall(r"^\s*(--[a-z0-9-]+)\s*:", tokens, flags=re.M))

    assert used, "base.css 一个变量都没用，说明没走令牌层"
    assert sorted(used - defined) == []


# ------------------------------------------------------------ 离线可用（AC6）


def test_new_assets_make_no_external_requests():
    """AC6：没有外链字体 / CDN / @import，NAS 断网也能显示。"""
    for path in (TOKENS_CSS, BASE_CSS, UPLOAD_HTML):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("http://", "https://", "@import", "fonts.googleapis", "cdn."):
            assert forbidden not in text, f"{path.name} 里出现了 {forbidden}"
        assert "url(http" not in text
