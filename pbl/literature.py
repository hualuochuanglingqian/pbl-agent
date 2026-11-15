"""文献分享：文献检索（PubMed，可按年份/影响因子/文献类型筛选）+ 影响因子参考
+ PMC 开放图片 + AI 解读（一页速览所需的研究过程、核心结论、与病例的联系）。

铁律：候选文献全部来自 PubMed 真实检索（默认排除综述/社论/评论等，可切换为只要综述或全部）；
图片全部来自 PMC 开放获取全文，AI 只做挑选与解读，不生成图片。
"""
from __future__ import annotations

import json
import re
import tarfile
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import requests
from pydantic import BaseModel, Field

from . import llm, net, pubmed
from .schema import Case, LitCandidate, PaperFigure, PaperShare

IF_FILE = Path(__file__).parent / "journal_if.json"
FIG_DIR = Path(__file__).resolve().parent.parent / "knowledge_base" / "lit_figs"

# 排除的非研究型文献类型（综述、社论、评论、通讯、病例报道等）
EXCLUDE_TYPES = {
    "review", "systematic review", "meta-analysis", "editorial", "comment", "letter",
    "published erratum", "retraction of publication", "retracted publication",
    "news", "newspaper article", "biography", "congress", "overall", "preprint",
    "case reports", "historical article", "address", "autobiography", "interview",
    "patient education handout", "practice guideline", "guideline",
    "consensus development conference", "consensus development conference, nih",
    "lecture", "video-audio media", "webcasts", "clinical conference",
}
SEARCH_YEARS = 10           # 默认只看近 10 年（可用 LitFilter 覆盖）
POOL = 40                   # 每级检索池大小
OA_POOL = 150               # 取图模式每级深检池（开放期刊在相关度排序中偏后，需加深）
SHOW = 15                   # 展示候选数

# 综述类文献类型（doc_type="review" 时只保留这些）
REVIEW_TYPES = {"review", "systematic review", "meta-analysis",
                "scoping review", "narrative review", "umbrella review"}


def _headers(session: requests.Session) -> None:
    net.ensure_network()
    session.trust_env = not net.direct_mode()


# ---------------------------------------------------------------- 影响因子
_if_cache: dict | None = None


def _if_table() -> dict:
    global _if_cache
    if _if_cache is None:
        try:
            data = json.loads(IF_FILE.read_text(encoding="utf-8"))
            _if_cache = {k.strip().lower(): v for k, v in data.items()
                         if isinstance(v, (int, float))}
        except (OSError, ValueError):
            _if_cache = {}
    return _if_cache


def journal_if(journal: str) -> float:
    """期刊影响因子（内置近年参考值）；未收录返回 0。"""
    if not journal:
        return 0.0
    t = _if_table()
    j = re.sub(r"\s+", " ", journal.strip().lower()).rstrip(".")
    variants = [j, re.sub(r"^(the )", "", j)]
    for sep in (" : ", " :", ": an ", " ("):
        if sep in j:
            variants.append(j.split(sep)[0].strip())
    for v in variants:
        if v in t:
            return float(t[v])
    return 0.0


# ---------------------------------------------------------------- 检索
def _parse_types(a: ET.Element) -> list[str]:
    return [(t.text or "").strip() for t in a.findall("PublicationTypeList/PublicationType")]


def search_research(query: str, retmax: int = POOL, flt=None) -> list[dict]:
    """文献检索：按 LitFilter 限定年份区间 / 影响因子区间 / 文献类型。

    检索式多级降级：原文（AND 链）→ 逐词 OR → 去引号平铺 → 首词；
    各级结果**累积合并**（按 PMID/DOI 去重），直到候选足够才停止——
    避免严格检索式只命中一两篇就提前返回。flt.oa_only=True 时追加开放获取过滤。
    返回按「检索层级优先、再按可取图、再按 IF 降序」排序。
    """
    if not query.strip():
        return []
    if flt is None:
        from .schema import LitFilter
        flt = LitFilter()
    oa_only = bool(flt.oa_only)
    session = requests.Session()
    _headers(session)
    mindate, maxdate, _window = flt.window()

    def _esearch(term: str, n: int | None = None) -> list[str]:
        r = session.get(
            f"{pubmed.EUTILS}/esearch.fcgi",
            params={"db": "pubmed", "term": term, "retmode": "json",
                    "retmax": n or retmax,
                    "tool": pubmed.TOOL, "email": pubmed.EMAIL,
                    "mindate": mindate, "maxdate": maxdate,
                    "datetype": "pdat", "sort": "relevance"},
            timeout=pubmed.TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("esearchresult", {}).get("idlist", [])

    suffix = " AND free full text[sb]" if oa_only else ""
    terms = [x.strip().strip('"') for x in re.split(r"\s+AND\s+", query, flags=re.I)
             if x.strip()]
    candidates = [query]
    if len(terms) > 1:
        candidates.append(" OR ".join(f'"{x}"' if " " in x else x for x in terms))
        candidates.append(" ".join(terms))
        candidates.append(terms[0])

    def _collect(extra_suffix: str, pool: int = POOL) -> list:
        """按层级累积检索；pool 为每级取用的相关度前 N 篇。返回排序后的完整候选列表。"""
        collected: dict = {}                  # key -> (层级, 记录)；层级越小越精确
        for level, term in enumerate(dict.fromkeys(candidates)):
            try:
                ids = _esearch(term + extra_suffix, pool)
                if not ids:
                    continue
                r2 = session.get(
                    f"{pubmed.EUTILS}/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml",
                            "tool": pubmed.TOOL, "email": pubmed.EMAIL},
                    timeout=60,
                )
                r2.raise_for_status()
                root = ET.fromstring(r2.content)
            except Exception as e:
                print(f"[literature] 检索失败（{term[:40]}）: {e}")
                continue
            for r in _parse_and_filter(root, flt):
                key = r["pmid"] or r["doi"] or r["title"]
                if key not in collected or level < collected[key][0]:
                    collected[key] = (level, r)
            if len(collected) >= (pool if oa_only else SHOW):
                break
        items = sorted(collected.values(),
                       key=lambda x: (x[0], -int(x[1]["oa"]), -x[1]["if_value"]))
        return [r for _, r in items]

    if oa_only:
        # 「只看可取图」承诺：深检（每级前 150 篇）+ 严格过滤到实测可下载的出版社；
        # 不足上限时再补一轮不带 OA 后缀的深检（双路合并，尽量凑满候选）
        verified = [r for r in _collect(suffix, OA_POOL) if r["oa"]]
        if len(verified) < SHOW:
            extra = [r for r in _collect("", OA_POOL) if r["oa"]]
            seen = {r["pmid"] or r["doi"] for r in verified}
            verified += [r for r in extra if (r["pmid"] or r["doi"]) not in seen]
        if verified:
            return verified[:SHOW]
        return []
    result = _collect(suffix)
    return result[:SHOW]


