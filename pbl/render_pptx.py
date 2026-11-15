"""PPT 渲染器：PPT 大纲（SlideOutline 列表）-> pptx。

固定版式 + 主题配色，内容来自可编辑的大纲（大纲层与样式层解耦，改样式不重生成内容）。
"""
from __future__ import annotations

from dataclasses import dataclass

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from .schema import Case, SlideOutline

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)


@dataclass
class Theme:
    name: str
    bg: RGBColor
    accent: RGBColor
    text: RGBColor
    sub: RGBColor


THEMES: dict[str, Theme] = {
    "医学蓝": Theme("医学蓝", RGBColor(0xF7, 0xFA, 0xFD), RGBColor(0x1F, 0x4E, 0x79),
                 RGBColor(0x22, 0x2A, 0x33), RGBColor(0x5A, 0x6B, 0x7B)),
    "墨绿": Theme("墨绿", RGBColor(0xF6, 0xFA, 0xF7), RGBColor(0x1E, 0x5E, 0x46),
               RGBColor(0x21, 0x2A, 0x25), RGBColor(0x59, 0x6B, 0x60)),
    "暖橙": Theme("暖橙", RGBColor(0xFF, 0xFB, 0xF6), RGBColor(0xC2, 0x5B, 0x1E),
               RGBColor(0x30, 0x2A, 0x24), RGBColor(0x6E, 0x64, 0x5A)),
    "石墨": Theme("石墨", RGBColor(0xF5, 0xF5, 0xF6), RGBColor(0x37, 0x3F, 0x48),
               RGBColor(0x21, 0x24, 0x28), RGBColor(0x60, 0x66, 0x6C)),
}

FONT = "微软雅黑"


def _set_run(run, text, size, bold=False, color=None, font=FONT):
    run.text = text
    f = run.font
    f.name = font
    f.size = Pt(size)
    f.bold = bold
    if color is not None:
        f.color.rgb = color
    rPr = run._r.get_or_add_rPr()
    latin = rPr.find(qn("a:latin"))
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        if latin is not None:
            latin.addnext(ea)
        else:
            rPr.append(ea)
    ea.set("typeface", font)


def _box(slide, l, t, w, h):
    tb = slide.shapes.add_textbox(l, t, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    return tf


def _fill_bg(slide, theme: Theme):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = theme.bg


def _accent_bar(slide, theme: Theme, l=Inches(0.55), t=Inches(0.5), w=Inches(0.14), h=Inches(0.52)):
    from pptx.enum.shapes import MSO_SHAPE
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, l, t, w, h)
    sh.fill.solid()
    sh.fill.fore_color.rgb = theme.accent
    sh.line.fill.background()
    return sh


def _title(slide, text, theme: Theme, size=30, t=Inches(0.42), l=Inches(0.85)):
    tf = _box(slide, l, t, SLIDE_W - l - Inches(0.6), Inches(0.75))
    p = tf.paragraphs[0]
    _set_run(p.add_run(), text, size, bold=True, color=theme.accent)


def _bullets(slide, items, theme: Theme, l=Inches(0.9), t=Inches(1.5),
             w=None, h=Inches(5.4), size=17, gap_after=8):
    tf = _box(slide, l, t, w or (SLIDE_W - l - Inches(0.7)), h)
    first = True
    for it in items:
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.space_after = Pt(gap_after)
        p.line_spacing = 1.15
        _set_run(p.add_run(), "· " + it, size, color=theme.text)
    return tf


def _body(slide, text, theme: Theme, l=Inches(0.9), t=Inches(1.5), size=16):
    tf = _box(slide, l, t, SLIDE_W - l - Inches(0.7), Inches(5.3))
    first = True
    for seg in (text or "").split("\n"):
        seg = seg.strip()
        if not seg:
            continue
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.line_spacing = 1.25
        p.space_after = Pt(6)
        _set_run(p.add_run(), seg, size, color=theme.text)


def _page_no(slide, i, theme: Theme):
    tf = _box(slide, SLIDE_W - Inches(0.9), SLIDE_H - Inches(0.5), Inches(0.6), Inches(0.3))
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.RIGHT
    _set_run(p.add_run(), str(i), 11, color=theme.sub)


def _render_slide(slide, s: SlideOutline, theme: Theme, case: Case, page: int):
    _fill_bg(slide, theme)
    layout = s.layout

    if layout == "cover":
        band = slide.shapes.add_shape(1, 0, Inches(2.5), SLIDE_W, Inches(0.09))  # 1=rect
        band.fill.solid()
        band.fill.fore_color.rgb = theme.accent
        band.line.fill.background()
        tf = _box(slide, Inches(0.9), Inches(2.9), SLIDE_W - Inches(1.8), Inches(1.6))
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _set_run(p.add_run(), s.title or case.title, 40, bold=True, color=theme.accent)
        sub = s.subtitle or f"{case.course} · PBL"
        tf2 = _box(slide, Inches(0.9), Inches(4.4), SLIDE_W - Inches(1.8), Inches(0.6))
        p2 = tf2.paragraphs[0]
        p2.alignment = PP_ALIGN.CENTER
        _set_run(p2.add_run(), sub, 17, color=theme.sub)
        return

    _accent_bar(slide, theme)
    _title(slide, s.title or "未命名页", theme)
    if s.subtitle:
        tf = _box(slide, Inches(0.88), Inches(1.12), SLIDE_W - Inches(1.6), Inches(0.4))
        _set_run(tf.paragraphs[0].add_run(), s.subtitle, 13, color=theme.sub)

    if layout == "case" and s.body:
        _body(slide, s.body, theme, size=15)
    elif s.bullets:
        _bullets(slide, s.bullets, theme)
    elif s.body:
        _body(slide, s.body, theme)
    else:
        tf = _box(slide, Inches(0.9), Inches(3.2), SLIDE_W - Inches(1.8), Inches(0.6))
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.CENTER
        _set_run(p.add_run(), "（本页内容待补充）", 15, color=theme.sub)
    _page_no(slide, page, theme)


