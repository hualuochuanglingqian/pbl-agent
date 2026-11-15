"""生成流水线：问题整理 -> 双路检索 -> 逐问题生成 -> 字段级局部重生成。

设计铁律：
1. 参考文献条目只来自 PubMed 真实返回（LLM 只返回"选了哪几条 + 推荐语"）；
2. 所有 LLM 产出先经 Pydantic 校验，失败自动重试；
3. 无 API Key 时各步骤提供演示降级，界面永不中断。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from . import llm, pubmed, textbook
from .schema import (QUESTION_TYPES, REF_SOURCE_AUTO, Case, LearningBlock,
                     MindmapNode, Question, Reference, SummaryNode,
                     collapse_adjacent_markers, coerce_table)

MAX_REFS = 5  # 每个问题的参考文献上限

_BRACKETS = re.compile(r"[【】]")
# 行首叠加编号，如「3.（3）」「3、（3）」-> 保留（3）
_COMBI_NUM = re.compile(r"^(\s*)(?:\d{1,2}\s*[.、．]\s*)+(（\s*\d+\s*）|[(]\s*\d+\s*[)])")


def _attach_marker(text: str, n: int) -> str:
    """在句末标点前补一个引用标注（仅用于文献表兜底，保证文献表与正文一一对应）。

    句末已有同一编号的标注时不重复补（避免 [1][1]）。
    """
    s = (text or "").rstrip()
    if not s:
        return text
    if s.endswith(f"[{n}]") or s.endswith(f"[{n}]。"):
        return text
    if s.endswith("。"):
        return s[:-1] + f"[{n}]。"
    if s[-1] in "!！?？；;：:）)":
        return s[:-1] + f"[{n}]" + s[-1]
    return s + f"[{n}]"


def _coerce_table(value) -> list[list[str]]:
    """表格规整统一走 schema.coerce_table（含双重编码自愈，载入旧案例时同样生效）。"""
    return coerce_table(value)


def sanitize_content(text) -> str:
    """内容消毒：禁用【】；行首不得叠加两级编号（3.（3）->（3））；
    相邻重复引用标注折叠（[1][1] -> [1]）。

    容忍模型畸形输出：body 为 None 返回空串；为列表时按行拼接。
    """
    if text is None:
        return ""
    if isinstance(text, list):
        text = "\n".join(str(x) for x in text if x is not None)
    elif not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    out = _BRACKETS.sub("", text)
    out = "\n".join(_COMBI_NUM.sub(r"\1\2", line) for line in out.split("\n"))
    out = collapse_adjacent_markers(out)
    return out


# 发给模型的病例文本上限（glm-4-flash / deepseek 上下文足够，超长时提醒用户精简）
CASE_TEXT_LIMIT = 6000


def _clip_str(text: str, n: int = CASE_TEXT_LIMIT) -> str:
    return text[:n]


def _clip_case(case: Case) -> str:
    return _clip_str(case.case_text)

_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "pbl.log"


def _log(msg: str) -> None:
    """诊断日志：logs/pbl.log（生成失败时先看这里）。"""
    try:
        _LOG_FILE.parent.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _LOG_FILE.open("a", encoding="utf-8").write(f"[{stamp}] {msg}\n")
    except OSError:
        pass

# ---------------------------------------------------------------- 问题整理
class _Organized(BaseModel):
    questions: list[dict]  # {text, qtype, en_keywords, cn_keywords}


_HUMAN_STYLE = """表达规范（学术自然化，按学生正常写作习惯行文）：
1. 语域克制准确：不用口语、网络语、修辞比喻、感叹语气；不写"具有重要的临床意义""提供了新的思路""值得进一步研究""意义深远"这类空泛评价——改为具体所指（对哪类患者、改变了哪项决策），给不出就删；
2. 句长要有落差：长句短句交错，至少一处简短判断句、一处结构完整的长复句；避免通篇同一句式；不要每段都用"概括句+几条并列支撑"的骨架，段落可长短不一，允许单句成段；
3. 衔接从简："首先/其次/再次/最后/此外/与此同时/综上所述"这类路标词总量要少，多用事理顺序、指代与因果连词（因、故、由此可见）自然衔接；
4. 术语全文统一：同一概念只用一个词（如统一"患者"，不混用"病人/病患"；统一"疗效"，不混用"效果/作用"）；
5. 去名词化：不写"进行…的分析""作出…的判断""实现对…的提升"，直接用动词；
6. 并列与对偶句式要节制：不连续使用"不仅…而且…""既…又…"；
7. 不堆砌无信息限定语（"在一定程度上""相对而言"）；用"显著"必须带具体数值；
8. 每句话都要有实际信息，不写"由此可见其重要性"式空句。"""


ORGANIZE_PROMPT = """你是一名医学教育专家，正在准备 PBL（Problem-Based Learning）教案。

【病例】
{case_text}

【教师给出的问题草稿】
{draft}

{scope_rule}
类型只能取：{types}。为每个问题生成 PubMed 英文检索词（2~4 个核心关键词或短语，
用逗号分隔，不要整句）与中文文献检索式（适合中国知网的核心词，空格分隔，2~4 个词）。

只输出 JSON，格式：
{{"questions": [{{"text": "问题文本", "qtype": "机制", "en_keywords": "a, b, c", "cn_keywords": "词1 词2"}}]}}"""


_SCOPE_STRICT = """【整理要求（严格遵守）】
1. 你的任务只是"整理"，不是重新出题：上方草稿列出的每一个问题都必须保留；
2. 不得新增草稿中没有提到的问题；不得删除问题（完全重复的除外）；
3. 问题文字尽量原样保留：只允许修正错别字与标点，不得改变原意、不得扩写或概括；
4. 按"基础机制 -> 诊断 -> 治疗/用药 -> 人文/预防"的学习递进重新排序。"""

_SCOPE_DESIGN = """【整理要求】
草稿为空：请根据病例本身设计 10 个左右（8~12 个）覆盖核心知识点的 PBL 问题，
按"基础机制 -> 诊断 -> 治疗/用药 -> 人文/预防"的学习递进排序。"""


def _bigrams(s: str) -> set[str]:
    s = "".join(ch for ch in s if ch.isalnum())
    if len(s) >= 2:
        return {s[i:i + 2] for i in range(len(s) - 1)}
    return {s} if s else set()


def _prune_added_questions(draft: str, qs: list[dict]) -> list[dict]:
    """草稿非空时的程序兜底：整理结果不得超过草稿条数；
    超出部分视为 AI 新增，按与草稿文字的重合度保留最相近的几条。"""
    lines = [draft]
    for sep in (chr(10), "；", ";"):
        lines = [y for x in lines for y in x.split(sep)]
    n = len([x for x in lines if x.strip()])
    if n == 0 or len(qs) <= n:
        return qs
    d = _bigrams(draft)
    scored = []
    for q in qs:
        tt = _bigrams(str(q.get("text", "")))
        ov = len(tt & d) / len(tt) if tt else 0.0
        scored.append((ov, q))
    scored.sort(key=lambda x: -x[0])
    return [q for _, q in scored[:n]]


def organize_questions(case_text: str, draft: str) -> tuple[list[Question], bool, str]:
    """整理问题清单。返回 (问题列表, 是否成功, 说明)。

    无 Key 时给演示整理（按行拆分）；有 Key 但模型调用失败时返回空列表并说明原因，
    不再静默降级——由界面提示重试，避免用户误把演示内容当成正式结果。
    """
    has_draft = bool((draft or "").strip())
    prompt = ORGANIZE_PROMPT.format(
        case_text=_clip_str(case_text, 6000), draft=draft or "（无）",
        types="、".join(QUESTION_TYPES),
        scope_rule=_SCOPE_STRICT if has_draft else _SCOPE_DESIGN)
    result = llm.chat_json(prompt, _Organized)
    if result is None:
        if not llm.has_key():
            return _demo_organize(draft), True, "（演示模式：未配置 Key，直接按行拆分）"
        _log("organize_questions 失败：" + (llm.last_error() or "模型未返回有效内容"))
        return [], False, "模型调用未返回有效内容，请重试" + llm.error_hint() + "；详见 logs/pbl.log"
    out = []
    qs = [q for q in result.questions if str(q.get("text", "")).strip()]
    if has_draft:
        qs = _prune_added_questions(draft, qs)
    for i, q in enumerate(qs, 1):
        out.append(Question(
            qid=f"Q{i}",
            text=q.get("text", f"问题{i}").strip(),
            qtype=q.get("qtype", "其他") if q.get("qtype") in QUESTION_TYPES else "其他",
            en_keywords=q.get("en_keywords", ""),
            cn_keywords=q.get("cn_keywords", ""),
        ))
    return out, True, ""


_CN_NUMS = "一二三四五六七八九十"
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _strip_leading_number(t: str) -> str:
    """去掉问题行首的编号（1. / （1） / ① / 问题一： 等），其余文字原样保留。"""
    t = t.strip()
    for _ in range(2):
        t2 = t
        if t2.startswith("问题"):
            t2 = t2[2:].lstrip()
        # 行首括号编号：（1） / (1)
        if t2[:1] in "（(":
            k = 1
            while k < len(t2) and t2[k].isdigit():
                k += 1
            if k > 1 and k < len(t2) and t2[k] in "）)":
                t2 = t2[k + 1:].strip()
                t = t2
                continue
        # 行首数字/中文数字/圈号 + 分隔符
        i = 0
        while i < len(t2) and (t2[i].isdigit() or t2[i] in _CN_NUMS or t2[i] in _CIRCLED):
            i += 1
        if i > 0:
            j = i
            while j < len(t2) and t2[j] in " .、．)）:：":
                j += 1
            if j > i and j < len(t2):
                t2 = t2[j:].strip()
        t = t2
    return t


def parse_questions_asis(draft: str) -> list[Question]:
    """无需整理模式：问题按原文逐行解析（仅去除行首编号），顺序原样保留、不改写。"""
    lines: list[str] = []
    for raw in (draft or "").splitlines():
        t = _strip_leading_number(raw)
        if t:
            lines.append(t)
    if len(lines) < 2:
        alt = [_strip_leading_number(x) for x in re.split("[；;]", draft or "") if x.strip()]
        if len(alt) > len(lines):
            lines = alt
    return [Question(qid=f"Q{i}", text=t) for i, t in enumerate(lines, 1)]


def _question_match_key(text: str) -> str:
    text = re.sub(r"^\s*(?:问题\s*)?(?:[Qq]\s*)?\d+\s*[:：.、)）-]*\s*", "", text or "")
    normalized = re.sub(r"[\W_]+", "", text.lower(), flags=re.UNICODE)
    for particle in ("的", "与", "和", "及"):
        normalized = normalized.replace(particle, "")
    return normalized


def merge_question_bank(old: list[Question], proposed: list[Question]) -> list[Question]:
    """Preserve content only for exact normalized question matches; never guess identities."""
    buckets: dict[str, list[Question]] = {}
    for q in old:
        buckets.setdefault(_question_match_key(q.text), []).append(q)
    merged: list[Question] = []
    for i, incoming in enumerate(proposed, 1):
        key = _question_match_key(incoming.text)
        matches = buckets.get(key) or []
        if matches:
            prior = matches.pop(0)
            prior.text = incoming.text
            prior.qtype = incoming.qtype
            if incoming.en_keywords:
                prior.en_keywords = incoming.en_keywords
            if incoming.cn_keywords:
                prior.cn_keywords = incoming.cn_keywords
            merged.append(prior)
        else:
            incoming.qid = f"Q{i}"
            merged.append(incoming)
    for i, q in enumerate(merged, 1):
        q.qid = f"Q{i}"
    return merged


class _AsisKW(BaseModel):
    items: list[dict] = Field(default_factory=list)


_ASIS_KW_PROMPT = """你是一名医学文献检索专家。下面是 PBL 病例和教师已排好的问题清单——
问题文字不需要任何修改。请只为每个问题生成检索词：
- en_keywords：PubMed 英文检索词（2~4 个核心关键词或短语，英文逗号分隔，不要整句）
- cn_keywords：中文文献检索式（适合中国知网的核心词，空格分隔，2~4 个词）

【病例】
{case_text}

【问题清单】
{questions}

只输出 JSON：{{"items":[{{"i":1,"en_keywords":"","cn_keywords":""}}]}}"""


def fill_question_keywords(case: Case) -> None:
    """无需整理模式：仅为问题补充检索词（不改动问题文字）。失败静默跳过。"""
    if not llm.has_key() or not case.questions:
        return
    qs = chr(10).join(f"{i}. {q.text}" for i, q in enumerate(case.questions, 1))
    result = llm.chat_json(_ASIS_KW_PROMPT.format(
        case_text=_clip_str(case.case_text, 3000), questions=qs[:3000]), _AsisKW)
    if result is None:
        return
    for item in result.items:
        try:
            idx = int(item.get("i", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(case.questions):
            q = case.questions[idx - 1]
            q.en_keywords = str(item.get("en_keywords", "")).strip()
            q.cn_keywords = str(item.get("cn_keywords", "")).strip()


def ensure_question_keywords(case: Case, q: Question) -> bool:
    """问题缺检索词时自动补（供 PubMed 检索）；成功返回 True。"""
    if (q.en_keywords or "").strip() or not llm.has_key():
        return bool((q.en_keywords or "").strip())
    result = llm.chat_json(_ASIS_KW_PROMPT.format(
        case_text=_clip_str(case.case_text, 2000), questions=f"1. {q.text}"), _AsisKW)
    if result and result.items:
        it = result.items[0]
        q.en_keywords = str(it.get("en_keywords", "")).strip()
        cn = str(it.get("cn_keywords", "")).strip()
        if cn:
            q.cn_keywords = cn
    return bool((q.en_keywords or "").strip())


class _CiteFix(BaseModel):
    learning: list[dict] = Field(default_factory=list)
    ref_indexes: list[int] = Field(default_factory=list)
    ref_notes: list[dict] = Field(default_factory=list)


CITE_FIX_PROMPT = """你是医学文献编辑。下面是一段 PBL 学习内容（含 [T编号] 等来源标注）和一批
PubMed 真实文献。学习内容没有引用任何文献，请补齐：

任务：
1. 从文献中选出与内容最相关的 2~3 条；
2. 把这些文献的编号插入到内容中**合适句子**的句末（紧贴句末标点之前，如 …的表现[P2]），
   每条选中文献至少标注一次；
3. 除插入 [P编号] 外，heading/body/table/source 必须与原文完全一致（保留原有的 [T编号] 标注），
   不得改写、增删任何文字；确实无从插入的文献不要选。

【学习内容】
{learning}

【PubMed 文献】（编号 P1..Pn）
{pm}

