import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbl import acts, demo, mindmap, pubmed, render_docx, render_pptx, schema, textbook
from pbl.schema import MindmapNode, SlideOutline, Question, LearningBlock, Reference, Case

OUT = Path(__file__).resolve().parent.parent / "output"
OUT.mkdir(exist_ok=True)

case = demo.demo_case()
print("1. schema OK:", case.title, "|", len(case.questions), "个问题 | 疾病导图根:",
      case.disease_mindmap.text if case.disease_mindmap else "无")

# 疾病中心导图三种导出 + Markdown 往返
root = case.disease_mindmap
png = mindmap.render_png(root, str(OUT / "_smoke_case.png"))
html = mindmap.node_to_markmap_html(root)
mm = mindmap.node_to_freemind(root)
print("2. disease mindmap OK:", Path(png).stat().st_size, "bytes PNG;",
      len(html), "chars HTML;", len(mm), "chars FreeMind")

md = mindmap.node_to_markdown(root)
back = mindmap.markdown_to_node(md, root_text=root.text)
n0, n1 = len(root.children), len(back.children)
assert n0 == n1, f"markdown 往返分支数不一致: {n0} != {n1}"
print("3. markdown 往返 OK:", n0, "个一级分支")

# 参考文献著录格式
from pbl import pubmed
r = case.questions[0].references[0]
print("3b. 引用格式[J]:", pubmed.format_reference(r)[:80])
print("3c. 引用格式[M]:", pubmed.format_reference({
    "ref_type": "M", "title": "诊断学", "authors": ["万学红", "卢雪峰"],
    "place": "北京", "publisher": "人民卫生出版社", "year": "2024",
    "edition": "10版", "pages": "125-128"}))

# 引用标注解析（GB/T 顺序编码制）
from pbl.pipeline import resolve_citations
q0 = case.questions[0]
tb_hits = [{"source": "内科（第10版）", "page": 78, "text": "x", "score": 0.9}]
q0.learning[0].body = "CAP 的诊断依据包括[T1]。"
resolve_citations(q0, tb_hits, extra_refs=q0.references)
assert "[1]" in q0.learning[0].body, "标注未转换为顺序编码"
m_ref = next((x for x in q0.references if x.ref_type == "M"), None)
assert m_ref is not None, "教材条目未入参考文献表"
print("3d. 标注解析 OK:", q0.learning[0].body, "| 教材条目:", pubmed.format_reference(m_ref))

# Word（模板填充：3 题 < 模板 9 块 → 删块路径；默认不附导图）
fmt = render_docx.WordFormat()
docx = render_docx.build_docx(case, fmt, str(OUT / "_smoke_case.docx"), None)
print("4. docx(模板,无导图) OK:", Path(docx).stat().st_size, "bytes")

# Word（12 题 > 模板 9 块 → 克隆块路径；附导图路径）
big = demo.demo_case()
big.questions = [q.model_copy(deep=True) for q in big.questions]
for i in range(4, 13):
    from pbl.schema import Question as _Q
    big.questions.append(_Q(qid=f"Q{i}", text=f"测试问题{i}：占位内容。"))
fmt2 = render_docx.WordFormat(include_mindmap=True)
docx2 = render_docx.build_docx(big, fmt2, str(OUT / "_smoke_case12.docx"), png)
print("4b. docx(12题克隆+导图) OK:", Path(docx2).stat().st_size, "bytes")

# PPT
outline = [SlideOutline(**s) for s in [
    {"layout": "cover", "title": case.title, "subtitle": "演示 · PBL"},
    {"layout": "case", "title": "病例摘要", "body": case.case_text[:300]},
    {"layout": "overview", "title": "学习问题", "bullets": [f"{q.qid} {q.text}" for q in case.questions]},
    {"layout": "question", "title": "Q1 学习要点", "bullets": [b.heading for b in case.questions[0].learning]},
    {"layout": "clinical", "title": "Q1 临床应用", "body": case.questions[0].clinical[:200]},
    {"layout": "refs", "title": "参考文献", "bullets": ["演示文献 1", "演示文献 2"]},
    {"layout": "summary", "title": "总结", "bullets": ["机制", "诊治", "管理"]},
]]
pptx = render_pptx.build_pptx(case, outline, "医学蓝", str(OUT / "_smoke_case.pptx"))
print("5. pptx OK:", Path(pptx).stat().st_size, "bytes")

# 回读校验
from docx import Document as _D
from pptx import Presentation as _P
_D(str(OUT / "_smoke_case.docx"))
_p = _P(str(OUT / "_smoke_case.pptx"))
print("6. 回读 OK: pptx", len(_p.slides), "页")

# PubMed 联网测试（失败不阻塞）
refs = pubmed.search_pubmed("community acquired pneumonia diagnosis", retmax=3)
print("7. PubMed:", f"返回 {len(refs)} 条", refs[0]["title"][:60] if refs else "（网络不可用或无结果）")