def build_pptx(case: Case, outline: list[SlideOutline], theme_name: str, out_path: str) -> str:
    theme = THEMES.get(theme_name, THEMES["医学蓝"])
    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H
    blank = prs.slide_layouts[6]
    for i, s in enumerate(outline, 1):
        slide = prs.slides.add_slide(blank)
        _render_slide(slide, s, theme, case, i)
    prs.save(out_path)
    return out_path


# ================================================================ PBL 作业 PPT（复用模板版式）
# 版式规律取自「问题.pptx」样例（与天医大模板同源的版式名）：
#   问题页      内容页-标题&副标题：标题=问题；中部学习内容（文字/表格）；底部参考文献小字
#   文献文字页  内容页-标题&副标题：标题=文献分享：中文标题；正文=背景/思路/结论/病例联系；底部引文
#   文献图片页  内容页-上短下长  ：小标签 + 2~4 张关键图 + 每图中文说明 + 底部引文
import math
from pathlib import Path as _Path

PPT_DIR = _Path(__file__).resolve().parent / "templates" / "ppt"
_LAYOUT_CONTENT = ("内容页-标题&副标题", "内容页-标题", "内容页-上下横线", "全空白")
_LAYOUT_PIC = ("内容页-上短下长", "内容页-标题", "全空白")
_CN_FONT = "微软雅黑"
REF_GRAY = RGBColor(0x66, 0x66, 0x66)


def list_ppt_templates() -> list[str]:
    PPT_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in PPT_DIR.iterdir()
                  if p.suffix.lower() == ".pptx" and not p.name.startswith("~$"))


def default_ppt_template() -> str:
    for name in list_ppt_templates():
        if "天医" in name and "参考样例" not in name:
            return name
    names = [n for n in list_ppt_templates() if "参考样例" not in n]
    return names[0] if names else ""


def template_path(name: str) -> str:
    return str(PPT_DIR / (name or default_ppt_template()))


def _strip_slides(prs: Presentation) -> None:
    """清空模板自带示例页，仅保留母版/版式（设计元素不变）。"""
    sld_id_lst = prs.slides._sldIdLst
    for sld in list(sld_id_lst):
        r_id = sld.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        try:
            prs.part.drop_rel(r_id)
        except Exception:
            pass
        sld_id_lst.remove(sld)


def _load_deck(template_name: str) -> Presentation:
    prs = Presentation(template_path(template_name))
    _strip_slides(prs)
    return prs


def _layout(prs: Presentation, names: tuple):
    for n in names:
        for layout in prs.slide_layouts:
            if layout.name == n:
                return layout
    return prs.slide_layouts[min(6, len(prs.slide_layouts) - 1)]


def _add_slide(prs: Presentation, names: tuple, title: str | None = None):
    """按版式名新建页；标题填入标题占位符；清掉多余占位符（保留页码）。"""
    slide = prs.slides.add_slide(_layout(prs, names))
    for ph in list(slide.placeholders):
        if ph.placeholder_format.idx == 0:
            if title is not None:
                ph.text_frame.text = title
                for p in ph.text_frame.paragraphs:
                    for r in p.runs:
                        r.font.name = _CN_FONT
        elif ph.placeholder_format.idx != 4:
            ph._element.getparent().remove(ph._element)
    return slide


