"""PBL 案例 —— 单一事实源数据模型。

所有内容生成、二次修改、Word/PPT/思维导图渲染都围绕 Case 这份 JSON 进行。
按「问题 -> 字段」粒度组织，是局部重生成的结构基础。
"""
from __future__ import annotations

import ast
import re
from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

# 问题类型（用于提示词与界面下拉）
QUESTION_TYPES = ["机制", "诊断", "治疗", "用药", "人文", "预防", "其他"]

# 参考文献来源
REF_SOURCE_AUTO = "PubMed"
REF_SOURCE_MANUAL = "手动导入"


class Reference(BaseModel):
    """一条参考文献（GB/T 7714-2015）。

    ref_type: "J" 期刊论文 / "M" 专著（教材书籍）。
    教材条目的 authors/place/publisher/year 由 pbl/book_catalog.json 提供（可编辑）。
    """

    ref_type: str = "J"
    title: str = ""
    authors: list[str] = Field(default_factory=list)
    journal: str = ""           # J: 刊名
    place: str = ""             # M: 出版地
    publisher: str = ""         # M: 出版社
    edition: str = ""           # M: 版本，如 "10版"
    year: str = ""
    volume: str = ""            # J: 卷
    issue: str = ""             # J: 期
    pages: str = ""             # J: 页码区间 / M: 引用页码
    pmid: str = ""
    doi: str = ""
    abstract: str = ""          # 检索到的摘要，仅供后续生成参考，不进正文
    relevance: str = ""         # LLM 写的一句话推荐语
    source: str = REF_SOURCE_MANUAL


def _parse_list_literal(s: str):
    """尝试把 "['a', 'b']" 形态的字符串解析成 list；失败或非列表返回 None。"""
    s = (s or "").strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    try:
        v = ast.literal_eval(s)
    except Exception:
        return None
    return v if isinstance(v, list) else None


def _rows_from_list(v) -> list[list[str]]:
    """解析出的列表 → 行序列：全标量的扁平列表视为一行，嵌套结构递归规整。"""
    if not isinstance(v, list) or not v:
        return []
    if all(isinstance(x, (str, int, float)) for x in v):
        return [[str(x) for x in v]]
    return coerce_table(v)


def coerce_table(value) -> list[list[str]]:
    """把模型输出的 table 字段规范化为字符串二维数组；无法识别时返回空表。

    已知畸形：{"content": "..."} / {"rows": [...]} / 字符串 / 单层列表，
    以及双重编码——整表被压成一行，每个格子又是一个列表字符串
    （如 ["['并发症类型', '具体表现']", "[['呼吸衰竭', '喘促'], …]"]）。
    """
    if not value:
        return []
    if isinstance(value, str):
        # 整表被编码成一个列表字符串："[['a','b'],['c','d']]"
        parsed = _parse_list_literal(value)
        return _rows_from_list(parsed) if parsed is not None else []
    if isinstance(value, dict):
        # dict 形态：rows/data 直接用列表；content/text 按行拆成单列表格
        for key in ("rows", "data"):
            nested = value.get(key)
            if isinstance(nested, list) and nested:
                return coerce_table(nested)
        text = ""
        for key in ("content", "text", "body"):
            if key in value:
                text = str(value[key])
                break
        if not text:
            return []
        rows = [line.strip() for line in text.split("\n") if line.strip()]
        return [[row] for row in rows] if rows else []
    if not isinstance(value, list):
        return []
    out: list[list[str]] = []
    for row in value:
        if isinstance(row, list):
            nonempty = [c for c in row if c is not None and str(c).strip()]
            list_like = [c for c in nonempty
                         if isinstance(c, list)
                         or (isinstance(c, str) and _parse_list_literal(c) is not None)]
            if nonempty and list_like and len(list_like) == len(nonempty):
                # 双重编码行：整表被压成一行，逐格还原为多行（表头格在前、表体格在后）
                for cell in nonempty:
                    parsed = cell if isinstance(cell, list) else _parse_list_literal(cell)
                    out.extend(_rows_from_list(parsed))
            else:
                cells = [str(c) if c is not None else "" for c in row]
                if any(c.strip() for c in cells):
                    out.append(cells)
        elif isinstance(row, dict):
            # 单行 dict：取值拼成一行
            cells = [str(v) for v in row.values() if v is not None and str(v).strip()]
            if cells:
                out.append(cells)
        elif isinstance(row, str) and row.strip():
            parsed = _parse_list_literal(row)
            if parsed is not None:
                out.extend(_rows_from_list(parsed))
            else:
                out.append([row.strip()])
        # 其他类型忽略
    return out