# 教材库状态
print("8. 教材库: books =", len(textbook.list_books()), "| index =", textbook.has_index())

# ---------------- 个人总结：分层目录 + 问题解答排序
from pbl import pipeline as _pl
from pbl.schema import SummaryNode, PaperShare, PaperFigure

sum_case = demo.demo_case()
sum_case.summary_nodes = [
    SummaryNode(text="诊断依据", children=[
        SummaryNode(text="实验室检查", children=[
            SummaryNode(text="血常规的临床意义", qids=["Q1"]),
            SummaryNode(text="尿常规的临床意义", qids=["Q3"])]),
        SummaryNode(text="影像学表现", qids=["Q2"])]),
    SummaryNode(text="治疗与用药", children=[
        SummaryNode(text="初始抗感染方案", qids=["Q2"])]),
]
rows = _pl.summary_numbering(sum_case.summary_nodes)
nums = [n for _lv, n, _t, _q in rows]
assert nums[0] == "1" and "1.1" in nums and "1.1.1" in nums, f"目录编号异常: {nums}"
order = [q.qid for q in _pl.ordered_questions(sum_case, selected_only=False)]
assert order[0] == "Q1" and order[1] == "Q3", f"问题解答未按目录排序: {order}"
print("9. 个人总结目录 OK:", " / ".join(nums[:4]), "| 问题解答顺序:", order)

# ---------------- Word：个人总结块 + 文献分享章节
lit_case = demo.demo_case()
lit_case.summary_nodes = sum_case.summary_nodes
fig_png = str(next((OUT.parent / "knowledge_base" / "lit_figs").rglob("figure*.png")))
lit_case.ppt.paper = PaperShare(
    title_en="X", title_cn="演示文献中文标题", journal="Front Immunol", year="2025",
    if_value=5.7,
    steps=[{"tag": "A", "text": "假设：演示 A"}, {"tag": "B", "text": "方法：演示 B"},
           {"tag": "C", "text": "验证：演示 C"}, {"tag": "D", "text": "因果：演示 D"}],
    approach="演示研究思路整段文字。" * 3,
    conclusion="演示核心结论。", case_link="演示与病例的联系。",
    limitations="演示局限性。", ref_text="Zhang H, et al. Front Immunol. 2025;16:1638818.",
    background="演示研究背景：该病负担重且机制未明。",
    figures=[PaperFigure(file=fig_png, label="Figure 1", note="演示图注", selected=True)])
lit_docx = render_docx.build_docx(lit_case, render_docx.WordFormat(),
                                  str(OUT / "_smoke_lit.docx"), None)
from docx import Document as _D2
_txt = "\n".join(p.text for p in _D2(lit_docx).paragraphs)
for must in ("个人总结", "1. 诊断依据", "1.1 实验室检查", "1.1.1 血常规的临床意义",
             "文献分享", "研究背景：演示研究背景", "（一）研究过程", "A. 假设：演示 A", "（二）核心结论",
             "（三）与病例的联系", "（四）关键图", "文献出处"):
    assert must in _txt, f"Word 缺少内容: {must}"
print("10. Word 个人总结+文献分享 OK:", Path(lit_docx).stat().st_size, "bytes")

# ---------------- 稳定题目 ID / 作答选择 / 教材引用回归
from pbl.schema import Case as _Case, Question as _Question, LearningBlock as _LB, Reference as _Ref
from pbl.pipeline import resolve_citations as _resolve, merge_question_bank as _merge_qs

q_a = _Question(qid="Q1", text="慢性肾病的诊断标准", learning=[_LB(body="教材内容[T1]。", source="[T1]"),
        _LB(body="对照如下[P1]。", source="", table=[["项目", "依据[T1]"], ["肾功能", "eGFR"]])])
q_b = _Question(qid="Q2", text="慢性肾病的治疗原则")
case_cite = _Case(questions=[q_a, q_b])
case_cite.answer_question_ids = [q_a.uid]
case_cite.summary_nodes = [SummaryNode(text="主题", children=[SummaryNode(text=q_a.text, qids=["Q1"]),
                                                             SummaryNode(text=q_b.text, qids=["Q2"])])]
case_cite = _Case.model_validate(case_cite.model_dump())
# Exercise resolver with a realistic retrieved book hit plus a journal hit.
tb = [{"source": "内科（第10版）", "page": 78, "text": "教材摘录", "score": .9}]
pm = [{"title": "A real review", "authors": ["A Author"], "journal": "Journal", "year": "2025",
       "volume": "1", "issue": "2", "pages": "3-9", "pmid": "123", "doi": "10/test", "abstract": "x"}]