def _box(slide, l, t, w, h):
    tb = slide.shapes.add_textbox(Inches(l), Inches(t), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    return tf


def _put(tf, text, size=12, bold=False, color=None, first=True, space=4):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.space_after = Pt(space)
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.name = _CN_FONT
    if color is not None:
        r.font.color.rgb = color
    return p


def _est_lines(text: str, per_line: int = 48) -> int:
    n = 0
    for seg in (text or "").split("\n"):
        seg = seg.strip()
        if seg:
            n += max(1, math.ceil(len(seg) / per_line))
    return n


_FS_BODY_PREF = 16       # 正文首选字号
_FS_BODY_MIN = 14        # 正文下限（用户要求：14 号也可接受，不得更小）
_FS_NOTE = 14            # 图片说明
_FS_LABEL = 16           # 图片页小标签
_FS_REF = 8              # 参考文献（允许小字号）
_BUDGET = 5.30          # 问题页内容区可用高度（1.02 → ~6.32in，参考文献从 6.72in 起）


def _item_height(it: dict, f: float = 1.0) -> float:
    """渲染单元的英寸估高（f = 正文字号/16，宁大勿小）。"""
    if it["kind"] == "table":
        return min(len(it["rows"]), 8) * 0.42 * f + 0.12
    if it["kind"] == "heading":
        return 0.34
    return _est_lines(it["text"]) * 0.30 * f + 0.03


def _add_table(slide, rows: list, t: float, l: float = 0.7, w: float = 12.0,
               size: int = 14):
    n_rows, n_cols = len(rows), max(len(r) for r in rows)
    row_h = 0.42 * (size / 16)
    shape = slide.shapes.add_table(n_rows, n_cols, Inches(l), Inches(t),
                                   Inches(w), Inches(row_h * n_rows))
    tbl = shape.table
    for i, row in enumerate(rows):
        for j in range(n_cols):
            cell = tbl.cell(i, j)
            cell.text = row[j] if j < len(row) else ""
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(size)
                    r.font.bold = i == 0
                    r.font.name = _CN_FONT
    return row_h * n_rows


def _question_items(q) -> list:
    """把学习内容拆成逐条渲染单元：heading / line / table。"""
    items = []
    for bi, blk in enumerate(q.learning, 1):
        head = f"{bi}. {blk.heading}" if blk.heading else f"{bi}."
        items.append({"kind": "heading", "text": head})
        for seg in (blk.body or "").split("\n"):
            if seg.strip():
                items.append({"kind": "line", "text": seg.strip()})
        if blk.table:
            items.append({"kind": "table", "rows": blk.table})
    return items


def _render_items(slide, items, y: float = 1.02, fs_body: int = _FS_BODY_PREF) -> None:
    """按顺序渲染渲染单元；表格独占段落、文本在表前/表后各用一个文本框，定位确定。

    fs_body：正文字号（16 优先，最低 14）；标题自动 +2。
    """
    segs = []
    cur = {"texts": []}

    def flush():
        if cur["texts"]:
            segs.append(dict(cur))
        cur["texts"] = []

    for it in items:
        if it["kind"] == "table":
            flush()
            segs.append({"table": it["rows"]})
        else:
            cur["texts"].append(it)
    flush()

    fs_b = fs_body
    fs_h = fs_body + 2
    f = fs_body / 16.0
    for seg in segs:
        if "table" in seg:
            th = _add_table(slide, seg["table"], y, size=fs_body)
            y += th + 0.08
            continue
        est = 0.0
        for it in seg["texts"]:
            est += _item_height(it, f)
        tf = _box(slide, 0.6, y, 12.1, max(est, 0.4))
        first = True
        for it in seg["texts"]:
            if it["kind"] == "heading":
                _put(tf, it["text"], size=fs_h, bold=True, first=first, space=2)
            else:
                _put(tf, it["text"], size=fs_b, first=first, space=2)
            first = False
        y += est + 0.06


def build_question_pptx(case, template: str, out_path: str, author: str = "",
                        content_map: dict | None = None) -> str:
    """问题 PPT：标题=问题（右上署名）；中部学习内容；底部参考文献。

    content_map：{qid: [LearningBlock]} 可选覆盖（精简模式产出的幻灯片版内容）。
    内容超两页容量时自动 0.87 缩放（10pt）；页数按实际内容展开，绝不挤压重叠。
    """
    from . import pubmed as _pm
    from . import pipeline as _pipeline
    _pipeline.sync_source_refs(case)          # 出处对账：学习内容的教材出处进参考文献
    prs = _load_deck(template)
    for qi, q in enumerate(case.questions, 1):
        src_q = q
        if content_map and q.qid in content_map:
            src_q = q.model_copy(deep=True)
            src_q.learning = content_map[q.qid]
        items = _question_items(src_q)
        # 字号：16 优先；超过两页容量时降到 14（下限），不缩到 14 以下
        fs_body = _FS_BODY_PREF
        if sum(_item_height(it) for it in items) > 2 * _BUDGET:
            fs_body = _FS_BODY_MIN
        f = fs_body / 16.0
        pages, cur, used = [], [], 0.0
        for it in items:
            h = _item_height(it, f)
            if used + h > _BUDGET and cur:
                pages.append(cur)
                cur, used = [], 0.0
            cur.append(it)
            used += h
        if cur:
            pages.append(cur)
        # 若某页最后一项恰好是标题，挪到下一页，避免标题孤悬
        for pi in range(len(pages) - 1):
            if pages[pi] and pages[pi][-1]["kind"] == "heading":
                pages[pi + 1].insert(0, pages[pi].pop())

        for pi, page_items in enumerate(pages):
            title = f"问题{qi}：{q.text}"
            if len(pages) > 1:
                title += f"（{pi + 1}/{len(pages)}）"
            if author and pi == 0:
                title += f"　　{author}"
            slide = _add_slide(prs, _LAYOUT_CONTENT, title)
            _render_items(slide, page_items, fs_body=fs_body)
            if q.references:
                refs = "  ".join(f"[{i}] {_pm.format_reference(r)}"
                                 for i, r in enumerate(q.references, 1))
                tfb = _box(slide, 0.5, 6.60, 12.3, 0.85)
                if len(refs) <= 175:
                    _put(tfb, refs, size=_FS_REF, color=REF_GRAY)
                else:                          # 文献多时分行排
                    import re as _re
                    parts = _re.findall(r"\[\d+\][^\[]+", refs)
                    lines, cur = [], ""
                    for ptext in parts:
                        if len(cur) + len(ptext) > 175 and cur:
                            lines.append(cur.strip())
                            cur = ""
                        cur += "  " + ptext
                    if cur.strip():
                        lines.append(cur.strip())
                    first = True
                    for ln in lines[:3]:
                        _put(tfb, ln, size=7.5, color=REF_GRAY, first=first, space=0)
                        first = False
    prs.save(out_path)
    return out_path


def _fit_image(path: str, box_w: float, box_h: float) -> tuple:
    """按比例缩放到框内，返回 (w, h) 英寸。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        ratio = min(box_w / w, box_h / h)
        return w * ratio, h * ratio
    except Exception:
        return box_w, box_h


def build_literature_pptx(paper, template: str, out_path: str) -> str:
    """【旧版·界面已不再调用】两页式文献分享：第1页文字（背景/思路/结论/病例联系+底部引文），
    第2页关键图 2~4 张+说明。

    已被 `build_literature_onepage`（一页速览）取代；保留是因为文字很多、一页确实放不下时
    仍可用它把内容摊到两页。
    """
    prs = _load_deck(template)
    slide = _add_slide(prs, _LAYOUT_CONTENT, f"文献分享：{paper.title_cn}")
    tf = _box(slide, 0.55, 1.02, 12.2, 5.6)
    # 正文不小于 16 号；文字过长时由生成端的 5 分钟篇幅限制保证不溢出
    fs_h, fs_b = 18, 16
    first = True
    if paper.background:
        _put(tf, "一、研究背景", size=fs_h, bold=True, first=first, space=2)
        first = False
        _put(tf, paper.background, size=fs_b, first=False, space=8)
    _put(tf, "二、研究思路与过程", size=fs_h, bold=True, first=first, space=2)
    first = False
    _put(tf, paper.approach, size=fs_b, first=False, space=8)
    if paper.conclusion:
        _put(tf, "三、研究结论", size=fs_h, bold=True, first=False, space=2)
        _put(tf, paper.conclusion, size=fs_b, first=False, space=8)
    if paper.case_link:
        _put(tf, "四、与病例的联系", size=fs_h, bold=True, first=False, space=2)
        _put(tf, paper.case_link, size=fs_b, first=False, space=8)
    tfb = _box(slide, 0.5, 6.70, 12.3, 0.65)
    _put(tfb, paper.ref_text, size=_FS_REF, color=REF_GRAY)

    figs = [f for f in paper.figures if f.file and _Path(f.file).exists()][:4]
    if figs:
        slide2 = _add_slide(prs, _LAYOUT_PIC, None)
        tfl = _box(slide2, 0.5, 0.8, 4.0, 0.4)
        _put(tfl, paper.section_label or "机制探索", size=_FS_LABEL, bold=True)
        n = len(figs)
        cols = 2 if n >= 2 else 1
        rows = 1 if n <= 2 else 2
        left0, top0, grid_w, grid_h = 0.7, 1.30, 12.0, 5.30
        cell_w, cell_h = grid_w / cols, grid_h / rows
        for k, f in enumerate(figs):
            r_i, c_i = divmod(k, cols)
            if n == 3 and k == 2:              # 三张时第三张居中
                c_i = 0.5
            x = left0 + c_i * cell_w
            y = top0 + r_i * cell_h
            note_h = min(1.15, cell_h * 0.38)
            img_w, img_h = _fit_image(f.file, cell_w - 0.3, cell_h - note_h - 0.10)
            slide2.shapes.add_picture(f.file, Inches(x + (cell_w - 0.3 - img_w) / 2),
                                      Inches(y), Inches(img_w), Inches(img_h))
            tfn = _box(slide2, x + 0.1, y + img_h + 0.03, cell_w - 0.2, note_h)
            _put(tfn, f"{f.label}：{f.note}", size=_FS_NOTE, space=0)
        tfb2 = _box(slide2, 0.5, 6.58, 12.3, 0.5)
        _put(tfb2, paper.ref_text[:180], size=_FS_REF, color=REF_GRAY)
    prs.save(out_path)
    return out_path


# ================================================================ 文献分享（一页速览）
# 版式参照《文献分享_一页速览_v2.pptx》：
#   顶部 2~4 张关键图（每图下方一行粗体图注）
#   左下 核心结论框 ｜ 右下 与病例的联系框
#   底部 A→B→C→D 研究过程流程条（假设→方法→验证→因果）
#   最底 参考文献小字；备注页写讲解思路
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
_LIT_ACCENT = RGBColor(0x1F, 0x4E, 0x79)
_LIT_CONC_BG = RGBColor(0xE8, 0xF0, 0xFA)
_LIT_LINK_BG = RGBColor(0xE9, 0xF5, 0xEC)
_LIT_LINK_LINE = RGBColor(0x2E, 0x7D, 0x46)

# 图片区（x, y, w, h, 图注高）——h 含图注；均以英寸计
_LIT_CELLS = {
    1: [(2.50, 0.70, 8.30, 4.40, 0.54)],
    2: [(0.70, 0.66, 5.80, 4.50, 0.66), (6.80, 0.66, 5.80, 4.50, 0.66)],
    3: [(0.40, 0.60, 5.70, 2.22, 0.62), (0.40, 3.02, 5.70, 2.24, 0.42),
        (6.40, 0.60, 6.40, 4.66, 0.44)],
    4: [(0.30, 0.60, 3.90, 2.26, 0.64), (4.36, 0.60, 3.70, 2.26, 0.64),
        (8.16, 0.60, 4.66, 4.62, 0.44), (0.30, 3.04, 7.76, 2.22, 0.42)],
}

# 图片区可用范围（结论框上沿 y=5.46 之前留 0.2 空隙）
_LIT_AREA = (0.30, 0.60, 13.03, 5.26)


def _lit_grid(cols: int, rows: int, cap: float = 0.54,
              gapx: float = 0.24, gapy: float = 0.16) -> list:
    """按行列均分的候选版式。"""
    x0, y0, x1, y1 = _LIT_AREA
    cw = (x1 - x0 - gapx * (cols - 1)) / cols
    ch = (y1 - y0 - gapy * (rows - 1)) / rows
    cells = []
    for r in range(rows):
        for c in range(cols):
            cells.append((x0 + c * (cw + gapx), y0 + r * (ch + gapy), cw, ch, cap))
    return cells


def _lit_candidates(n: int) -> list[list]:
    """同一图片数量下的若干候选版式；最后按「图实际能占多大面积」择优。"""
    ref = _LIT_CELLS.get(n)
    cands: list[list] = []
    if ref:
        cands.append(ref)
    if n == 1:
        cands.append(_lit_grid(1, 1))
        cands.append(_lit_grid(2, 1))          # 偏左一张，右侧留白给文字感
    elif n == 2:
        cands.append(_lit_grid(2, 1))
        cands.append(_lit_grid(1, 2))
    elif n == 3:
        cands.append(_lit_grid(3, 1))
        cands.append(_lit_grid(1, 3))
        cands.append(_lit_grid(2, 2)[:3])
    elif n == 4:
        cands.append(_lit_grid(2, 2))
        cands.append(_lit_grid(4, 1))
        cands.append(_lit_grid(1, 4))
    return cands


def _pick_layout(figs: list) -> list:
    """在候选版式中选「四张图合计显示面积最大」的那个，避免宽/高图旁边大片留白。"""
    n = len(figs)
    best, best_area = None, -1.0
    for cells in _lit_candidates(n):
        if len(cells) < n:
            continue
        area = 0.0
        for f, (_x, _y, w, h, cap) in zip(figs, cells):
            iw, ih = _fit_image(f.file, w - 0.08, max(0.3, h - cap - 0.06))
            area += iw * ih
        if area > best_area:
            best, best_area = cells, area
    return best or _LIT_CELLS.get(n) or _lit_grid(1, 1)


def _lit_shape(slide, kind, x, y, w, h, fill=None, line=None, radius=0.08):
    sh = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid()
        sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
        sh.line.width = Pt(0.75)
    if radius is not None:
        try:
            sh.adjustments[0] = radius
        except Exception:
            pass
    try:
        sh.shadow.inherit = False
    except Exception:
        pass
    return sh


def _lit_shape_text(sh, text, size, bold=True, color=None,
                    align=None, anchor=None):
    from pptx.enum.shapes import MSO_SHAPE  # noqa: F401  （仅确保枚举已加载）
    tf = sh.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.07)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    if anchor is not None:
        tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.name = _CN_FONT
    if color is not None:
        r.font.color.rgb = color


def _aspect(path: str) -> float:
    """图片宽高比（失败时按 1.0 处理）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size[0] / max(1, im.size[1])
    except Exception:
        return 1.0


def _lit_figure(slide, path, label, note, x, y, w, h, cap_h, cap_size=13):
    """一张关键图 + 其下方粗体图注（图片在格内垂直居中，避免宽图顶部留白）。"""
    note = (note or "").strip()
    if len(note) > 42:
        note = note[:41] + "…"
    cap = f"{label}：{note}" if (label and note) else (note or label or "")
    img_h_max = max(0.3, h - cap_h - 0.06)
    iw, ih = _fit_image(path, w - 0.08, img_h_max)
    oy = y + max(0.0, (img_h_max - ih) / 2)
    slide.shapes.add_picture(path, Inches(x + (w - iw) / 2), Inches(oy),
                             Inches(iw), Inches(ih))
    tf = _box(slide, x, oy + ih + 0.04, w, cap_h)
    _put(tf, cap, size=cap_size, bold=True, space=0)


def build_literature_onepage(paper, template: str, out_path: str,
                             author: str = "") -> str:
    """文献分享「一页速览」：关键图 + 核心结论 + 与病例的联系 + A→D 研究过程 + 出处。

    与旧的两页版（文字页 + 图片页）不同，这里把全部内容压在一页——
    背景默认省略（仅当提示词明确要求时 paper.background 才有值）。
    """
    from pptx.enum.shapes import MSO_SHAPE

    prs = _load_deck(template)
    # 标题写进模板的标题占位符（沿用其位置与字体环境），过长自动降字号
    title = paper.title_cn or paper.title_en or "文献分享"
    tsize = 21 if len(title) <= 28 else (19 if len(title) <= 36 else 17)
    slide = _add_slide(prs, _LAYOUT_CONTENT, title)
    for ph in slide.placeholders:
        if ph.placeholder_format.idx != 0:
            continue
        try:                        # 显式给全四个值：只改部分会把其余补写成 0
            ph.left, ph.top = Inches(0.40), Inches(0.10)
            ph.width, ph.height = Inches(8.05), Inches(0.62)
        except Exception:
            pass
        for para in ph.text_frame.paragraphs:
            for run in para.runs:
                run.font.size = Pt(tsize)
                run.font.bold = True
                run.font.name = _CN_FONT
                run.font.color.rgb = _LIT_ACCENT

    # ---- 汇报人（留空自填，与 Word 里的姓名/学号一致；右缘 11.0 之前避开校徽）
    atf = _box(slide, 8.00, 0.18, 2.70, 0.38)
    ap = atf.paragraphs[0]
    ap.alignment = PP_ALIGN.RIGHT
    ar = ap.add_run()
    ar.text = f"汇报人：{author or '＿＿＿＿'}"
    ar.font.size = Pt(15)
    ar.font.bold = True
    ar.font.name = _CN_FONT
    ar.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    # ---- 研究背景（有则显示：标题装饰红条下方一条横排文字，图区整体下移让位）
    bg = (getattr(paper, "background", "") or "").strip()
    if bg:
        if len(bg) > 66:
            bg = bg[:65] + "…"
        btf = _box(slide, 0.40, 1.00, 12.53, 0.34)
        bp = btf.paragraphs[0]
        br = bp.add_run()
        br.text = f"研究背景：{bg}"
        br.font.size = Pt(12)
        br.font.bold = False
        br.font.name = _CN_FONT
        br.font.color.rgb = RGBColor(0x3A, 0x44, 0x50)
        _bg_shift = 0.78          # 图区下移量（避开标题红条装饰）
    else:
        _bg_shift = 0.0

    # ---- 关键图区
    figs = [f for f in (paper.figures or [])
            if getattr(f, "selected", True) and f.file and _Path(f.file).exists()]
    # 分图里常有 40:1 这种细长条（单条泳道），排上去就是一条缝，先剔掉
    if len(figs) > 2:
        good = [f for f in figs if 0.16 <= _aspect(f.file) <= 6.0]
        if len(good) >= 2:
            figs = good
    figs = figs[:4]
    if figs:
        cells = _pick_layout(figs)
        for f, cell in zip(figs, cells):
            x, y, w, h, cap = cell
            if _bg_shift:                      # 有背景条：格子下移并压高，底沿不越过 5.26
                y = y + _bg_shift
                h = min(max(0.90, h - _bg_shift), max(0.90, 5.26 - y))
            _lit_figure(slide, f.file, f.label, f.note, x, y, w, h, cap)

    # ---- 左下：核心结论
    conc = (paper.conclusion or "").strip()
    if conc:
        box = _lit_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 0.30, 5.46, 7.72, 0.84,
                         fill=_LIT_CONC_BG, line=_LIT_ACCENT, radius=0.10)
        _lit_shape_text(box, f"核心结论：{conc}", 14 if len(conc) <= 62 else 13,
                        bold=True, color=RGBColor(0x1A, 0x36, 0x55),
                        align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE)

    # ---- 右下：与病例的联系（替代参考版式的「局限性」位）
    link = (paper.case_link or "").strip()
    if link:
        box = _lit_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 8.16, 5.46, 4.66, 0.84,
                         fill=_LIT_LINK_BG, line=_LIT_LINK_LINE, radius=0.10)
        _lit_shape_text(box, f"与病例的联系：{link}", 12 if len(link) <= 52 else 11,
                        bold=True, color=RGBColor(0x1E, 0x4D, 0x2E),
                        align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.MIDDLE)

    # ---- 底部：A→B→C→D 研究过程流程条
    steps = [s for s in (paper.steps or []) if str(s.get("text", "")).strip()][:5]
    if steps:
        n = len(steps)
        gap = 0.28
        total_w = 12.73
        bw = (total_w - gap * (n - 1)) / n
        y, bh = 6.40, 0.56
        for i, s in enumerate(steps):
            x = 0.30 + i * (bw + gap)
            txt = str(s.get("text", "")).strip()
            tag = str(s.get("tag", "")).strip() or "ABCD"[i % 4]
            label = txt if txt[:2].rstrip(".．、:：").upper() == tag.upper() else f"{tag}. {txt}"
            size = 13 if len(label) <= 20 else (12 if len(label) <= 30 else 11)
            box = _lit_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, bw, bh,
                             fill=_LIT_ACCENT, line=None, radius=0.14)
            _lit_shape_text(box, label, size, bold=True, color=WHITE,
                            align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
            if i < n - 1:
                ar = _lit_shape(slide, MSO_SHAPE.RIGHT_ARROW,
                                x + bw + 0.015, y + 0.16, gap - 0.03, 0.24,
                                fill=RGBColor(0x99, 0xA6, 0xB2), line=None, radius=None)
    elif (paper.approach or "").strip():
        box = _lit_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 0.30, 6.40, 12.73, 0.56,
                         fill=RGBColor(0xF2, 0xF5, 0xF9), line=RGBColor(0xC9, 0xD4, 0xE0))
        _lit_shape_text(box, paper.approach.strip(), 12, bold=False,
                        color=RGBColor(0x2A, 0x33, 0x3D), align=PP_ALIGN.LEFT,
                        anchor=MSO_ANCHOR.MIDDLE)

    # ---- 最底：参考文献（x 从 1.30 起，避开左下角「求真至善」校训装饰字）
    if paper.ref_text:
        rtf = _box(slide, 1.60, 7.00, 11.10, 0.40)
        _put(rtf, paper.ref_text[:200], size=9, color=REF_GRAY, space=0)

    # ---- 备注：讲解思路（照着念即可，与参考版式的备注同构）
    try:
        chain = " → ".join(f"{s.get('tag', '')}）{str(s.get('text', '')).strip()}"
                           for s in (paper.steps or []) if s.get("text"))
        note = []
        if bg:
            note.append("开场交代研究背景：" + (getattr(paper, "background", "") or "").strip())
        note.append("讲解思路：" + (chain or (paper.approach or "")))
        if paper.conclusion:
            note.append("收尾点出核心结论：" + paper.conclusion)
        if paper.case_link:
            note.append("再联系本病例：" + paper.case_link)
        if paper.limitations:
            note.append("局限性：" + paper.limitations)
        slide.notes_slide.notes_text_frame.text = "\n".join(note)
    except Exception:
        pass

    prs.save(out_path)
    return out_path