def _parse_and_filter(root: ET.Element, flt=None) -> list[dict]:
    """解析 efetch XML 并按筛选条件过滤文献。

    doc_type：research=排除综述/指南/社论/病例报道；review=只保留综述与荟萃分析；
              all=不做类型过滤。
    if_min/if_max：影响因子区间。注意未收录进 journal_if.json 的期刊 if_value=0，
              设了 if_min 就会被一并排除。
    """
    doc_type = getattr(flt, "doc_type", "research") or "research"
    if_min = getattr(flt, "if_min", 0.0) or 0.0
    if_max = getattr(flt, "if_max", 0.0) or 0.0
    out = []
    for art in root.iter("PubmedArticle"):
        cit = art.find("MedlineCitation")
        a = cit.find("Article") if cit is not None else None
        if a is None:
            continue
        types = _parse_types(a)
        low_types = {t.lower() for t in types}
        is_review = bool(low_types & REVIEW_TYPES)
        if doc_type == "review" and not is_review:
            continue
        if doc_type == "research" and (low_types & EXCLUDE_TYPES):
            continue
        at = a.find("ArticleTitle")
        title = " ".join("".join(at.itertext()).split()) if at is not None else ""
        if not title or title.strip().lower() in ("[not available]", "[not available]."):
            continue
        tl = title.lower().rstrip(".")
        # 类型标记不可靠时，用标题措辞兜底排除综述/评论体
        if doc_type != "all" and any(k in tl for k in (
                "a systematic review", "meta-analysis", "scoping review",
                "narrative review", "an overview", "a review of",
                "umbrella review", "what is ", "we dissent",
                "consensus document", "consensus statement",
                "in reply", "reply to ", "lessons from",
                "point of view", "viewpoint", "perspective on",
                "editorial", "commentary")):
            continue
        abstract = " ".join("".join(t.itertext()).strip()
                            for t in a.findall("Abstract/AbstractText"))
        journal = (a.findtext("Journal/Title")
                   or a.findtext("Journal/ISOAbbreviation") or "").strip()
        yr = (a.findtext("Journal/JournalIssue/PubDate/Year")
              or a.findtext("Journal/JournalIssue/PubDate/MedlineDate")
              or a.findtext("ArticleDate/Year") or "").strip()[:4]
        authors = []
        for au in a.findall("AuthorList/Author"):
            last = (au.findtext("LastName") or "").strip()
            fore = (au.findtext("ForeName") or "").strip()
            if last:
                authors.append(f"{last} {''.join(p[0].upper() for p in fore.split() if p)}")
        pmid = (cit.findtext("PMID") or "").strip() if cit is not None else ""
        doi, pmcid = "", ""
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            it = aid.get("IdType")
            if it == "doi":
                doi = (aid.text or "").strip()
            elif it == "pmc":
                pmcid = (aid.text or "").strip()
        ifv = journal_if(journal)
        if if_min and ifv < if_min:
            continue
        if if_max and ifv > if_max:
            continue
        out.append({
            "pmid": pmid, "pmcid": pmcid, "doi": doi, "title": title,
            "authors": authors[:8], "journal": journal, "year": yr,
            "if_value": ifv, "abstract": abstract[:2000],
            "pub_types": types, "oa": pdf_supported(doi),
        })
    return out


# ---------------------------------------------------------------- AI 简介与创新点
class CandidateNotes(BaseModel):
    title_cn: str = ""
    intro: str = ""
    innovation: str = ""


CAND_PROMPT = """下面是 PBL 病例和一个检索方向下找到的研究文献（真实 PubMed 文献）。
请为这篇文献写：
1. title_cn：标题的中文翻译（简洁准确）；
2. intro：文献简介（2~3 句，说明研究做了什么、怎么做的、主要结果）；
3. innovation：创新点（1~2 句，这项研究新在哪里、对临床/病例有何启发）。

【病例摘要】
{case_text}

【检索方向】{direction}

【文献标题】{title}
【期刊与年份】{journal} {year}
【摘要】
{abstract}

只输出 JSON：{{"title_cn":"","intro":"","innovation":""}}"""