只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}],
"ref_indexes":[1],"ref_notes":[{{"i":1,"relevance":""}}]}}"""


DIGEST_PROMPT = """你是幻灯片编辑。把下面的 PBL 学习内容压缩成适合一页幻灯片展示的要点版。

【问题】{q_text}
【学习内容】
{learning}

要求：
1. 保留 2~4 个小节，标题措辞可精简；
2. 每小节保留 2~4 条最关键要点，每条独占一行、不超过 40 字，层级用（1）（2）、①②；
3. **保留正文中的 [数字] 引用标注**（与参考文献一一对应），标注紧贴句末标点前；
4. 全部正文合计不超过 13 行；只做删减压缩，不得虚构、不得新增知识点；
5. 每小节的 source 字段原样保留（教材出处，供参考文献对账）；
5. 表格仅 3 行以内时保留，否则改写为文字要点。

只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}]}}"""


class _DigestOut(BaseModel):
    learning: list[dict] = Field(default_factory=list)


def digest_for_slides(case: Case, q: Question) -> tuple[list[LearningBlock], str]:
    """PPT 精简模式：把学习内容压缩成一页幻灯片版（保留引用标注）。"""
    if not llm.has_key() or not q.learning:
        return q.learning, ""
    learning = chr(10).join(f"【{b.heading}】{b.body}" for b in q.learning)
    result = llm.chat_json(DIGEST_PROMPT.format(q_text=q.text, learning=learning[:4000]),
                           _DigestOut)
    if result is None or not result.learning:
        return q.learning, "精简失败，已用原始内容"
    out = [LearningBlock(heading=sanitize_content(b.get("heading", "")),
                         body=sanitize_content(b.get("body", "")),
                         source=b.get("source", ""), table=_coerce_table(b.get("table")))
           for b in result.learning if b.get("body") or b.get("table")]
    return (out, "已精简") if out else (q.learning, "精简结果为空，已用原始内容")


# ---------------------------------------------------------------- 海报式单页问题 PPT
class _PosterTable(BaseModel):
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


class _PosterOut(BaseModel):
    flow: list[str] = Field(default_factory=list)
    table: _PosterTable | None = None
    note: str = ""
    key_points: list[str] = Field(default_factory=list)


POSTER_PROMPT = """你是医学教育 PPT 设计师。请把下面这道 PBL 问题的学习内容，设计成一张
「单页学术海报」的内容结构（机制流程链 + 对比表格 + 核心要点）。

【病例】
{case_text}

【问题】{q_text}
【学习内容】
{qa}

【参考文献】
{refs}

要求：
1. flow：3~5 步的发病机制/作用环节流程链，每步 ≤22 字，写成因果短语（如「黏膜免疫异常→Gd-IgA1↑」），
   按发生顺序排列；渲染时会自动加①②③编号和箭头，不要自己加编号；
2. table：一张对比表格，4~6 列 × 4~8 行（含表头），把学习内容按类别/机制层面组织
   （如「机制层｜药物｜作用机制｜关键证据｜状态」）；
   - 表头 ≤6 字；单元格 ≤30 字，可用「；」分隔多层信息；
   - 证据列尽量带研究/试验名称与数据（数据只能来自学习内容或文献，材料里没有的写「待核实」）；
   - 不同试验的入组与对照不同，不要在注里横向比较数值；
3. note：一行表格注释（≤70 字），澄清术语、数据口径或适用范围；
4. key_points：2~3 条核心要点，每条 ≤48 字，只给正文短语（渲染时自动加项目符号）；
5. 内容必须来自学习内容、病例与文献，不得编造；字段留空用空字符串/空数组；
6. 只输出 JSON：{{"flow":[""],"table":{{"headers":[""],"rows":[[""]]}},
   "note":"","key_points":[""]}}"""


def _poster_qa_markdown(q: Question) -> str:
    """题目学习内容的 Markdown 形态（含表格），供海报提示词使用。"""
    parts = []
    for blk in q.learning:
        seg = f"■ {blk.heading}\n{blk.body}".strip()
        if blk.table:
            seg += "\n" + to_md_table(blk.table)
        if seg.strip():
            parts.append(seg)
    if getattr(q, "clinical", ""):
        parts.append("【临床应用】" + q.clinical)
    return "\n\n".join(parts)[:9000]


def generate_question_poster(case: Case, q: Question,
                             provider: str | None = None,
                             model: str | None = None) -> tuple[dict | None, str]:
    """生成单页海报的内容结构（流程链/对比表/注释/要点）。

    provider/model 按次覆盖全局 .env（推荐 DeepSeek——知识密度高）。
    返回 (spec dict | None, 失败原因)。参考文献不由模型生成，渲染时直接用 q.references。
    """
    if not q.learning:
        return None, "该题还没有学习内容，请先在 ② 区生成。"
    refs = "\n".join(f"[{i + 1}] {pubmed.format_reference(r)}"
                     for i, r in enumerate(q.references)) or "（无）"
    spec = llm.chat_json(
        POSTER_PROMPT.format(case_text=_clip_case(case), q_text=q.text,
                             qa=_poster_qa_markdown(q), refs=refs[:600]),
        _PosterOut, provider=provider, model=model)
    if spec is None:
        return None, "海报内容生成失败：" + (llm.last_error() or "模型未返回有效内容")
    d = spec.model_dump()
    tb = d.get("table") or {}
    headers = [str(h).strip() for h in (tb.get("headers") or []) if str(h).strip()][:6]
    rows = []
    for r in (tb.get("rows") or [])[:8]:
        cells = [str(c).strip() for c in r]
        if any(cells):
            rows.append(cells[:len(headers)] if headers else cells[:6])
    if headers:
        rows = [r + [""] * (len(headers) - len(r)) for r in rows]
    d["table"] = {"headers": headers, "rows": rows} if headers and rows else None
    d["flow"] = [str(s).strip() for s in (d.get("flow") or []) if str(s).strip()][:5]
    d["key_points"] = [str(s).strip() for s in (d.get("key_points") or []) if str(s).strip()][:3]
    d["note"] = str(d.get("note") or "").strip()
    if not (d["flow"] or d["table"] or d["key_points"]):
        return None, "模型没有返回有效的海报内容（流程/表格/要点均为空）。"
    return d, ""


def _normalize_pages(pages: list) -> str:
    """页码归并：单页被区间覆盖则去掉单页（474,475,474-475 -> 474-475）。"""
    singles, ranges = [], []
    for pg in pages:
        pg = pg.strip()
        if not pg:
            continue
        m = re.fullmatch(r"(\d+)\s*[-~～]\s*(\d+)", pg)
        if m:
            ranges.append((int(m.group(1)), int(m.group(2))))
        elif pg.isdigit():
            singles.append(int(pg))
        else:
            singles.append(pg)      # 非数字原样保留
    out = [f"{a}-{b}" for a, b in sorted(set(ranges))]
    for s in sorted({x for x in singles if isinstance(x, int)}):
        if not any(a <= s <= b for a, b in ranges):
            out.append(str(s))
    out += [str(x) for x in singles if not isinstance(x, int)]
    return ",".join(out)


_SRC_SEG = re.compile(r"[；;]")
_SRC_BOOK = re.compile(r"《([^》]+)》")
_SRC_EDITION = re.compile(r"第(\d{1,2})版")
_SRC_PAGES = re.compile(r"第([0-9０-９、,，\s\-~～]+?)页")
_TYPED_T = re.compile(r"\[\s*[Tt]\s*(\d+)\s*\]")


def _citation_texts(q: Question):
    """Yield mutable text slots that may contain typed source markers."""
    for blk in q.learning:
        yield lambda b=blk: b.body, lambda value, b=blk: setattr(b, "body", value)
        for row_i, row in enumerate(blk.table):
            for col_i, _cell in enumerate(row):
                yield (lambda b=blk, i=row_i, j=col_i: b.table[i][j],
                       lambda value, b=blk, i=row_i, j=col_i: b.table[i].__setitem__(j, value))
    yield lambda: q.clinical, lambda value: setattr(q, "clinical", value)


def _book_from_source_segment(seg: str) -> tuple[str, str, list[str]] | None:
    """Parse a canonical book source, returning (title, edition, pages)."""
    mb = _SRC_BOOK.search(seg)
    if not mb:
        return None
    book = re.sub(r"（第\d{1,2}版）", "", mb.group(1)).strip()
    me = _SRC_EDITION.search(seg)
    edition = f"{me.group(1)}版" if me else ""
    info = textbook.find_catalog_entry(book, edition) or {}
    title = str(info.get("title") or book).strip()
    edition = str(info.get("edition") or edition).strip()
    pages: list[str] = []
    mp = _SRC_PAGES.search(seg)
    if mp:
        for piece in re.split(r"[、,，]", mp.group(1)):
            page = piece.strip().replace(" ", "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            if page and page not in pages:
                pages.append(page)
    return title, edition, pages


def _merge_book_reference(q: Question, title: str, edition: str, pages: list[str]) -> bool:
    info = textbook.find_catalog_entry(title, edition) or textbook.book_ref_info(title)
    key = (str(info.get("title") or title).strip(), str(info.get("edition") or edition).strip())
    for ref in q.references:
        if ref.ref_type == "M" and (ref.title.strip(), ref.edition.strip()) == key:
            current = [p.strip() for p in ref.pages.split(",") if p.strip()]
            ref.pages = _normalize_pages(current + pages)
            return False
    q.references.append(Reference(
        ref_type="M", title=key[0],
        authors=[a.strip() for a in re.split(r"[,，;；]", info.get("authors", "")) if a.strip()],
        place=info.get("place", ""), publisher=info.get("publisher", ""),
        year=info.get("year", ""), edition=key[1], pages=_normalize_pages(pages),
        source="教材", relevance="学习内容出处"))
    return True


def _sync_question_sources(q: Question) -> int:
    """Per-question source reconciliation; avoids constructing a Case (Pydantic identity)."""
    added = 0
    if not q.learning:
        return 0
    q.citation_warning = ""
    source_books: list[tuple[LearningBlock, tuple[str, str]]] = []
    for blk in q.learning:
        normalized: list[str] = []
        for seg in _SRC_SEG.split(blk.source or ""):
            seg = seg.strip()
            if not seg:
                continue
            if _TYPED_T.search(seg):
                q.citation_warning = (
                    "旧出处只保存了 [Tn] 编号，无法确认原书目；请重新生成/审校本题，"
                    "或在学习内容出处中填写《书名》（版次）和页码。")
                normalized.append(seg)
                continue
            parsed = _book_from_source_segment(seg)
            if not parsed:
                normalized.append(seg)
                continue
            info = textbook.find_catalog_entry(parsed[0], parsed[1]) or textbook.book_ref_info(parsed[0])
            _key = (str(info.get("title") or parsed[0]).strip(),
                    str(info.get("edition") or parsed[1]).strip())
            _exists = any(r.ref_type == "M" and (r.title.strip(), r.edition.strip()) == _key
                          for r in q.references)
            if _exists or len(q.references) < MAX_REFS:   # 参考文献上限：满员且书目不在列时不再新增
                added += int(_merge_book_reference(q, *parsed))
            else:
                _log(f"sync_source_refs {q.qid} 参考文献已达上限 {MAX_REFS}，"
                     f"《{_key[0]}》未加入文献表")
            key = _key
            source_books.append((blk, key))
            label = f"《{key[0]}》" + (f"（第{key[1]}）" if key[1] else "")
            if parsed[2]:
                label += f" 第{_normalize_pages(parsed[2])}页"
            normalized.append(label)
        blk.source = "；".join(dict.fromkeys(normalized))

    # New book entries are appended to the reference list; cite their actual list
    # position without reinterpreting pre-existing numeric citations.
    for blk, key in source_books:
        ref_idx = next((i for i, ref in enumerate(q.references, 1)
                        if ref.ref_type == "M" and
                        (ref.title.strip(), ref.edition.strip()) == key), None)
        if ref_idx and not re.search(rf"(?<!\d)\[{ref_idx}\](?!\d)", blk.body or ""):
            blk.body = _attach_marker(blk.body, ref_idx)
    return added


def sync_source_refs(case: Case) -> int:
    """对账 canonical book sources；不猜测历史裸 [Tn] 的原始书目。"""
    return sum(_sync_question_sources(q) for q in case.questions)


def enforce_max_refs(q: Question) -> int:
    """硬性上限：参考文献超过 MAX_REFS 时截断到前 MAX_REFS 条，并重排正文标号。

    返回被移除的条数。正文里指向被截断文献的标号随 renumber_citations 一并清除。
    """
    if len(q.references) <= MAX_REFS:
        return 0
    old = list(q.references)
    q.references = old[:MAX_REFS]
    renumber_citations(q, old)
    return len(old) - MAX_REFS


def renumber_citations(q: Question, old_refs: list[Reference]) -> int:
    """删除参考文献后重排正文引用标注（GB/T 7714 顺序编码制）。

    old_refs 为删除前的文献列表（q.references 已是删除后的新列表）。
    旧编号按幸存文献的新位置改号；指向已删文献的标号删除；悬空编号（超出旧表长）删除。
    已删的教材文献[M]同时从对应小节的 source 里清掉出处段，避免导出时被 sync_source_refs 加回。
    返回被改写的段落数。
    """
    new_refs = list(q.references)
    old_to_new: dict[int, int] = {}
    for k, ref in enumerate(old_refs, 1):
        for p, cur in enumerate(new_refs, 1):
            if cur is ref:
                old_to_new[k] = p
                break
    if len(old_to_new) == len(old_refs):
        return 0                                # 没有删除发生（只是重排同样的列表）
    n_old = len(old_refs)

    def _remap(text: str) -> tuple[str, set[int]]:
        """改写正文标号；返回 (新文本, 该段中被删除标号对应的旧编号集合)。"""
        removed: set[int] = set()

        def sub(m: re.Match) -> str:
            k = int(m.group(1))
            if k in old_to_new:
                return f"[{old_to_new[k]}]"
            if 1 <= k <= n_old:
                removed.add(k)
            return ""                          # 指向已删文献 / 悬空编号
        return re.sub(r"\[(\d+)\]", sub, text), removed

    changed = 0
    for blk in q.learning:
        new_body, removed = _remap(blk.body or "")
        if new_body != (blk.body or ""):
            blk.body = new_body
            changed += 1
        if removed and blk.source:
            # 清掉被删教材文献的出处段（按书名+引用页码匹配）
            for k in sorted(removed):
                ref = old_refs[k - 1]
                if getattr(ref, "ref_type", "J") != "M":
                    continue
                segs = [_s for _s in re.split(r"[；;]", blk.source) if _s.strip()]
                kept = []
                for seg in segs:
                    same_book = f"《{ref.title}》" in seg
                    same_page = (not str(ref.pages or "").strip()
                                 or str(ref.pages).split(",")[0].split("-")[0] in seg)
                    if same_book and same_page:
                        continue
                    kept.append(seg)
                blk.source = "；".join(kept)
    if q.clinical:
        new_clinical, _removed = _remap(q.clinical)
        if new_clinical != q.clinical:
            q.clinical = new_clinical
            changed += 1
    return changed


class _NatOut(BaseModel):
    learning: list[dict] = Field(default_factory=list)
    clinical: str = ""


NATURALIZE_PROMPT = """你是中文医学学术写作编辑。作者是学生本人，你做的是语言层重写，不是代写：
只调整表达方式，不产生新观点、新数据、新文献。把下面内容改写成一名医学生正常学术训练下写出的文字。