# ================================================================ 海报式问题 PPT（单页/每题一页）
# 版式基准：用户的「问题6：靶向生物制剂」海报——机制流程链 + 密集对比表 + 核心要点 + 参考文献脚注。
# 列数/行数/字号按内容自动适配，不逐像素照搬。
_POSTER_HDR = _LIT_ACCENT                 # 表头填充（深蓝）
_POSTER_HDR_TXT = WHITE
_POSTER_ROW_ALT = RGBColor(0xF2, 0xF6, 0xFB)
_POSTER_TXT = RGBColor(0x22, 0x2A, 0x33)


def _poster_cell(cell, size, bold=False, color=_POSTER_TXT, fill=None):
    if fill is not None:
        cell.fill.solid()
        cell.fill.fore_color.rgb = fill
    cell.margin_left = cell.margin_right = Inches(0.05)
    cell.margin_top = cell.margin_bottom = Inches(0.02)
    cell.vertical_anchor = MSO_ANCHOR.MIDDLE
    for p in cell.text_frame.paragraphs:
        p.line_spacing = 1.0
        for r in p.runs:
            r.font.size = Pt(size)
            r.font.bold = bold
            r.font.name = _CN_FONT
            r.font.color.rgb = color


def _poster_table(slide, headers: list[str], rows: list[list[str]],
                  t: float, max_h: float = 3.72):
    """密集对比表：表头强调色、首列加粗并纵向合并同值、隔行浅色、字号随内容自适应。"""
    n_cols = len(headers)
    n_rows = len(rows) + 1
    max_cell = max([len(h) for h in headers]
                   + [len(c) for r in rows for c in r] or [0])
    body_size = 10 if max_cell <= 24 else (9.5 if max_cell <= 34 else 8.5)
    row_h = 0.36 if body_size >= 9.5 else 0.32
    total_h = row_h * n_rows
    while total_h > max_h and row_h > 0.26:          # 超高则压行高
        row_h -= 0.02
        total_h = row_h * n_rows
    shape = slide.shapes.add_table(n_rows, n_cols, Inches(0.47), Inches(t),
                                   Inches(12.40), Inches(total_h))
    tbl = shape.table
    weights = [max(1.2, min(len(h), 8)) for h in headers]
    sw = sum(weights)
    for j, wgt in enumerate(weights):
        tbl.columns[j].width = Inches(12.40 * wgt / sw)
    for j, h in enumerate(headers):
        cell = tbl.cell(0, j)
        cell.text = h
        _poster_cell(cell, 11, bold=True, color=_POSTER_HDR_TXT, fill=_POSTER_HDR)
    for i, r in enumerate(rows, 1):
        fill = _POSTER_ROW_ALT if i % 2 == 0 else WHITE
        for j in range(n_cols):
            cell = tbl.cell(i, j)
            cell.text = r[j] if j < len(r) else ""
            _poster_cell(cell, body_size, bold=(j == 0), fill=fill)
    try:                        # 首列同值纵向合并（机制层分组列）
        i = 1
        while i < n_rows:
            j = i
            val = tbl.cell(i, 0).text.strip()
            while val and j + 1 < n_rows and tbl.cell(j + 1, 0).text.strip() == val:
                tbl.cell(j + 1, 0).text = ""     # 先清空被并格，避免合并后文字堆叠
                j += 1
            if j > i:
                tbl.cell(i, 0).merge(tbl.cell(j, 0))
            i = j + 1
    except Exception:
        pass
    return t + total_h