class LearningBlock(BaseModel):
    """学习内容的一个小节。source 为教材出处；table 为可选表格（首行表头，适合对比类内容）。"""

    heading: str = ""
    body: str = ""
    source: str = ""
    table: list[list[str]] = Field(default_factory=list)

    @field_validator("table", mode="before")
    @classmethod
    def _fix_table(cls, v):
        """模型双重编码或历史坏数据在载入时自愈；规整失败则原样交给 pydantic 校验。"""
        try:
            return coerce_table(v)
        except Exception:
            return v


_ADJ_DUP = re.compile(r"\[(\d+)\]\[\1\]")


def collapse_adjacent_markers(text: str) -> str:
    """相邻重复引用标注折叠：[1][1] → [1]（同处连挂同一文献只保留一个）。

    不同文献连挂（[1][2]）是合法的，不受影响；[1][1][1] 迭代折叠到只剩一个。
    """
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = _ADJ_DUP.sub(r"[\1]", text)
    return text


def heal_tables(case: "Case") -> None:
    """就地修复已载入会话的旧案例里的数据瑕疵。

    model_validate 只在载入 JSON 时跑校验器；session_state 里已存在的
    Case 对象不会再被校验，导出前需经此函数补一次（幂等，正常内容不动）。
    修复项：双重编码表格；相邻重复引用标注 [1][1]。
    """
    for q in getattr(case, "questions", None) or []:
        for lb in getattr(q, "learning", None) or []:
            old = lb.table
            if old:
                try:
                    new = coerce_table(old)
                except Exception:
                    new = None
                if new and new != old:
                    lb.table = new
            if lb.body:
                fixed = collapse_adjacent_markers(lb.body)
                if fixed != lb.body:
                    lb.body = fixed
        if getattr(q, "clinical", ""):
            fixed = collapse_adjacent_markers(q.clinical)
            if fixed != q.clinical:
                q.clinical = fixed


class MindmapNode(BaseModel):
    text: str
    children: list["MindmapNode"] = Field(default_factory=list)
    in_case: bool = False       # 该要点来自本病例（导图里标黄/划线，不单列「结合病例」分支）


class SummaryNode(BaseModel):
    """个人总结目录节点；question_ids 是稳定映射，qids 仅兼容旧案例。"""

    text: str = ""
    qids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(default_factory=list)
    children: list["SummaryNode"] = Field(default_factory=list)


class Question(BaseModel):
    uid: str = Field(default_factory=lambda: uuid4().hex)
    qid: str                                    # Q1、Q2……（显示编号，可重排）
    text: str
    qtype: str = "其他"
    en_keywords: str = ""                       # 英文检索词，供 PubMed 使用
    cn_keywords: str = ""                       # 中文检索式，供知网手动检索
    learning: list[LearningBlock] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    clinical: str = ""                          # 临床应用（结合病例）
    review_note: str = ""                       # 审校智能体最近一次的修改说明
    citation_warning: str = ""                   # 引用来源无法自动恢复时的可见提示


class SlideOutline(BaseModel):
    """PPT 大纲中的一页。layout 决定渲染版式。"""

    layout: str = "bullets"     # cover / case / overview / question / clinical / refs / summary
    title: str = ""
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)
    body: str = ""


class LitCandidate(BaseModel):
    """文献分享的候选研究型文献（均来自 PubMed 真实检索）。"""

    pmid: str = ""
    pmcid: str = ""
    doi: str = ""
    title: str = ""             # 英文原标题
    title_cn: str = ""          # 中文标题（AI 翻译）
    authors: list[str] = Field(default_factory=list)
    journal: str = ""
    year: str = ""
    if_value: float = 0.0       # 影响因子（内置近年参考值，0=未收录）
    abstract: str = ""
    intro: str = ""             # 文献简介（中文，AI 生成）
    innovation: str = ""        # 创新点（中文，AI 生成）
    oa: bool = False            # 是否可获取开放全文图片（PMC OA）
    pub_types: list[str] = Field(default_factory=list)