q_a.learning[0].body = "教材内容[T1]。论文观点[P1]。"
q_a.learning[0].source = "[T1]"
q_a.learning[1].table = [["项目", "教材依据[T1]"]]
_resolve(q_a, tb, pm_refs=pm, extra_refs=q_a.references)
assert any(r.ref_type == "M" for r in q_a.references), "教材 [M] 条目丢失"
assert any(r.ref_type == "J" for r in q_a.references), "论文 [J] 条目丢失"
assert "[1]" in q_a.learning[0].body and "[2]" in q_a.learning[0].body, q_a.learning[0].body
assert "[1]" in q_a.learning[1].table[0][1], q_a.learning[1].table
assert "[T1]" not in q_a.learning[0].source and "《内科学》" in q_a.learning[0].source
print("11. 教材+论文引用解析 OK:", [(r.ref_type, r.title) for r in q_a.references])

# A historical bare T marker with no hit-to-book map must warn and must not guess.
legacy_q = _Question(qid="Q1", text="旧题", learning=[_LB(body="旧正文[1]。", source="[T1]")],
                     references=[_Ref(ref_type="J", title="Existing paper", journal="J", year="2024")])
_pl.sync_source_refs(_Case(questions=[legacy_q]))
assert legacy_q.citation_warning and "[T1]" in legacy_q.learning[0].source
assert not any(r.ref_type == "M" for r in legacy_q.references)
print("11b. 历史未知 [Tn] 不猜书目并显示告警 OK")

# Double-encoded tables (model squeezed a whole table into one row of list-string cells) must self-heal.
_bad_tbl = [["['并发症类型', '具体表现']",
             "[['呼吸衰竭', '呼吸困难、紫绀、意识障碍'], ['肺心病', '颈静脉怒张'], ['肺部感染', '发热']]"]]
healed = _LB(heading="（1）呼吸系统并发症", body="占位", table=_bad_tbl)
assert [r[0] for r in healed.table] == ["并发症类型", "呼吸衰竭", "肺心病", "肺部感染"], healed.table
assert healed.table[0] == ["并发症类型", "具体表现"] and all(len(r) == 2 for r in healed.table)
# Whole table double-encoded as one string, nested-list rows, and normal tables keep their shapes.
assert _LB(table="[['症状', '体征'], ['湿啰音', '中下野']]").table == [["症状", "体征"], ["湿啰音", "中下野"]]
assert _LB(table=[["症状", "体征"], ["湿啰音", "中下野"]]).table == [["症状", "体征"], ["湿啰音", "中下野"]]
assert _LB(table=[["项目", "依据[T1]"], ["肾功能", "eGFR"]]).table == [["项目", "依据[T1]"], ["肾功能", "eGFR"]]
# Bracketed prose like "[P30]" is not a list and must stay verbatim.
assert _LB(table=[["标注", "[P30]"]]).table == [["标注", "[P30]"]]
print("11c. 双重编码表格载入自愈 OK:", len(healed.table), "rows")

# Reordering does not change summary UID mapping or answer selection.
old_uid = q_a.uid
case_cite.questions.reverse(); case_cite.rename_qids()
assert case_cite.answer_question_ids == [old_uid]
assert old_uid in _pl.summary_question_order(case_cite.summary_nodes, case_cite)
print("12. 稳定题目 ID / 总结映射 / 作答选择 OK")

# Reorganizing retains matching answer/reference state and initializes only new questions.
new_list = [_Question(qid="Q1", text="慢性肾病诊断标准"),
            _Question(qid="Q2", text="新增的管理问题")]
merged = _merge_qs([q_a, q_b], new_list)
assert merged[0].uid == q_a.uid and merged[0].learning
assert merged[1].uid != q_a.uid and not merged[1].learning
print("13. 问题清单重整保留匹配题内容 OK")

# Draft summary is generated from the draft and remains unchanged when the question bank changes.
draft_case = _Case(case_text="病例占位", question_draft="机制问题\n诊断问题")
_orig_has_key = _pl.llm.has_key
_orig_chat_json = _pl.llm.chat_json
try:
    _pl.llm.has_key = lambda: True
    _pl.llm.chat_json = lambda *_a, **_k: _pl._SummaryOut(nodes=[
        {"text": "机制", "children": [{"text": "病因机制", "children": []}]},
        {"text": "诊断", "children": [{"text": "诊断标准", "children": []}]},
    ])
    ok, _msg = _pl.generate_draft_summary(draft_case, draft_case.question_draft)
    assert ok and len(draft_case.draft_summary_nodes) == 2
    saved_draft_nodes = [n.model_dump() for n in draft_case.draft_summary_nodes]
    draft_case.questions = [_Question(qid="Q1", text="用户新增题")]
    assert [n.model_dump() for n in draft_case.draft_summary_nodes] == saved_draft_nodes
    assert not draft_case.draft_summary_is_stale()
    ok, _msg = _pl.generate_questions_from_draft_summary(draft_case)
    assert ok and len(draft_case.questions) == 2
    assert [q.text for q in draft_case.questions] == ["病因机制", "诊断标准"]
    assert [n.model_dump() for n in draft_case.draft_summary_nodes] == saved_draft_nodes
    print("14. 草稿总结先行、题库按总结叶子原文生成且彼此独立 OK")
