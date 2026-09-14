# -*- coding: utf-8 -*-
"""WinUI 主题令牌对比度体检（WCAG 2.1 相对亮度）。

判据（可执行的一句话）：正文与背景对比度须 >= 4.5:1，
次要/大字号信息 >= 3.0:1，不达标的组合必须改色。

深色与浅色两套都要过 —— 深色主题最容易"灰字看不清"，
浅色主题最容易"强调色在白底上糊掉"（#4CC2FF 在白底仅 ~1.9:1）。

⚠️ 下面的色值必须与 winui/GlassPalette.cs 保持一致 ——
   自 2026-09-12 起，**颜色令牌的唯一来源是 GlassPalette.cs**（代码注入，保证实时生效），
   App.xaml 的 ThemeDictionaries 只是 XAML 解析期的占位。改色时两处都要动，然后重跑本脚本。
"""


def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(hexcolor):
    h = hexcolor.lstrip('#')
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(fg, bg):
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# 取自 winui/GlassPalette.cs（改了那边就要改这里并重跑）
DARK = dict(
    base='#101418', deep='#0B0E12', card='#171C22', card_hi='#1C222A',
    divider='#252C35', accent='#4CC2FF', accent_fill='#0A6CA8', accent_soft='#1A3A4D',
    on_accent='#FFFFFF', text_main='#E8EDF2', text_sub='#B8C2CE',
    text_muted='#8B96A5', success='#3DDC97', warn='#FFC24B', danger='#FF6B6B',
    log='#171C22',
)
LIGHT = dict(
    base='#F2F4F7', deep='#E7EBF0', card='#FFFFFF', card_hi='#FFFFFF',
    divider='#C3CBD5', accent='#0D6396', accent_fill='#0D6396', accent_soft='#DCEEF9',
    on_accent='#FFFFFF', text_main='#15202B', text_sub='#47535F',
    text_muted='#5A6674', success='#0F8A5F', warn='#9A6600', danger='#C0392B',
    log='#FFFFFF',
)

# (前景键, 背景键, 用途, 最低要求)
CHECKS = [
    ('text_main', 'base', '正文 / 窗口底', 4.5),
    ('text_main', 'card', '正文 / 卡片', 4.5),
    ('text_sub', 'card', '次要文字 / 卡片', 4.5),
    ('text_sub', 'deep', '次要文字 / 深底', 4.5),
    ('text_muted', 'card', '弱化文字 / 卡片', 3.0),
    ('text_muted', 'base', '弱化文字 / 窗口底', 3.0),
    ('accent', 'base', '强调文字 / 窗口底', 4.5),
    ('accent', 'card', '强调文字 / 卡片', 4.5),
    ('accent', 'accent_soft', '强调文字 / 强调淡底', 3.0),
    ('on_accent', 'accent_fill', '按钮文字 / 主按钮填充', 4.5),
    ('accent_fill', 'card', '主按钮填充 / 卡片（可见性）', 1.5),
    ('text_main', 'card_hi', '正文 / 强调块', 4.5),
    ('text_muted', 'card_hi', '弱化文字 / 强调块', 3.0),
    ('success', 'card', 'KPI 数值(绿)', 3.0),
    ('warn', 'card', 'KPI 数值(黄)', 3.0),
    ('danger', 'card', '危险色 / 卡片', 3.0),
    ('divider', 'card', '分隔线/描边(仅需可见)', 1.2),
    ('text_sub', 'log', '日志文字 / 日志底', 4.5),
]

fails = []
for name, pal in (('DARK', DARK), ('LIGHT', LIGHT)):
    print(f'===== {name} =====')
    print(f'{"前景":9s} {"背景":11s} {"对比度":>7s} {"要求":>5s}  用途')
    for fk, bk, use, need in CHECKS:
        fg, bg = pal[fk], pal[bk]
        r = ratio(fg, bg)
        ok = r >= need
        if not ok:
            fails.append((name, fg, bg, use, r, need))
        print(f'{fg:9s} {bg:11s} {r:7.2f} {need:5.1f}  {"OK " if ok else "FAIL"} {use}')
    print()

if fails:
    print(f'不达标 {len(fails)} 项：')
    for name, fg, bg, use, r, need in fails:
        print(f'  [{name}] {use}: {fg} on {bg} = {r:.2f} < {need}')
    raise SystemExit(1)

# ---------------------------------------------------------------------------
# 防漂移：把上面的色值与 GlassPalette.cs 的实际取值比对。
# 只在一个文件里改色、忘了另一个，会在这里立刻暴露（而不是等到用户说"颜色不对"）。
# ---------------------------------------------------------------------------
import os
import re