class PaperFigure(BaseModel):
    """文献分享的示意图：来自开放全文的原始图片（可含可选的局部块）。"""

    file: str = ""              # 当前选用的图片路径（整图或某个局部块）
    label: str = ""             # Figure 1 等
    caption: str = ""           # 原图注（英文，截断）
    note: str = ""              # 中文说明：图中讲了什么、证明了什么
    selected: bool = False
    panels: list[dict] = Field(default_factory=list)  # [{panel, file}] 可选的局部块
    whole_file: str = ""        # 整图路径（选择局部后仍可切回）


class PaperShare(BaseModel):
    """一篇文献的分享内容（二次修改对象）。"""

    pmid: str = ""
    title_en: str = ""
    title_cn: str = ""
    authors: list[str] = Field(default_factory=list)
    journal: str = ""
    year: str = ""
    if_value: float = 0.0
    ref_text: str = ""          # GB/T 7714 引文（页面底部出处）
    background: str = ""        # 研究背景（可空：一页速览版式默认省略）
    approach: str = ""          # 研究思路/过程（整段文字，供 Word 与文字版 PPT 用）
    conclusion: str = ""        # 核心结论（可空）
    case_link: str = ""         # 与病例的联系
    limitations: str = ""       # 局限性（可空）
    # 研究过程 A→B→C→D：PPT 底部流程条与 Word「研究过程」共用
    steps: list[dict] = Field(default_factory=list)   # [{tag:"A", text:"假设：…"}]
    section_label: str = "机制探索"   # 图片页小标签
    figures: list[PaperFigure] = Field(default_factory=list)


class LitFilter(BaseModel):
    """文献检索的筛选条件（④ 文献检索区）。"""

    year_mode: str = "recent"   # recent=近 N 年 / range=自定义起止年 / all=不限
    recent_years: int = 10
    year_from: int = 0
    year_to: int = 0
    if_min: float = 0.0         # 影响因子下限（0=不限；未收录 IF 的期刊会被下限排除）
    if_max: float = 0.0         # 影响因子上限（0=不限）
    doc_type: str = "research"  # research=研究型 / review=综述 / all=全部
    oa_only: bool = False       # 只保留可自动取图的开放获取文献

    def window(self) -> tuple[str, str, str]:
        """返回 (mindate, maxdate, 说明)。日期串形如 '2016'、'2026'。"""
        import datetime
        this_year = datetime.date.today().year
        if self.year_mode == "all":
            return "1900", str(this_year + 1), "不限年份"
        if self.year_mode == "range":
            lo = self.year_from or (this_year - 10)
            hi = self.year_to or this_year
            return str(lo), str(hi + 1), f"{lo}–{hi} 年"
        n = max(1, int(self.recent_years or 10))
        return str(this_year - n), str(this_year + 1), f"近 {n} 年"

    def if_text(self) -> str:
        lo, hi = self.if_min or 0, self.if_max or 0
        if lo and hi:
            return f"IF {lo:g}–{hi:g}"
        if lo:
            return f"IF ≥ {lo:g}"
        if hi:
            return f"IF ≤ {hi:g}"
        return "IF 不限"


class PptState(BaseModel):
    """⑤区 PPT 制作的持久状态。"""

    template: str = ""                       # 当前选用的模板文件名
    author: str = ""                         # 制作人署名（问题页标题后）
    direction: str = ""                      # 文献方向，如 发病机制
    lit_prompt: str = ""                     # 用户的检索提示词
    candidates: list[LitCandidate] = Field(default_factory=list)
    paper: PaperShare | None = None
    lit_filter: LitFilter = Field(default_factory=LitFilter)