finally:
    _pl.llm.has_key = _orig_has_key
    _pl.llm.chat_json = _orig_chat_json

# Word keeps all prompts/summary, but omits answers outside the selected UID set.
select_case = _Case(title="选择导出测试", questions=[
    _Question(qid="Q1", text="题目甲", learning=[_LB(heading="甲", body="答案甲")]),
    _Question(qid="Q2", text="题目乙", learning=[_LB(heading="乙", body="答案乙")]),
], answer_question_ids=[])
select_case.draft_summary_nodes = [SummaryNode(text="独立总结", children=[SummaryNode(text="总结知识点")])]
select_docx = render_docx.build_docx(select_case, fmt, str(OUT / "_smoke_selected.docx"), None)
_sd = _D(select_docx)
_select_text = "\n".join(p.text for p in _sd.paragraphs)
for _tbl in _sd.tables:
    _select_text += "\n" + "\n".join(cell.text for row in _tbl.rows for cell in row.cells)
assert "题目甲" in _select_text and "题目乙" in _select_text and "总结知识点" in _select_text
assert "答案甲" not in _select_text and "答案乙" not in _select_text
print("15. Word 全量题目/总结 + 空作答选择 OK")
Path(select_docx).unlink(missing_ok=True)

# ---------------- 手动导入文献：引用格式解析 + PDF 分享生成
from pbl.literature import parse_citation_text, build_paper_share_from_manual
from pbl import literature as _lit_mod
cite = parse_citation_text(
    "Si M, Fu J, Fang M, et al. Lysosome-mediated aggregation of galactose-deficient IgA1"
    " with transferrin receptor 1 links to IgA nephropathy.[J]. Nature communications, 2025."
    " DOI: 10.1038/s41467-025-60812-3.")
assert "lysosome" in cite["title"].lower(), f"title parse fail: {cite}"
assert cite["journal"].lower() == "nature communications", f"journal: {cite}"
assert cite["year"] == "2025"
assert cite["doi"].startswith("10.1038/"), f"doi: {cite}"
assert len(cite["authors"]) >= 1
cite2 = parse_citation_text("Wang L, Zhang Y. A study on CKD.[J]. Kidney Int, 2024, 105(3): 456-467.")
assert cite2["journal"] == "Kidney Int"
assert cite2["volume"] == "105" and cite2["issue"] == "3"
assert cite2["pages"] == "456-467"
print("16. 引用格式解析 OK:", cite["title"][:50], "|", cite["journal"])

# Mock LLM to verify build_paper_share_from_manual produces PaperShare fields
_lit_orig_has_key = _pl.llm.has_key
_lit_orig_chat_json = _pl.llm.chat_json
try:
    _pl.llm.has_key = lambda: True
    _pl.llm.chat_json = lambda *_a, **_k: _lit_mod.PaperDraft(
        title_cn="溶酶体介导Gd-IgA1聚集",
        steps=[{"tag": "A", "text": "假设：TfR1结合Gd-IgA1"},
               {"tag": "B", "text": "方法：单细胞测序"},
               {"tag": "C", "text": "验证：体外实验"},
               {"tag": "D", "text": "因果：敲除验证"}],
        approach="研究思路与过程。", conclusion="核心结论。",
        case_link="与病例联系。", limitations="局限性。",
        section_label="机制探索", chosen=[])
    manual_case = _Case(case_text="IgA肾病患者。", question_draft="")
    paper, note = build_paper_share_from_manual(
        manual_case,
        "Si M, et al. Lysosome-mediated…[J]. Nature communications, 2025. DOI: 10.1038/…")
    assert paper is not None
    assert paper.title_cn == "溶酶体介导Gd-IgA1聚集"
    assert len(paper.steps) == 4
    assert paper.ref_text.startswith("Si M, et al.")
    assert paper.conclusion == "核心结论。"
    print("17. 手动导入文献分享生成 OK:", paper.title_cn)
finally:
    _pl.llm.has_key = _lit_orig_has_key
    _pl.llm.chat_json = _lit_orig_chat_json

# ---------------- 文献分享 PPT：一页速览版式
from pbl import render_pptx as _rp
onepage = _rp.build_literature_onepage(
    lit_case.ppt.paper, _rp.default_ppt_template(), str(OUT / "_smoke_lit_onepage.pptx"),
    author="演示")