def annotate_candidate(case: Case, cand: LitCandidate, direction: str) -> None:
    """为候选文献补中文标题、简介与创新点（AI；失败则留空不阻塞）。"""
    if not llm.has_key() or not cand.abstract:
        cand.intro = cand.intro or (cand.abstract[:120] + "…" if cand.abstract else "")
        return
    result = llm.chat_json(CAND_PROMPT.format(
        case_text=(case.case_text or "")[:1500], direction=direction,
        title=cand.title, journal=cand.journal, year=cand.year,
        abstract=cand.abstract[:1500]), CandidateNotes)
    if result:
        cand.title_cn = result.title_cn or cand.title_cn
        cand.intro = result.intro or cand.intro
        cand.innovation = result.innovation or cand.innovation


class SearchQuery(BaseModel):
    query: str = ""


QUERY_PROMPT = """为 PBL 病例生成标准 PubMed 英文检索式（供文献检索使用）。

【病例摘要】
{case_text}

【研究方向】{direction}
【用户提示词】{prompt}

检索式写作规范（严格遵守）：
1. 按「概念组」组织，一般 2~3 组：概念组 A = 疾病/主题名（含常用英文同义词），
   概念组 B = 研究方向相关术语（如 pathogenesis 的机制词、diagnosis 的诊断词、treatment 的治疗词）；
2. **组内用 OR 连接同义词**并加括号，**组间用 AND**；每组 2~4 个词；
3. 疾病名加 [MeSH] 词条（若确为该病标准 MeSH 词），如 "IgA Nephropathy"[Mesh]；
4. 英文用标准医学术语；短语加双引号；不要包含年份、期刊、文献类型、open access 等条件；
5. 总长度适中（40~90 词符），不要堆砌 5 个以上概念组，也不要只用单个泛词。

示例：
方向=发病机制，提示词=IgA肾病：
("IgA Nephropathy"[Mesh] OR "IgA nephropathy" OR "Berger disease") AND (pathogenesis OR mechanism OR pathophysiology OR "immune complex")
方向=诊断，提示词=社区获得性肺炎：
("community-acquired pneumonia" OR "community acquired pneumonia" OR CAP) AND (diagnosis OR "diagnostic criteria" OR biomarker)

只输出 JSON：{{"query": "..."}}"""


def build_query(case: Case, direction: str, prompt: str) -> str:
    """把病例+方向+中文提示词转成英文 PubMed 检索式；无 Key 时直接返回提示词。"""
    if not llm.has_key():
        return prompt or f"{direction} " + (case.title or "")
    result = llm.chat_json(QUERY_PROMPT.format(
        case_text=(case.case_text or "")[:1200], direction=direction or "（未指定）",
        prompt=prompt or "（无，请按病例核心问题拟定）"), SearchQuery)
    if result and result.query.strip():
        return result.query.strip()
    return prompt or direction


class CandidateBatch(BaseModel):
    items: list[dict] = Field(default_factory=list)


BATCH_PROMPT = """下面是 PBL 病例和一批研究文献（真实 PubMed 检索结果）。
请为**每篇**给出：标题中文翻译（title_cn）、文献简介（intro，2~3 句：做了什么/怎么做/主要结果）、
创新点（innovation，1~2 句：新在哪里、对临床或本病例有何启发）。
必须覆盖全部编号，不得遗漏、不得虚构。

【病例摘要】
{case_text}

【检索方向】{direction}

{listing}

只输出 JSON：{{"items":[{{"i":1,"title_cn":"","intro":"","innovation":""}}]}}"""


def annotate_candidates(case: Case, cands: list, direction: str) -> None:
    """批量补中文标题/简介/创新点（一次调用；失败则逐篇兜底为摘要截断）。"""
    if not cands:
        return
    if not llm.has_key():
        for cand in cands:
            cand.intro = cand.intro or (cand.abstract[:120] + "…" if cand.abstract else "")
        return
    listing = chr(10).join(
        f"[C{i}] {cand.title}（{cand.journal}, {cand.year}，IF≈{cand.if_value:.1f}）{chr(10)}"
        f"摘要：{cand.abstract[:450]}"
        for i, cand in enumerate(cands, 1))
    result = llm.chat_json(BATCH_PROMPT.format(
        case_text=(case.case_text or "")[:1200], direction=direction,
        listing=listing[:13000]), CandidateBatch)
    by_i = {}
    if result:
        for item in result.items:
            try:
                by_i[int(item.get("i", 0))] = item
            except (TypeError, ValueError):
                continue
    for i, cand in enumerate(cands, 1):
        item = by_i.get(i)
        if item:
            cand.title_cn = str(item.get("title_cn", "")).strip() or cand.title_cn
            cand.intro = str(item.get("intro", "")).strip() or cand.intro
            cand.innovation = str(item.get("innovation", "")).strip() or cand.innovation
        if not cand.intro:
            cand.intro = cand.abstract[:120] + "…" if cand.abstract else ""


# ---------------------------------------------------------------- 图片获取（出版社直连 PDF + 图注锚定裁剪）
FIG_DIR.mkdir(parents=True, exist_ok=True)

# 直连 PDF 的出版社模式；VERIFIED 为实测可下载（Frontiers/PLOS/BMC/Nature系），
# 界面的「可提取原文图 ✓」据此标注——标了就保证能取到图。
PDF_PATTERNS = {
    "10.3389/": "https://www.frontiersin.org/articles/{doi}/pdf",
    "10.1371/": "https://journals.plos.org/plosone/article/file?id={doi}&type=printable",
    "10.1186/": "https://link.springer.com/content/pdf/{doi}.pdf",
    "10.1038/": "https://www.nature.com/articles/{suffix}.pdf",
}
VERIFIED_PREFIXES = ("10.3389/", "10.1371/", "10.1186/", "10.1038/")


def pdf_supported(doi: str) -> bool:
    """是否为实测可下载 PDF 的出版社（用于「可提取原文图 ✓」标注）。"""
    return any((doi or "").strip().startswith(p) for p in VERIFIED_PREFIXES)