【问题】{q_text}
【学习内容】
{learning}
【临床应用】
{clinical}

改写幅度：**中度**——逐句重写表达方式（句法、语序、措辞都要动），**不得整句照抄原句**
（专有名词、标准术语、数值、[数字] 引用标注除外）；临床应用部分尤其要按叙述性段落写，
段落长短不一，避免"①…②…③…"式的罗列节奏。

改写目标：
1. 语域：学术书面语，克制准确，不用口语/网络语/比喻/感叹；
2. 句法：句长有落差（至少一处短判断句、一处长复句），避免通篇"主语+谓语+宾语"平铺；
3. 段落与要点：允许单句成段；连续要点不得用相同句式开头；不写"不仅…而且…"连续对偶；
4. 衔接：显性路标词（首先/其次/再次/最后/综上所述/与此同时）总量减半，改用事理顺序、指代与因果词（因、故、由此可见）；
5. 允许重组句序、拆分或合并句子、调整从句位置；改写后**不得与原文出现连续 15 字以上的相同片段**
   （专有名词、标准术语、数值、[数字] 引用标注除外）。

必须避免（出现即改）：
- 空泛评价："具有重要的临床意义""提供了新的思路""值得进一步研究""意义深远"——改为具体所指或删除；
- 万能开头结尾："随着…的不断发展""在当今…背景下""综上所述，本文…"；
- 名词化堆叠："进行…的分析""作出…的判断""实现对…的提升"→ 改回动词；
- 同义词轮换：患者/病人/病患混用、疗效/效果/作用混用——全文统一一个词；
- 无信息限定："在一定程度上""相对而言"；用"显著"必须带具体数值；
- 每段首句都是概括句的机器排版习惯。

红线（禁止触碰）：
1. 数据零改动：所有数值、单位、剂量、时间窗原样保留；
2. 正文中的 [数字] 引用标注**不得增删、不得改号、不得移句**；
3. 不得新增知识点、文献或结论；不得升降医学结论强度（相关≠导致，提示≠证明）；
4. 保持分层编号结构（（1）（2）/①②）与每节 source 不变；
5. 表格除非文字有误否则原样保留。

只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}],"clinical":""}}"""


def naturalize_question(case: Case, q: Question) -> tuple[bool, str]:
    """语言层自然化润色：改写措辞与句式，保留数据、[n] 引用标注与编号结构。

    改写度低于阈值会自动再试一次（模型的改写力度不稳定）；引用标注有任何差异则放弃。
    """
    if not llm.has_key():
        return False, "未配置 API Key"
    if not q.learning and not q.clinical:
        return False, "该问题尚无可润色的内容，请先生成"
    import difflib

    before_text = chr(10).join(b.body for b in q.learning) + chr(10) + (q.clinical or "")
    marks_before = sorted(re.findall(r"\[\d+\]", before_text))
    prompt = NATURALIZE_PROMPT.format(
        q_text=q.text,
        learning=chr(10).join(f"【{b.heading}】{b.body}" for b in q.learning) or "（空）",
        clinical=q.clinical or "（空）")

    best = None          # (相似度, learning, clinical)
    for _ in range(2):
        result = llm.chat_json(prompt, _NatOut, temperature=0.85)
        if result is None or (not result.learning and not result.clinical):
            continue
        new_learning = []
        for i, b in enumerate(result.learning):
            if not b.get("body"):
                continue
            old = q.learning[i] if i < len(q.learning) else None
            new_learning.append(LearningBlock(
                heading=sanitize_content(b.get("heading", "")),
                body=sanitize_content(b.get("body", "")),
                source=(old.source if old else b.get("source", "")),
                table=_coerce_table(b.get("table")) or (old.table if old else [])))
        new_clinical = sanitize_content(result.clinical) if result.clinical else q.clinical
        if not new_learning:
            continue
        after_text = chr(10).join(b.body for b in new_learning) + chr(10) + (new_clinical or "")
        if sorted(re.findall(r"\[\d+\]", after_text)) != marks_before:
            _log(f"naturalize {q.qid} 引用标注不一致，弃用该次结果")
            continue
        sim = difflib.SequenceMatcher(None, before_text, after_text).ratio()
        if best is None or sim < best[0]:
            best = (sim, new_learning, new_clinical)
        if sim <= 0.94:                      # 改写足够充分
            break

    if best is None:
        return False, "润色失败（模型未返回合规内容或改动了引用标注），请重试"
    sim, new_learning, new_clinical = best
    if 1 - sim < 0.03:                       # 几乎没改写，如实告知
        return False, f"本次模型几乎未改写（改写度仅 {1 - sim:.0%}），请再点一次或调整指令"
    q.learning = new_learning
    q.clinical = new_clinical
    _log(f"naturalize {q.qid} 润色完成（改写度 {1 - sim:.0%}，引用 {len(marks_before)} 处未变）")
    return True, f"已润色：改写度约 {1 - sim:.0%}；数据、引用标注与编号结构保持不变"


def _demo_organize(draft: str) -> list[Question]:
    lines = [x.strip(" \t0123456789.、?？?；;：:　") for x in re.split(r"[\n;；]", draft or "") if x.strip()]
    if not lines:
        lines = ["该患者可能的诊断是什么？其诊断依据有哪些？",
                 "该疾病的主要发病机制是什么？",
                 "针对该患者应制定怎样的治疗方案？"]
    return [Question(qid=f"Q{i}", text=t, qtype="其他") for i, t in enumerate(lines, 1)]


# ---------------------------------------------------------------- 逐问题生成
class _GenOut(BaseModel):
    learning: list[dict]          # {heading, body, table, source}
    ref_indexes: list[int] = Field(default_factory=list)
    ref_notes: list[dict] = Field(default_factory=list)  # {i, relevance} 每条的一句话推荐语


_QUALITY_BAR = """
【内容质量硬门槛】
1. 具体性：机制要落到分子/细胞/通路（如 IL-8、MMP-9、NF-κB 通路），诊断要落到标准与数值
   （如 FEV1/FVC<0.7、分级标准），治疗要落到药名/剂量/靶点；
   **禁止空话**：「某些 X」「与 Y 相关」「不容忽视」「可能起重要作用」「有待进一步研究」
   等无信息量表述一律不得出现——宁可少写一条，也不写空话；
2. 反凑数：禁止「研究方法/研究现状概述」这类方法论或综述式小节；「最新研究进展」只在题目
   明确问到时写，且必须落到具体发现（通路/靶点/药物/试验结论），不得泛泛罗列方向；
3. 深度与去重：每节 body 至少 5 条分层要点（首行总起 +（1）（2）…，要点 1~2 句）；
   两个小节若内容会互相重叠必须合并——三个浅薄重复的小节不如两个扎实的小节；
   材料不足以支撑某小节时直接砍掉，不硬凑节数；
4. 表格优先：分级/对比/鉴别/方案类内容（如 GOLD 分级、药物对比）一律用 table 呈现；
5. 引用诚实：标注只给该句内容真正对应的资料，同一 [Tn] 不得覆盖整节每一句；
   source 只在该节主体确实来自对应 [Tn] 时填写，不得整题共用同一页码，不得编造页码。"""

GEN_PROMPT = """你是一名医学教育专家，正在为 PBL 教案的下面这个问题生成学习内容。

【病例】
{case_text}

【问题】{q_text}（类型：{q_type}）

【教材检索结果】（编号 T1..Tn，含出处；为空表示未检索教材）
{tb}

【PubMed 文献检索结果】（编号 P1..Pn，均为真实文献）
{pm}

【已有手动参考文献】（编号 R1..Rn，来自教师/学生导入；为空则忽略）
{rm}

请生成学习内容 learning：2~4 个小节，每节含：
- heading：小标题（如"补液扩容治疗概述"）；
- body：正文，采用分层编号的条理化结构——首行直接展开，子要点用"（1）（2）"，
  再下级用"①②"，每个要点独占一行（用换行符分隔），关键处带具体数值/药名/标准；
- table：当小节内容适合表格呈现（药物对比、疾病鉴别对比、方案对比等）时，
  给出字符串二维数组（首行为表头，建议 3~5 列、2~8 行），body 写表格之外的补充说明；
  不适合表格则 table 为空数组 []；
- source：教材出处（如《内科学》（第10版）第三章），依据教材检索结果填写；无则留空。
内容要求（重要）：
0. **范围红线**：每个小节必须直接回答【问题】本身。题目问机制就只写机制，
   问诊断就只写诊断；**禁止**顺带写临床表现、诊断标准、治疗原则等与本题无关的内容。
   教材检索结果中与本题无关的段落一律不纳入。
1. 学习内容为**通用医学知识**，不要联系本病例、不要出现"该患者/本例"等病例表述
   ——病例分析在"实际应用"部分单独生成；
2. 内容务必**精简、不重复**：各小节之间、同一小节内不得重复同一知识点或近似表述，
   每个要点一句话讲清，不铺陈；
3. 内容以教材检索结果为主要依据；**PubMed 文献用于补充最新研究进展、机制细节与循证数据**
   ——若提供了 PubMed 结果，须将其相关内容纳入正文并标注 [P编号]。
""" + _QUALITY_BAR + """

{human_style}
引用标注（重要）：在正文里，凡依据教材检索结果的句子，句末标 [T编号]；
依据 PubMed 文献的句末标 [P编号]；依据手动文献的句末标 [R编号]；
标注紧贴句末标点之前，标注必须与该句内容对应，不要为凑数标注。
**你选中的每一条文献（ref_indexes 里的每条 P）以及每条手动文献（R）都必须在正文中至少标注一次**——
未被标注的文献不会进入参考文献表。
这些标注最终会被转换为论文的顺序编码 [1][2]。
注意：整个问题的参考文献总数不超过 5 篇（含教材与手动文献）；
PubMed 有检索结果时 ref_indexes **至少选 2 条**（最多 3 条），只选能在正文中引用的，选中的必须引用。