_p = _P(onepage)
assert len(_p.slides) == 1, f"一页速览应为 1 页，实际 {len(_p.slides)}"
_sh = _p.slides[0].shapes
assert any(s.has_text_frame and "核心结论" in s.text_frame.text for s in _sh), "缺少核心结论框"
assert any(s.has_text_frame and "与病例的联系" in s.text_frame.text for s in _sh), "缺少病例联系框"
assert any(s.has_text_frame and "研究背景：演示研究背景" in s.text_frame.text
           for s in _sh), "研究背景未显示在速览页"
assert sum(1 for s in _sh if s.has_text_frame
           and s.text_frame.text.startswith(("A.", "B.", "C.", "D."))) == 4, "研究过程流程条不足 4 步"
_notes = _p.slides[0].notes_slide.notes_text_frame.text
assert "讲解思路" in _notes and "研究背景" in _notes, "备注缺少讲解思路/研究背景"
print("11. 文献分享一页速览 OK:", len(_sh), "个形状，备注", len(_notes), "字")

# ---------------- 病例分幕存档（第一/二幕 + Word 并入前幕）
act1 = demo.demo_case()
act1.series = "_smoke_series"
act1.act_no = 1
act1.act_title = "首次就诊"
act1.act_note = "首发血尿"
_p1 = acts.save_act(act1)
act2 = acts.make_next_act(act1)
act2.act_title = "病理回报"
act2.questions = act1.questions[:2]
_p2 = acts.save_act(act2)
_listed = acts.list_acts("_smoke_series")
assert [m["act_no"] for m in _listed] == [1, 2], f"分幕列表异常: {_listed}"
assert acts.next_act_no(act2) == 3
assert acts.load_act(_p1).act_title == "首次就诊"
act2.word_prev_acts = [1]
_prev = acts.prev_cases(act2)
assert [p.act_no for p in _prev] == [1], "前幕读取异常"
_prev_docx = render_docx.build_docx(
    act2, render_docx.WordFormat(include_literature=False),
    str(OUT / "_smoke_prev.docx"), None, prev_cases=_prev)
_txt2 = "\n".join(p.text for p in _D2(_prev_docx).paragraphs)
assert "附录：第一幕" in _txt2 and "问题解答（回顾）" in _txt2, "Word 前幕附录缺失"
_p1.unlink(missing_ok=True)
_p2.unlink(missing_ok=True)
assert not acts.list_acts("_smoke_series"), "测试存档未清理干净"
print("12. 病例分幕 OK: 存档/载入/下一幕/前幕并入 Word（附录", len(_txt2), "字）")

# ---------------- 导图：病例内容标注（==要点== -> 标黄/划线，且不单列分支）
mm_root = MindmapNode(text="IgA肾病", children=[
    MindmapNode(text="病因学", children=[
        MindmapNode(text="感染后黏膜免疫异常"),
        MindmapNode(text="本例：咽痛后 3 天出现肉眼血尿", in_case=True)]),
    MindmapNode(text="诊断及鉴别诊断", children=[
        MindmapNode(text="本例：尿蛋白 2+，血压 160/100", in_case=True)])])
_md2 = mindmap.node_to_markdown(mm_root)
assert "==本例：咽痛后 3 天出现肉眼血尿==" in _md2, "Markdown 未输出 == 标记"
_back2 = mindmap.markdown_to_node(_md2, root_text="IgA肾病")
assert mindmap.count_case_nodes(_back2) == 2, "病例标注往返丢失"
_png2 = mindmap.render_png(mm_root, str(OUT / "_smoke_mm_case.png"))
assert Path(_png2).stat().st_size > 5000
import io as _io
import json as _json
import zipfile as _zip
_raw = mindmap.node_to_xmind(mm_root)
with _zip.ZipFile(_io.BytesIO(_raw)) as _z:
    _sheet = _json.loads(_z.read("content.json").decode("utf-8"))
assert "svg:fill" in _json.dumps(_sheet, ensure_ascii=False), "XMind 未带病例着色"
assert "<mark>" in mindmap.node_to_markmap_html(mm_root), "HTML 未高亮病例内容"
print("13. 导图病例标注 OK: ==标记往返 2 处；PNG/XMind/HTML 均已着色")

# ---- ⑤ 区「导出前编辑」面板：表格网格读回 + 暂存应用回归
from pbl.pipeline import table_from_grid as _tfg, apply_export_edits as _aee
import math as _math

assert _tfg([["表头1", None], [3, "x"], [None, None], ["", "  "]]) == [["表头1", ""], ["3", "x"]], \
    "网格读回需剔除全空行并把 None/数字规整为字符串"
assert _tfg([[_math.nan, "x"]]) == [["", "x"]], "NaN（data_editor 空格）应转为空串"
assert _tfg(None) == [] and _tfg([]) == []

case_we = _Case(title="旧标题", keywords=["旧词"], questions=[
    _Question(qid="Q1", text="题一", clinical="旧临床",
              learning=[_LB(heading="块1", body="旧正文", source="[T1]",
                            table=[["项目", "值"], ["血常规", "异常"]]),
                        _LB(heading="块2", body="块二正文")]),
    _Question(qid="Q2", text="题二")])