def _pdf_url(doi: str) -> str | None:
    d = (doi or "").strip()
    for prefix, tpl in PDF_PATTERNS.items():
        if d.startswith(prefix):
            suffix = d.split("/", 1)[1] if "/" in d else ""
            suffix = re.sub(r"^eLife\.", "", suffix)       # eLife.85564 -> 85564
            return tpl.format(doi=d, suffix=suffix)
    return None


_OALEX_CACHE: dict = {}


def _openalex_pdf_url(session, doi: str) -> str | None:
    """OpenAlex 兜底：查询该 DOI 的最佳开放 PDF 地址（免费 API，无需 Key）。"""
    if doi in _OALEX_CACHE:
        return _OALEX_CACHE[doi]
    try:
        r = session.get(f"https://api.openalex.org/works/doi:{doi}",
                        params={"mailto": "pbl-agent@example.com"}, timeout=45)
        if r.status_code == 200:
            d = r.json()
            locs = ([d.get("best_oa_location")] + (d.get("locations") or []))
            for loc in locs:
                if not loc:
                    continue
                u = loc.get("pdf_url")
                if u:                          # 只认真正的 PDF 直链，不用落地页
                    _OALEX_CACHE[doi] = u
                    return u
    except Exception as e:
        print(f"[literature] OpenAlex 查询失败 {doi}: {e}")
    _OALEX_CACHE[doi] = None
    return None


def download_pdf(doi: str, timeout: int = 150) -> bytes | None:
    """下载开放 PDF：出版社直连模式优先，失败后用 OpenAlex 兜底。返回 PDF 字节或 None。"""
    session = requests.Session()
    _headers(session)
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"}
    urls = []
    direct = _pdf_url(doi)
    if direct:
        urls.append(direct)
    alt = _openalex_pdf_url(session, doi)
    if alt and alt not in urls:
        urls.append(alt)
    for url in urls:
        try:
            r = session.get(url, headers=ua, timeout=timeout, allow_redirects=True)
            if r.content[:4] == b"%PDF":
                return r.content
            print(f"[literature] PDF 非文件响应 {doi}: HTTP {r.status_code} "
                  f"{r.headers.get('Content-Type', '')[:40]}")
        except Exception as e:
            print(f"[literature] PDF 下载失败 {doi}: {e}")
    return None


def _letter_panel_boxes(page, rect) -> list:
    """从 PDF 页面的文字层读取图上印的面板字母（A/B/C…）位置，按字母划分面板区域。

    返回 [(字母, (x0, y0, x1, y1))]，字母按行、列排序；识别不到（<2 个）返回 []。
    """
    try:
        words = page.get_text("words")
    except Exception:
        return []
    cand = []
    for w in words:
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if x0 < rect.x0 - 2 or x1 > rect.x1 + 2 or y0 < rect.y0 - 2 or y1 > rect.y1 + 2:
            continue
        s = txt.strip(" .:：()（）[]【】")
        if len(s) != 1 or not ("A" <= s <= "Z"):
            continue
        if (y1 - y0) > 26:            # 字号过滤：面板字母是小号字
            continue
        cand.append((s, x0, y0, x1, y1))
    if len(cand) < 2:
        return []
    # 按 y 聚成行
    cand.sort(key=lambda c: (c[2], c[1]))
    rows: list = []
    for c in cand:
        for r in rows:
            if abs(r[0] - c[2]) <= 16:
                r[1].append(c)
                break
        else:
            rows.append([c[2], [c]])
    rows.sort(key=lambda r: r[0])
    if len(rows) > 4:
        return []
    row_ys = [r[0] for r in rows]
    boxes = []
    for ri, (_, items) in enumerate(rows):
        items.sort(key=lambda c: c[1])
        if len(items) > 4:
            return []
        # 本行纵向范围：与相邻行字母 y 的中点
        y_top = rect.y0 if ri == 0 else (row_ys[ri - 1] + row_ys[ri]) / 2
        y_bot = rect.y1 if ri == len(rows) - 1 else (row_ys[ri] + row_ys[ri + 1]) / 2
        for ci, (letter, x0, y0, x1, y1) in enumerate(items):
            left = rect.x0 if ci == 0 else (items[ci - 1][1] + x0) / 2
            right = rect.x1 if ci == len(items) - 1 else (x0 + items[ci + 1][1]) / 2
            boxes.append((letter, (left, y_top, right, y_bot)))
    return boxes