palette_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'GlassPalette.cs')
if os.path.exists(palette_path):
    src = open(palette_path, encoding='utf-8').read()
    # 只取 Add(map, "Key", "#RRGGBB", ...) 里的 key→hex
    found = dict(re.findall(r'Add\(map,\s*"(\w+)",\s*"(#[0-9A-Fa-f]{6})"', src))
    mapping = {
        'GlassBaseBrush': 'base', 'GlassDeepBrush': 'deep', 'GlassCardBrush': 'card',
        'GlassCardHiBrush': 'card_hi', 'GlassDividerBrush': 'divider',
        'GlassAccentBrush': 'accent',
        'GlassAccentFillBrush': 'accent_fill',
        'GlassAccentSoftBrush': 'accent_soft',
        'GlassOnAccentBrush': 'on_accent', 'GlassTextMainBrush': 'text_main',
        'GlassTextSubBrush': 'text_sub', 'GlassTextMutedBrush': 'text_muted',
        'GlassSuccessBrush': 'success', 'GlassWarnBrush': 'warn',
        'GlassDangerBrush': 'danger',
    }
    # GlassPalette 里浅色分支在 if(light) 之后、深色在 else 之后
    light_src, _, dark_src = src.partition('else')
    drift = []
    for cs_key, py_key in mapping.items():
        hexes = re.findall(r'Add\(map,\s*"' + cs_key + r'",\s*"(#[0-9A-Fa-f]{6})"', src)
        if len(hexes) != 2:
            continue
        cs_light, cs_dark = hexes[0].upper(), hexes[1].upper()
        if cs_light != LIGHT[py_key].upper():
            drift.append(f'light.{py_key}: py={LIGHT[py_key]} cs={cs_light}')
        if cs_dark != DARK[py_key].upper():
            drift.append(f'dark.{py_key}: py={DARK[py_key]} cs={cs_dark}')
    if drift:
        print()
        print('⚠️ 色值漂移（contrast_check.py 与 GlassPalette.cs 不一致）：')
        for d in drift:
            print('  -', d)
        raise SystemExit(1)
    print(f'与 GlassPalette.cs 一致（比对 {len(mapping)} 个令牌）✓')
else:
    print('（未找到 GlassPalette.cs，跳过一致性比对）')

# ---------------------------------------------------------------------------
# 半透明表面的最坏情况可读性。
#
# 卡片/文本框是半透明的，背后可能是**任意**壁纸。浅色主题的半透明白底一旦压到
# 深色壁纸上，就会被拉暗 → 深色文字随之糊掉，用户看到的现象是"配色反了"。
# 判据：把表面按其最小允许不透明度分别合成到**纯黑**与**纯白**背景上，
# 两种极端下文字对比度都必须 >= 4.5:1。
# ---------------------------------------------------------------------------
def composite(fg_hex, alpha, bg_hex):
    """把 fg_hex 以 alpha 合成到不透明的 bg_hex 上，返回结果色 hex。"""
    def rgb(h):
        h = h.lstrip('#')
        return [int(h[i:i + 2], 16) for i in (0, 2, 4)]

    f, b = rgb(fg_hex), rgb(bg_hex)
    out = [round(f[i] * alpha + b[i] * (1 - alpha)) for i in range(3)]
    return '#%02X%02X%02X' % tuple(out)


# 两个主题的最小允许表面不透明度（与 GlassPalette.cs 的 MinSurfaceOpacity 一致）。
# 0.77 是"次要文字在任意壁纸下仍 >= 4.5:1"的解；实现取 0.80 留余量。
MIN_SURFACE = {'DARK': 0.80, 'LIGHT': 0.80}
# 日志/文本框表面是**完全不透明**的（GlassSurfaceBrush / GlassLogBrush 的 alpha = FF）：
# 那才是真正"读字"的地方，不允许壁纸透上来 —— 用户看到的"文本框配色反了"正出在这里。
# 改这个值必须同步 GlassPalette.cs。
SURFACE_OPAQUE = {'DARK': 1.00, 'LIGHT': 1.00}
# 需要检查的「表面上的文字」：(文字键, 表面键, 不透明度来源, 用途)
SURFACE_TEXT = [
    ('text_main', 'card', 'MIN', '正文 / 半透明卡片'),
    ('text_sub', 'card', 'MIN', '次要文字 / 半透明卡片'),
    ('text_main', 'log', 'OPAQUE', '正文 / 文本框（不透明）'),
    ('text_sub', 'log', 'OPAQUE', '次要文字 / 文本框（不透明）'),
]

surface_fails = []
for name, pal in (('DARK', DARK), ('LIGHT', LIGHT)):
    pal = dict(pal, surface=pal['card'])   # surface 与 card 同色，仅不透明度不同
    print(f'===== {name} 表面可读性（卡片下限 {MIN_SURFACE[name]:.2f} / 文本框不透明）=====')
    for fk, sk, src, use in SURFACE_TEXT:
        alpha = MIN_SURFACE[name] if src == 'MIN' else SURFACE_OPAQUE[name]
        fg = pal[fk]
        for backdrop, label in (('#000000', '深色壁纸'), ('#FFFFFF', '浅色壁纸')):
            eff = composite(pal[sk], alpha, backdrop)
            r = ratio(fg, eff)
            ok = r >= 4.5
            if not ok:
                surface_fails.append((name, use, label, fg, eff, r))
            print(f'  {use:24s} 壁纸={label} 表面合成={eff} 文字={fg} 对比度={r:5.2f} '
                  f'{"OK" if ok else "FAIL"}')
    print()

if surface_fails:
    print('半透明表面最坏情况不达标：')
    for name, use, label, fg, eff, r in surface_fails:
        print(f'  [{name}] {use} @ {label}: {fg} on {eff} = {r:.2f} < 4.5')
        print(f'        ⇒ 需要提高该主题的最小表面不透明度')
    raise SystemExit(1)
print('半透明表面在深/浅壁纸两个极端下均达标 ✓')
print()
print('深浅两套主题全部达标 ✓')