class Case(BaseModel):
    title: str = "未命名 PBL 案例"
    course: str = "临床医学"
    audience: str = "本科生"
    case_text: str = ""
    question_draft: str = ""             # 草稿输入：先生成独立个人总结
    draft_summary_nodes: list[SummaryNode] = Field(default_factory=list)  # 独立于后续题库
    draft_summary_signature: str = ""
    answer_question_ids: list[str] | None = None  # None 用于旧案例迁移；加载后默认全选
    # ---- 病例分幕：同一病例按第一幕/第二幕/第三幕推进，各幕完整存档在 cases/acts/
    series: str = ""                 # 系列标识（同一病例的各幕共用，空则按标题生成）
    act_no: int = 1                  # 第几幕
    act_title: str = ""              # 本幕标题，如「首次就诊」「病理回报」
    act_note: str = ""               # 本幕新增信息摘要（写给自己/老师看）
    word_prev_acts: list[int] = Field(default_factory=list)  # 要并入 Word 的前幕幕号
    # ---- 思维导图：每位老师的要求不同，这里保存上一次用过的要求与分支框架
    mm_requirement: str = ""
    mm_branches: list[str] = Field(default_factory=list)
    mm_connector: str = "xmind"      # 连线样式：xmind/spine/curve/straight/none
    questions: list[Question] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)   # 案例关键词，填入模板「关键词」栏
    # 学习内容参考文献类型：all / research / review（只影响 ② 生成时的 PubMed 检索）
    ref_doc_type: str = "all"
    # 个人总结：按主题归并的层级目录（Word 中呈 1. / 1.1 / 1.2）
    summary_nodes: list[SummaryNode] = Field(default_factory=list)
    summary_question_signature: str = ""  # 题目集合/文本签名；变化后提示重做总结
    word_follow_summary: bool = True   # Word「问题解答」是否按个人总结的层级顺序排列
    # 以疾病为中心的总结性知识导图（PBL 最后一节课结束后单独生成）
    disease_mindmap: MindmapNode | None = None
    ppt_outline: list[SlideOutline] = Field(default_factory=list)
    ppt: PptState = Field(default_factory=PptState)
    updated: str = ""

    @model_validator(mode="after")
    def migrate_question_identity(self) -> "Case":
        """旧案例补稳定 ID，并将总结里的旧 QID 映射到稳定 ID。"""
        by_qid = {q.qid: q.uid for q in self.questions}
        by_uid = {q.uid: q for q in self.questions}
        valid_ids = set(by_uid)

        def migrate_nodes(nodes: list[SummaryNode]) -> None:
            for node in nodes:
                migrate_nodes(node.children)
                if node.children:
                    node.question_ids = []
                elif not node.question_ids and node.qids:
                    node.question_ids = [by_qid[qid] for qid in node.qids if qid in by_qid]
                else:
                    node.question_ids = [uid for uid in node.question_ids if uid in valid_ids]
                node.qids = [by_uid[uid].qid for uid in node.question_ids if uid in by_uid]

        migrate_nodes(self.summary_nodes)
        if "question_draft" not in self.model_fields_set and self.questions:
            self.question_draft = "\n".join(q.text for q in self.questions)
        if self.summary_nodes and not self.summary_question_signature:
            self.summary_question_signature = "legacy-unverified"
        if self.answer_question_ids is None:
            self.answer_question_ids = [q.uid for q in self.questions]
        else:
            self.answer_question_ids = list(dict.fromkeys(
                uid for uid in self.answer_question_ids if uid in valid_ids))
        return self

    def touch(self) -> None:
        self.updated = datetime.now().strftime("%Y-%m-%d %H:%M")

    def question(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.qid == qid), None)

    def question_by_uid(self, uid: str) -> Question | None:
        return next((q for q in self.questions if q.uid == uid), None)

    def question_signature(self) -> str:
        payload = "\n".join(sorted(f"{q.uid}\t{q.text.strip()}" for q in self.questions))
        return sha256(payload.encode("utf-8")).hexdigest()

    def draft_summary_source_signature(self) -> str:
        return sha256(f"{self.case_text}\n{self.question_draft}".encode("utf-8")).hexdigest()

    def draft_summary_is_stale(self) -> bool:
        return bool(self.draft_summary_nodes and self.draft_summary_signature
                    and self.draft_summary_signature != self.draft_summary_source_signature())

    @property
    def personal_summary_nodes(self) -> list[SummaryNode]:
        return self.draft_summary_nodes or self.summary_nodes

    def summary_is_stale(self) -> bool:
        return bool(self.summary_nodes and self.summary_question_signature
                    and (self.summary_question_signature == "legacy-unverified"
                         or self.summary_question_signature != self.question_signature()))

    def rename_qids(self) -> None:
        for i, q in enumerate(self.questions, 1):
            q.qid = f"Q{i}"

        def sync(nodes: list[SummaryNode]) -> None:
            for node in nodes:
                if node.question_ids:
                    node.qids = [q.qid for uid in node.question_ids
                                 if (q := self.question_by_uid(uid)) is not None]
                sync(node.children)

        sync(self.summary_nodes)
        if self.answer_question_ids is not None:
            valid = {q.uid for q in self.questions}
            self.answer_question_ids = [uid for uid in self.answer_question_ids if uid in valid]


MindmapNode.model_rebuild()
SummaryNode.model_rebuild()