case_we.draft_summary_nodes = [SummaryNode(text="旧主题", children=[])]
_aee(case_we, {"title": " 新标题 ", "keywords": "高血压；肾衰竭, 水肿", "summary_md": "新主题",
               "questions": {"Q1": {"text": "新题一", "clinical": "新临床",
                                    "blocks": [{"heading": "新块1", "source": "", "body": "新正文",
                                                "table": [["项目", "值"], ["血糖", "升高"], [None, None]]},
                                               {"heading": "越界块应被忽略"}]},
                             "Q9": {"text": "不存在的题"}}})
assert case_we.title == "新标题" and case_we.keywords == ["高血压", "肾衰竭", "水肿"]
assert case_we.questions[0].text == "新题一" and case_we.questions[0].clinical == "新临床"
_b1 = case_we.questions[0].learning[0]
assert _b1.heading == "新块1" and _b1.body == "新正文"
assert _b1.table == [["项目", "值"], ["血糖", "升高"]], _b1.table
assert case_we.questions[0].learning[1].body == "块二正文", "越界块编辑不应影响后续块"
assert case_we.questions[1].text == "题二" and "Q9" not in {q.qid for q in case_we.questions}, \
    "未提及/不存在的题目不应被修改"
assert case_we.draft_summary_nodes[0].text == "新主题"
assert case_we.draft_summary_signature == case_we.draft_summary_source_signature(), \
    "编辑总结后签名应刷新以免误报过期"
_aee(case_we, {"questions": {"Q2": {"clinical": "补充临床", "blocks": []}}})
assert case_we.questions[1].clinical == "补充临床"
print("14. 导出前编辑面板（表格网格读回 + 暂存应用）OK")

# ---- ⑤ 区 Word→PDF（本机 Word COM）+ 预览图回归（用测试 4 生成的 docx，未被清理）
from pbl import docx_preview as _dp

_pdf = _dp.export_pdf(docx)
assert _pdf and Path(_pdf).read_bytes()[:5] == b"%PDF-", f"Word COM 转 PDF 失败: {_pdf}"
assert Path(_pdf).stat().st_size > 5000
_pngs = _dp.export_pngs(docx, str(OUT / "_smoke_pv"))
assert _pngs and Path(_pngs[0]).stat().st_size > 1000, "重构后的 PNG 预览失败"
assert _dp.export_pdf(str(OUT / "不存在.docx")) == "", "缺失文件应返回空串"
print("15. Word→PDF（本机 Word COM）+ 预览图回归 OK:",
      Path(_pdf).stat().st_size, "bytes /", len(_pngs), "页")

# ---- 小组问题=用户原始清单（问题草稿逐行原文，含（选做）），个人总结叶子=问题原文
_gq = _Case(title="小组问题回归",
            question_draft="1.慢性支气管炎的病因、诱因\n2.吸烟对呼吸系统的影响\n14.慢性支气管炎的并发症（选做）",
            questions=[_Question(qid="Q1", text="慢性支气管炎的病因、诱因有哪些？",
                                 learning=[_LB(heading="块", body="正文")])],
            answer_question_ids=None)
_gq_docx = render_docx.build_docx(_gq, render_docx.WordFormat(), str(OUT / "_smoke_gq.docx"), None)
_gp = [p.text.strip() for p in _D(_gq_docx).paragraphs if p.text.strip()]
_group = [t for t in _gp[_gp.index("小组问题") + 1:_gp.index("个人总结")] if t.startswith("（")]
assert _group == ["（一）慢性支气管炎的病因、诱因", "（二）吸烟对呼吸系统的影响",
                  "（三）慢性支气管炎的并发症（选做）"], _group
# 草稿为空时回退题库
_gq2 = _Case(title="回退回归", questions=[_Question(qid="Q1", text="题库问题甲")])
_gq2_docx = render_docx.build_docx(_gq2, render_docx.WordFormat(), str(OUT / "_smoke_gq2.docx"), None)
assert "（一）题库问题甲" in [p.text.strip() for p in _D(_gq2_docx).paragraphs]
# 总结生成：叶子必须逐字保留问题原文（只去编号），不改写不截断
_orig_hk, _orig_cj = _pl.llm.has_key, _pl.llm.chat_json
try:
    _pl.llm.has_key = lambda: True
    _pl.llm.chat_json = lambda *_a, **_k: _pl._SummaryOut(nodes=[
        {"text": "诊断", "children": [{"text": "12.“无肌卫”的临床意义", "children": []}]},
        {"text": "其他", "children": [{"text": "13.“病理反射未引出”的临床意义", "children": []}]}])
    ok, _msg = _pl.generate_draft_summary(_gq, _gq.question_draft)
    assert ok, _msg
    _leaves = [t for t, _p in _pl._summary_question_leaves(_gq.draft_summary_nodes)]
    assert _leaves == ["“无肌卫”的临床意义", "“病理反射未引出”的临床意义"], _leaves