只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}],
"ref_indexes":[1],"ref_notes":[{{"i":1,"relevance":""}}]}}"""


def _fmt_tb_hits(hits: list[dict]) -> str:
    if not hits:
        return "（未检索教材或未建库）"
    lines = []
    for i, h in enumerate(hits, 1):
        loc = f"《{h['source']}》" + (f" 第{h['page']}页" if h.get("page") else "")
        lines.append(f"[T{i}] {loc}\n{h['text']}")
    return "\n\n".join(lines)


def _fmt_pm_hits(refs: list[dict]) -> str:
    if not refs:
        return "（无检索结果）"
    lines = []
    for i, r in enumerate(refs, 1):
        lines.append(f"[P{i}] {r['title']}（{', '.join(r['authors'][:3])}, {r['journal']}, {r['year']}）\n摘要：{r['abstract'][:400]}")
    return "\n\n".join(lines)


_BARE_T = re.compile(r"\b[Tt](\d+)\b")


def _canonical_source_from_markers(source: str, tb_hits: list[dict]) -> str:
    """Map model-emitted [Tn]/Tn source labels using this generation's exact hit list.

    兼容模型偷懒写的裸标记（如 "T3, T1"）；P/R 文献标记不是教材出处，一律剔除。
    """
    normalized: list[str] = []
    for seg in _SRC_SEG.split(source or ""):
        seg = seg.strip()
        if not seg:
            continue
        indexes = [int(x) for x in _TYPED_T.findall(seg)] or \
            [int(x) for x in _BARE_T.findall(seg)]
        if not indexes:
            residue = re.sub(r"\b[PpRr]\d+\b", "", seg).strip(" ,，、/;；+")
            if residue:
                normalized.append(residue)
            continue
        parts: list[str] = []
        for idx in indexes:
            if not 1 <= idx <= len(tb_hits):
                parts.append(f"[T{idx}]")
                continue
            hit = tb_hits[idx - 1]
            parsed = _book_from_source_segment(
                f"《{hit.get('source', '')}》 第{hit.get('page', '')}页")
            if parsed:
                title, edition, pages = parsed
                label = f"《{title}》" + (f"（第{edition}）" if edition else "")
                if pages:
                    label += f" 第{_normalize_pages(pages)}页"
                parts.append(label)
            else:
                parts.append(f"[T{idx}]")
        residue = re.sub(r"\b[PpRr]\d+\b", "", _TYPED_T.sub("", _BARE_T.sub("", seg)))
        residue = residue.strip(" ,，、/;；+")
        head = "；".join(dict.fromkeys(parts))
        normalized.append(head + (f"（{residue}）" if residue else ""))
    return "；".join(dict.fromkeys(normalized))


# ---------------------------------------------------------------- 引用标注解析（GB/T 7714 顺序编码制）
_CITE = re.compile(r"\[\s*([TtPpRr])?\s*(\d+)\s*\]")
_CITE_PH = re.compile(r"⟦CITE:([TPRN])(\d+)⟧")


def _mk_tb_ref(h: dict) -> Reference:
    """教材检索命中 -> [M] 专著条目（著录信息来自 book_catalog.json）。"""
    info = textbook.book_ref_info(h["source"])
    return Reference(
        ref_type="M", title=info["title"],
        authors=[a.strip() for a in re.split(r"[,，;；]", info["authors"]) if a.strip()],
        place=info["place"], publisher=info["publisher"], year=info["year"],
        edition=info["edition"], pages=str(h["page"]), source="教材",
        relevance=f"教材依据（{h['source']} 第{h['page']}页）")


def _mk_pm_ref(r: dict, relevance: str = "") -> Reference:
    """检索命中/现有文献 -> Reference 条目（保留 ref_type 与专著字段，兼容手动书籍）。"""
    keys = ("ref_type", "title", "authors", "journal", "place", "publisher", "edition",
            "year", "volume", "issue", "pages", "pmid", "doi", "abstract")
    kw = {kk: r[kk] for kk in keys if kk in r and r[kk]}
    kw.setdefault("ref_type", "J")
    return Reference(source=REF_SOURCE_AUTO, relevance=relevance, **kw)


def resolve_citations(q: Question, tb_hits: list[dict], pm_refs: list[dict] | None = None,
                      pm_notes: dict | None = None, manual_refs: list[Reference] | None = None,
                      extra_refs: list[Reference] | None = None,
                      original_refs: list[Reference] | None = None) -> None:
    """把正文中的 [T2]/[P3]/[R1] 标注解析为论文顺序编码 [1][2]…，并重建参考文献列表。

    - 教材依据 [T#] → [M] 专著条目（书名著录信息来自 pbl/book_catalog.json，可编辑补作者）；
      同一本书被引到多页时合并为一条、页码并列（GB/T 规范）；
    - 文献观点 [P#] → [J] 期刊条目（仅来自 PubMed 真实检索结果）；
    - 手动文献 [R#] → [J]/[M] 条目（教师/学生导入，如知网中文文献）；
    - 编号按正文首次出现顺序；未被引用的挑选/手动条目殿后；越界标注删除。
    """
    pm_refs = pm_refs or []
    pm_notes = pm_notes or {}
    manual_refs = manual_refs or []
    original_refs = original_refs or []
    has_numeric_markers = any(re.search(r"\[\d+\]", get_text() or "")
                              for get_text, _ in _citation_texts(q))

    def _t_key(idx: int) -> str:
        info = textbook.book_ref_info(tb_hits[idx - 1]["source"])
        return info["title"] + "|" + info["edition"]

    def _j_key(idx: int) -> tuple:
        r = pm_refs[idx - 1]
        return (r["title"][:60], r["year"])

    def _r_key(idx: int) -> tuple:
        r = manual_refs[idx - 1]
        return (r.ref_type, r.title[:60], r.year)

    def _pool(kind: str) -> int:
        return {"T": len(tb_hits), "P": len(pm_refs), "R": len(manual_refs)}[kind]

    order: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    slot_maps: list[tuple[object, object]] = []
    marker_notes: list[str] = []
    book_refs: dict[tuple[str, str], Reference] = {}
    j_refs: dict[tuple, Reference] = {}
    r_refs: dict[tuple, Reference] = {}
    final_order: list[tuple[str, object]] = []

    def _collect(m: re.Match) -> str:
        kind = (m.group(1) or "N").upper()
        idx = int(m.group(2))
        if kind == "N":
            if not 1 <= idx <= len(original_refs):
                marker_notes.append(f"旧引用 [{idx}] 已无对应参考文献，保留原号待核对")
                return m.group(0)
            old = original_refs[idx - 1]
            if old.ref_type == "M":
                key = (old.title.strip(), old.edition.strip())
                book_refs.setdefault(key, old.model_copy(deep=True))
                pair = ("M", key)
            elif old.ref_type == "J":
                key = (old.title[:60], old.year)
                j_refs.setdefault(key, old.model_copy(deep=True))
                pair = ("J", key)
            else:
                key = (old.ref_type, old.title[:60], old.year)
                r_refs.setdefault(key, old.model_copy(deep=True))
                pair = ("R", key)
            if pair not in final_order:
                final_order.append(pair)
            key = ("N", idx)
            if key not in seen:
                seen.add(key); order.append(key)
            return f"⟦CITE:N{idx}⟧"
        if not 1 <= idx <= _pool(kind):
            marker_notes.append(f"[{kind}{idx}] 超出本次可验证来源范围")
            return m.group(0)
        key = (kind, idx)
        if key not in seen:
            seen.add(key)
            order.append(key)
        return f"⟦CITE:{kind}{idx}⟧"

    for get_text, set_text in _citation_texts(q):
        original = get_text() or ""
        converted = _CITE.sub(_collect, original)
        if converted != original:
            set_text(converted)
        slot_maps.append((get_text, set_text))

    if has_numeric_markers:
        for ref in original_refs:
            if ref.ref_type == "M":
                key = (ref.title.strip(), ref.edition.strip())
                book_refs.setdefault(key, ref.model_copy(deep=True))
                pair = ("M", key)
            elif ref.ref_type == "J":
                key = (ref.title[:60], ref.year)
                j_refs.setdefault(key, ref.model_copy(deep=True))
                pair = ("J", key)
            else:
                key = (ref.ref_type, ref.title[:60], ref.year)
                r_refs.setdefault(key, ref.model_copy(deep=True))
                pair = ("R", key)
            if pair not in final_order:
                final_order.append(pair)

    def add_book(hit: dict) -> tuple[str, str]:
        ref = _mk_tb_ref(hit)
        key = (ref.title.strip(), ref.edition.strip())
        if key not in book_refs:
            book_refs[key] = ref
            final_order.append(("M", key))
        else:
            current = [x.strip() for x in book_refs[key].pages.split(",") if x.strip()]
            page = str(hit.get("page", "")).strip()
            if page and page not in current:
                current.append(page)
                book_refs[key].pages = _normalize_pages(current)
        return key

    def add_book_source(title: str, edition: str, pages: list[str]) -> tuple[str, str]:
        info = textbook.find_catalog_entry(title, edition) or textbook.book_ref_info(title)
        key = (str(info.get("title") or title).strip(), str(info.get("edition") or edition).strip())
        if key not in book_refs:
            book_refs[key] = Reference(
                ref_type="M", title=key[0],
                authors=[a.strip() for a in re.split(r"[,，;；]", info.get("authors", "")) if a.strip()],
                place=info.get("place", ""), publisher=info.get("publisher", ""),
                year=info.get("year", ""), edition=key[1], pages=_normalize_pages(pages),
                source="教材", relevance="学习内容出处")
        else:
            current = [x.strip() for x in book_refs[key].pages.split(",") if x.strip()]
            book_refs[key].pages = _normalize_pages(current + pages)
        pair = ("M", key)
        if pair not in final_order:
            final_order.append(pair)
        return key

    # Add valid sources in citation order, then append parsed source-only references.
    for kind, idx in order:
        if kind == "N":
            continue  # already loaded from original_refs above
        if kind == "T":
            add_book(tb_hits[idx - 1])
        elif kind == "P":
            raw = pm_refs[idx - 1]
            key = (raw.get("title", "")[:60], str(raw.get("year", "")))
            if key not in j_refs:
                j_refs[key] = _mk_pm_ref(raw, pm_notes.get(idx, ""))
                final_order.append(("J", key))
        else:
            raw_ref = manual_refs[idx - 1]
            key = (raw_ref.ref_type, raw_ref.title[:60], raw_ref.year)
            if key not in r_refs:
                r_refs[key] = raw_ref.model_copy(deep=True)
                final_order.append(("R", key))

    source_only: list[tuple[LearningBlock, tuple[str, str]]] = []
    for blk in q.learning:
        normalized = []
        for seg in _SRC_SEG.split(blk.source or ""):
            seg = seg.strip()
            if not seg:
                continue
            marker_idxs = [int(x) for x in _TYPED_T.findall(seg)]
            if marker_idxs:
                for idx in marker_idxs:
                    if not 1 <= idx <= len(tb_hits):
                        marker_notes.append(f"[T{idx}] 出处无法匹配本次教材检索结果；请核对教材书名和页码")
                        normalized.append(f"[T{idx}]")
                        continue
                    hit = tb_hits[idx - 1]
                    key = add_book(hit)
                    source_only.append((blk, key))
                    tb_ref = _mk_tb_ref(hit)
                    normalized.append(
                        f"《{tb_ref.title}》" + (f"（第{tb_ref.edition}）" if tb_ref.edition else "")
                        + (f" 第{_normalize_pages([str(hit.get('page', ''))])}页" if hit.get("page") else ""))
                continue
            parsed = _book_from_source_segment(seg)
            if parsed:
                key = add_book_source(*parsed)
                source_only.append((blk, key))
                normalized.append(f"《{key[0]}》" + (f"（第{key[1]}）" if key[1] else "")
                                  + (f" 第{_normalize_pages(parsed[2])}页" if parsed[2] else ""))
            else:
                normalized.append(seg)
        blk.source = "；".join(dict.fromkeys(normalized))

    # Keep selected/manual references after explicitly cited entries, with the same map
    # used for numbering so body citations and the bibliography cannot drift apart.
    for ref in extra_refs or []:
        if ref.ref_type == "M":
            key = (ref.title.strip(), ref.edition.strip())
            if key not in book_refs:
                book_refs[key] = ref.model_copy(deep=True)
                final_order.append(("M", key))
        elif ref.ref_type == "J":
            key = (ref.title[:60], ref.year)
            if key not in j_refs:
                j_refs[key] = ref.model_copy(deep=True)
                final_order.append(("J", key))
        else:
            key = (ref.ref_type, ref.title[:60], ref.year)
            if key not in r_refs:
                r_refs[key] = ref.model_copy(deep=True)
                final_order.append(("R", key))
    numbering = {key: i + 1 for i, key in enumerate(final_order)}
    refs = [{"M": book_refs, "J": j_refs, "R": r_refs}[kind][key]
            for kind, key in final_order]

    def _num(m: re.Match) -> str:
        kind, idx = m.group(1), int(m.group(2))
        if kind == "N":
            if not 1 <= idx <= len(original_refs):
                return m.group(0)
            old = original_refs[idx - 1]
            if old.ref_type == "M":
                pair = ("M", (old.title.strip(), old.edition.strip()))
            elif old.ref_type == "J":
                pair = ("J", (old.title[:60], old.year))
            else:
                pair = ("R", (old.ref_type, old.title[:60], old.year))
            return f"[{numbering[pair]}]" if pair in numbering else m.group(0)
        if kind == "T":
            tb_ref = _mk_tb_ref(tb_hits[idx - 1])
            pair = ("M", (tb_ref.title.strip(), tb_ref.edition.strip()))
        elif kind == "P":
            raw = pm_refs[idx - 1]
            pair = ("J", (raw.get("title", "")[:60], str(raw.get("year", ""))))
        else:
            raw_ref = manual_refs[idx - 1]
            pair = ("R", (raw_ref.ref_type, raw_ref.title[:60], raw_ref.year))
        return f"[{numbering[pair]}]" if pair in numbering else m.group(0)

    for get_text, set_text in slot_maps:
        set_text(_CITE_PH.sub(_num, get_text() or ""))

    # Source provenance is independent of existing numeric references; append only
    # a missing book citation and keep previous numbering intact.
    source_seen: set[tuple[int, tuple[str, str]]] = set()
    for blk, key in source_only:
        source_key = (id(blk), key)
        if source_key in source_seen:
            continue
        source_seen.add(source_key)
        ref_index = next((i for i, ref in enumerate(refs, 1)
                          if ref.ref_type == "M" and (ref.title.strip(), ref.edition.strip()) == key), None)
        if ref_index and not re.search(rf"(?<!\d)\[{ref_index}\](?!\d)", blk.body or ""):
            blk.body = _attach_marker(blk.body, ref_index)

    if not refs and (tb_hits or pm_refs):
        ref = _mk_tb_ref(tb_hits[0]) if tb_hits else _mk_pm_ref(pm_refs[0])
        refs = [ref]
        if q.learning:
            q.learning[0].body = _attach_marker(q.learning[0].body, 1)
        _log(f"resolve_citations {q.qid} 无有效标注，兜底采用最相关来源")
    q.references = refs
    q.citation_warning = "；".join(dict.fromkeys(marker_notes))
    # Canonical book sources from the generation prompt are reliable; add the matching [M]
    # entry and body marker while preserving the resolver's numeric citation map.
    # Call per-question directly to avoid Case re-validation (Streamlit hot-reload identity).
    _sync_question_sources(q)


def _demo_generate(q: Question, case_text: str) -> Question:
    """无 Key 演示降级。"""
    q.learning = [
        LearningBlock(heading="概念与机制（演示）",
                      body=f"此处为演示内容：围绕「{q.text}」梳理核心概念、发生机制与关键环节。"
                           "配置 API Key 后将基于教材检索结果生成，并标注教材出处。",
                      source=""),
        LearningBlock(heading="诊疗要点（演示）",
                      body="此处为演示内容：诊断标准、鉴别要点、治疗原则与常用药物。配置 API Key 后自动生成。",
                      source=""),
    ]
    q.references = [
        Reference(title="（演示）配置 API Key 后将自动检索 PubMed 真实文献",
                  journal="—", year="—", relevance="演示占位", source=REF_SOURCE_AUTO)]
    q.clinical = f"（演示）结合本病例，围绕「{q.text}」分析其临床表现与处置思路。配置 API Key 后自动生成结合病例具体数据的分析。"
    return q


def _fmt_rm_hits(refs: list[Reference]) -> str:
    if not refs:
        return "（无）"
    return "\n".join(f"[R{i}] {r.title}（{r.journal or r.publisher}, {r.year}）"
                     for i, r in enumerate(refs, 1))


def _tb_query(q: Question, k: int = 4) -> list[dict]:
    """教材检索：先按问题原文查；全部低于相关性阈值时，用中文检索词兜底再查一次。"""
    if not textbook.has_index():
        return []
    hits = textbook.query(q.text, k=k)
    if not hits and q.cn_keywords:
        kws = " ".join(q.cn_keywords.split()[:3])
        if kws.strip():
            hits = textbook.query(kws, k=k)
    return hits


def generate_question(case: Case, q: Question, use_textbook: bool = True,
                      use_pubmed: bool = True) -> tuple[bool, str]:
    """单个问题的学习内容生成（检索模式）；临床应用由 generate_clinical 单独生成。

    返回 (是否成功, 说明)。失败时保留该问题原有内容不变，不再静默降级为演示文本。
    """
    tb_hits: list[dict] = _tb_query(q) if use_textbook else []
    if use_pubmed and not (q.en_keywords or "").strip():
        ensure_question_keywords(case, q)      # 缺检索词先自动补，保证文献检索不跳过
    pm_refs: list[dict] = []
    if use_pubmed and q.en_keywords:
        pm_refs = pubmed.search_pubmed(q.en_keywords, retmax=8,
                                       doc_type=getattr(case, "ref_doc_type", "all") or "all")
    original_refs = [r.model_copy(deep=True) for r in q.references]
    manual = [r for r in q.references if r.source != REF_SOURCE_AUTO]

    if not llm.has_key():
        _demo_generate(q, case.case_text)
        return True, "（演示模式：已填入演示内容，配置 API Key 后可生成正式内容）"

    prompt = GEN_PROMPT.format(
        case_text=_clip_case(case), q_text=q.text, q_type=q.qtype,
        tb=_fmt_tb_hits(tb_hits), pm=_fmt_pm_hits(pm_refs), rm=_fmt_rm_hits(manual),
        human_style=_HUMAN_STYLE,
    )
    result = llm.chat_json(prompt, _GenOut)
    if result is None:
        _log(f"generate_question {q.qid} 失败：" + (llm.last_error() or "模型未返回有效内容"))
        return False, "模型调用未返回有效内容，内容保留原样" + llm.error_hint() + "；详见 logs/pbl.log"

    q.learning = [LearningBlock(
        heading=sanitize_content(b.get("heading", "")),
        body=sanitize_content(b.get("body", "")), source=b.get("source", ""),
        table=_coerce_table(b.get("table")))
        for b in result.learning if b.get("body") or b.get("table")]
    if use_textbook and tb_hits:
        for blk in q.learning:
            if blk.source and _TYPED_T.search(blk.source):
                blk.source = _canonical_source_from_markers(blk.source, tb_hits)

    notes = {int(x.get("i", 0)): x.get("relevance", "") for x in result.ref_notes if isinstance(x, dict)}
    ref_indexes = list(result.ref_indexes)

    # 补引用：正文未引用任何 PubMed 文献时，自动把检索结果补标到对应句子
    supplemented = False
    raw_text = chr(10).join(b.body for b in q.learning)
    if pm_refs and not re.search(r"\[P\d+\]", raw_text, re.I) and llm.has_key():
        _log(f"generate_question {q.qid} 正文未引用文献，尝试自动补引用（候选 {len(pm_refs)} 篇）")
        fixed = llm.chat_json(CITE_FIX_PROMPT.format(
            learning=chr(10).join(f"【{b.heading}】{b.body}" for b in q.learning),
            pm=_fmt_pm_hits(pm_refs)), _CiteFix)
        if fixed and fixed.learning:
            new_learning = []
            for i, b in enumerate(fixed.learning):
                if not b.get("body"):
                    continue
                old_blk = q.learning[i] if i < len(q.learning) else None
                new_learning.append(LearningBlock(
                    heading=sanitize_content(b.get("heading", "")),
                    body=sanitize_content(b.get("body", "")),
                    source=b.get("source", "") or (old_blk.source if old_blk else ""),
                    table=_coerce_table(b.get("table")) or (old_blk.table if old_blk else [])))
            if new_learning:
                q.learning = new_learning
                ref_indexes = list(dict.fromkeys(ref_indexes + list(fixed.ref_indexes)))
                for x in fixed.ref_notes:
                    if isinstance(x, dict):
                        try:
                            notes.setdefault(int(x.get("i", 0)), x.get("relevance", ""))
                        except (TypeError, ValueError):
                            pass
                supplemented = True

    picked: list[Reference] = []
    for idx in ref_indexes:
        if 1 <= idx <= len(pm_refs):
            r = pm_refs[idx - 1]
            picked.append(Reference(source=REF_SOURCE_AUTO, relevance=notes.get(idx, ""), **{
                kk: r[kk] for kk in ("title", "authors", "journal", "year", "volume",
                                     "issue", "pages", "pmid", "doi", "abstract")}))
    existing_books = [r.model_copy(deep=True) for r in q.references if r.ref_type == "M"]
    resolve_citations(q, tb_hits, pm_refs, pm_notes=notes,
                      manual_refs=manual, extra_refs=manual + picked + existing_books)
    _trimmed = enforce_max_refs(q)
    if _trimmed:
        _log(f"generate_question {q.qid} 参考文献超出上限 {MAX_REFS}，已截断 {_trimmed} 条并重排标号")
    if supplemented:
        _log(f"generate_question {q.qid} 补引用成功")
    elif pm_refs:
        _log(f"generate_question {q.qid} 补引用失败（模型未返回有效内容或未触发）")

    # 硬兜底：检索到文献但文献表仍无期刊条目时，强制纳入最相关的 2~3 篇并补标注
    if pm_refs and not any(r.ref_type == "J" for r in q.references):
        forced_idx = [i for i in ref_indexes if 1 <= i <= len(pm_refs)][:3]             or list(range(1, min(3, len(pm_refs)) + 1))
        base = len(q.references)
        for k, fi in enumerate(forced_idx):
            r = pm_refs[fi - 1]
            q.references.append(Reference(source=REF_SOURCE_AUTO, relevance=notes.get(fi, ""), **{
                kk: r[kk] for kk in ("title", "authors", "journal", "year", "volume",
                                     "issue", "pages", "pmid", "doi", "abstract")}))
            if q.learning:
                blk = q.learning[k % len(q.learning)]
                blk.body = _attach_marker(blk.body, base + k + 1)
        _log(f"generate_question {q.qid} 期刊文献未被引用，强制纳入 {len(forced_idx)} 篇并补标注")
    warns = []
    if use_textbook and not tb_hits:
        warns.append("未检索到相关教材段落，内容未依据教材")
    if use_pubmed and q.en_keywords and not pm_refs:
        detail = pubmed.last_error() or pubmed.last_note()
        warns.append("PubMed 无检索结果" + (f"（{detail}）" if detail else ""))
    if supplemented:
        warns.append("已自动补充期刊文献引用")
    return True, "；".join(warns)


# ---------------------------------------------------------------- 字段级局部重生成
FIELD_PROMPTS = {
    "learning": "重新生成该问题的「学习内容」（2~4 小节，JSON 中 learning 数组）",
}

REGEN_PROMPT = """你是一名医学教育专家。请按用户要求重写 PBL 教案问题的「学习内容」。

【病例】
{case_text}

【问题】{q_text}（类型：{q_type}）

【当前学习内容】
{learning}

【教材检索结果】
{tb}

【参考文献】（编号 P1..Pn，含本次 PubMed 检索结果与原有文献；重写后如引用，请在正文对应处标注 [P编号]，无标注的不会保留）
{current_refs}

【任务】{task}
【用户补充要求】{instruction}

要求：保持分层编号结构（（1）①），小节表格按需保留或修正；
引用标注：依据教材检索结果的句子句末标 [T编号]；依据当前参考文献的句子句末标 [P编号]（均紧贴句末标点之前）。
若【参考文献】非空，正文必须至少引用其中 2 条；确实与本题不直接相关时，选最接近的 1 条并说明其间接关系，不得一条都不引。
**参考文献总数不超过 5 篇**（含教材与文献）；超过时只保留与正文关联最强的 5 篇，正文标号随之收敛。

内容要求：学习内容为通用医学知识，不要联系本病例、不要出现"该患者/本例"等病例表述
（对病例的具体分析在"实际应用"部分单独生成）；
**范围红线**：每个小节必须直接回答【问题】本身，禁止顺带写临床表现、诊断标准、治疗原则等与本题无关的内容；
各小节之间、同一小节内不得重复同一知识点或近似表述，内容务必精简。
""" + _QUALITY_BAR + """

{human_style}

格式禁则：全文禁止使用【】括号；编号只用（1）（2）与①②两级，
行首不得叠加两级编号（如"3.（3）"）；heading 不要自带序号。
只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}]}}"""


def _doc_type_from_instruction(instruction: str, default: str = "all") -> str:
    """从「修改指令」里识别文献类型：提到综述/荟萃就检索综述，提到研究型就检索研究型。"""
    low = (instruction or "").lower()
    if any(k in low for k in ("综述", "系统评价", "系统综述", "荟萃", "meta", "review")):
        return "review"
    if any(k in low for k in ("研究型", "原创研究", "论著", "原创论文")):
        return "research"
    return default if default in ("all", "research", "review") else "all"


def regenerate_field(case: Case, qid: str, field: str, instruction: str) -> bool:
    """只重生成学习内容（临床应用请用 generate_clinical）。instruction 为空则按原内容优化。"""
    q = case.question(qid)
    if q is None or field not in FIELD_PROMPTS:
        return False
    if not llm.has_key():
        _demo_generate(q, case.case_text)
        return True
    tb_hits = _tb_query(q)
    original_refs = [r.model_copy(deep=True) for r in q.references]
    old_books = [r.model_copy(deep=True) for r in q.references if r.ref_type == "M"]
    cur = [r for r in q.references if r.ref_type != "M"]
    existing_manual = [r for r in q.references if r.source != REF_SOURCE_AUTO and r.ref_type != "M"]
    existing_books = old_books

    # 指令要求“综述/研究型”时按该类型重新检索 PubMed；否则沿用案例设置。
    doc_type = _doc_type_from_instruction(instruction, getattr(case, "ref_doc_type", "all"))
    pm_new: list[dict] = []
    if q.en_keywords:
        pm_new = pubmed.search_pubmed(q.en_keywords, retmax=8, doc_type=doc_type)
    merged: list[dict] = []
    seen: set[tuple] = set()
    # 明确要求“只要综述/只要研究型”且新检索有结果时，不再混入旧的异类文献
    base_cur = [] if (doc_type in ("review", "research") and pm_new) else [x.model_dump() for x in cur]
    for r in list(pm_new) + base_cur:
        key = (r.get("pmid") or "", (r.get("title") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    if pm_new:
        _log(f"regenerate_field {q.qid} 按“{doc_type}”重新检索 PubMed，命中 {len(pm_new)} 篇")
    elif q.en_keywords:
        _log(f"regenerate_field {q.qid} PubMed 无结果："
             + (pubmed.last_error() or pubmed.last_note() or "无命中"))

    cur_fmt = chr(10).join(f"[P{i}] {pubmed.format_reference(r)}"
                           for i, r in enumerate(merged, 1)) or "（无）"
    prompt = REGEN_PROMPT.format(
        case_text=_clip_case(case), q_text=q.text, q_type=q.qtype,
        learning="\n".join(f"【{b.heading}】{b.body}" for b in q.learning) or "（空）",
        tb=_fmt_tb_hits(tb_hits), current_refs=cur_fmt, human_style=_HUMAN_STYLE,
        task=FIELD_PROMPTS[field],
        instruction=instruction or "（无，请在此基础上优化表述与完整性）",
    )
    result = llm.chat_json(prompt, _GenOut)
    if result is None:
        return False
    if result.learning:
        old_by_heading = {_question_match_key(b.heading): b for b in q.learning}
        new_learning = []
        for i, b in enumerate(result.learning):
            if not b.get("body"):
                continue
            heading = sanitize_content(b.get("heading", ""))
            old = old_by_heading.get(_question_match_key(heading))
            if old is None and i < len(q.learning):
                old = q.learning[i]
            new_learning.append(LearningBlock(
                heading=heading,
                body=sanitize_content(b.get("body", "")),
                source=_canonical_source_from_markers(
                    b.get("source", "") or (old.source if old else ""), tb_hits),
                table=_coerce_table(b.get("table")) or (old.table if old else [])))
        q.learning = new_learning
    resolve_citations(q, tb_hits, pm_refs=merged,
                      manual_refs=existing_manual,
                      extra_refs=existing_manual + existing_books,
                      original_refs=original_refs)
    # 硬兜底：重新检索到了文献，但模型一条都没引用 → 强制纳入最相关 2~3 篇并补标注
    if merged and not any(r.ref_type == "J" for r in q.references):
        base = len(q.references)
        forced = merged[:3]
        for k, r in enumerate(forced):
            q.references.append(_mk_pm_ref(r))
            if q.learning:
                blk = q.learning[k % len(q.learning)]
                blk.body = _attach_marker(blk.body, base + k + 1)
        _log(f"regenerate_field {q.qid} 正文未引用期刊文献，强制纳入 {len(forced)} 篇并补标注")
    _trimmed = enforce_max_refs(q)
    if _trimmed:
        _log(f"regenerate_field {q.qid} 参考文献超出上限 {MAX_REFS}，已截断 {_trimmed} 条并重排标号")
    return True


def refresh_references(case: Case, q: Question, limit: int = 3) -> tuple[int, str]:
    """只重新检索 PubMed 并补进参考文献表，不改写学习内容。返回 (新增条数, 说明)。"""
    if not (q.en_keywords or "").strip():
        return 0, "该问题还没有英文检索词，请先在上方填写或重新生成"
    doc_type = getattr(case, "ref_doc_type", "all") or "all"
    pm = pubmed.search_pubmed(q.en_keywords, retmax=8, doc_type=doc_type)
    if not pm:
        reason = pubmed.last_error() or pubmed.last_note()
        return 0, "PubMed 没有检索到符合条件的文献" + (f"（{reason}）" if reason else "")
    existing = {(r.pmid or "", (r.title or "")[:80]) for r in q.references}
    base = len(q.references)
    added = 0
    for r in pm:
        if added >= limit:
            break
        key = (r.get("pmid") or "", (r.get("title") or "")[:80])
        if key in existing:
            continue
        q.references.append(_mk_pm_ref(r))
        if q.learning:
            blk = q.learning[added % len(q.learning)]
            num = base + added + 1
            if not re.search(rf"\[{num}\]", blk.body or ""):
                blk.body = _attach_marker(blk.body, num)
        existing.add(key)
        added += 1
    if added == 0:
        return 0, "检索到的文献都已在本问参考文献表中"
    _log(f"refresh_references {q.qid} 新增 {added} 篇（{doc_type}）")
    return added, ""


# ---------------------------------------------------------------- Markdown 表格 <-> 二维数组
def parse_md_table(text: str) -> list[list[str]]:
    """解析 Markdown 表格文本为二维数组；忽略分隔行（---）。"""
    rows: list[list[str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        if cells:
            rows.append(cells)
    return rows


def to_md_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [r + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(norm[0]) + " |"
    sep = "|" + "|".join([" --- "] * width) + "|"
    body = ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join([head, sep] + body)


def table_from_grid(rows) -> list[list[str]]:
    """st.data_editor 读回的行序列 → 规整表格：字符串化、None/NaN→空串、去全空行。"""
    out: list[list[str]] = []
    for row in rows or []:
        cells = []
        for v in row:
            if v is None or (isinstance(v, float) and v != v):   # NaN（空格子读回）
                cells.append("")
            else:
                cells.append(str(v))
        if any(c.strip() for c in cells):
            out.append(cells)
    return out


def apply_export_edits(case: Case, edits: dict) -> None:
    """把 ⑤ 区「导出前编辑」面板的暂存内容写入案例。

    edits 形如：
        {"title": str, "keywords": str 或 list, "summary_md": str(可选),
         "questions": {qid: {"text": str, "clinical": str,
                             "blocks": [{"heading","source","body","table"}, ...]}}}
    blocks 与 q.learning 按下标一一对应：越界忽略，不足不动；未提及的题目不修改。
    """
    if not isinstance(edits, dict):
        return
    title = edits.get("title")
    if isinstance(title, str) and title.strip():
        case.title = title.strip()
    kw = edits.get("keywords")
    if isinstance(kw, str):
        kw = [x.strip() for x in re.split(r"[;；,，]", kw) if x.strip()]
    if isinstance(kw, list):
        case.keywords = [str(x).strip() for x in kw if str(x).strip()]
    summary_md = edits.get("summary_md")
    if isinstance(summary_md, str) and summary_md.strip():
        nodes = markdown_to_summary_nodes(summary_md)
        if case.draft_summary_nodes:
            case.draft_summary_nodes = nodes
            case.draft_summary_signature = case.draft_summary_source_signature()
        elif case.summary_nodes:
            case.summary_nodes = nodes
    q_edits = edits.get("questions") or {}
    for q in case.questions:
        qe = q_edits.get(q.qid)
        if not isinstance(qe, dict):
            continue
        text = qe.get("text")
        if isinstance(text, str) and text.strip():
            q.text = text.strip()
        clinical = qe.get("clinical")
        if isinstance(clinical, str):
            q.clinical = clinical
        blocks = qe.get("blocks")
        if not isinstance(blocks, list):
            continue
        for j, be in enumerate(blocks):
            if j >= len(q.learning) or not isinstance(be, dict):
                continue
            blk = q.learning[j]
            for field in ("heading", "source", "body"):
                v = be.get(field)
                if isinstance(v, str):
                    setattr(blk, field, v)
            if be.get("table") is not None:
                blk.table = table_from_grid(be["table"])


# ---------------------------------------------------------------- 直接生成 + 智能体审校
class _DirectOut(BaseModel):
    learning: list[dict]


DIRECT_PROMPT = """你是医学教育专家。请直接回答这个 PBL 问题（不提供检索资料，凭你的医学知识作答）。