def _split_panels(img_path: str, max_panels: int = 8) -> list:
    """把拼图递归切分为单个面板块（先切行，再在每行内切列；相对阈值+局部投影）。

    返回 [PIL.Image]；无可靠切分时返回 []（调用方退回整图）。
    """
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return []
    try:
        im = Image.open(img_path).convert("L")
        arr = np.asarray(im)
        ink = arr < 238

        def segments(density: np.ndarray) -> list:
            n = len(density)
            if n < 20:
                return [(0, n)]
            med = float(np.median(density))
            thr = max(0.015, 0.22 * med)
            min_run = max(5, int(n * 0.012))
            gaps, i = [], 0
            while i < n:
                if density[i] < thr:
                    j = i
                    while j < n and density[j] < thr:
                        j += 1
                    if j - i >= min_run:
                        gaps.append((i, j))
                    i = j
                else:
                    i += 1
            segs, prev = [], 0
            for a0, b0 in gaps:
                if a0 - prev > int(n * 0.07):
                    segs.append((prev, a0))
                prev = b0
            if n - prev > int(n * 0.07):
                segs.append((prev, n))
            return segs if segs else [(0, n)]

        def blocks(box, depth=0):
            x0, y0, x1, y1 = box
            sub = ink[y0:y1, x0:x1]
            if sub.size == 0 or depth > 3:
                return [box]
            rows = segments(sub.mean(axis=1))
            if len(rows) > 1:
                out = []
                for ry0, ry1 in rows:
                    out.extend(blocks((x0, y0 + ry0, x1, y0 + ry1), depth + 1))
                return out
            cols = segments(sub.mean(axis=0))
            if len(cols) > 1:
                out = []
                for cx0, cx1 in cols:
                    out.extend(blocks((x0 + cx0, y0, x0 + cx1, y1), depth + 1))
                return out
            return [box]

        boxes = blocks((0, 0, ink.shape[1], ink.shape[0]))
        boxes = [b for b in boxes
                 if (b[2] - b[0]) * (b[3] - b[1]) > 0.02 * ink.size
                 and ink[b[1]:b[3], b[0]:b[2]].mean() > 0.004]
        if len(boxes) < 2:
            return []
        if len(boxes) > max_panels:
            rb = segments(ink.mean(axis=1))
            if 2 <= len(rb) <= 6:
                boxes = [(0, a, ink.shape[1], b) for a, b in rb]
            else:
                return []
        panels = []
        for x0, y0, x1, y1 in boxes:
            crop = im.crop((x0, y0, x1, y1))
            if np.asarray(crop).size == 0:
                continue
            panels.append(crop.convert("RGB"))
        return panels if 2 <= len(panels) <= 8 else []
    except Exception as e:
        print(f"[literature] 分图切割失败: {e}")
        return []


_CAP = re.compile(r"(?:Fig(?:ure)?\.?\s*)(\d{1,2})[.\s:：]", re.IGNORECASE)


def extract_figures_from_pdf(pdf_bytes: bytes, tag: str, max_figs: int = 8) -> list[dict]:
    """按「图注在上、图在下」的通用排版，从 PDF 中裁剪图片区域。

    图注锚定：先找到形如 "Figure 1." 的图注文本块，取该块上方、距其最近的
    文本块底部（或页顶 40pt）到图注之间的区域作为图片，渲染为 PNG。
    """
    try:
        import fitz
    except ImportError:
        import pymupdf as fitz
    dest = FIG_DIR / re.sub(r"[^0-9A-Za-z]+", "_", tag)[:60]
    dest.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        print(f"[literature] PDF 打开失败: {e}")
        return []
    _embedded_fallback = []
    for pno in range(doc.page_count):
        if len(out) >= max_figs:
            break
        page = doc[pno]
        blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
        for b in sorted(blocks, key=lambda x: x[1]):
            m = _CAP.match(b[4].strip())
            if not m:
                continue
            label = f"Figure {m.group(1)}"
            if any(f["label"] == label for f in out):
                continue
            caption = " ".join(b[4].split())[:500]
            cap_top = b[1]
            # 上方最近的正文块底部（限定与图注横向有重叠，避免分栏误判）
            prev_bottom = 40.0
            for pb in blocks:
                if pb is b or pb[3] >= cap_top - 2:
                    continue
                overlap = min(pb[2], b[2]) - max(pb[0], b[0])
                if overlap > 10 and pb[3] > prev_bottom:
                    prev_bottom = pb[3]
            top = max(prev_bottom + 3, 40.0)
            bottom = cap_top - 3
            if bottom - top < 85:          # 区域过小，多半不是图
                continue
            x0 = max(min(b[0], 36.0) - 4, 8.0)
            x1 = min(max(b[2], page.rect.width - 36.0) + 4, page.rect.width - 8)
            try:
                pix = page.get_pixmap(matrix=fitz.Matrix(2.4, 2.4),
                                      clip=fitz.Rect(x0, top, x1, bottom))
                fp = dest / f"{label.replace(' ', '').lower()}.png"
                pix.save(str(fp))
                if fp.stat().st_size < 9000:   # 空白/极小图丢弃
                    fp.unlink(missing_ok=True)
                    continue
                panel_info = []
                # 一：按图上的字母标注精确切分（可得到 Figure 1A 这样的单图）
                boxes = _letter_panel_boxes(page, fitz.Rect(x0, top, x1, bottom))
                for letter, box in boxes:
                    try:
                        pix2 = page.get_pixmap(matrix=fitz.Matrix(2.6, 2.6), clip=fitz.Rect(*box))
                        pfile = dest / f"{label.replace(' ', '').lower()}_{letter}.png"
                        pix2.save(str(pfile))
                        if pfile.stat().st_size >= 7000:
                            panel_info.append({"panel": letter, "file": str(pfile)})
                        else:
                            pfile.unlink(missing_ok=True)
                    except Exception as e:
                        print(f"[literature] 分图裁剪失败 {label}{letter}: {e}")
                # 二：字母识别不到时，退回白缝投影切分
                if not panel_info:
                    panels = _split_panels(str(fp))
                    cap_letters = list(dict.fromkeys(
                        re.findall(r"[(（]\s*([A-H])\s*[)）]", caption or "")))
                    tags = cap_letters if panels and len(cap_letters) == len(panels)                         else [str(k + 1) for k in range(len(panels))]
                    for pi, pim in enumerate(panels):
                        pfile = dest / f"{label.replace(' ', '').lower()}_p{tags[pi]}.png"
                        pim.save(str(pfile))
                        panel_info.append({"panel": tags[pi], "file": str(pfile)})
                out.append({"file": str(fp), "label": label, "caption": caption,
                            "panels": panel_info})
            except Exception as e:
                print(f"[literature] 裁剪失败 p{pno + 1} {label}: {e}")
        # 记录本页内嵌大图（矢量裁剪全部失败时兜底）
        try:
            for img in page.get_images(full=True):
                xref = img[0]
                info = doc.extract_image(xref)
                if info and len(info.get("image", b"")) > 40000:
                    _embedded_fallback.append((pno + 1, xref, info))
        except Exception:
            pass

    if not out and _embedded_fallback:      # 兜底：直接用 PDF 内嵌位图
        for n, (pno, xref, info) in enumerate(_embedded_fallback[:max_figs], 1):
            try:
                fp = dest / f"embedded{n}.{info.get('ext', 'png')}"
                fp.write_bytes(info["image"])
                out.append({"file": str(fp), "label": f"图 {n}（原文内嵌图，第{pno}页）",
                            "caption": "（该 PDF 图片为矢量/特殊格式，已提取内嵌位图）"})
            except Exception as e:
                print(f"[literature] 内嵌图提取失败: {e}")
    return out


