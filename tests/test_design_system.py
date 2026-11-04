"""设计守卫（卡 P0-a / P0-a-fix / P0-b / P0-c / D2a / G2-1）：令牌唯一、色值只住在令牌之家
（tokens.css=旧体系、v3.css :root=v3 体系）、每个 var() 都落得到本体系的定义、
v1 的 52 个旧令牌名字一个不少、双主题的 21 个令牌两套值逐字对上、图标雪碧图的符号名单与几何属性齐全、
组件基类只用令牌且靶达标。

这些是对着真实文件跑的静态检查，没有"跑一遍页面看看"这种软断言：

- `test_tokens_no_duplicate_definition`：同一个 `--name:` 在**同一个块里**只许定义一次
  （跨主题块重定义是双主题的唯一例外，见 `test_theme_tokens_*`）；
- `test_no_colour_literals_outside_tokens`：色值字面量只许出现在令牌之家——
  tokens.css 全文 + v3.css 的 `:root` 块（G2-1 升级：v3 页面不加载 tokens.css，
  v3.css 自带色板；两处之外一律不许）；
- `test_all_var_references_resolve`：`var(--x)` 与 `var(--x, fallback)` 里的 `--x`
  都必须在**本体系**的令牌之家里有定义（旧文件→tokens.css，v3 文件→v3.css；
  拼错或跨体系引用都会报错——页面不加载对方样式表，运行时就是静默丢样式）；
- `test_v3_tokens_match_the_wireframe`（G2-1）：v3 色板与红线尺寸逐字等于线框定稿；
- `test_legacy_tokens_preserved`：v1 的 52 个令牌**名字**写死在测试里，删一个就红
  ——前三条只认 var() 引用，`--gray-700` / `--shadow-2` 这种没人引用的旧令牌
  删掉它们不会红，所以必须有这份显式名单。值不再冻结：D2a 起是整站换皮（见下方注释）。
- `test_theme_tokens_light_and_dark` / `test_theme_override_blocks_repeat_the_two_palettes`：
  21 个主题令牌在亮 / 暗四处的值逐字对上卡面色板；
- `test_manual_theme_blocks_come_last`：`[data-theme]` 两个块排在媒体查询**之后**，
  同特异度下才能压过系统偏好（否则手动切换在暗色系统里失效）。
- `test_sprite_has_all_symbols`：`icons.svg` 的符号 id 集合与卡上 39 个名字**相等**（多一个少一个都红）；
- `test_sprite_symbol_geometry`：每个符号带齐 6 个几何属性，且至少含一个绘图子节点
  ——`viewBox` 少了图会按错误尺寸裁切，`stroke="currentColor"` 少了图标不会跟随文字色；
- `test_components_use_tokens_only`：`components.css` 里 0 个色值字面量、0 个 margin/padding/gap
  字面量，且九个基类一个不少（防止"空文件全绿"）；
- `test_tap_targets`：`.btn` / `.tabbar__item` / `.list-row` 三条规则都写了
  `min-height: var(--tap-min)`——少一条，手指就按不准；
- `test_icon_size_classes`：`.icon--*` 恰好是 18 / 20 / 24 三档，宽度高度各自对得上
  ——加第四档说明有人在造第七个字号那种事。
- `test_only_six_font_sizes`（卡 D3）：`components.css` 与模板里的 `font-size` 只许
  `var(--text-*)`，任何数字字面量（`15px`、`0.9rem`）都红；
- `test_no_font_weight_above_600`（卡 D3）：字重只许 400 / 500 / 600（或
  `var(--weight-*)`），`700` / `bold` 一律红。
- `test_more_panel_display_follows_open_state`（卡 W1-fix2）：`.more__panel` 的
  `display` 只许按开合状态成对写（关闭 `none` / 打开 `grid`），基础规则里不许有
  ——一条 `display` 就会盖掉 `<details>` 关闭时的隐藏，把整站撑出视口。
- `test_verdict_meta_wraps_long_tokens`（卡 W1-fix3）：`.verdict__meta` 必须有
  `overflow-wrap: anywhere` 与 `min-inline-size: 0`——机器串（质量指标 JSON、
  路径、设备 ID）没有断点，删掉这两条，390 视口会被顶到 715。

`scan_frame.html`、`scan_questions.html` 的内联样式已迁入 Cairn 语义令牌；页面内不再
保留色值字面量。此前的阶段性放行名单已清空，下面的断言继续保证名单不会悄悄长大。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from cairn import web as web_module

_SRC = Path(web_module.__file__).parent
STATIC_DIR = _SRC / "static"
TEMPLATES = _SRC / "templates"
TOKENS_CSS = STATIC_DIR / "tokens.css"
# 卡 G2-1：v3 网页层自带令牌之家（v3 页面不加载 tokens.css）。色值字面量的
# 许可范围从"只住 tokens.css"升级为"每个设计体系一个令牌之家"——tokens.css
# （旧体系）之外，只有 v3.css 的 :root 块能写色值；两个体系互不许引用对方令牌。
V3_CSS = STATIC_DIR / "v3.css"

# 阶段性内联样式清理白名单：当前为空。需要临时例外时必须经计划和评审记录。
PENDING_COLOUR_PAGES: tuple[str, ...] = ()

# `--name:` 开头的定义行；缩进允许（tokens.css 全在 :root 里）。
_DEFINITION = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.M)
# var(--x) 与 var(--x, fallback) 一并收；fallback 里再套 var() 也会被继续扫到。
_VAR_USE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
# 色值字面量：#rgb / #rrggbb / rgb( / rgba( / hsl( / hsla(。
_COLOUR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_code(path: Path) -> str:
    """读文件，并把 CSS 注释剥掉。

    注释里会写"别在这里写 #rrggbb""组件只能引用 var(--x)"这类说明——
    那是文档不是代码，混进扫描会得出假红。
    """
    text = _read(path)
    if path.suffix != ".css":
        return text
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _token_names() -> list[str]:
    """tokens.css 里出现的全部令牌名（按出现顺序，含重复）。"""
    return _DEFINITION.findall(_read_code(TOKENS_CSS))


def _rule_blocks(text: str, prefix: str = "") -> list[tuple[str, str]]:
    """把样式表拆成 `[(作用域标签, 声明块文本)]`，`@media` 里的规则带上前缀。

    双主题要按"块"判重（跨块同名是故意的），所以不能只看整份文件。
    本项目只有 `@media` 里再包一条 `:root` 这一层嵌套，手写解析够用，也好审。
    """
    blocks: list[tuple[str, str]] = []
    index = 0
    while True:
        open_at = text.find("{", index)
        if open_at < 0:
            return blocks
        selector = " ".join(text[index:open_at].split())
        depth, cursor = 1, open_at + 1
        while depth and cursor < len(text):
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
            cursor += 1
        body = text[open_at + 1 : cursor - 1]
        label = f"{prefix}{selector}"
        blocks.append((label, body))
        if selector.startswith("@media"):
            blocks.extend(_rule_blocks(body, f"{label} "))
        index = cursor


def _scope_names() -> list[tuple[str, list[str]]]:
    """每个作用域里出现的令牌名（保留重复，用来抓同块重复定义）。"""
    return [
        (label, _DEFINITION.findall(body))
        for label, body in _rule_blocks(_read_code(TOKENS_CSS))
    ]


# 双主题的四个作用域标签（`:root` 亮色 + 媒体查询里的暗色 + 两个手动覆盖块）。
ROOT_SCOPE = ":root"
DARK_MEDIA_SCOPE = "@media (prefers-color-scheme: dark) :root"
DARK_SCOPE = '[data-theme="dark"]'
LIGHT_SCOPE = '[data-theme="light"]'


def _scope_values(label: str) -> dict[str, str]:
    """取一个作用域里的 `名字 → 值`（空白折叠成单空格，方便跨行比对）。"""
    for scope, body in _rule_blocks(_read_code(TOKENS_CSS)):
        if scope == label:
            return {
                name: " ".join(value.split())
                for name, value in _DECLARATION.findall(body)
            }
    raise AssertionError(f"没有找到作用域：{label}")


def _style_and_template_files() -> list[Path]:
    return sorted([*STATIC_DIR.glob("*.css"), *TEMPLATES.glob("*.html")])


# ----------------------------------- 守卫一：一个名字一个值（同一个块里）


def test_tokens_no_duplicate_definition():
    """同一个块里同一个令牌只许定义一次：后一条会静默盖掉前一条，两边都不报错。

    跨块同名是双主题的正当写法（:root / @media / [data-theme] 各给一套值），
    所以判重按块来；"四个块必须是同一批名字"由下面的主题测试单独钉住。
    """
    scopes = _scope_names()
    assert sum(len(names) for _, names in scopes) >= 100, (
        f"只解析到 {sum(len(names) for _, names in scopes)} 个令牌，正则或文件结构不对"
    )

    duplicated = {
        label: sorted({name for name in names if names.count(name) > 1})
        for label, names in scopes
    }
    duplicated = {label: names for label, names in duplicated.items() if names}
    assert duplicated == {}, f"这些令牌在同一个块里定义了不止一次：{duplicated}"


# ----------------------------------------------- 守卫二：色值只住在令牌文件


def test_no_colour_literals_outside_tokens():
    """tokens.css 之外不许写死颜色，否则换肤与深色模式都会漏。

    G2-1 升级（断言只强不弱）：v3 体系的令牌住在 v3.css，而 v3 页面不加载
    tokens.css——所以 v3.css 的 **:root 令牌块** 是第二个（也是最后一个）可写
    色值的地方；:root 之外（含媒体查询里的一切规则）仍一个色值不许有。
    其余所有 css / 模板维持原口径：零色值。
    """
    offenders: dict[str, list[str]] = {}
    for path in _style_and_template_files():
        if path == TOKENS_CSS or path.name in PENDING_COLOUR_PAGES:
            continue
        text = _read_code(path)
        if path == V3_CSS:
            hits = [
                hit
                for label, body in _rule_blocks(text)
                if label != ":root"
                for hit in _COLOUR_LITERAL.findall(body)
            ]
        else:
            hits = _COLOUR_LITERAL.findall(text)
        if hits:
            offenders[path.name] = sorted(set(hits))[:5]
    assert offenders == {}, f"色值只能住在令牌之家（tokens.css / v3.css :root），这些文件里出现了字面量：{offenders}"

    # 模板色值不设例外：遗留样式清理后必须为空。
    still_literal = sorted(
        path.name for path in TEMPLATES.glob("*.html") if _COLOUR_LITERAL.search(_read_code(path))
    )
    assert still_literal == sorted(PENDING_COLOUR_PAGES), (
        "模板色值残留名单变了："
        f"{still_literal}。新页面必须引用对应设计令牌。"
    )


# ------------------------------------------- 守卫三：每个引用都落得到定义


def _v3_home_files() -> set[str]:
    """v3 体系的文件名：v3.css 自己 + 引用它或 `_v3.html` 的模板（新页自动认领）。

    旧模板绝不会出现这两个串（它们引 tokens.css / base.css），名单不用人手维护。
    """
    names = {V3_CSS.name}
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "v3.css" in text or "_v3.html" in text:
            names.add(path.name)
    return names


def test_all_var_references_resolve():
    """`var(--x)` 拼错不会报错，只会让这条声明整行失效——所以逐个对回令牌之家。

    G2-1 升级（断言只强不弱）：令牌之家一分为二——旧体系文件（base.css /
    components.css / 旧模板）只许用 tokens.css 的名字，v3 文件只许用 v3.css
    :root 的名字；跨体系引用同样报错（页面根本不加载对方的样式表，运行时就是
    取不到值）。比旧口径（一律对 tokens.css）多抓"v3 页误用旧令牌名"这类静默丢样式。
    """
    legacy_home = set(_token_names())
    assert legacy_home, "tokens.css 里一个令牌都没解析到"
    v3_home = set(_DEFINITION.findall(_read_code(V3_CSS)))
    assert v3_home, "v3.css 里一个令牌都没解析到"
    v3_files = _v3_home_files()

    unresolved: dict[str, set[str]] = {}
    used = 0
    for path in _style_and_template_files():
        home = v3_home if path.name in v3_files else legacy_home
        for match in _VAR_USE.finditer(_read_code(path)):
            used += 1
            name = match.group(1)
            if name not in home:
                unresolved.setdefault(name, set()).add(path.name)

    assert used > 100, f"只扫到 {used} 处 var()，扫描范围不对"
    detail = {name: sorted(files) for name, files in unresolved.items()}
    assert unresolved == {}, f"这些 var() 找不到定义（或引了对方体系的令牌）：{detail}"


# ------------------------------- 守卫四：v1 的 52 个令牌名字一个不许少
#
# 前三条守卫都靠 var() 引用找目标，`--gray-700` 与 `--shadow-2` 没有任何
# var() 引用——删掉它们，三条守卫全绿。所以这里另抄一份 v1 名单，不从 git
# 动态取（测试要能在离线 / 无历史的环境跑）。名单来源：
#   git show 79bb637^:src/cairn/static/tokens.css
#
# 值只作为历史记录：D2a 起是整站换皮，旧名字的值改指新色板，冻结的是**名字**。
# 每条值仍是 v1 的原始写法，方便将来对照"换皮前长什么样"。

LEGACY_TOKENS = {
    # 中性灰阶 6 档 + 纸面白
    "--gray-50": "#f8fafc",
    "--gray-100": "#eef1f4",
    "--gray-300": "#cbd5dc",
    "--gray-500": "#6b7785",
    "--gray-700": "#38424d",
    "--gray-900": "#1a222b",
    "--white": "#ffffff",
    # 强调色 1 + 状态色 3（各带浅底与描边）
    "--accent": "#0f766e",
    "--accent-strong": "#115e59",
    "--accent-weak": "#e6f4f1",
    "--accent-border": "#99d3cb",
    "--ok": "#15803d",
    "--ok-weak": "#e8f6ec",
    "--ok-border": "#a6d8b5",
    "--warn": "#b45309",
    "--warn-weak": "#fdf3e3",
    "--warn-border": "#eccb9a",
    "--danger": "#b91c1c",
    "--danger-weak": "#fdeaea",
    "--danger-border": "#f0b3b3",
    "--on-accent": "#ffffff",
    # 语义别名（8 个，v1 就是 var() 形式）
    "--bg": "var(--gray-50)",
    "--surface": "var(--white)",
    "--surface-muted": "var(--gray-100)",
    "--text": "var(--gray-900)",
    "--text-muted": "var(--gray-500)",
    "--border": "var(--gray-100)",
    "--border-strong": "var(--gray-300)",
    "--focus-ring": "var(--accent-border)",
    # 字体栈（两行写，比对前已折叠空白）
    "--font-sans": (
        '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", '
        '"Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif'
    ),
    "--font-mono": (
        'ui-monospace, SFMono-Regular, Menlo, Consolas, "Courier New", monospace'
    ),
    # 字号 5 档
    "--text-xs": "12px",
    "--text-sm": "14px",
    "--text-base": "16px",
    "--text-lg": "20px",
    "--text-xl": "26px",
    # 行高 2 档
    "--leading-tight": "1.25",
    "--leading-normal": "1.6",
    # 间距 6 档
    "--space-1": "4px",
    "--space-2": "8px",
    "--space-3": "12px",
    "--space-4": "16px",
    "--space-5": "24px",
    "--space-6": "32px",
    # 圆角 3 档（v1 值；v2 只许新增名字，不许改这三个的值）
    "--radius-sm": "4px",
    "--radius-md": "8px",
    "--radius-lg": "14px",
    # 阴影（颜色走变量）
    "--shadow-color": "rgba(26, 34, 43, 0.12)",
    "--shadow-1": "0 1px 2px var(--shadow-color)",
    "--shadow-2": "0 4px 16px var(--shadow-color)",
    # 交互尺寸
    "--tap-min": "44px",
    "--content-width": "860px",
}

# `--name: value;`（值可跨行；注释先剥掉再匹配）。
_DECLARATION = re.compile(r"^\s*(--[a-z0-9-]+)\s*:\s*([^;]*);", re.M)
# 整条声明就是一个别名：`var(--x)`，没有 fallback、没有拼别的东西。
_PURE_ALIAS = re.compile(r"^var\(\s*(--[a-z0-9-]+)\s*\)$")


def _definitions(path: Path) -> dict[str, str]:
    """名字 → 值（空白折叠成单空格，方便跨行比对）。"""
    text = _read_code(path)
    return {name: " ".join(value.split()) for name, value in _DECLARATION.findall(text)}


def _resolved(table: dict[str, str]) -> dict[str, str]:
    """把纯别名的值一路解到字面量：`--bg` → `var(--gray-50)` → `#f8fafc`。"""
    out: dict[str, str] = {}
    for name, raw in table.items():
        value, seen = raw, {name}
        while True:
            match = _PURE_ALIAS.match(value)
            if match is None:
                break
            target = match.group(1)
            assert target in table, f"{name} 别名指向了不存在的 {target}"
            assert target not in seen, f"别名成环：{name} → {target}"
            seen.add(target)
            value = table[target]
        out[name] = value
    return out


def test_legacy_tokens_preserved():
    """v1 的 52 个令牌**名字**一个不许少；值不再冻结。

    为什么放开值：Phase 0 的地基阶段是"只加不改"，那时名字和值一起冻住；
    本卡（D2a）是 9 哥拍板的整站换皮——旧名字必须改指新色板，页面不动模板就能
    跟着换主题。名字仍然一个不能少：旧组件和旧模板还在引用它们，少一个就是白屏级事故。
    """
    assert len(LEGACY_TOKENS) == 52, f"名单条数写错了：{len(LEGACY_TOKENS)}"

    # 名单里的值留着当"v1 长什么样"的历史记录，不再参与断言；
    # 解一遍别名链，顺带保证 `--bg: var(--x)` 这类指向没有拼错（错了 CSS 只会静默失效）。
    current = _resolved(_definitions(TOKENS_CSS))

    missing = sorted(name for name in LEGACY_TOKENS if name not in current)
    assert missing == [], f"v1 令牌被删了：{missing}"


# ------------------------- 守卫四之一：双主题的 21 个令牌（卡 D2a · 令牌表）
#
# 值逐字抄卡：亮 = 方格纸，暗 = 夜读。两套值只有一处真值来源（这里），
# tokens.css 的四个块都得跟上——写错一个 hex 不会报错，只会让某一页在夜里瞎掉。

THEME_TOKENS = {
    "--bg": ("#F2F1EC", "#14120F"),
    "--surface": ("#FAF9F5", "#1C1A16"),
    "--surface-raised": ("#FFFFFF", "#26231D"),
    "--border": ("#D9D6CC", "#332F27"),
    "--border-strong": ("#8C8779", "#6B6455"),
    "--text": ("#1C1A17", "#EDE9E0"),
    "--text-secondary": ("#55524A", "#B0AA9C"),
    "--text-tertiary": ("#6E6A60", "#948E80"),
    "--accent": ("#8A5A12", "#E9B44C"),
    "--accent-fill": ("#E9B44C", "#D08C3A"),
    "--accent-on": ("#1C1A17", "#1C1A17"),
    "--ok": ("#1F6B45", "#6FBF8E"),
    "--ok-weak": ("#E4EFE7", "#15291C"),
    "--ok-border": ("#A8C9B6", "#2E5C3E"),
    "--warn": ("#8A5000", "#E0A040"),
    "--warn-weak": ("#FBF0D6", "#2B1F0C"),
    "--warn-border": ("#D9BE86", "#5C4416"),
    "--danger": ("#A32E22", "#F07A66"),
    "--danger-weak": ("#FBE6E3", "#2B1613"),
    "--danger-border": ("#E0AEA6", "#5C2B22"),
    "--focus": ("#8A5A12", "#E9B44C"),
}

# 几何令牌两模式相同：控件 4、面板 8、描边永远 1px（1px 写在组件里）。
THEME_GEOMETRY = {"--radius-control": "4px", "--radius-surface": "8px"}

# 高程（卡 D2b）：亮色一律 none；暗色只有 --elev-raised 这一层，且只给
# surface-raised（浮层 / 弹层 / 下拉）。--elev-1/2/3 两个模式都是 none，
# 所以只在 :root 写一次，主题块里不重复。
THEME_SHADOWS = {"--elev-raised": ("none", "0 8px 24px rgba(0, 0, 0, 0.55)")}


def test_theme_tokens_light_and_dark():
    """AC2：21 个令牌在亮 / 暗两处的值逐字等于卡面（少一个、错一位都红）。"""
    assert len(THEME_TOKENS) == 21, f"名单条数写错了：{len(THEME_TOKENS)}"

    light = _scope_values(ROOT_SCOPE)
    dark = _scope_values(DARK_MEDIA_SCOPE)

    missing = sorted(set(THEME_TOKENS) - set(light))
    assert missing == [], f"亮色 :root 缺令牌：{missing}"
    # 暗色块只装这一批颜色 + 那一层高程：多出来的是有人往主题块里塞了别的东西。
    expected_dark = set(THEME_TOKENS) | set(THEME_SHADOWS)
    assert set(dark) == expected_dark, (
        f"暗色 @media 块的令牌集合不对：多 {sorted(set(dark) - expected_dark)}，"
        f"少 {sorted(expected_dark - set(dark))}"
    )

    wrong: dict[str, dict[str, str | None]] = {}
    for name, (want_light, want_dark) in THEME_TOKENS.items():
        if light.get(name) != want_light:
            wrong[name] = {"亮·want": want_light, "亮·got": light.get(name)}
        if dark.get(name) != want_dark:
            wrong.setdefault(name, {}).update(
                {"暗·want": want_dark, "暗·got": dark.get(name)}
            )
    assert wrong == {}, f"主题令牌的值和卡面不一致：{wrong}"


def test_elevation_is_flat_in_light_and_one_layer_in_dark():
    """卡 D2b：亮色 elev 全 none；暗色只给 --elev-raised 一层，其余仍是 none。"""
    light = _scope_values(ROOT_SCOPE)
    dark = _scope_values(DARK_MEDIA_SCOPE)

    for name in ("--elev-0", "--elev-1", "--elev-2", "--elev-3"):
        assert light.get(name) == "none", f"亮色 {name} 必须 none：{light.get(name)}"
        assert dark.get(name) is None, f"{name} 不该在暗色块里重写（两模式都是 none）"

    for name, (want_light, want_dark) in THEME_SHADOWS.items():
        assert light.get(name) == want_light, f"{name} 亮值不对：{light.get(name)}"
        assert dark.get(name) == want_dark, f"{name} 暗值不对：{dark.get(name)}"


def test_theme_override_blocks_repeat_the_two_palettes():
    """AC3 其一：两个 `data-theme` 块各自写全整套色板，值与亮 / 暗逐字相同。"""
    assert _scope_values(DARK_SCOPE) == {
        **{name: pair[1] for name, pair in THEME_TOKENS.items()},
        **{name: pair[1] for name, pair in THEME_SHADOWS.items()},
    }, "[data-theme=\"dark\"] 和暗色 @media 不是同一套值"
    assert _scope_values(LIGHT_SCOPE) == {
        **{name: pair[0] for name, pair in THEME_TOKENS.items()},
        **{name: pair[0] for name, pair in THEME_SHADOWS.items()},
    }, "[data-theme=\"light\"] 和亮色 :root 不是同一套值"


def test_manual_theme_blocks_come_last():
    """AC3 其二：两个 `data-theme` 块排在媒体查询**之后**，手动覆盖才压得过系统偏好。

    `:root` 与 `[data-theme]` 的特异度相同（都是 0,1,0），同分只看先后：
    顺序反了，暗色系统里 `[data-theme="light"]` 会被 @media 盖掉，手动切亮色失效。
    """
    order = [label for label, _ in _rule_blocks(_read_code(TOKENS_CSS))]
    for label in (ROOT_SCOPE, DARK_MEDIA_SCOPE, DARK_SCOPE, LIGHT_SCOPE):
        assert label in order, f"tokens.css 里没有这个块：{label}"
    assert (
        order.index(ROOT_SCOPE)
        < order.index(DARK_MEDIA_SCOPE)
        < order.index(DARK_SCOPE)
        < order.index(LIGHT_SCOPE)
    ), f"块顺序不对（媒体查询必须在 data-theme 之前）：{order}"


def test_theme_geometry_is_not_redefined_per_theme():
    """AC6：圆角两模式一致 —— 只在 :root 定义一次，任何主题块都不许重写。"""
    values = _scope_values(ROOT_SCOPE)
    wrong = {
        name: {"want": want, "got": values.get(name)}
        for name, want in THEME_GEOMETRY.items()
        if values.get(name) != want
    }
    assert wrong == {}, f"几何令牌的值不对：{wrong}"

    for label in (DARK_MEDIA_SCOPE, DARK_SCOPE, LIGHT_SCOPE):
        redefined = sorted(set(THEME_GEOMETRY) & set(_scope_values(label)))
        assert redefined == [], f"{label} 里不该重写几何令牌：{redefined}"


# ---------------------- 守卫四之二：D1 的六个遮罩 / 框线令牌（卡 D1 · A.1）
#
# 值逐字抄卡。这六个不是"好看的变量"，是裁剪页看得见的判据：
#   - 遮罩盖住框外、纸保持全亮 → 亮度差全靠 --crop-scrim 的 0.55；
#   - 红底和白纸上都要看得见 → 框线是深底 + 白线两层，缺一层就有一种底色看不见；
#   - 角点描边与投影分两层 → 白纸上是深环、红底上是投影。
# 值改一点点都不会报错，只会让"框"在某类照片上消失，所以在这里冻住。

CROP_TOKENS = {
    "--crop-scrim": "rgba(16, 20, 24, 0.55)",
    "--crop-edge-halo": "rgba(0, 0, 0, 0.30)",
    "--crop-edge-inner": "#ffffff",
    "--crop-handle-ring": "rgba(0, 0, 0, 0.25)",
    "--crop-handle-shadow": "rgba(16, 20, 24, 0.35)",
    "--crop-bracket-size": "24px",
}


def test_crop_tokens_are_frozen():
    """六个令牌一个不少、一个值不错（多一个少一个、改一位小数都红）。"""
    assert len(CROP_TOKENS) == 6, f"名单条数写错了：{len(CROP_TOKENS)}"

    current = _definitions(TOKENS_CSS)
    missing = sorted(name for name in CROP_TOKENS if name not in current)
    assert missing == [], f"D1 的令牌被删了：{missing}"

    changed = {
        name: {"want": want, "got": current[name]}
        for name, want in CROP_TOKENS.items()
        if current[name] != want
    }
    assert changed == {}, f"D1 的令牌值被改了：{changed}"


# -------------------------------------- 守卫五 / 六：图标雪碧图（Phase 0-b）
#
# 名单同样写死在测试里：不从 `static/icons.svg` 自己推，否则删掉一个符号后
# "集合相等"会变成"自己跟自己相等"，守卫永远绿。
# 路径数据来自 /tmp/lucide/<name>.svg（Lucide v1.66.0），本测试只查结构与属性，
# 不比对 `d=`（那是 EVIDENCE 里的抽查）。

ICONS_SVG = STATIC_DIR / "icons.svg"

SPRITE_SYMBOLS = (
    "house", "images", "camera", "book-open-check", "ellipsis", "inbox", "crop",
    "scan", "scan-text", "pencil", "eye", "volume-2", "circle-check", "circle-x",
    "triangle-alert", "circle-alert", "loader-circle", "book-open", "calendar-days",
    "chart-column", "webcam", "upload", "download", "wrench", "settings", "trash-2",
    "undo-2", "zoom-in", "zoom-out", "maximize", "hand", "pen-tool", "x",
    "chevron-left", "chevron-right", "check", "plus", "tag", "search",
)

# 每个 <symbol> 都要带齐的属性（缺一个就画不出来或画错）。
SPRITE_GEOMETRY = {
    "viewBox": "0 0 24 24",
    "fill": "none",
    "stroke": "currentColor",
    "stroke-width": "2",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
}

# 允许作为图标本体的子节点；空 <symbol> 等于一个看不见的图标。
DRAWING_TAGS = ("path", "circle", "rect", "line", "polyline", "polygon")


def _tag(element: ET.Element) -> str:
    """去掉 XML 命名空间后的标签名（文件加不加 xmlns 都能读）。"""
    return element.tag.rsplit("}", 1)[-1]


def _sprite() -> ET.Element:
    assert ICONS_SVG.exists(), f"雪碧图不存在：{ICONS_SVG}"
    return ET.fromstring(_read(ICONS_SVG))


def _symbols() -> list[ET.Element]:
    return [el for el in _sprite() if _tag(el) == "symbol"]


def test_sprite_has_all_symbols():
    """39 个 id 一个不少、一个不多：集合相等，不是"包含"。"""
    assert len(SPRITE_SYMBOLS) == 39, f"卡上的名单条数写错了：{len(SPRITE_SYMBOLS)}"

    found = [el.get("id") for el in _symbols()]
    duplicated = sorted({sid for sid in found if found.count(sid) > 1})
    assert duplicated == [], f"符号 id 重复了：{duplicated}"

    expected = {f"i-{name}" for name in SPRITE_SYMBOLS}
    missing = sorted(expected - set(found))
    extra = sorted(set(found) - expected)
    assert missing == [] and extra == [], f"符号集合对不上，缺：{missing}，多：{extra}"


def test_sprite_symbol_geometry():
    """每个符号的属性值与子节点都齐全，且检查数量对得上（防"零个符号全绿"）。"""
    symbols = _symbols()
    assert len(symbols) == len(SPRITE_SYMBOLS), (
        f"只解析到 {len(symbols)} 个符号，名单是 {len(SPRITE_SYMBOLS)} 个"
    )

    problems: dict[str, object] = {}
    for symbol in symbols:
        sid = symbol.get("id") or "<无 id>"
        wrong = {
            name: {"want": want, "got": symbol.get(name)}
            for name, want in SPRITE_GEOMETRY.items()
            if symbol.get(name) != want
        }
        drawings = [child for child in symbol if _tag(child) in DRAWING_TAGS]
        if wrong or not drawings:
            problems[sid] = {
                "属性不符": wrong,
                "绘图子节点": len(drawings),
            }
    assert problems == {}, f"这些符号的几何属性不齐全：{problems}"


# ------------------------------- 守卫七 / 八 / 九：组件基类（Phase 0-c）
#
# components.css 现在没有任何页面引用，但它是 Phase 1 全部页面的地基。
# 三条守卫都对着文件本身跑：值有没有走令牌、靶够不够大、图标尺寸有没有多出第四档。

COMPONENTS_CSS = STATIC_DIR / "components.css"

# 卡的类清单：少一个说明文件被截断或规则被误删（否则空文件也能让前两条全绿）。
REQUIRED_COMPONENTS = (
    ".page", ".btn", ".list-row", ".badge", ".sheet",
    ".tabbar", ".tabbar__item", ".icon", ".field",
)

# margin / padding / gap 及其逻辑属性（margin-inline、padding-block-end、row-gap…）。
_SPACING_PROPERTY = re.compile(
    r"^(?:margin|padding|gap|row-gap|column-gap)(?:-[a-z-]+)?$", re.I
)
# 拆掉 var()/env() 之后仍剩下的长度字面量。
_SPACING_LITERAL = re.compile(r"\d*\.?\d+(?:px|rem|em|%)", re.I)
_VAR_OR_ENV = re.compile(r"(?:var|env)\([^)]*\)")

# 卡 D2b 的印刷表单把间距写死成 px（2 / 9 / 10 / 12 / 14 / 16 / 18），卡面要求逐字
# 照抄。所以这几条规则按"选择器 + 属性 + 值"逐个放行，**放行的是具体值**：同一规则
# 换成别的字面量、或者别处再写字面量，仍然红（名单不许长大）。
PRINT_FORM_SPACING = {
    ".verdict": {"padding": "9px 0", "gap": "10px", "margin-block": "0 14px"},
    ".facts": {"gap": "12px", "margin-block": "0 16px"},
    ".facts__cell:not(:first-child)": {"padding-inline-start": "12px"},
    ".crop-actions": {"margin-block-start": "18px"},
    ".crop-actions .btn": {"padding": "0 2px"},
}

# `selector { a: b; c: d }`：组件文件是平铺的，没有 @media 嵌套，所以够用。
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_DECLARATION_ANY = re.compile(r"([-\w]+)\s*:\s*([^;]+)")
_ICON_MODIFIER = re.compile(r"\.icon--(\d+)")

ICON_SIZES = {"18", "20", "24"}


def _rules(path: Path) -> list[tuple[list[str], dict[str, str]]]:
    """把样式表拆成 [(选择器列表, {属性: 值})]，注释先剥掉。"""
    return [
        (
            [sel.strip() for sel in selector.split(",") if sel.strip()],
            {
                prop.strip().lower(): " ".join(value.split())
                for prop, value in _DECLARATION_ANY.findall(body)
            },
        )
        for selector, body in _RULE.findall(_read_code(path))
    ]


def test_components_use_tokens_only():
    """组件基类：0 个色值字面量、0 个间距字面量，九个基类一个不少。"""
    assert COMPONENTS_CSS.exists(), f"组件样式不存在：{COMPONENTS_CSS}"

    css = _read_code(COMPONENTS_CSS)
    rules = _rules(COMPONENTS_CSS)
    assert len(rules) >= 10, f"只解析到 {len(rules)} 条规则，文件结构不对"

    defined = {sel for selectors, _ in rules for sel in selectors}
    missing = [name for name in REQUIRED_COMPONENTS if name not in defined]
    assert missing == [], f"这些基类没写：{missing}"

    colours = sorted(set(_COLOUR_LITERAL.findall(css)))
    assert colours == [], f"components.css 不许出现色值字面量：{colours}"

    offenders: dict[str, dict[str, str]] = {}
    for selectors, declarations in rules:
        for prop, value in declarations.items():
            if _SPACING_PROPERTY.match(prop) is None:
                continue
            leftover = _VAR_OR_ENV.sub("", value)
            if _SPACING_LITERAL.search(leftover) is None:
                continue
            allowed = any(
                PRINT_FORM_SPACING.get(selector, {}).get(prop) == value
                for selector in selectors
            )
            if not allowed:
                offenders.setdefault(", ".join(selectors), {})[prop] = value
    assert offenders == {}, f"间距只能用 var(--space-*)，这些规则写了字面量：{offenders}"


def test_primary_button_fills_with_accent_fill():
    """V8：主按钮的底是"填色"，不是链接色。

    `--color-accent` 在亮色是 #8A5A12（深棕），拿它当底 + 墨字 = 整块发暗；
    tokens.css 的 `--accent-fill` 一直注明"主按钮 / 选中块的填色"，却从没被引用。
    这条同时钉住：别名存在、底/描边走填色、hover 不再退回链接色。
    """
    assert _definitions(TOKENS_CSS).get("--color-accent-fill") == "var(--accent-fill)"

    rules = _rules(COMPONENTS_CSS)

    def declarations_of(selector: str) -> dict[str, str]:
        found = [decls for selectors, decls in rules if selector in selectors]
        assert len(found) == 1, f"{selector} 应该恰好一条规则，实际 {len(found)}"
        return found[0]

    base = declarations_of(".btn--primary")
    assert base.get("background") == "var(--color-accent-fill)", base
    assert base.get("border-color") == "var(--color-accent-fill)", base
    assert base.get("color") == "var(--color-on-accent)", base

    hover = declarations_of(".btn--primary:hover")
    assert hover.get("background") == "var(--color-accent-fill)", hover
    assert hover.get("opacity") == "0.92", hover
    fell_back = [prop for prop, value in hover.items() if "var(--color-accent)" in value]
    assert fell_back == [], f"hover 退回了链接色（深棕）：{fell_back}"


def test_tap_targets():
    """按钮 / 标签项 / 侧栏项 / 列表行都必须有 min-height: var(--tap-min)。"""
    rules = _rules(COMPONENTS_CSS)

    problems: dict[str, list[str | None]] = {}
    for target in (".btn", ".tabbar__item", ".nav__item", ".list-row"):
        heights = [
            declarations.get("min-height")
            for selectors, declarations in rules
            if target in selectors
        ]
        if "var(--tap-min)" not in heights:
            problems[target] = heights
    assert problems == {}, f"这些规则没有 44px 靶高：{problems}"


def test_more_panel_display_follows_open_state():
    """「更多」面板的 `display` 只许按开合状态给，成对写，基础规则里一条都不许有。

    <details> 关着时浏览器靠 UA 样式隐藏非 summary 的子内容；作者样式里只要有一条
    `display` 落在面板上（哪怕只是基础规则里的 `display: grid`），面板就会带着
    320 的宽度继续参与布局，把整站每个用 `_nav.html` 的页面撑出视口（W1-fix2）。
    所以两条规则必须同时存在、值必须是 none / grid；删掉 `display: none` 那条，
    这条守卫立刻红——这是它存在的唯一理由。
    """
    rules = _rules(COMPONENTS_CSS)

    def declarations_of(selector: str) -> list[dict[str, str]]:
        return [decls for selectors, decls in rules if selector in selectors]

    closed = declarations_of(".more:not([open]) > .more__panel")
    opened = declarations_of(".more[open] > .more__panel")

    assert closed, "缺少 .more:not([open]) > .more__panel 规则"
    assert opened, "缺少 .more[open] > .more__panel 规则"
    assert any(d.get("display") == "none" for d in closed), (
        f"关闭态没有 display: none，面板会继续占位：{closed}"
    )
    assert any(d.get("display") == "grid" for d in opened), (
        f"打开态没有 display: grid，面板展不开：{opened}"
    )

    base = declarations_of(".more__panel")
    assert base, "缺少 .more__panel 基础规则"
    leaked = [d["display"] for d in base if "display" in d]
    assert leaked == [], (
        f"基础规则里不许写 display（会盖掉 <details> 的关闭隐藏，W1-fix2 回归）：{leaked}"
    )


def test_verdict_meta_wraps_long_tokens():
    """`.verdict__meta` 必须允许任意断点，且作为 flex 项能被压到比内容更窄。

    结论行里右边那串是机器值：质量指标 JSON、落盘路径、设备 ID，一整串没有断点。
    flex 项的 `min-width: auto` 取 min-content，没有断点时 min-content 就是整串的宽，
    项不肯缩，整页就被顶出视口（W1-fix3：390 视口下 scrollWidth 715）。
    所以两条必须同时在：

    - `overflow-wrap: anywhere`：只有 `anywhere` 会同时缩小 min-content；
      `break-word` 只影响换行、不影响 min-content，照样撑宽；
    - `min-inline-size: 0`：把 flex 的 auto 最小尺寸让开。

    删掉任意一条，这条守卫立刻红——它存在的唯一理由就是防这两条被人"清干净"。
    """
    rules = _rules(COMPONENTS_CSS)

    blocks = [decls for selectors, decls in rules if ".verdict__meta" in selectors]
    assert blocks, "缺少 .verdict__meta 规则"

    breaks = [
        decls
        for decls in blocks
        if decls.get("overflow-wrap") == "anywhere"
        or decls.get("word-break") == "break-all"
    ]
    assert breaks, (
        "`.verdict__meta` 没有允许断点（overflow-wrap: anywhere / word-break: break-all），"
        f"长机器串会把页面顶出 390 视口。`break-word` 不算数（不影响 min-content）：{blocks}"
    )

    min_sizes = [decls.get("min-inline-size") for decls in blocks]
    assert "0" in min_sizes, (
        f"`.verdict__meta` 少了 min-inline-size: 0，flex 项仍按 min-content 撑宽：{min_sizes}"
    )


def test_icon_size_classes():
    """`.icon--*` 只有 18 / 20 / 24 三档，且宽高与档位一致。"""
    rules = _rules(COMPONENTS_CSS)

    sizes: dict[str, dict[str, str]] = {}
    for selectors, declarations in rules:
        for selector in selectors:
            match = _ICON_MODIFIER.fullmatch(selector)
            if match is not None:
                sizes[match.group(1)] = declarations

    assert set(sizes) == ICON_SIZES, (
        f"图标尺寸必须恰好是 {sorted(ICON_SIZES)}，实际：{sorted(sizes)}"
    )

    wrong = {
        size: {"width": decl.get("width"), "height": decl.get("height")}
        for size, decl in sizes.items()
        if decl.get("width") != f"{size}px" or decl.get("height") != f"{size}px"
    }
    assert wrong == {}, f"图标宽高和档位对不上：{wrong}"


# ----------------------------------------- 守卫十 / 十一：字号六档、字重封顶（卡 D3）
#
# 卡 D3 把「一屏 8 个字号」收成六档：11 / 12 / 14 / 16 / 20 / 28。两条守卫都扫
# components.css 与模板（内联 <style> 与 style=""）：看不到数字就不算数——写成
# `font-size: 15px` 这种字面量，层级会再漂回去，而且没有任何报错。

SIX_TEXT_TOKENS = (
    "--text-meta",
    "--text-caption",
    "--text-label",
    "--text-body",
    "--text-value",
    "--text-display",
)

# 阶段性字号/字重放行名单；清理遗留内联样式后与色值名单共用。
PENDING_TYPE_PAGES = PENDING_COLOUR_PAGES

_FONT_SIZE = re.compile(r"font-size\s*:\s*([^;}{\"']+)", re.I)
_FONT_WEIGHT = re.compile(r"font-weight\s*:\s*([^;}{\"']+)", re.I)
_TEMPLATE_STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_INLINE_STYLE = re.compile(r'style\s*=\s*"([^"]*)"', re.I)
_TEXT_SIZE_REF = re.compile(r"var\(\s*--text-[a-z0-9-]+\s*\)")
_WEIGHT_REF = re.compile(r"var\(\s*--weight-(?:regular|medium|semibold)\s*\)")
_ALLOWED_WEIGHTS = {"400", "500", "600"}


def _declared(prop: re.Pattern[str], path: Path) -> list[str]:
    """取一个文件里某属性的值：CSS 走全文，模板只走 <style> 与 style=""。"""
    text = _read_code(path)
    if path.suffix == ".css":
        chunks = [text]
    else:
        chunks = [
            re.sub(r"/\*.*?\*/", "", chunk, flags=re.S)
            for chunk in [*_TEMPLATE_STYLE.findall(text), *_INLINE_STYLE.findall(text)]
        ]
    return [
        " ".join(value.split())
        for chunk in chunks
        for value in prop.findall(chunk)
    ]


def _type_offenders(prop: re.Pattern[str], allowed) -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for path in (COMPONENTS_CSS, *sorted(TEMPLATES.glob("*.html"))):
        bad = sorted({value for value in _declared(prop, path) if not allowed(value)})
        if bad:
            offenders[path.name] = bad
    return offenders


def test_only_six_font_sizes():
    """AC2：font-size 只认 var(--text-*)；出现任何数字字面量就红。

    六个档位：11 / 12 / 14 / 16 / 20 / 28（tokens.css 的 --text-meta … --text-display）。
    旧名（--text-sm / --text-heading 等）是指向这六档的别名，可以出现；但它们
    解析出来的值仍然是六个之一，所以不会多出第七档。
    """
    assert len(SIX_TEXT_TOKENS) == 6, f"六档名单条数写错了：{len(SIX_TEXT_TOKENS)}"

    offenders = _type_offenders(_FONT_SIZE, _TEXT_SIZE_REF.fullmatch)
    assert set(offenders) == set(PENDING_TYPE_PAGES), (
        "font-size 只许 var(--text-*)，这些文件写了数字字面量："
        f"{offenders}。放行名单固定为 {sorted(PENDING_TYPE_PAGES)}。"
    )


def test_no_font_weight_above_600():
    """AC3：字重只有 400 / 500 / 600，`700` 与 `bold` 都红。"""
    offenders = _type_offenders(
        _FONT_WEIGHT,
        lambda value: value in _ALLOWED_WEIGHTS or _WEIGHT_REF.fullmatch(value),
    )
    assert set(offenders) == set(PENDING_TYPE_PAGES), (
        "字重只许 400 / 500 / 600（或 var(--weight-*)），这些文件超了："
        f"{offenders}。放行名单固定为 {sorted(PENDING_TYPE_PAGES)}。"
    )


# ------------------------- 守卫十二：v3 色板逐字冻结（卡 G2-1 · 线框定稿）
#
# 值逐字抄 docs/homepage-wireframe-v3.html（9哥 2026-11-02 拍板）。和主题令牌一样，
# 改一个 hex 不会报错，只会让界面悄悄偏离定稿线框，所以冻在这里。

V3_PALETTE = {
    "--v3-bg": "#F1EFE8",
    "--v3-surface": "#FFFFFF",
    "--v3-ink": "#2C2C2A",
    "--v3-muted": "#5F5E5A",
    "--v3-hint": "#888780",
    "--v3-border": "#D3D1C7",
    "--v3-primary": "#0F6E56",
    "--v3-primary-soft": "#E1F5EE",
    "--v3-primary-deep": "#085041",
    "--v3-on-primary": "#FFFFFF",
    "--v3-primary-label": "#CDE9DF",
    "--v3-primary-sub": "#9FE1CB",
}

# 红线尺寸：主按钮 48 高；桌面磁贴 240 高、侧栏 250、内容列 720（附录 C.1）。
V3_GEOMETRY = {
    "--v3-btn-h": "48px",
    "--v3-tile-h": "240px",
    "--v3-side-w": "250px",
    "--v3-col-w": "720px",
}


def test_v3_tokens_match_the_wireframe():
    """v3 色板与红线尺寸逐字等于线框定稿（少一个、改一位都红）。"""
    current = _definitions(V3_CSS)
    wrong = {
        name: {"want": want, "got": current.get(name)}
        for name, want in {**V3_PALETTE, **V3_GEOMETRY}.items()
        if current.get(name) != want
    }
    assert wrong == {}, f"v3 令牌和线框定稿不一致：{wrong}"