【病例】
{case_text}

【问题】{q_text}（类型：{q_type}）

请生成学习内容 learning：2~4 个小节，每节含：
- heading：小标题（如"补液扩容治疗概述"）；
- body：正文，采用分层编号的条理化结构——子要点用"（1）（2）"，再下级用"①②"，
  每个要点独占一行（用换行 \\n 分隔），关键处带具体数值/药名/标准；
- table：当小节内容适合表格呈现（药物对比、疾病鉴别对比、方案对比等）时，给出字符串
  二维数组（首行为表头，建议 3~5 列、2~8 行），body 写表格之外的补充；不适合则 table 为 []；
- source 一律留空字符串（出处由后续审校环节补充）。

内容要求：学习内容为通用医学知识，不要联系本病例、不要出现"该患者/本例"等病例表述
（对病例的具体分析在"实际应用"部分单独生成）；
**范围红线**：每个小节必须直接回答【问题】本身，禁止顺带写临床表现、诊断标准、治疗原则等与本题无关的内容；
各小节之间、同一小节内不得重复同一知识点或近似表述，内容务必精简。
""" + _QUALITY_BAR + """

{human_style}

格式禁则：禁止使用【】括号；编号只用（1）（2）与①②两级，
行首不得叠加两级编号（如"3.（3）"）；heading 不要自带序号。
只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}]}}"""


class _RefPick(BaseModel):
    ref_indexes: list[int]
    ref_notes: list[dict] = Field(default_factory=list)


PICK_PROMPT = """从下面的 PubMed 检索结果中挑出与问题最相关的文献（最多 5 条，按相关性排序），
并为每条写一句话推荐语（说明它为何值得读，20 字内）。只能从 P1..Pn 中选，不得虚构。