finally:
    _pl.llm.has_key, _pl.llm.chat_json = _orig_hk, _orig_cj
print("16. 小组问题保留原始清单 + 总结叶子逐字保留 OK")

# ---- llm 按次 provider/model 覆盖（纯逻辑，无网络）
from pbl import llm as _llm
assert _llm._resolve(None, None) == (_llm.PROVIDER, _llm.MODEL, False)
assert _llm._resolve("deepseek", None) == ("deepseek", "deepseek-chat", True)
assert _llm._resolve(None, "glm-4.6")[2] is True
assert isinstance(_llm.has_key_for("deepseek"), bool)
print("17. llm 按次 provider/model 覆盖 OK")

# ---- 海报 spec 生成（mock LLM）与规整
from pbl.pipeline import _PosterOut, generate_question_poster as _gqp
_orig_hk2, _orig_cj2 = _pl.llm.has_key, _pl.llm.chat_json
try:
    _pl.llm.has_key = lambda: True
    _pl.llm.chat_json = lambda *_a, **_k: _PosterOut(
        flow=["黏膜免疫异常→Gd-IgA1↑", "系膜沉积→补体激活", "足细胞损伤→蛋白尿", "", "多余步骤"],
        table={"headers": ["机制层", "药物", "作用机制", "关键证据"],
               "rows": [["上游", "布地奈德迟释", "抑制Gd-IgA1", "NefIgA：eGFR差+5.05"],
                        ["中游", "斯贝普仑", "抗APRIL", "VISIONARY"],
                        [], ["", "", "", ""],
                        ["上游", "短行", "x", "y"]]},
        note="口径说明占位", key_points=["要点一", "", "要点二", "要点三"])
    _qq = _Question(qid="Q1", text="治疗进展",
                    learning=[_LB(heading="机制", body="正文")],
                    references=[_Ref(ref_type="J", title="Trial paper", journal="J", year="2025")])
    _case_q = _Case(case_text="病例占位", questions=[_qq])
    spec, err = _gqp(_case_q, _qq)
    assert spec is not None, err
    assert spec["flow"] == ["黏膜免疫异常→Gd-IgA1↑", "系膜沉积→补体激活",
                            "足细胞损伤→蛋白尿", "多余步骤"]
    assert len(spec["table"]["rows"]) == 3 and len(spec["table"]["rows"][0]) == 4
    assert spec["table"]["rows"][0][0] == "上游" and spec["key_points"] == ["要点一", "要点二", "要点三"]
finally:
    _pl.llm.has_key, _pl.llm.chat_json = _orig_hk2, _orig_cj2
print("18. 海报 spec 生成与规整 OK")

# ---- 海报 PPT 渲染（表格/流程/要点/备注/参考文献）
_ps = {"flow": ["①机制A", "②机制B", "③机制C"],
       "table": {"headers": ["机制层", "药物", "作用机制", "关键证据"],
                 "rows": [["上游", "药物甲", "抑制X", "试验A：n=100"],
                          ["上游", "药物乙", "抑制Y", "试验B"],
                          ["下游", "药物丙", "阻断Z", "试验C：−38%"]]},
       "note": "完全批准已有硬终点；加速批准以替代终点为准",
       "key_points": ["要点一句一", "要点一句二"]}
_poster = render_pptx.build_poster_pptx(
    [{"title": "问题1：测试疾病的治疗进展", "spec": _ps, "refs_text": "[1] Test J. 2025."}],
    "林同学", render_pptx.default_ppt_template(), str(OUT / "_smoke_poster.pptx"))
from pptx import Presentation as _PPrs
_pp = _PPrs(_poster)
assert len(_pp.slides) == 1
_sl = _pp.slides[0]
_tbls = [sh for sh in _sl.shapes if sh.has_table]
assert len(_tbls) == 1 and len(_tbls[0].table.rows) == 4 and len(_tbls[0].table.columns) == 4
_arrows = [sh for sh in _sl.shapes if sh.shape_type == 1 and sh.name not in ("",)]
_notes = _sl.notes_slide.notes_text_frame.text
assert "人工核对" in _notes and "要点一句一" in _notes
_all_txt = "\n".join(sh.text_frame.text for sh in _sl.shapes if sh.has_text_frame)
assert "机制A" in _all_txt and "核心要点" in _all_txt and "人工核对" not in _all_txt
print("19. 海报式问题 PPT 渲染 OK: 1 页, 4x4 表格 + 流程链 + 要点块")

