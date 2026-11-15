"""Word 渲染器：优先按《PBL作业word模板.docx》底版填充；模板缺失时回退简洁样式。

模板填充规则：
- 标题、姓名/班级/学号：清空留白，由学生自行填写；
- 「小组问题」：（一）（二）… 逐条填入问题清单；
- 「关键词」：取 case.keywords（可为空）；
- 「问题解答」：模板预置问题一~问题九，按案例问题数动态删块/克隆块；
  每题填（一）学习内容（含教材出处）（二）参考文献（三）实际应用。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.text.paragraph import Paragraph

from . import mindmap, pubmed
from .schema import Case

TEMPLATE = Path(__file__).parent / "templates" / "PBL作业word模板.docx"

GRAY = RGBColor(0x88, 0x88, 0x88)
DARK = RGBColor(0x1F, 0x4E, 0x79)

_CN = "一二三四五六七八九十"


def cn_num(n: int) -> str:
    """1~99 的中文数字。"""
    if n <= 0:
        return "零"
    if n < 10:
        return _CN[n - 1]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN[n - 11]
    tens, ones = divmod(n, 10)
    return _CN[tens - 1] + "十" + (_CN[ones - 1] if ones else "")


def _ordered(case: Case, selected_only: bool = True) -> list:
    """Word 答案按个人总结排序，默认只导出选中作答题。"""
    from . import pipeline as _pipeline
    return _pipeline.ordered_questions(case, selected_only=selected_only)


def _summary_rows(case: Case):
    """个人总结目录 -> [(层级, 编号如 '1.2.1', 标题, 对应问题编号)]。"""
    from . import pipeline as _pipeline
    nodes = case.personal_summary_nodes
    # Draft-derived summaries are intentionally independent of the editable question bank.
    return _pipeline.summary_numbering(nodes, None if case.draft_summary_nodes else case)


@dataclass
class WordFormat:
    include_mindmap: bool = False   # 导图为最后一节课单独成果，默认不进 Word
    include_refs: bool = True
    include_literature: bool = True  # 文末「文献分享」章节（需在 ④ 区选定文献）
    include_prev_acts: bool = True   # 并入前几幕的「问题解答」作为附录（需 ① 区勾选前幕）
    line_spacing: float = 1.5        # 正文倍数行距（覆盖模板行距；紧凑可设 1.0/1.15/1.25）


# ---------------------------------------------------------------- 低层工具
def _template_run_el(p: Paragraph):
    runs = p._p.findall(qn("w:r"))
    return copy.deepcopy(runs[0]) if runs else None


def _strip_runs(p_el) -> None:
    for r in p_el.findall(qn("w:r")):
        p_el.remove(r)


def _set_text(p: Paragraph, text, bold=None, size=None, color=None):
    """替换段落文本；克隆段内第一个 run 但剥离其字符格式，仅保留显式传入的属性，
    避免链式插入时把上一段的加粗/灰色等格式传播开来。"""
    tmpl = _template_run_el(p)
    _strip_runs(p._p)
    if tmpl is not None:
        rpr = tmpl.find(qn("w:rPr"))
        if rpr is not None:
            tmpl.remove(rpr)
        p._p.append(tmpl)
    runs = p.runs
    r = runs[0] if runs else p.add_run()
    r.text = text
    if bold is not None:
        r.font.bold = bold
    if size is not None:
        r.font.size = Pt(size)
    if color is not None:
        r.font.color.rgb = color
    return r


def _strip_numpr(p_el) -> None:
    """去掉段落属性里的自动编号，避免插入段混入模板「一、二、三」编号列表。"""
    ppr = p_el.find(qn("w:pPr"))
    if ppr is not None:
        numpr = ppr.find(qn("w:numPr"))
        if numpr is not None:
            ppr.remove(numpr)


def _strip_ind(p_el) -> None:
    """去掉克隆来的首行缩进——缩进一律显式设置，杜绝从锚点段落意外继承。"""
    ppr = p_el.find(qn("w:pPr"))
    if ppr is not None:
        ind = ppr.find(qn("w:ind"))
        if ind is not None:
            ppr.remove(ind)


_CITE_NUM = re.compile(r"(\[\d+\])")

# 以编号开头的段落（（1）/1./①/一、等）不缩进
_NUM_START = re.compile(
    r"^\s*(?:[（(]\s*\d+\s*[）).、.．]|[0-9]+\s*[.、．）)]|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫]"
    r"|[一二三四五六七八九十]{1,3}\s*[、.．])")


def _indent_two_chars(p: Paragraph) -> None:
    """首行缩进两字符（firstLineChars=200）。"""
    ind = p._p.get_or_add_pPr().get_or_add_ind()
    ind.set(qn("w:firstLineChars"), "200")
    ind.set(qn("w:firstLine"), "480")


def _insert_body_after(anchor: Paragraph, text, size=None, color=None) -> Paragraph:
    """插入正文段落：普通段首行缩进两字符，编号开头的段顶格；[1] 渲染为上标。"""
    new_el = copy.deepcopy(anchor._p)
    _strip_numpr(new_el)
    _strip_ind(new_el)
    anchor._p.addnext(new_el)
    p = Paragraph(new_el, anchor._parent)
    if not _NUM_START.match(text):
        _indent_two_chars(p)
    tmpl = _template_run_el(anchor)
    _strip_runs(new_el)
    for seg in _CITE_NUM.split(text):
        if not seg:
            continue
        if tmpl is not None:
            r_el = copy.deepcopy(tmpl)
            rpr = r_el.find(qn("w:rPr"))
            if rpr is not None:
                r_el.remove(rpr)
            new_el.append(r_el)
        r = p.runs[-1]
        r.text = seg
        if size is not None:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color
        if _CITE_NUM.fullmatch(seg):
            r.font.superscript = True
    return p


def _fill_table(tbl, rows: list[list[str]]) -> None:
    n_cols = max(len(r) for r in rows)
    for i, row in enumerate(rows):
        for j in range(n_cols):
            cell = tbl.cell(i, j)
            cell.text = row[j] if j < len(row) else ""
            p0 = cell.paragraphs[0]
            run = p0.runs[0] if p0.runs else p0.add_run("")
            run.font.size = Pt(10.5)
            run.font.bold = i == 0
            run.font.name = "Times New Roman"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")


def _insert_table_after(doc, anchor: Paragraph, rows: list[list[str]]) -> Paragraph:
    """在 anchor 后插入带边框表格（首行表头加粗），返回表格后的空段以供续链。"""
    tbl = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
    try:
        tbl.style = "Table Grid"
    except KeyError:
        _set_table_borders(tbl)
    _fill_table(tbl, rows)
    anchor._p.addnext(tbl._tbl)
    empty = doc.add_paragraph()      # 表格后补空段，避免表格紧贴后续内容
    tbl._tbl.addnext(empty._p)
    return empty


def _set_table_borders(tbl) -> None:
    from docx.oxml import OxmlElement
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), "808080")
        borders.append(el)
    tbl._tbl.tblPr.append(borders)


_LINE_SPACING = 1.5        # 本次导出的正文行距（build_docx 按 fmt.line_spacing 设置）


def _apply_line_spacing(p: Paragraph, spacing=None) -> None:
    """按配置给段落设倍数行距；未配置（None/<=0）时保持模板原样。"""
    if not spacing or spacing <= 0:
        return
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    pf.line_spacing = float(spacing)


def _insert_after(anchor: Paragraph, text, bold=None, size=None, color=None) -> Paragraph:
    """在 anchor 后插入新段落，继承缩进/对齐，但行距按本次导出配置覆盖（模板行距往往偏大）。"""
    new_el = copy.deepcopy(anchor._p)
    _strip_numpr(new_el)
    _strip_ind(new_el)
    anchor._p.addnext(new_el)
    p = Paragraph(new_el, anchor._parent)
    _set_text(p, text, bold, size, color)
    _apply_line_spacing(p, _LINE_SPACING)
    return p


def _clear(p: Paragraph) -> None:
    _strip_runs(p._p)


def _set_ea(run, name: str = "宋体") -> None:
    run.font.name = "Times New Roman"
    run._element.get_or_add_rPr()
    if run._element.rPr.rFonts is not None:
        run._element.rPr.rFonts.set(qn("w:eastAsia"), name)


def _set_toc_indent(p: Paragraph, level: int) -> None:
    """个人总结目录：按层级左缩进，取消首行缩进（目录要一眼看出从属关系）。"""
    ind = p._p.get_or_add_pPr().get_or_add_ind()
    for attr in ("w:firstLineChars", "w:firstLine"):
        if ind.get(qn(attr)) is not None:
            del ind.attrib[qn(attr)]
    ind.set(qn("w:left"), str(int(Cm(0.62) * (level - 1) / 635)))  # EMU -> twips
    ind.set(qn("w:leftChars"), str(0))


# ---------------------------------------------------------------- 文献分享章节
def _append_prev_acts(doc, prev_cases: list, fmt: "WordFormat") -> None:
    """把前几幕的「问题解答」整段并入文末，作为回顾附录。

    一幕一节：幕标题 + 病例摘要（截断）+ 该幕各问题的学习内容/参考文献/实际应用。
    幕内容取自 cases/acts/ 下的存档，不在本幕重新生成，因此与当时提交的版本完全一致。
    """
    if not prev_cases:
        return

    def head(text, size=14, bold=True, ea="黑体", before=8, after=4):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        r = p.add_run(text)
        r.font.bold, r.font.size, r.font.name = bold, Pt(size), "Times New Roman"
        r._element.rPr.rFonts.set(qn("w:eastAsia"), ea)
        return p

    def bodyp(text, after=3):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(after)
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = getattr(fmt, "line_spacing", 1.5) or 1.5
        if not _NUM_START.match(text):
            pf.first_line_indent = Pt(24)
        for seg in _CITE_NUM.split(text):
            if not seg:
                continue
            r = p.add_run(seg)
            r.font.name, r.font.size = "Times New Roman", Pt(12)
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
            if _CITE_NUM.fullmatch(seg):
                r.font.superscript = True
        return p

    for act in prev_cases:
        doc.add_paragraph()
        label = _acts_label(act)
        head(f"附录：{label}问题解答（回顾）", size=14, before=12)
        if getattr(act, "act_note", ""):
            bodyp(f"本幕要点：{act.act_note}")
        if act.case_text:
            snippet = " ".join(act.case_text.split())
            bodyp(f"病例摘要：{snippet[:300]}{'…' if len(snippet) > 300 else ''}")
        for i, q in enumerate(_ordered(act, selected_only=False), 1):
            head(f"问题{cn_num(i)}：{q.text}", size=12, before=6)
            if q.learning:
                head("（一）学习内容", size=12, before=2)
                for j, blk in enumerate(q.learning, 1):
                    if blk.heading:
                        head(f"{j}. {blk.heading}", size=12, before=3)
                    for seg in blk.body.split("\n"):
                        if seg.strip():
                            bodyp(seg.strip())
                    if blk.table:
                        tbl = doc.add_table(rows=len(blk.table),
                                            cols=max(len(r) for r in blk.table))
                        try:
                            tbl.style = "Table Grid"
                        except KeyError:
                            _set_table_borders(tbl)
                        _fill_table(tbl, blk.table)
                        doc.add_paragraph()
            if fmt.include_refs and q.references:
                head("（二）参考文献", size=12, before=2)
                for k2, ref in enumerate(q.references, 1):
                    p = doc.add_paragraph()
                    p.paragraph_format.first_line_indent = Pt(21)
                    r = p.add_run(f"[{k2}] {pubmed.format_reference(ref)}")
                    r.font.size, r.font.name = Pt(12), "Times New Roman"
                    r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
            if q.clinical:
                head("（三）实际应用", size=12, before=2)
                for seg in q.clinical.split("\n"):
                    if seg.strip():
                        bodyp(seg.strip())


def _acts_label(case: Case) -> str:
    """「第二幕 · 病理回报」；不依赖 pbl.acts，避免渲染层循环导入。"""
    cn = "一二三四五六七八九十"
    n = min(max(int(getattr(case, "act_no", 1) or 1), 1), len(cn))
    name = f"第{cn[n - 1]}幕"
    t = (getattr(case, "act_title", "") or "").strip()
    return f"{name} · {t} " if t else f"{name} "


def _append_literature(doc, case: Case, fmt: "WordFormat") -> None:
    """文末追加「文献分享」：研究过程 -> 核心结论 -> 与病例的联系 -> 关键图 -> 出处。

    内容全部来自 ④ 区选定的那篇真实文献（case.ppt.paper），AI 只负责解读与挑图。
    """
    paper = case.ppt.paper
    if paper is None:
        return

    def head(text, size=14, bold=True, ea="黑体", before=8, after=3):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(before)
        p.paragraph_format.space_after = Pt(after)
        r = p.add_run(text)
        r.font.bold, r.font.size = bold, Pt(size)
        _set_ea(r, ea)
        return p

    def bodyp(text, after=3, indent=True, size=12):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(after)
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = getattr(fmt, "line_spacing", 1.5) or 1.5
        if indent and not _NUM_START.match(text):
            pf.first_line_indent = Pt(24)
        for seg in _CITE_NUM.split(text):
            if not seg:
                continue
            r = p.add_run(seg)
            r.font.name, r.font.size = "Times New Roman", Pt(size)
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
            if _CITE_NUM.fullmatch(seg):
                r.font.superscript = True
        return p

    doc.add_paragraph()
    head("文献分享", size=14, ea="黑体", before=14)
    title = paper.title_cn or paper.title_en
    if title:
        head(f"分享文献：{title}", size=12, bold=True, ea="宋体", before=2)
    meta = "　".join(x for x in [paper.journal, paper.year,
                                 f"IF≈{paper.if_value:g}" if paper.if_value else ""] if x)
    if meta:
        bodyp(meta, after=4, indent=False, size=10.5)

    if getattr(paper, "background", "").strip():
        bodyp(f"研究背景：{paper.background.strip()}", after=4, indent=False, size=10.5)

    head("（一）研究过程", size=13)
    steps = [s for s in (paper.steps or []) if str(s.get("text", "")).strip()]
    if steps:
        for s in steps:
            bodyp(f"{s.get('tag', '')}. {str(s.get('text', '')).strip()}".replace("..", "."),
                  indent=False)
    elif paper.approach:
        bodyp(paper.approach)
    if steps and paper.approach and len(paper.approach) > 30:
        bodyp(paper.approach)

    if paper.conclusion:
        head("（二）核心结论", size=13)
        bodyp(paper.conclusion)

    if paper.case_link:
        head("（三）与病例的联系", size=13)
        bodyp(paper.case_link)

    figs = [f for f in paper.figures if f.selected and f.file and Path(f.file).exists()]
    if figs:
        head("（四）关键图", size=13)
        for f in figs:
            pic = doc.add_paragraph()
            pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic.paragraph_format.space_before = Pt(4)
            try:
                pic.add_run().add_picture(f.file, width=Cm(13.5))
            except Exception:
                continue
            cap = f"{f.label}：{f.note}" if f.note else f.label
            cp = bodyp(cap, after=6, indent=False, size=10.5)
            cp.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if paper.ref_text:
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Pt(21)
        r = p.add_run(f"文献出处：{paper.ref_text}")
        r.font.name, r.font.size = "Times New Roman", Pt(10.5)
        r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")


# ---------------------------------------------------------------- 模板填充
_Q_LABEL = re.compile(r"^问题\s*([一二三四五六七八九十]{1,3}|\d{1,2})\s*[：:]?\s*$")
_SUB_MAP = {"学习内容": "learn", "参考文献": "ref", "实际应用": "app", "临床应用": "app"}


def _sub_key(text: str) -> str | None:
    """识别小节标题：兼容（一）学习内容 / 一、学习内容 / 学习内容： 等写法。"""
    t = text.strip()
    t = re.sub(r"^[（(][一二三123][）)]\s*", "", t)
    t = re.sub(r"^[一二三123]\s*[、.．]\s*", "", t)
    t = t.rstrip("：:．. ")
    return _SUB_MAP.get(t)


def _scan_blocks(paras: list[Paragraph]) -> list[dict]:
    """定位每个「问题N：」块：label + 学习内容 + 参考文献 + 实际应用 + 尾部空段。

    兼容中文/阿拉伯序号、带不带（一）前缀、带不带冒号的模板写法。
    """
    blocks: list[dict] = []
    i = 0
    n = len(paras)
    while i < n:
        if _Q_LABEL.match(paras[i].text.strip()):
            b = {"label": paras[i]}
            j = i + 1
            for key in ("learn", "ref", "app"):
                while j < n and _sub_key(paras[j].text) != key:
                    j += 1
                b[key] = paras[j] if j < n else None
                j += 1
            b["sep"] = paras[j] if j < n and not paras[j].text.strip() else None
            blocks.append(b)
            i = j
        else:
            i += 1
    return blocks


def build_docx(case: Case, fmt: WordFormat, out_path: str, mindmap_png: str | None = None,
               template_path: str | None = None, prev_cases: list | None = None) -> str:
    """template_path：用户上传的自定义模板；缺省用内置模板；均无则用简洁样式。

    prev_cases：要并入文末的前几幕 Case（来自 cases/acts/ 存档），为空则只出本幕。
    """
    from . import pipeline as _pipeline
    _pipeline.sync_source_refs(case)          # 出处对账：学习内容的教材出处进参考文献
    global _LINE_SPACING
    _LINE_SPACING = getattr(fmt, "line_spacing", 1.5) or 1.5
    tpath = Path(template_path) if template_path else TEMPLATE
    if tpath.exists():
        try:
            return _build_from_template(case, fmt, out_path, mindmap_png, tpath, prev_cases)
        except Exception as e:  # 任意模板结构异常时兜底为追加模式
            print(f"[docx] 模板填充失败，改用追加模式：{e}")
            doc = Document(str(tpath))
            _append_fill(doc, case, fmt, mindmap_png, prev_cases)
            doc.save(out_path)
            return out_path
    return _build_plain(case, fmt, out_path, mindmap_png, prev_cases)


_NUM_LEAD = re.compile(
    r"^\s*(?:Q?\d{1,3}\s*[.、．:：)）]|（\s*\d{1,3}\s*）|[（(][一二三四五六七八九十百]{1,4}[）)])\s*")


def _group_question_lines(case: Case) -> list[str]:
    """小组问题显示行：优先用 ① 区「问题草稿」的逐行原文——那是用户输入的原始清单，
    必须完完整整保留（含「（选做）」等标记、原始措辞与引号术语）；没有草稿才回退题库。
    返回已统一加（一）（二）… 编号的行。"""
    draft_lines = [x.strip() for x in (case.question_draft or "").splitlines() if x.strip()]
    if draft_lines:
        src: list[str] = []
        for line in draft_lines:
            t = line
            for _ in range(2):                      # 连剥两层行首编号，如「3.（3）」
                stripped = _NUM_LEAD.sub("", t)
                if stripped == t:
                    break
                t = stripped
            if t:
                src.append(t)
    else:
        src = [q.text for q in case.questions]
    return [f"（{cn_num(i)}）{t}" for i, t in enumerate(src, 1)]


def _build_from_template(case: Case, fmt: WordFormat, out_path: str,
                         mindmap_png: str | None, tpath: Path,
                         prev_cases: list | None = None) -> str:
    doc = Document(str(tpath))
    paras = list(doc.paragraphs)

    # 1) 标题、姓名/班级/学号：清空留白（找不到对应行则跳过）
    if paras:
        _clear(paras[0])
    info_p = next((p for p in paras if "姓名" in p.text or "学号" in p.text), None)
    if info_p is not None:
        _set_text(info_p, "姓名：　　　　　　班级：　　　　　　学号：")

    # 2) 小组问题/问题清单：优先用户原始清单（问题草稿逐行原文），否则回退全量题库
    xz = next((i for i, p in enumerate(paras)
               if p.text.strip().rstrip("：:") in ("小组问题", "问题清单", "全部问题")), None)
    if xz is not None:
        lines = _group_question_lines(case)
        if lines:
            anchor = next((p for p in paras[xz + 1:] if p.text.strip().startswith("（一）")), None)
            if anchor is not None:
                _set_text(anchor, lines[0], size=12)
                _apply_line_spacing(anchor, _LINE_SPACING)
                for line in lines[1:]:
                    anchor = _insert_after(anchor, line, size=12)

    # 2.5) 个人总结：按主题归并的分层目录（1. / 1.1 / 1.2），与小组问题分开
    if case.personal_summary_nodes:
        sm = next((i for i, p in enumerate(paras)
                   if p.text.strip().rstrip("：:") == "个人总结"), None)
        if sm is not None:
            anchor = next((p for p in paras[sm + 1:] if p.text.strip().startswith("（一）")), None)
            if anchor is not None:
                cur = anchor
                for k, (level, num, text, _qids) in enumerate(_summary_rows(case)):
                    label = f"{num}. {text}" if level == 1 else f"{num} {text}"
                    if k == 0:
                        _set_text(cur, label, size=12)
                        _apply_line_spacing(cur, _LINE_SPACING)
                    else:
                        cur = _insert_after(cur, label, size=12)
                    _set_toc_indent(cur, level)
                    if level == 1:
                        for r in cur.runs:
                            r.font.bold = True

    # 3) 关键词
    kw_p = next((p for p in doc.paragraphs
                 if p.text.strip().rstrip("：: ") == "关键词"), None)
    kws = case.keywords or []
    if kw_p is not None and kws:
        _insert_after(kw_p, "；".join(kws), size=12)

    # 4) 问题解答：动态块数；模板没有标准问题块则整体追加
    qs = _ordered(case, selected_only=True)   # 未选题保留在全量清单/总结中，只省略答案排列
    blocks = _scan_blocks(list(doc.paragraphs))
    if not blocks:
        _append_fill(doc, case, fmt, mindmap_png)
        doc.save(out_path)
        return out_path
    k, n = len(qs), len(blocks)
    if n and k < n:
        for b in blocks[k:]:
            for key in ("label", "learn", "ref", "app", "sep"):
                if b.get(key) is not None:
                    el = b[key]._p
                    el.getparent().remove(el)
    elif n and k > n:
        last = blocks[-1]
        tail = last["sep"] or last["app"]
        for _ in range(k - n):
            newb, prev = {}, tail
            for key in ("label", "learn", "ref", "app"):
                el = copy.deepcopy(last[key]._p)
                prev._p.addnext(el)
                newb[key] = Paragraph(el, last[key]._parent)
                prev = newb[key]
            if last["sep"] is not None:
                sep_el = copy.deepcopy(last["sep"]._p)
                prev._p.addnext(sep_el)
                newb["sep"] = Paragraph(sep_el, last["sep"]._parent)
            else:
                newb["sep"] = None
            blocks.append(newb)
            tail = newb["sep"] or newb["app"]

    blocks = blocks[:k]
    for i, q in enumerate(qs):
        b = blocks[i]
        # 问题N：+ 题目（模板标签为 14pt）
        _set_text(b["label"], f"问题{cn_num(i + 1)}：{q.text}", size=14)
        # （一）学习内容
        if b.get("learn") is not None:
            a = b["learn"]
            for j, blk in enumerate(q.learning, 1):
                a = _insert_after(a, f"{j}. {blk.heading}" if blk.heading else f"{j}.",
                                  bold=True, size=12)
                for seg in blk.body.split("\n"):
                    if seg.strip():
                        a = _insert_body_after(a, seg.strip(), size=12)
                if blk.table:
                    a = _insert_table_after(doc, a, blk.table)
        # Citation provenance warnings should stay visible in the generated deliverable.
        if q.citation_warning and b.get("learn") is not None:
            a = _insert_after(b["learn"], f"教材引用核对：{q.citation_warning}", size=10, color=GRAY)
        # （二）参考文献
        if fmt.include_refs and q.references and b["ref"] is not None:
            a = b["ref"]
            for j, ref in enumerate(q.references, 1):
                a = _insert_after(a, f"[{j}] {pubmed.format_reference(ref)}", size=12)
                _indent_two_chars(a)
        # （三）实际应用
        if b.get("app") is not None:
            a = b["app"]
            for seg in (q.clinical or "").split("\n"):
                if seg.strip():
                    a = _insert_body_after(a, seg.strip(), size=12)

    # 5) 文末附整体思维导图
    if fmt.include_mindmap and mindmap_png:
        h = doc.add_paragraph()
        r = h.add_run("附：案例思维导图（配套 .mm / .md 文件可在 XMind 中继续编辑）")
        r.font.size = Pt(12)
        r.font.bold = True
        _set_ea(r, "黑体")
        pic = doc.add_paragraph()
        pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pic.add_run().add_picture(mindmap_png, width=Cm(15.5))

    # 6) 前幕回顾（① 区勾选的前几幕存档，整段并入作为附录）
    if fmt.include_prev_acts and prev_cases:
        _append_prev_acts(doc, prev_cases, fmt)

    # 7) 文献分享（④ 区选定的那篇文献：研究过程 / 核心结论 / 与病例的联系 / 关键图）
    if fmt.include_literature:
        _append_literature(doc, case, fmt)

    doc.save(out_path)
    return out_path


# ---------------------------------------------------------------- 追加模式兜底
def _append_fill(doc, case: Case, fmt: WordFormat, mindmap_png: str | None,
                 prev_cases: list | None = None) -> None:
    """模板里找不到标准问题块时：把教案按作业格式整体追加到模板末尾。"""
    def head(text, size=14, bold=True, ea="黑体", before=8):
        p = doc.add_paragraph()
        p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(before), Pt(4)
        r = p.add_run(text)
        r.font.bold, r.font.size, r.font.name = bold, Pt(size), "Times New Roman"
        r._element.rPr.rFonts.set(qn("w:eastAsia"), ea)
        return p

    def bodyp(text, after=3):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(after)
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = getattr(fmt, "line_spacing", 1.5) or 1.5
        if not _NUM_START.match(text):
            pf.first_line_indent = Pt(24)
        for seg in _CITE_NUM.split(text):
            if not seg:
                continue
            r = p.add_run(seg)
            r.font.name, r.font.size = "Times New Roman", Pt(12)
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
            if _CITE_NUM.fullmatch(seg):
                r.font.superscript = True
        return p

    if case.questions:
        head("小组问题")
        for i, q in enumerate(case.questions, 1):
            bodyp(f"（{cn_num(i)}）{q.text}")
    if case.personal_summary_nodes:
        head("个人总结")
        for level, num, text, _qids in _summary_rows(case):
            p = bodyp(f"{num}. {text}" if level == 1 else f"{num} {text}")
            p.paragraph_format.left_indent = Cm(0.62 * (level - 1))

    for i, q in enumerate(_ordered(case, selected_only=True), 1):
        head(f"问题{cn_num(i)}：{q.text}")
        head("（一）学习内容", size=13)
        for j, blk in enumerate(q.learning, 1):
            if blk.heading:
                head(f"{j}. {blk.heading}", size=12, before=4)
            for seg in blk.body.split("\n"):
                if seg.strip():
                    bodyp(seg.strip())
            if blk.table:
                tbl = doc.add_table(rows=len(blk.table), cols=max(len(r) for r in blk.table))
                try:
                    tbl.style = "Table Grid"
                except KeyError:
                    _set_table_borders(tbl)
                _fill_table(tbl, blk.table)
                doc.add_paragraph()
        if q.citation_warning:
            bodyp(f"教材引用核对：{q.citation_warning}", after=2)
        if fmt.include_refs and q.references:
            head("（二）参考文献", size=13)
            for k2, ref in enumerate(q.references, 1):
                p = doc.add_paragraph()
                p.paragraph_format.first_line_indent = Pt(21)
                r = p.add_run(f"[{k2}] {pubmed.format_reference(ref)}")
                r.font.size, r.font.name = Pt(12), "Times New Roman"
                r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
        head("（三）实际应用", size=13)
        for seg in (q.clinical or "").split("\n"):
            if seg.strip():
                bodyp(seg.strip(), after=4)
    if fmt.include_prev_acts and prev_cases:
        _append_prev_acts(doc, prev_cases, fmt)
    if fmt.include_literature:
        _append_literature(doc, case, fmt)
    if fmt.include_mindmap and mindmap_png:
        head("附：案例思维导图", size=13)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(mindmap_png, width=Cm(15.5))


# ---------------------------------------------------------------- 简洁样式兜底
def _build_plain(case: Case, fmt: WordFormat, out_path: str, mindmap_png: str | None,
                 prev_cases: list | None = None) -> str:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    def para(text, size=12, bold=False, ea="宋体", align=None, before=0, after=6, color=None):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_before, pf.space_after = Pt(before), Pt(after)
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = getattr(fmt, "line_spacing", 1.5) or 1.5
        if align is not None:
            pf.alignment = align
        r = p.add_run(text)
        r.font.name = "Times New Roman"
        r.font.size = Pt(size)
        r.font.bold = bold
        if color is not None:
            r.font.color.rgb = color
        r._element.rPr.rFonts.set(qn("w:eastAsia"), ea)
        return p

    def body(text, after=3):
        """正文段：普通段首行缩进两字符、编号段顶格；引用 [n] 上标。"""
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(after)
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = getattr(fmt, "line_spacing", 1.5) or 1.5
        if not _NUM_START.match(text):
            pf.first_line_indent = Pt(24)
        for seg in _CITE_NUM.split(text):
            if not seg:
                continue
            r = p.add_run(seg)
            r.font.name = "Times New Roman"
            r.font.size = Pt(12)
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
            if _CITE_NUM.fullmatch(seg):
                r.font.superscript = True
        return p

    para(case.title, size=20, bold=True, ea="黑体", align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    para(f"{case.course} · {case.audience} · PBL 教案", size=11,
         align=WD_ALIGN_PARAGRAPH.CENTER, color=GRAY)
    para("一、病例摘要", size=16, bold=True, ea="黑体", before=10)
    for seg in case.case_text.split("\n"):
        if seg.strip():
            para(seg.strip(), after=4)
    if case.questions or case.question_draft:
        para("二、小组问题", size=16, bold=True, ea="黑体", before=10)
        for line in _group_question_lines(case):
            para(line, after=2)
    if case.personal_summary_nodes:
        para("三、个人总结", size=16, bold=True, ea="黑体", before=10)
        for level, num, text, _qids in _summary_rows(case):
            lp = para(f"{num}. {text}" if level == 1 else f"{num} {text}",
                      bold=(level == 1), after=2)
            lp.paragraph_format.left_indent = Cm(0.62 * (level - 1))
        nxt = "四、问题解答"
    else:
        nxt = "二、问题解答"
    para(nxt, size=16, bold=True, ea="黑体", before=10)
    for q in _ordered(case, selected_only=True):
        para(f"{q.qid}　{q.text}", size=15, bold=True, ea="黑体", before=10)
        for b in q.learning:
            if b.heading:
                para(b.heading, bold=True, ea="黑体", after=2)
            for seg in b.body.split("\n"):
                if seg.strip():
                    body(seg.strip())
            if b.table:
                tbl = doc.add_table(rows=len(b.table), cols=max(len(r) for r in b.table))
                try:
                    tbl.style = "Table Grid"
                except KeyError:
                    _set_table_borders(tbl)
                _fill_table(tbl, b.table)
                doc.add_paragraph()
        if q.citation_warning:
            para(f"教材引用核对：{q.citation_warning}", size=10, color=GRAY)
        if fmt.include_refs and q.references:
            para("参考文献", bold=True, ea="黑体", after=2)
            for i2, ref in enumerate(q.references, 1):
                rp = para(f"[{i2}] {pubmed.format_reference(ref)}", size=10.5)
                rp.paragraph_format.first_line_indent = Pt(21)
        para("临床应用", bold=True, ea="黑体", after=2)
        for seg in q.clinical.split("\n"):
            if seg.strip():
                body(seg.strip(), after=4)
    if fmt.include_prev_acts and prev_cases:
        _append_prev_acts(doc, prev_cases, fmt)
    if fmt.include_literature:
        _append_literature(doc, case, fmt)
    if fmt.include_mindmap and mindmap_png:
        para("案例思维导图", size=16, bold=True, ea="黑体", before=10)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(mindmap_png, width=Cm(16.5))
    doc.save(out_path)
    return out_path