【问题】{q_text}
【检索结果】
{pm}

只输出 JSON：{{"ref_indexes":[1],"ref_notes":[{{"i":1,"relevance":""}}]}}"""


def generate_question_direct(case: Case, q: Question, use_pubmed: bool = True) -> tuple[bool, str]:
    """直接作答模式：LLM 凭自身医学知识先答学习内容（不依赖教材检索）；
    文献仍只从 PubMed 真实检索结果中挑选。审校与临床应用由后续步骤单独完成。

    返回 (是否成功, 说明)。失败时保留原有内容，不静默降级。
    """
    if not llm.has_key():
        _demo_generate(q, case.case_text)
        return True, "（演示模式：已填入演示内容，配置 API Key 后可生成正式内容）"
    tb_hits = _tb_query(q)
    result = llm.chat_json(DIRECT_PROMPT.format(
        case_text=_clip_case(case), q_text=q.text, q_type=q.qtype,
        human_style=_HUMAN_STYLE), _DirectOut)
    if result is None:
        _log(f"generate_question_direct {q.qid} 失败：" + (llm.last_error() or "模型未返回有效内容"))
        return False, "模型调用未返回有效内容，内容保留原样" + llm.error_hint() + "；详见 logs/pbl.log"
    q.learning = [LearningBlock(
        heading=sanitize_content(b.get("heading", "")),
        body=sanitize_content(b.get("body", "")), source="",
        table=_coerce_table(b.get("table")))
        for b in result.learning if b.get("body") or b.get("table")]
    # This mode intentionally does not have T# sources; do not attach every retrieved
    # book to every block. The review button adds precise textbook citations later.
    if use_pubmed and q.en_keywords:
        pm_refs = pubmed.search_pubmed(q.en_keywords, retmax=8,
                                       doc_type=getattr(case, "ref_doc_type", "all") or "all")
        if pm_refs:
            pick = llm.chat_json(PICK_PROMPT.format(q_text=q.text, pm=_fmt_pm_hits(pm_refs)), _RefPick)
            if pick:
                notes = {int(x.get("i", 0)): x.get("relevance", "")
                         for x in pick.ref_notes if isinstance(x, dict)}
                picked = [Reference(source=REF_SOURCE_AUTO, relevance=notes.get(idx, ""), **{
                    kk: pm_refs[idx - 1][kk]
                    for kk in ("title", "authors", "journal", "year", "volume",
                               "issue", "pages", "pmid", "doi", "abstract")})
                    for idx in pick.ref_indexes if 1 <= idx <= len(pm_refs)]
                manual = [r for r in q.references if r.source != REF_SOURCE_AUTO]
                q.references = picked + manual
                enforce_max_refs(q)
                return True, ""
        enforce_max_refs(q)
        return True, "PubMed 无检索结果，本问文献表为空" if not q.references else ""
    enforce_max_refs(q)
    return True, ""


class _ReviewOut(BaseModel):
    learning: list[dict]
    notes: str = ""


REVIEW_PROMPT = """你是严格的医学教案审校官。请审核并修订下面这道 PBL 问题的「学习内容」。

【病例】
{case_text}

【问题】{q_text}（类型：{q_type}）

【当前学习内容】
{learning}

【教材检索结果】（权威依据，含出处；为空表示未检索到教材）
{tb}

【当前参考文献】（编号 P1..Pn；修订后如仍需保留这些文献，请在正文对应处标注 [P编号]）
{current_refs}

审校要求：
1. 用教材检索结果核对学习内容：与教材表述有出入的按教材修正；教材要点有明显遗漏的补入；
2. 为引用教材内容的小节补上 source（依据检索结果的出处，如《内科学》（第10版）第三章）；
3. 保持分层编号结构（（1）①）；小节表格除非内容有误否则原样保留；
4. 检索结果未覆盖、无法核实的内容保留原样，不要编造，也不要删除；
5. 学习内容必须是**通用医学知识**：若出现联系本病例的表述（如"该患者/本例"），
   改写为通用医学表述（病例分析属于"实际应用"部分，不放进学习内容）；
5b. **范围红线**：所有小节必须直接回答【问题】本身。如果存在与本题无关的小节
   （如题目问机制却出现诊断标准或治疗原则），删除该小节；不要补充与本题无关的新小节；
6. **精简去重**：删除各小节之间重复或近似的知识点与表述，能删则删、能短则短；
7. 引用标注（重要）：在正文中，凡依据教材检索结果的句子句末标 [T编号]；
   凡依据当前参考文献的句子句末标 [P编号]（正文中已有的 [数字] 引用请改写为对应编号）；
   标注紧贴句末标点之前、与该句内容对应，不要为凑数标注；
   **尽量保留原有引用**：原句保留的，其引用编号一并保留，只有整句被删除时才可去掉对应引用；
   **没有任何标注的文献将不会保留在文献表里**。
8. 表达规范（审定后通读一遍）：删除空泛评价句（"具有重要的临床意义""提供了新的思路"）、万能开头结尾（"随着…的不断发展"）、名词化堆叠；**改写无信息量表述**——"某些 X""与 Y 相关""不容忽视""可能起重要作用"必须改写成具体对象（基因/分子/数值/药名），改不出来就整句删除；同义术语全文统一；"首先/其次/最后/综上所述"等路标词过量时精简；不改变任何数值与引用标注。
在 notes 里逐条说明改了什么（100 字内）；无修改则写"无修改"。

格式禁则：禁止使用【】括号；编号只用（1）（2）与①②两级，
行首不得叠加两级编号（如"3.（3）"）；heading 不要自带序号。
只输出 JSON：{{"learning":[{{"heading":"","body":"","table":[],"source":""}}],"notes":""}}"""


def review_question(case: Case, q: Question) -> str:
    """审校智能体：对照教材检索结果修订学习内容并补出处/引用标注，返回修改说明。"""
    if not llm.has_key():
        return "（演示模式：未配置 Key，跳过审校）"
    if not q.learning:
        return "（该问题尚无学习内容，请先生成再审校）"
    tb_hits = _tb_query(q, k=5)
    original_refs = [r.model_copy(deep=True) for r in q.references]
    old_books = [r.model_copy(deep=True) for r in q.references if r.ref_type == "M"]
    manual = [r.model_copy(deep=True) for r in q.references
              if r.ref_type != "M" and r.source != REF_SOURCE_AUTO]
    cur = [r for r in q.references if r.ref_type != "M"]
    cur_fmt = chr(10).join(f"[P{i}] {pubmed.format_reference(r)}"
                           for i, r in enumerate(cur, 1)) or "（无）"
    result = llm.chat_json(REVIEW_PROMPT.format(
        case_text=_clip_case(case), q_text=q.text, q_type=q.qtype,
        learning=chr(10).join(f"【{b.heading}】{b.body}" for b in q.learning) or "（空）",
        tb=_fmt_tb_hits(tb_hits), current_refs=cur_fmt,
    ), _ReviewOut)
    if result is None:
        return "（审校失败：模型调用出错，请重试）"
    if result.learning:
        old_by_heading = {_question_match_key(b.heading): b for b in q.learning}
        new_learning = []
        for i, b in enumerate(result.learning):
            if not b.get("body"):
                continue
            heading = sanitize_content(b.get("heading", ""))
            old = old_by_heading.get(_question_match_key(heading))
            if old is None and i < len(q.learning):
                old = q.learning[i]
            new_learning.append(LearningBlock(
                heading=heading,
                body=sanitize_content(b.get("body", "")),
                source=b.get("source", "") or (old.source if old else ""),
                table=_coerce_table(b.get("table")) if b.get("table") else (old.table if old else [])))
        q.learning = new_learning
    resolve_citations(q, tb_hits, pm_refs=[r.model_dump() for r in cur],
                      manual_refs=manual, extra_refs=manual + old_books,
                      original_refs=original_refs)
    enforce_max_refs(q)
    q.review_note = (result.notes or "无修改").strip()
    return q.review_note


# ---------------------------------------------------------------- 个人总结（层级目录）
class _SummaryOut(BaseModel):
    nodes: list[dict] = Field(default_factory=list)


SUMMARY_PROMPT = """你是 PBL 教案的结构整理者。下面是一次 PBL 作业的**小组问题清单**（编号 Q1..Qn）。

【病例】
{case_text}

【小组问题清单】
{questions}

请把这些问题**按主题归并成一份分层目录**（就是教案里「个人总结」那一节），要求：
1. 两级或三级：一级是主题大类（如「诊断依据」「发病机制」「治疗与用药」「人文与预防」），
   二级/三级是更具体的知识点；层级用 children 嵌套表示，最多三级；
2. **每个问题编号必须出现且只出现一次**（放在叶子节点的 qids 里），不得遗漏、不得重复；
   不得新增清单里没有的问题；
3. 同一主题下相近的问题并到同一个二级节点，便于对照阅读；
4. 节点 text 只写知识点本身，简洁（不超过 20 字），**不要自带编号**、不要以「问题」开头、
   不要出现「Q1」这类编号；
5. 一级主题按「基础机制 → 诊断 → 治疗/用药 → 人文/预防」的学习递进排序；
6. 至少 2 个一级主题；如某个问题确实归不进任何主题，单独放一个「其他」主题；
7. **不要为了凑层级而多加一层**：若某主题下只有一个知识点，就把该知识点直接写成二级节点，
   不要再套一个同义的中间层（反例：「1 基础机制 → 1.1 蛋白尿形成机制 → 1.1.1 蛋白尿的形成机制」，
   正例：「1 基础机制 → 1.1 蛋白尿的形成机制」）。