def fetch_article_figures(cand) -> list[dict]:
    """获取文献的候选图片：出版社直连 PDF（OpenAlex 兜底）-> 图注锚定裁剪 -> 内嵌图兜底。"""
    doi = cand.get("doi", "") if isinstance(cand, dict) else getattr(cand, "doi", "")
    if not doi:
        return []
    pdf = download_pdf(doi)
    if not pdf:
        return []
    return extract_figures_from_pdf(pdf, doi)


# ---------------------------------------------------------------- AI 解读与选图
class PaperDraft(BaseModel):
    title_cn: str = ""
    background: str = ""
    approach: str = ""
    conclusion: str = ""
    case_link: str = ""
    limitations: str = ""
    steps: list[dict] = Field(default_factory=list)   # [{tag:"A", text:"…"}]
    section_label: str = ""
    chosen: list[dict] = Field(default_factory=list)   # {i, note}


PAPER_PROMPT = """你是文献分享讲解人。基于下面这篇真实研究文献与 PBL 病例，
准备一份「一页速览」式的文献分享内容（课堂上 5 分钟讲完，全部内容放一页）：

【病例摘要】
{case_text}

【文献标题】{title}
【期刊/年份/影响因子】{journal} {year}（IF≈{ifv}）
【摘要】
{abstract}

【全文图片清单】（编号 F1..Fn，含原图注；AI 只能从中挑选，不得虚构）
{figs}

请生成：
1. title_cn：论文中文标题；
2. steps：**研究过程**，拆成 4 步（这是重点，页面底部会画成 A→B→C→D 流程条）。
   每步 {{tag, text}}：tag 依次为 A/B/C/D；text 格式「三字小标签：一句结论」，总长 16~26 字。
   四步要构成完整的论证链——例如
   A「假设：系膜细胞自己也能产生Gd-IgA1」→ B「基因：单细胞PCR/FISH证实其转录IGHA」
   → C「通路：SEB经TLR4诱导，TAK242可阻断」→ D「因果：敲除IGHA后病理与临床均改善」。
   用本文真实的方法与结果填空，不得虚构；论文步骤少于 4 步时合并，不要凑数编造。
3. approach：研究思路与过程整段文字（3~4 句，说明用什么模型/方法/分组、怎么验证），供 Word 用；
4. conclusion：核心结论（1~2 句、不超过 60 字，必须是本文最关键的发现）；
5. case_link：**与病例的联系**（1~2 句、不超过 60 字，说明这篇文献对本病例的机制理解/诊断/治疗有何启发）；
6. limitations：局限性（1 句、不超过 45 字；讲不出就留空）；
7. background：研究背景（**必填**，1 句、不超过 50 字——说明该疾病/问题的负担或为何值得研究，
   将显示在速览页标题下方；不要复述结论）；
8. section_label：图片页小标签（4~6 字，如"机制探索""实验结果"）；
9. chosen：从图片清单中选出**最能直接证明结论的 2~4 张**（把整页排满、每张都要能讲），
   每张给出 i（图片编号数字）与 note（中文说明：图中讲了什么、证明了什么，1~2 句、不超过 40 字）。
   只选图注信息明确支持的图；图与结论无关的不选。清单为空则 chosen 为空数组。

只输出 JSON：{{"title_cn":"","steps":[{{"tag":"A","text":""}}],
"approach":"","conclusion":"","case_link":"","limitations":"","background":"",
"section_label":"","chosen":[{{"i":1,"note":""}}]}}"""