def build_poster_pptx(items: list[dict], author: str,
                      template_name: str, out_path: str) -> str:
    """海报式问题 PPT：items 每项一页。

    item = {"title": "问题N：题干", "spec": pipeline.generate_question_poster 的返回,
            "refs_text": 参考文献整段小字}
    """
    from pptx.enum.shapes import MSO_SHAPE

    prs = _load_deck(template_name)
    for item in items:
        spec = item.get("spec") or {}
        title = (item.get("title") or "").strip() or "问题海报"
        tsize = 20 if len(title) <= 30 else (18 if len(title) <= 40 else 16)
        slide = _add_slide(prs, _LAYOUT_CONTENT, title)
        for ph in list(slide.placeholders):      # 海报页不需要页码/汇报人占位（模板幻影文字）
            if ph.placeholder_format.idx == 4:
                ph._element.getparent().remove(ph._element)
        for ph in slide.placeholders:
            if ph.placeholder_format.idx != 0:
                continue
            try:
                ph.left, ph.top = Inches(0.40), Inches(0.10)
                ph.width, ph.height = Inches(7.90), Inches(0.62)   # 右侧让位给汇报人与校徽
            except Exception:
                pass
            for para in ph.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(tsize)
                    run.font.bold = True
                    run.font.name = _CN_FONT
                    run.font.color.rgb = _LIT_ACCENT

        # 汇报人（右上，右缘 10.45in 之前，避开模板校徽 logo）
        atf = _box(slide, 7.55, 0.20, 2.90, 0.38)
        ap = atf.paragraphs[0]
        ap.alignment = PP_ALIGN.RIGHT
        ar = ap.add_run()
        ar.text = f"汇报人：{author or '＿＿＿＿'}"
        ar.font.size = Pt(13)
        ar.font.bold = True
        ar.font.name = _CN_FONT
        ar.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

        # ---- 机制流程链（圆角矩形 + 箭头，工艺同文献一页速览的 A→D 条）
        steps = [str(s).strip() for s in (spec.get("flow") or []) if str(s).strip()][:5]
        if steps:
            n = len(steps)
            gap = 0.30
            total_w = 12.53
            bw = (total_w - gap * (n - 1)) / n
            y, bh = 0.94, 0.52
            marks = "①②③④⑤"
            for i, txt in enumerate(steps):
                x = 0.40 + i * (bw + gap)
                label = f"{marks[i]} {txt}"
                size = 12.5 if len(label) <= 18 else (11.5 if len(label) <= 26 else 10.5)
                box = _lit_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, bw, bh,
                                 fill=_LIT_ACCENT, line=None, radius=0.16)
                _lit_shape_text(box, label, size, bold=True, color=WHITE,
                                align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
                if i < n - 1:
                    _lit_shape(slide, MSO_SHAPE.RIGHT_ARROW,
                               x + bw + 0.02, y + 0.15, gap - 0.04, 0.22,
                               fill=RGBColor(0x99, 0xA6, 0xB2), line=None, radius=None)

        # ---- 对比表格
        table_bottom = 1.62
        tb = spec.get("table") or {}
        headers = [str(h) for h in (tb.get("headers") or [])][:6]
        rows = [[str(c) for c in r] for r in (tb.get("rows") or [])]
        if headers and rows:
            rows = [r + [""] * (len(headers) - len(r)) for r in rows]
            table_bottom = _poster_table(slide, headers, rows, t=1.62, max_h=3.66)

        # ---- 注释行（紧贴表格下沿）
        note = str(spec.get("note") or "").strip()
        if note:
            ntf = _box(slide, 0.47, table_bottom + 0.04, 12.40, 0.26)
            _put(ntf, f"注：{note}", size=9.5, color=REF_GRAY, space=0)

        # ---- 核心要点（位置随内容自适应，表格较矮时上移减少中部留白）
        kps = [str(k).strip() for k in (spec.get("key_points") or []) if str(k).strip()][:3]
        if kps:
            content_bottom = max(table_bottom + (0.36 if note else 0.08),
                                 1.52 if steps else 0.0)
            ky = min(5.52, content_bottom + 0.26)
            kh = max(0.92, min(1.14, 6.62 - ky))
            kbox = _lit_shape(slide, MSO_SHAPE.ROUNDED_RECTANGLE, 0.30, ky, 12.73, kh,
                              fill=_LIT_CONC_BG, line=_LIT_ACCENT, radius=0.08)
            tf = kbox.text_frame
            tf.word_wrap = True
            tf.margin_left = tf.margin_right = Inches(0.10)
            tf.margin_top = tf.margin_bottom = Inches(0.04)
            p0 = tf.paragraphs[0]
            p0.alignment = PP_ALIGN.LEFT
            r0 = p0.add_run()
            r0.text = "核心要点"
            r0.font.size = Pt(12.5)
            r0.font.bold = True
            r0.font.name = _CN_FONT
            r0.font.color.rgb = _LIT_ACCENT
            ksize = 11.5 if max(len(k) for k in kps) <= 46 else 10.5
            for k in kps:
                p = tf.add_paragraph()
                p.space_after = Pt(2)
                p.alignment = PP_ALIGN.LEFT
                rk = p.add_run()
                rk.text = "▪ " + k
                rk.font.size = Pt(ksize)
                rk.font.bold = False
                rk.font.name = _CN_FONT
                rk.font.color.rgb = RGBColor(0x1A, 0x36, 0x55)

        # ---- 参考文献脚注（上移避开模板左下角「求真至善」校训装饰字）
        refs_text = str(item.get("refs_text") or "").strip()
        if refs_text:
            rtf = _box(slide, 1.30, 6.68, 11.70, 0.40)
            _put(rtf, refs_text[:220], size=9, color=REF_GRAY, space=0)

        # ---- 备注
        try:
            note_lines = []
            if steps:
                note_lines.append("机制链讲解：" + " → ".join(steps))
            if kps:
                note_lines.append("收尾强调：" + "；".join(kps))
            if note:
                note_lines.append("口径说明：" + note)
            note_lines.append("提醒：表格中的研究数据来自模型生成，汇报前请人工核对。")
            slide.notes_slide.notes_text_frame.text = "\n".join(note_lines)
        except Exception:
            pass

    prs.save(out_path)
    return out_path