只输出 JSON：{{"nodes":[{{"text":"诊断依据","children":[
{{"text":"实验室检查","children":[{{"text":"血常规的临床意义","qids":["Q1"]}}]}}]}}]}}"""

_NUM_PREFIX = re.compile(
    r"^\s*(?:[（(]\s*[0-9一二三四五六七八九十]+\s*[）)]|[0-9]+\s*[.、．）)]|"
    r"[一二三四五六七八九十]+\s*[、.．]|[0-9]+(?:\.[0-9]+)+)\s*")
_Q_PREFIX = re.compile(r"^\s*问题\s*[0-9一二三四五六七八九十]+\s*[:：]\s*")


def _clean_node_title(text: str) -> str:
    """去掉模型自作主张加的编号与「问题N：」前缀。"""
    t = _Q_PREFIX.sub("", (text or "").strip())
    t = _NUM_PREFIX.sub("", t)
    return t.strip().strip("：:。;；").strip()[:120]


def _parse_summary(raw, valid: set[str], seen: set[str]) -> list[SummaryNode]:
    nodes: list[SummaryNode] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        text = _clean_node_title(str(r.get("text", "")))
        if not text:
            continue
        qids: list[str] = []
        for q in (r.get("qids") or []):
            qs = str(q).strip()
            if qs in valid and qs not in seen and qs not in qids:
                seen.add(qs)
                qids.append(qs)
        children = _parse_summary(r.get("children") or [], valid, seen)
        if not qids and not children:
            continue
        node = SummaryNode(text=text, qids=qids, children=children)
        if children:
            node.question_ids = list(dict.fromkeys(
                uid for child in children for uid in child.question_ids))
        nodes.append(node)
    return nodes


def _demo_summary(case: Case) -> list[SummaryNode]:
    """无 Key 时的目录：按问题类型分组，类型顺序即学习递进顺序。"""
    order = ["机制", "诊断", "治疗", "用药", "人文", "预防", "其他"]
    groups: dict[str, list[Question]] = {}
    for q in case.questions:
        groups.setdefault(q.qtype if q.qtype in order else "其他", []).append(q)
    nodes = []
    for t in order:
        if not groups.get(t):
            continue
            kids = [SummaryNode(text=_clean_node_title(q.text)[:20] or q.qid,
                                qids=[q.qid], question_ids=[q.uid])
                    for q in groups[t]]
            topic = SummaryNode(text=f"{t}相关", children=kids)
            topic.question_ids = [q.uid for q in groups[t]]
            nodes.append(topic)
    return nodes


def _norm_title(text: str) -> str:
    """比较用的归一化：去标点、去「的/与」这类虚词与常见后缀。"""
    t = re.sub(r"[\s（）()、，,。.：:；;·\-—_/]", "", text or "")
    for w in ("的临床意义", "临床意义", "相关", "检测", "指标", "分析", "探讨",
              "的", "与", "和", "及"):
        t = t.replace(w, "")
    return t.lower()


def _similar_title(a: str, b: str) -> bool:
    """两个标题是否在说同一件事（用于折叠「一层套一层」的同义嵌套）。"""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    sa, sb = set(na), set(nb)
    return len(sa & sb) / max(1, min(len(sa), len(sb))) >= 0.8


def _collapse_single(nodes: list[SummaryNode], top: bool = True) -> list[SummaryNode]:
    """折叠单链冗余层级：如「1 基础机制 → 1.1 蛋白尿形成机制 → 1.1.1 蛋白尿的形成机制」
    这种父子同义的套娃，直接提上来变成「1 基础机制 → 1.1 蛋白尿的形成机制」。

    一级主题不折叠——它始终充当分组标题（对齐「1.诊断依据 / 1.1 血常规的临床意义」的形态），
    哪怕该主题下只有一个知识点。
    """
    out: list[SummaryNode] = []
    for n in nodes or []:
        n.children = _collapse_single(n.children, top=False)
        if not top:
            while len(n.children) == 1 and _similar_title(n.text, n.children[0].text):
                child = n.children[0]
                n.text = child.text
                n.qids = list(dict.fromkeys(n.qids + child.qids))
                n.children = _collapse_single(child.children, top=False)
        out.append(n)
    return out


def _bind_summary_ids(case: Case) -> None:
    by_qid = {q.qid: q.uid for q in case.questions}
    def walk(node: SummaryNode) -> list[str]:
        if node.children:
            child_ids = [uid for child in node.children for uid in walk(child)]
            node.question_ids = []
            node.qids = list(dict.fromkeys(
                q.qid for uid in child_ids for q in case.questions if q.uid == uid))
            return child_ids
        if not node.question_ids:
            node.question_ids = [by_qid[qid] for qid in node.qids if qid in by_qid]
        node.qids = [q.qid for uid in node.question_ids for q in case.questions if q.uid == uid]
        return node.question_ids
    for node in case.summary_nodes:
        walk(node)


def summary_nodes_to_markdown(nodes: list[SummaryNode]) -> str:
    lines: list[str] = []
    def walk(items: list[SummaryNode], depth: int) -> None:
        for node in items:
            lines.append("  " * depth + node.text)
            walk(node.children, depth + 1)
    walk(nodes, 0)
    return "\n".join(lines)


def markdown_to_summary_nodes(markdown: str) -> list[SummaryNode]:
    flat = []
    for line in (markdown or "").splitlines():
        if not line.strip():
            continue
        level = len(line) - len(line.lstrip(" "))
        text = _clean_node_title(line.strip())
        if text:
            flat.append((level // 2, text))
    root: list[SummaryNode] = []
    stack: list[tuple[int, SummaryNode]] = []
    for level, text in flat:
        node = SummaryNode(text=text)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            root.append(node)
        stack.append((level, node))
    return root


DRAFT_SUMMARY_PROMPT = """你是 PBL 教案的个人问题总结设计者。
下面是一次 PBL 作业的**小组问题清单**（逐行，用户原始输入）。请把它们归类整理成一份分层目录。

【病例】
{case_text}

【小组问题清单】
{draft}

要求：
1. 这份目录是对上述小组问题的**归类整理**，不是重新出题：每个叶子必须逐字保留小组问题原文
   （仅允许去掉行首编号），不得改写措辞、不得合并、不得拆分、不得增删任何一条问题；
2. 一级主题用 3~5 个贴合病例学科的知识大类（如 病因学 / 发病机制 / 诊断与鉴别诊断 / 治疗 / 并发症），
   按基础机制→临床表现与诊断→治疗→并发症等递进；不要发明清单之外的新主题；
3. 归不进大类的小问题统一挂到「其他」类下，必要时用三级层级细分；
4. 保留问题中的关键术语与引号（如“无肌卫”“病理反射未引出”），不要丢失；
5. 不要生成答案正文；只输出 JSON：{{"nodes":[{{"text":"大类","children":[{{"text":"小组问题原文","children":[]}}]}}]}}"""


def _parse_outline_nodes(raw) -> list[SummaryNode]:
    """解析不绑定题库的独立总结目录；叶子不要求 qids。"""
    out: list[SummaryNode] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        text = _clean_node_title(str(item.get("text", "")))
        children = _parse_outline_nodes(item.get("children") or [])
        if text:
            out.append(SummaryNode(text=text, children=children))
    return out


def _outline_text(nodes: list[SummaryNode], depth: int = 0) -> str:
    lines: list[str] = []
    for node in nodes:
        lines.append("  " * depth + node.text)
        lines.extend(_outline_text(node.children, depth + 1).splitlines())
    return "\n".join(lines)


def generate_draft_summary(case: Case, draft: str = "") -> tuple[bool, str]:
    """从草稿先生成独立个人问题总结，不读取或修改当前问题清单。"""
    draft = (draft or case.question_draft or "").strip()
    if not draft:
        return False, "请先填写问题草稿，再生成个人问题总结。"
    case.question_draft = draft
    if not llm.has_key():
        leaves = [x.strip() for x in re.split(r"[\n；;]", draft) if x.strip()]
        case.draft_summary_nodes = [SummaryNode(text="草稿问题主题",
                                                 children=[SummaryNode(text=_clean_node_title(x)) for x in leaves])]
        case.draft_summary_signature = case.draft_summary_source_signature()
        return True, "（演示模式）已根据草稿生成独立个人问题总结。"
    result = llm.chat_json(DRAFT_SUMMARY_PROMPT.format(
        case_text=_clip_case(case), draft=draft[:6000]), _SummaryOut)
    if result is None:
        return False, "个人问题总结生成失败：" + (llm.last_error() or "模型未返回有效内容")
    nodes = _parse_outline_nodes(result.nodes)
    if not nodes:
        return False, "个人问题总结生成失败：模型没有返回有效目录。"
    case.draft_summary_nodes = nodes
    case.draft_summary_signature = case.draft_summary_source_signature()
    return True, f"已生成独立个人问题总结：{len(nodes)} 个一级主题。"


def _summary_question_leaves(nodes: list[SummaryNode]) -> list[tuple[str, str]]:
    """Return (leaf text, inherited topic path) in the user's exact reading order."""
    leaves: list[tuple[str, str]] = []
    def walk(items: list[SummaryNode], parents: tuple[str, ...] = ()) -> None:
        for node in items:
            path = parents + (node.text.strip(),)
            if node.children:
                walk(node.children, path)
            elif node.text.strip():
                leaves.append((node.text.strip(), " ".join(path)))
    walk(nodes)
    return leaves


def _question_type_from_summary(path: str) -> str:
    text = path.lower()
    if any(x in text for x in ("治疗", "用药", "药物")):
        return "治疗"
    if any(x in text for x in ("诊断", "鉴别", "检查")):
        return "诊断"
    if any(x in text for x in ("机制", "病因", "发病")):
        return "机制"
    if any(x in text for x in ("预防", "康复")):
        return "预防"
    return "其他"


def generate_questions_from_draft_summary(case: Case) -> tuple[bool, str]:
    """Copy each summary leaf verbatim, in order, into the editable question bank."""
    if not case.draft_summary_nodes:
        return False, "请先从问题草稿生成个人问题总结。"
    if case.draft_summary_is_stale():
        return False, "问题草稿已变化，请先重新生成个人问题总结。"
    leaves = _summary_question_leaves(case.draft_summary_nodes)
    if not leaves:
        return False, "个人问题总结中没有可生成题目的叶子知识点。"

    old_questions = list(case.questions)
    old_selected = set(case.answer_question_ids if case.answer_question_ids is not None
                       else [q.uid for q in old_questions])
    proposed = [Question(qid=f"Q{i}", text=text, qtype=_question_type_from_summary(path))
                for i, (text, path) in enumerate(leaves, 1)]
    case.questions = merge_question_bank(old_questions, proposed)
    case.rename_qids()
    old_ids = {q.uid for q in old_questions}
    case.answer_question_ids = [q.uid for q in case.questions
                                if q.uid not in old_ids or q.uid in old_selected]
    return True, (f"已按个人问题总结的原顺序和原文生成 {len(case.questions)} 道题；"
                  "总结保持独立，可继续删减或添加题目。")


def generate_personal_summary(case: Case, instruction: str = "") -> tuple[bool, str]:
    """生成「个人总结」分层目录：把小组问题按主题归并成 1. / 1.1 / 1.2 结构。

    与「小组问题」互不影响——小组问题永远是用户输入的原始清单（原顺序），
    这里只新建一棵目录树，叶子节点用 qid 指向原问题。
    返回 (是否成功, 说明信息)。
    """
    if not case.questions:
        return False, "还没有问题，请先在 ① 区整理/输入小组问题。"

    valid = {q.qid for q in case.questions}
    if not llm.has_key():
        case.summary_nodes = _demo_summary(case)
        _bind_summary_ids(case)
        case.summary_question_signature = case.question_signature()
        return True, "（演示模式）已按问题类型生成目录，配置 API Key 后可由模型按主题归并。"

    qs = "\n".join(f"{q.qid}: {q.text}" for q in case.questions)
    result = llm.chat_json(SUMMARY_PROMPT.format(
        case_text=_clip_case(case), questions=qs), _SummaryOut)
    if result is None:
        _log("generate_personal_summary 失败：" + (llm.last_error() or "模型未返回有效内容"))
        return False, "目录生成失败" + llm.error_hint() + "，请重试。"

    seen: set[str] = set()
    nodes = _collapse_single(_parse_summary(result.nodes, valid, seen))
    if not nodes:
        _log("generate_personal_summary 失败：解析后为空")
        return False, "目录生成失败（模型输出不含有效节点），请重试。"

    missing = [q.qid for q in case.questions if q.qid not in seen]
    if missing:
        kids = []
        for qid in missing:
            q = case.question(qid)
            if q is not None:
                kids.append(SummaryNode(text=_clean_node_title(q.text)[:20] or qid, qids=[qid]))
        nodes.append(SummaryNode(text="其他", children=kids))
        _log(f"generate_personal_summary 补入未覆盖问题：{', '.join(missing)}")

    case.summary_nodes = nodes
    _bind_summary_ids(case)
    case.summary_question_signature = case.question_signature()
    n1 = len(nodes)
    n_leaf = _count_leaves(nodes)
    msg = f"已生成 {n1} 个主题、{n_leaf} 个知识点，覆盖全部 {len(case.questions)} 个问题。"
    if missing:
        msg += f"（其中 {len(missing)} 个未归类，已放入「其他」）"
    return True, msg


def _count_leaves(nodes: list[SummaryNode]) -> int:
    seen: set[str] = set()
    def walk(items: list[SummaryNode]) -> None:
        for node in items:
            if not node.children:
                seen.update(node.question_ids or node.qids)
            walk(node.children)
    walk(nodes)
    return len(seen)


def _summary_qids_in_tree(node: SummaryNode, case: Case | None) -> list[str]:
    by_uid = {q.uid: q.qid for q in case.questions} if case else {}
    if node.children:
        return list(dict.fromkeys(qid for child in node.children
                                  for qid in _summary_qids_in_tree(child, case)))
    if case and node.question_ids:
        return [by_uid[uid] for uid in node.question_ids if uid in by_uid]
    return list(node.qids)


def summary_numbering(nodes: list[SummaryNode], case: Case | None = None) -> list[tuple[int, str, str, list[str]]]: 
    """目录树 -> [(层级, 编号, 标题, 显示 QID)]，供 Word/界面渲染。"""
    out: list[tuple[int, str, str, list[str]]] = []
    def walk(items: list[SummaryNode], prefix: str, depth: int) -> None:
        for i, n in enumerate(items, 1):
            num = f"{prefix}{i}" if not prefix else f"{prefix}.{i}"
            qids = _summary_qids_in_tree(n, case)
            out.append((depth, num, n.text, qids))
            if n.children:
                walk(n.children, num, depth + 1)

    walk(nodes, "", 1)
    return out


def summary_question_order(nodes: list[SummaryNode], case: Case | None = None) -> list[str]:
    """Directory depth-first stable IDs; maps legacy QIDs when supplied a case."""
    uid_by_qid = {q.qid: q.uid for q in case.questions} if case else {}
    order: list[str] = []
    def walk(items: list[SummaryNode]) -> None:
        for node in items:
            if not node.children:
                if node.question_ids:
                    order.extend(node.question_ids)
                elif case:
                    order.extend(uid_by_qid[qid] for qid in node.qids if qid in uid_by_qid)
                else:
                    order.extend(node.qids)
            walk(node.children)
    walk(nodes)
    return list(dict.fromkeys(order))


def summary_qid_order(nodes: list[SummaryNode]) -> list[str]:
    """Compatibility helper returning QIDs on leaf nodes only."""
    out: list[str] = []
    def walk(items: list[SummaryNode]) -> None:
        for node in items:
            if not node.children:
                out.extend(node.qids)
            walk(node.children)
    walk(nodes)
    return list(dict.fromkeys(out))


def selected_questions(case: Case) -> list[Question]:
    selected = set(case.answer_question_ids or [])
    return [q for q in case.questions if q.uid in selected]


def ordered_questions(case: Case, selected_only: bool = False) -> list[Question]:
    """全量题按总结排序；selected_only 用于实际答案/生成范围。"""
    source = selected_questions(case) if selected_only else list(case.questions)
    if not (case.word_follow_summary and case.summary_nodes):
        return source
    order = summary_question_order(case.summary_nodes, case)
    pos = {uid: i for i, uid in enumerate(order)}
    idx = {q.uid: i for i, q in enumerate(source)}
    return sorted(source, key=lambda q: (pos.get(q.uid, 10 ** 6), idx.get(q.uid, 0)))