# ---- 导图提示词占位符 + 深层解析（含文献分享块）
_mm_spec = _pl._DiseaseMM(disease="IgA肾病（原发性）合并CKD 4期",
                          mindmap_md="- 病因学\n  - 黏膜免疫\n    - ==本例：感冒后血尿==\n"
                                     "- 发病机制\n  - 四重打击\n    - Gd-IgA1\n      - 沉积系膜区\n"
                                     "- 前沿：TfR1-溶酶体轴（Nat Commun 2025）\n  - 溶酶体破坏")
from pbl import mindmap as _mm
_node = _mm.markdown_to_node(_mm_spec.mindmap_md, root_text=_mm_spec.disease)
assert _node.text == "IgA肾病（原发性）合并CKD 4期"
assert _node.children[0].children[0].children[0].in_case, "==标记应转 in_case"
assert _node.children[2].text.startswith("前沿：")
_promoted = _pl.DISEASE_MM_PROMPT.format(case_text="x", qa="y", materials="（无）",
                                         paper="（无）", branches="a", requirement="z")
assert "前沿" in _promoted and "4~8" in _promoted
print("20. 导图提示词（4层/前沿分支/预防标注）+ 深层解析 OK")

# ---- 删除参考文献后引用标号重排
from pbl.pipeline import renumber_citations as _renum
_r1 = _Ref(ref_type="M", title="内科学", pages="23", publisher="人民卫生出版社", year="2024")
_r2 = _Ref(ref_type="J", title="Trial A", journal="J", year="2025")
_r3 = _Ref(ref_type="M", title="病理", pages="177", publisher="人民卫生出版社", year="2024")

_rq = _Question(qid="Q1", text="标号重排测试", clinical="临床依据[3]。",
                learning=[_LB(heading="块", body="甲[1]。乙[2]。丙[3]。再引[2]。悬空[5]。",
                              source="《内科学》（第10版） 第23页；《病理》（第10版） 第177页")])
_rq.references = [_r1, _r2, _r3]
_old = list(_rq.references)
_rq.references.pop(1)                     # 删掉第 2 条（期刊）——正文中两处 [2] 均指向它
_n = _renum(_rq, _old)
assert _rq.learning[0].body == "甲[1]。乙。丙[2]。再引。悬空。", _rq.learning[0].body
assert _rq.clinical == "临床依据[2]。"
assert _n >= 1 and len(_rq.references) == 2

_old = list(_rq.references)
_rq.references.pop(0)                     # 再删原第 1 条（教材 M，含出处段）
_renum(_rq, _old)
assert _rq.learning[0].body == "甲。乙。丙[1]。再引。悬空。", _rq.learning[0].body
assert _rq.learning[0].source == "《病理》（第10版） 第177页", _rq.learning[0].source
assert _rq.clinical == "临床依据[1]。"
print("21. 删除文献后引用标号重排 OK")

# ---- 参考文献每题上限 5 条硬约束
from pbl.pipeline import enforce_max_refs as _emr
_rq2 = _Question(qid="Q1", text="上限测试",
                 learning=[_LB(heading="块", body="甲[1]。乙[2]。丙[3]。丁[4]。戊[5]。己[6]。戊再引[5]。")])
_rq2.references = [_Ref(ref_type="J", title=f"Paper{i}", journal="J", year="2025") for i in range(1, 7)]
assert _emr(_rq2) == 1 and len(_rq2.references) == 5
assert _rq2.learning[0].body == "甲[1]。乙[2]。丙[3]。丁[4]。戊[5]。己。戊再引[5]。", _rq2.learning[0].body
assert _emr(_rq2) == 0 and len(_rq2.references) == 5, "限额内不应再动"
print("22. 参考文献每题上限 5 条硬约束 OK")

# ---- 相邻重复引用标注折叠（[1][1] → [1]；[1][2] 合法连挂不受影响）
from pbl.schema import collapse_adjacent_markers as _collapse, heal_tables as _heal
assert _collapse("甲[1][1]。乙[2][2][2]。丙[1][2]。丁[1]。") == "甲[1]。乙[2]。丙[1][2]。丁[1]。"
assert _collapse("") == "" and _collapse("无标注句子。") == "无标注句子。"
assert _pl.sanitize_content("甲[1][1]。") == "甲[1]。"
assert _pl._attach_marker("句尾已有标注[1]。", 1) == "句尾已有标注[1]。", "同号不应重复补"
assert _pl._attach_marker("句尾已有标注[1]。", 2) == "句尾已有标注[1][2]。", "异号连挂合法，补在句末标点前"
_heal_case = _Case(questions=[_Question(qid="Q1", text="折叠自愈",
                                         learning=[_LB(heading="块", body="甲[1][1]。乙[2]。")],
                                         clinical="临床[3][3]。")])
_heal(_heal_case)
assert _heal_case.questions[0].learning[0].body == "甲[1]。乙[2]。"
assert _heal_case.questions[0].clinical == "临床[3]。"
print("23. 相邻重复引用标注折叠 OK")

print("\n全部冒烟测试通过 ✅")