def build_paper_share(case: Case, cand: LitCandidate) -> tuple[PaperShare | None, str]:
    """选中的文献 -> 分享内容（含挑图与解读）。返回 (PaperShare, 提示/错误)。"""
    paper = PaperShare(
        pmid=cand.pmid, title_en=cand.title, title_cn=cand.title_cn or cand.title,
        authors=cand.authors, journal=cand.journal, year=cand.year, if_value=cand.if_value,
        ref_text=pubmed.format_reference({
            "ref_type": "J", "title": cand.title, "authors": cand.authors,
            "journal": cand.journal, "year": cand.year, "doi": cand.doi}),
    )
    figs: list[dict] = []
    figs = fetch_article_figures(cand)
    fig_lines = chr(10).join(
        f"[F{i}] {f['label']}: {f['caption'][:400]}" for i, f in enumerate(figs, 1)) or "（无）"

    if not llm.has_key():
        paper.approach = "（演示）配置 API Key 后自动生成研究思路与过程。"
        paper.conclusion = "（演示）配置 API Key 后自动生成研究结论。"
        paper.case_link = "（演示）配置 API Key 后自动生成与病例的联系。"
        paper.steps = [{"tag": "A", "text": "假设：演示步骤 A"},
                       {"tag": "B", "text": "方法：演示步骤 B"},
                       {"tag": "C", "text": "验证：演示步骤 C"},
                       {"tag": "D", "text": "因果：演示步骤 D"}]
        return paper, "（演示模式）"

    result = llm.chat_json(PAPER_PROMPT.format(
        case_text=(case.case_text or "")[:2000], title=cand.title, journal=cand.journal,
        year=cand.year, ifv=int(cand.if_value), abstract=cand.abstract[:1800],
        figs=fig_lines), PaperDraft)
    if result is None:
        return None, ("文献解读生成失败：" + (llm.last_error() or "模型未返回有效内容")
                      + "。请按原因处理后重试。")
    paper.title_cn = result.title_cn or paper.title_cn
    paper.background = result.background
    paper.approach = result.approach
    paper.conclusion = result.conclusion
    paper.case_link = result.case_link
    paper.limitations = result.limitations
    steps = []
    for k, s in enumerate(result.steps or []):
        if not isinstance(s, dict):
            continue
        txt = str(s.get("text", "")).strip()
        if not txt:
            continue
        tag = str(s.get("tag", "")).strip() or "ABCD"[len(steps) % 4]
        steps.append({"tag": tag[:2], "text": txt})
    paper.steps = steps[:5]
    paper.section_label = result.section_label or "机制探索"
    for ch in result.chosen:
        try:
            i = int(ch.get("i", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(figs):
            f = figs[i - 1]
            paper.figures.append(PaperFigure(
                file=f["file"], label=f["label"], caption=f["caption"],
                note=str(ch.get("note", "")).strip(), selected=True,
                whole_file=f["file"], panels=f.get("panels") or []))
    note = ""
    if not cand.oa:
        note = "该出版社暂不支持自动取图；建议优先选择标注「可提取图 ✓」的文献。"
    elif not figs:
        note = "未能提取到图片（PDF 下载或裁剪失败），页面将不含图。"
    return paper, note


# ---------------------------------------------------------------- 二次修改
class PaperRevise(BaseModel):
    title_cn: str = ""
    background: str = ""
    approach: str = ""
    conclusion: str = ""
    case_link: str = ""
    limitations: str = ""
    section_label: str = ""
    steps: list[dict] = Field(default_factory=list)


REVISE_PROMPT = """下面是一份文献分享的内容。请按用户要求修改（未要求改动的部分保持原样）。

【病例摘要】
{case_text}

【文献标题】{title}

【当前内容】
论文中文标题：{title_cn}
研究背景：{background}
研究过程（A→D 流程条）：{steps}
研究思路与过程：{approach}
核心结论：{conclusion}
文中局限：{limitations}
与病例的联系：{case_link}
图片页标签：{section_label}

【用户修改要求】
{instruction}

要求：保持医学表述严谨；原本为空且用户未要求补充的字段保持为空；
steps 仍需保持四步论证链、每步 16~26 字（tag 依次 A/B/C/D）。
只输出 JSON：{{"title_cn":"","background":"","steps":[{{"tag":"A","text":""}}],
"approach":"","conclusion":"","limitations":"","case_link":"","section_label":""}}"""


def _steps_text(paper: PaperShare) -> str:
    return " / ".join(f"{s.get('tag', '')}. {s.get('text', '')}"
                      for s in (paper.steps or [])) or "（无）"


def revise_paper(case: Case, paper: PaperShare, instruction: str) -> tuple[bool, str]:
    """按用户提示词修改文献分享文字。"""
    if not llm.has_key():
        return False, "未配置 API Key"
    result = llm.chat_json(REVISE_PROMPT.format(
        case_text=(case.case_text or "")[:1500], title=paper.title_en,
        title_cn=paper.title_cn, background=paper.background, approach=paper.approach,
        conclusion=paper.conclusion, case_link=paper.case_link,
        limitations=paper.limitations, steps=_steps_text(paper),
        section_label=paper.section_label, instruction=instruction), PaperRevise)
    if result is None:
        return False, ("修改失败：" + (llm.last_error() or "模型未返回有效内容")
                       + "。请按原因处理后重试。")
    paper.title_cn = result.title_cn or paper.title_cn
    paper.background = result.background
    paper.approach = result.approach
    paper.conclusion = result.conclusion
    paper.case_link = result.case_link
    paper.limitations = result.limitations
    if result.steps:
        paper.steps = [{"tag": str(s.get("tag", ""))[:2] or "ABCD"[i % 4],
                        "text": str(s.get("text", "")).strip()}
                       for i, s in enumerate(result.steps) if s.get("text")]
    paper.section_label = result.section_label or paper.section_label
    return True, "已按提示词修改"


# ---------------------------------------------------------------- 手动导入（引用格式 + PDF）
_DOI_RE = re.compile(r"10\.\d{4,}/[^\s]+", re.I)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_PAGES_RE = re.compile(r"(?::\s*|\(\s*)(\d{1,5}\s*[-–—~]\s*\d{1,5})\s*\)?")
_VOL_ISS_RE = re.compile(r"(\d{1,4})\s*\(\s*(\d{1,4}|[Ss]\d*|[Mm]\d*)\s*\)")


def parse_citation_text(text: str) -> dict:
    """从粘贴的 GB/T 7714 / DOI / 任意引用格式文本中提取元数据。

    返回与 Reference 兼容的 dict；无法解析时关键字段留空，由调用方决定如何处理。
    """
    text = (text or "").strip()
    if not text:
        return {}
    # DOI
    doi_m = _DOI_RE.search(text)
    doi = doi_m.group(0).rstrip(".,;") if doi_m else ""
    # 年份（取最后一个 19xx/20xx，避免误抓 DOI 中的数字）
    years = _YEAR_RE.findall(text)
    year = years[-1] if years else ""
    # 作者：第一个句号/逗号之前的部分
    author_end = min(
        (pos for pos in (text.find("."), text.find("．")) if pos > 0),
        default=0)
    raw_authors = text[:author_end].strip() if author_end else ""
    authors = [a.strip() for a in re.split(r"[,，;；]", raw_authors) if a.strip()][:6]
    # 题名：[J]/[M] 标记之前最后一个 ". " 之后的文字
    title_m = re.search(r"([^\[\]]{8,200}?)\s*\[([JM])\]", text)
    title = ""
    if title_m:
        raw_title = title_m.group(1).strip()
        # 去掉作者前缀（最后一个 ". " 之后的部分就是题名）
        last_dot = raw_title.rfind(". ")
        title = raw_title[last_dot + 2:].strip() if last_dot > 0 else raw_title
    # 刊名：[J] 之后到逗号/句号
    journal = ""
    if title_m:
        after_bracket = text[title_m.end():]
        j_m = re.match(r"[\s.]*([^\[\]]+?)(?:,\s*\d{4}|\.\s|$)", after_bracket)
        if j_m:
            journal = j_m.group(1).strip()
    # 卷(期)
    volume, issue = "", ""
    vi_m = _VOL_ISS_RE.search(text)
    if vi_m:
        volume, issue = vi_m.group(1), vi_m.group(2)
    # 页码
    pages = ""
    pg_m = _PAGES_RE.search(text)
    if pg_m:
        pages = pg_m.group(1).replace("–", "-").replace("—", "-").replace("~", "-")
    return {"ref_type": "J", "title": title, "authors": authors,
            "journal": journal, "year": year, "volume": volume,
            "issue": issue, "pages": pages, "doi": doi}


def build_paper_share_from_manual(
        case: Case, ref_text: str, pdf_bytes: bytes | None = None,
        filename: str = "") -> tuple[PaperShare | None, str]:
    """从粘贴的引用格式 + 可选 PDF 生成文献分享内容。"""
    ref_text = (ref_text or "").strip()
    meta = parse_citation_text(ref_text)
    title = meta.get("title", "")
    if not title:
        # 尝试取第一行非空文本作为标题兜底
        first_line = ref_text.split("\n")[0].strip() if ref_text else ""
        title = first_line[:200] if first_line else "未命名文献"
    paper = PaperShare(
        title_en=title, title_cn=title,
        authors=meta.get("authors", []),
        journal=meta.get("journal", ""), year=meta.get("year", ""),
        ref_text=ref_text,  # 保留用户原文，不猜字段
    )
    if not llm.has_key():
        paper.approach = "（演示）配置 API Key 后自动生成研究思路与过程。"
        paper.conclusion = "（演示）配置 API Key 后自动生成研究结论。"
        paper.case_link = "（演示）配置 API Key 后自动生成与病例的联系。"
        paper.steps = [{"tag": "A", "text": "假设：演示步骤 A"},
                       {"tag": "B", "text": "方法：演示步骤 B"},
                       {"tag": "C", "text": "验证：演示步骤 C"},
                       {"tag": "D", "text": "因果：演示步骤 D"}]
        return paper, "（演示模式）"

    figs: list[dict] = []
    fig_lines = "（无）"
    pdf_text = ""
    if pdf_bytes:
        # 提取图表
        tag = re.sub(r"[^0-9A-Za-z]+", "_", title or filename or "manual")[:60]
        try:
            figs = extract_figures_from_pdf(pdf_bytes, tag)
        except Exception as e:
            print(f"[literature] 手动导入取图失败: {e}")
        fig_lines = "\n".join(
            f"[F{i}] {f['label']}: {f['caption'][:400]}" for i, f in enumerate(figs, 1)) or "（无）"
        # 提取正文
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)
        try:
            from . import textbook as _tb
            pages = _tb._extract_pdf(tmp_path)
            pdf_text = "\n".join(t for _, t in pages)[:6000]
        except Exception as e:
            print(f"[literature] 手动导入正文提取失败: {e}")
        finally:
            tmp_path.unlink(missing_ok=True)

    abstract = pdf_text or meta.get("title", "")
    result = llm.chat_json(PAPER_PROMPT.format(
        case_text=(case.case_text or "")[:2000], title=title,
        journal=meta.get("journal", ""), year=meta.get("year", ""),
        ifv=0, abstract=abstract[:1800], figs=fig_lines), PaperDraft)
    if result is None:
        return None, ("文献解读生成失败：" + (llm.last_error() or "模型未返回有效内容")
                      + "。请按原因处理后重试。")

    paper.title_cn = result.title_cn or paper.title_cn
    paper.background = result.background
    paper.approach = result.approach
    paper.conclusion = result.conclusion
    paper.case_link = result.case_link
    paper.limitations = result.limitations
    steps = []
    for k, s in enumerate(result.steps or []):
        if not isinstance(s, dict):
            continue
        txt = str(s.get("text", "")).strip()
        if not txt:
            continue
        tag = str(s.get("tag", "")).strip() or "ABCD"[len(steps) % 4]
        steps.append({"tag": tag[:2], "text": txt})
    paper.steps = steps[:5]
    paper.section_label = result.section_label or "机制探索"

    for ch in result.chosen:
        try:
            i = int(ch.get("i", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= i <= len(figs):
            f = figs[i - 1]
            paper.figures.append(PaperFigure(
                file=f["file"], label=f["label"], caption=f["caption"],
                note=str(ch.get("note", "")).strip(), selected=True,
                whole_file=f["file"], panels=f.get("panels") or []))

    note = ""
    if pdf_bytes and not figs:
        note = "未能从 PDF 提取到图片，页面将不含图。"
    elif not pdf_bytes:
        note = "未上传 PDF，页面将不含图；建议上传原文 PDF 以自动提取关键图。"
    return paper, note