# ---------------------------------------------------------------- 临床应用（病例+问题+学习内容 单独生成）
class _ClinicalOut(BaseModel):
    clinical: str


CLINICAL_PROMPT = """你是临床医学教育专家。请基于病例、问题和已生成的学习内容，
撰写该 PBL 问题的「临床应用」部分——把学习内容的知识应用到本病例的具体诊疗分析中。

【病例】
{case_text}

【问题】{q_text}（类型：{q_type}）

【学习内容】
{learning}

要求：
1. 篇幅：250 字左右，**最多不得超过 500 字**；
2. 只写"把知识用到本例上"的内容：结合病例的具体症状、体征、检验数值、用药，
   给出针对该患者的判断、取舍与处置建议；
3. **不得重复学习内容**：学习内容是通用知识，这里不要复述概念、标准、原理，
   直接写本例的分析与决策；段落之间、句子之间也不得重复；
{human_style}

4. 不需要标注参考文献出处，正文中不要出现 [数字] 引用标记；
5. 医学表述严谨，不得编造病例中没有的信息。
{extra}
只输出 JSON：{{"clinical":""}}"""


def generate_clinical(case: Case, q: Question, instruction: str = "") -> tuple[bool, str]:
    """临床应用单独生成：输入病例+问题+学习内容，模型撰写两者的联系。不标注文献。

    返回 (是否成功, 诊断信息)——失败原因会写入 logs/pbl.log 并返回给界面显示。
    """
    if not q.learning:
        _log(f"generate_clinical {q.qid} 失败：没有学习内容")
        return False, "该问题还没有学习内容，请先生成学习内容再试。"
    if not llm.has_key():
        if not q.clinical:
            q.clinical = f"（演示）结合本病例，围绕「{q.text}」分析其临床表现与处置思路。配置 API Key 后自动生成。"
        return True, "（演示模式）已填入演示文本，配置 API Key 后可生成正式内容。"
    prompt = CLINICAL_PROMPT.format(
        case_text=_clip_case(case), q_text=q.text, q_type=q.qtype,
        learning="\n".join(f"【{b.heading}】{b.body}" for b in q.learning) or "（空）",
        extra=f"【用户补充要求】{instruction}\n" if instruction else "",
        human_style=_HUMAN_STYLE,
    )
    _log(f"generate_clinical {q.qid} 开始调用 {llm.PROVIDER}/{llm.MODEL}（学习内容 {len(q.learning)} 节）")
    result = llm.chat_json(prompt, _ClinicalOut)
    if result is None or not result.clinical:
        _log(f"generate_clinical {q.qid} 失败：{llm.last_error() or '模型未返回有效内容'}"
             f"（has_key={llm.has_key()}）")
        return False, "模型调用未返回有效内容，请重试" + llm.error_hint() + "；详见 logs/pbl.log"
    q.clinical = sanitize_content(re.sub(r"\[\d+\]", "", result.clinical))
    _log(f"generate_clinical {q.qid} 成功，{len(q.clinical)} 字")
    return True, f"已生成初稿（{len(q.clinical)} 字），可直接在下方修改。"



# ---------------------------------------------------------------- 关键词
class _Keywords(BaseModel):
    keywords: list[str]


def _ground_keywords(case: Case, kws: list[str]) -> list[str]:
    """关键词落地：优先保留确实出现在病例/问题原文中的词；不足 3 个则全部保留。"""
    hay = re.sub(r"\s+", "", case.case_text + "\n" + "\n".join(q.text for q in case.questions))

    def hit(k: str) -> bool:
        k2 = re.sub(r"\s+", "", k)
        if not k2:
            return False
        if k2 in hay:
            return True
        parts = [x for x in re.split(r"[、，,；;（）()·/]", k2) if len(x) >= 2]
        return any(x in hay for x in parts)

    kept = [k for k in kws if hit(k)]
    return kept if len(kept) >= 3 else kws


def generate_keywords(case: Case) -> list[str]:
    """从病例与问题提炼关键词；优先选用病例/问题原文中出现的词语。"""
    if llm.has_key():
        qa = "\n".join(f"{q.qid} {q.text}" for q in case.questions)
        prompt = (f"【病例】\n{_clip_str(case.case_text)}\n\n【问题清单】\n{qa}\n\n"
                  "请提炼 3~6 个能概括本案例核心知识的中文检索关键词。"
                  "关键词必须优先选用病例原文或问题中出现的词语（疾病名、症状、检查、治疗等），"
                  "不要生造病例里没有的概括词。"
                  '只输出 JSON：{"keywords": ["词1","词2"]}')
        result = llm.chat_json(prompt, _Keywords)
        if result and result.keywords:
            kws = [k.strip() for k in result.keywords if k.strip()][:6]
            return _ground_keywords(case, kws)
    seen: list[str] = []
    for q in case.questions:
        for w in re.split(r"[\s；,，]+", q.cn_keywords or ""):
            w = w.strip()
            if w and w not in seen:
                seen.append(w)
    return seen[:6]


# ---------------------------------------------------------------- 疾病中心思维导图
class _DiseaseMM(BaseModel):
    disease: str
    mindmap_md: str


# 默认一级分支框架（多数老师按这个看）；用户可在 ③ 区改成自己老师的要求。
# 注意：预防默认**不单列**（很多老师要求把它并进治疗与预后），需要时可自行加回。
DEFAULT_MM_BRANCHES = ["病因学（病因、诱因、危险因素）", "发病机制", "诊断及鉴别诊断",
                       "治疗", "预后"]

DISEASE_MM_PROMPT = """你是医学教育专家。一个 PBL 病例跨多次课学习，现在全部课程结束，
请以疾病为中心生成一张总结性知识思维导图，供学生整合全部学习内容，并标出哪些内容是**本病例特有**的。

【病例】
{case_text}

【各问题学习要点汇总】
{qa}

【用户提供的补充资料】（从上传的 Word/PDF 提取；为空表示无）
{materials}

【文献分享的核心发现】（学生已做过的文献分享；为空表示无，此时不要写「前沿」分支）
{paper}

【本例要采用的一级分支框架】（老师指定，顺序即导图顺序）
{branches}

【老师对导图的其他要求】（可能为空）
{requirement}

要求：
1. 中心词为「核心疾病名（分型）＋关键分期/合并症」，如「IgA肾病（原发性）合并CKD 4期及高血压」
   （不含"病例"等字样），分期与合并症信息取自病例；
2. 一级分支按上面的框架写，最多四级；**老师要求不单列、或本例确实没有对应内容的分支，不要留空壳**，
   把它要讲的内容并入最贴合的分支里体现；一级分支合计 4~8 个；
   **「预防」默认不单列**：如框架里没有它，就把预防内容落到「治疗」（二级预防、随访、患者教育）
   与「病因学」（避免诱因、控制危险因素）里，预防类要点行内标注适用级别，如「【一级预防】清除感染灶」
   「【三级预防】延缓CKD进展」；
3. 每个一级分支下 2~6 个二级要点，要点必须具体（含关键数值、指标、药名、分型、标准），可设三级/四级；
   **每条要点不超过 40 字**；病例要点可到 48 字，只留最有说服力的数值（别把整张化验单搬进来），
   信息确实多就拆成同一父节点下的多条要点；
4. **必须结合本病例**：凡是这个病人身上的具体表现、数值、检查结果、病理所见（如血压 160/100、
   尿蛋白 2+、系膜区 IgA 沉积），都作为对应分支下的末级要点写进去，并用 ==要点== 包起来
   （Markdown 高亮语法，渲染时会标黄加下划线）；纯疾病通用知识不要加 ==；
5. **禁止单列「结合病例」「病例特点」「本例情况」这类分支**——病例内容一律按第 4 条就地标注，
   分散落在各自所属的分支里；
6. 如上方给了「文献分享的核心发现」，在机制或治疗分支下增设「前沿：核心机制/药物（期刊 年份）」
   子分支，纳入其关键发现（2~4 条），期刊与年份写在分支名里；
7. 内容必须来自上方学习要点、病例、补充资料与文献分享，不得编造；补充资料中的要点应尽量纳入；
8. mindmap_md 用 Markdown 无序列表缩进格式（"- " 两空格一层），第一层是一级分支。

只输出 JSON：{{"disease":"疾病名","mindmap_md":"- 病因学\\n  - 感染后免疫\\n  - ==本例：咽痛后 3 天出现血尿=="}}"""


def generate_disease_mindmap(case: Case, materials_text: str = "",
                             requirement: str | None = None,
                             branches: list[str] | None = None,
                             provider: str | None = None,
                             model: str | None = None):
    """以疾病为中心的总结性知识导图（最后一节课结束后生成）。

    materials_text：用户上传的 Word/PDF 资料全文（可选）。
    requirement：老师对导图的具体要求（为空则用 case.mm_requirement）。
    branches：一级分支框架（为空则用 case.mm_branches，再空则用 DEFAULT_MM_BRANCHES）。
    provider/model：按次覆盖全局 .env（如导图单独走 DeepSeek）。
    病例相关内容以 ==要点== 标记，渲染时标黄。返回 MindmapNode 或 None。
    """
    if requirement is not None:
        case.mm_requirement = requirement
    if branches is not None:
        case.mm_branches = [b for b in branches if b.strip()]
    req = (case.mm_requirement or "").strip()
    brs = [b for b in (case.mm_branches or DEFAULT_MM_BRANCHES) if b.strip()]

    paper = case.ppt.paper if case.ppt is not None else None
    if paper is not None:
        paper_desc = "；".join(x for x in [
            (getattr(paper, "title_cn", "") or getattr(paper, "title_en", "")),
            (getattr(paper, "conclusion", "") or ""),
            (getattr(paper, "case_link", "") or "")] if x)[:400] or "（无）"
    else:
        paper_desc = "（无）"

    if llm.has_key():
        qa_lines = []
        for q in case.questions:
            pts = []
            for b in q.learning:
                pts.append(f"{b.heading}：{' '.join(b.body.split())[:120]}")
            refs = [r.title[:50] for r in q.references[:2]]
            qa_lines.append(f"{q.qid} {q.text}\n  " + "\n  ".join(pts)
                            + (f"\n  文献：{'；'.join(refs)}" if refs else ""))
        branch_lines = "\n".join(f"{i}. {b}" for i, b in enumerate(brs, 1))
        prompt = DISEASE_MM_PROMPT.format(
            case_text=_clip_str(case.case_text), qa="\n".join(qa_lines)[:4000],
            materials=(materials_text or "（无）")[:3000], paper=paper_desc,
            branches=branch_lines, requirement=(req or "（无特别要求，按上面框架即可）"))
        result = llm.chat_json(prompt, _DiseaseMM, provider=provider, model=model)
        if result is not None:
            from . import mindmap
            return mindmap.markdown_to_node(result.mindmap_md, root_text=result.disease)
    return _demo_disease_mindmap(case, brs)


def _demo_disease_mindmap(case: Case, branches: list[str] | None = None):
    """无 Key 降级：按分支框架 + 问题/学习小节骨架拼一张结构导图。"""
    disease = re.split(r"[（(的]", case.title or "核心疾病")[0].strip() or "核心疾病"
    root = MindmapNode(text=disease)
    brs = branches or DEFAULT_MM_BRANCHES
    for b in brs:
        root.children.append(MindmapNode(text=b))
    for q in case.questions:
        branch = MindmapNode(text=q.text[:20], children=[
            MindmapNode(text=b.heading or "要点") for b in q.learning])
        root.children.append(branch)
    return root


# ---------------------------------------------------------------- PPT 大纲
class _Outline(BaseModel):
    slides: list[dict]  # {layout, title, subtitle, bullets, body}


PPT_PROMPT = """你是医学教育专家。基于以下 PBL 案例生成课堂 PPT 大纲。

【案例】
课程：{course}　对象：{audience}　标题：{title}
病例摘要：
{case_text}
问题与要点：
{qa}

【要求】
用途：{purpose}；期望页数约 {n} 页（含封面与结尾，可上下浮动 2 页）。
layout 只能取：cover（封面）、case（病例展示）、overview（问题总览）、
question（某问题的学习要点，每个问题一页或多页）、clinical（临床应用讨论页）、
refs（参考文献精选，可 1~2 页）、summary（总结/反思页）。
bullets 每条不超过 25 字；question/clinical 页的 bullets 要落到具体知识点。
只输出 JSON：{{"slides":[{{"layout":"cover","title":"","subtitle":"","bullets":[],"body":""}}]}}"""


def build_ppt_outline(case: Case, purpose: str, n_slides: int) -> list[dict]:
    """返回 outline dict 列表。无 Key 时给程序化演示大纲。"""
    qa = []
    for q in case.questions:
        pts = []
        for b in q.learning[:2]:
            pts += [b.heading] + [x.strip() for x in b.body.split("\n") if x.strip()][:3]
        refs = [r.title[:40] for r in q.references[:2]]
        qa.append(f"{q.qid} {q.text}\n  要点：{'；'.join(pts[:6])}\n  文献：{'；'.join(refs)}")
    prompt = PPT_PROMPT.format(
        course=case.course, audience=case.audience, title=case.title,
        case_text=case.case_text[:1500], qa="\n".join(qa)[:3000],
        purpose=purpose, n=n_slides,
    )
    result = llm.chat_json(prompt, _Outline)
    if result is None:
        return _demo_outline(case)
    slides = []
    for s in result.slides:
        layout = s.get("layout", "bullets")
        if layout not in ("cover", "case", "overview", "question", "clinical", "refs", "summary"):
            layout = "bullets"
        slides.append({
            "layout": layout, "title": s.get("title", ""),
            "subtitle": s.get("subtitle", ""),
            "bullets": [b for b in s.get("bullets", []) if b],
            "body": s.get("body", ""),
        })
    return slides


def _demo_outline(case: Case) -> list[dict]:
    out = [{"layout": "cover", "title": case.title, "subtitle": f"{case.course} · PBL"},
           {"layout": "case", "title": "病例摘要", "body": case.case_text[:400]},
           {"layout": "overview", "title": "学习问题", "bullets": [f"{q.qid} {q.text}" for q in case.questions]}]
    for q in case.questions:
        out.append({"layout": "question", "title": f"{q.qid} {q.text}",
                    "bullets": [b.heading for b in q.learning] or ["（待生成）"]})
        out.append({"layout": "clinical", "title": f"{q.qid} 临床应用",
                    "body": q.clinical[:300]})
    out.append({"layout": "summary", "title": "总结与反思", "bullets": ["知识收获", "临床思维", "有待深入的问题"]})
    return out
