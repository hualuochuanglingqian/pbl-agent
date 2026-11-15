"""PubMed 文献检索（NCBI E-utilities，免费官方 API）。

返回的每条文献都是 API 的真实返回，供 pipeline 让 LLM 从中筛选，杜绝编造。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import requests

from . import net

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TOOL = "pbl-agent"
EMAIL = "pbl-agent@example.com"
TIMEOUT = 30

# 文献类型筛选（与 literature.py 的判定口径保持一致）
REVIEW_TYPES = {"review", "systematic review", "meta-analysis",
                "scoping review", "narrative review", "umbrella review"}
EXCLUDE_TYPES = {
    "review", "systematic review", "meta-analysis", "editorial", "comment", "letter",
    "published erratum", "retraction of publication", "retracted publication",
    "news", "newspaper article", "biography", "congress", "overall", "preprint",
    "case reports", "historical article", "address", "autobiography", "interview",
    "patient education handout", "practice guideline", "guideline",
    "consensus development conference", "consensus development conference, nih",
    "lecture", "video-audio media", "webcasts", "clinical conference",
}

_LAST_ERROR = ""
_LAST_NOTE = ""


def last_error() -> str:
    return _LAST_ERROR


def last_note() -> str:
    return _LAST_NOTE


def _keep(ref: dict, doc_type: str) -> bool:
    types = {t.lower().strip() for t in (ref.get("pub_types") or [])}
    if doc_type == "review":
        return bool(types & REVIEW_TYPES)
    if doc_type == "research":
        return not (types & EXCLUDE_TYPES)
    return True


_session = requests.Session()


def _build_terms(query: str) -> list[str]:
    """逗号/顿号分隔的检索词拆开；含空格的短语加引号（PubMed 短语检索）。"""
    parts = [p.strip() for p in re.split(r"[,，、;；]", query) if p.strip()]
    return [f'"{p}"' if " " in p else p for p in parts]


def _type_clause(doc_type: str) -> str:
    """把文献类型直接写进 PubMed 检索式，避免先取回一堆无关文献再过滤。"""
    if doc_type == "review":
        return ('AND (review[pt] OR "systematic review"[pt] OR meta-analysis[pt] '
                'OR "scoping review"[pt] OR "narrative review"[pt])')
    if doc_type == "research":
        return ('NOT (review[pt] OR "systematic review"[pt] OR meta-analysis[pt] '
                'OR editorial[pt] OR comment[pt] OR letter[pt] OR "case reports"[pt] '
                'OR "practice guideline"[pt])')
    return ""


def _esearch(term: str, retmax: int, mindate: str, maxdate: str) -> list[str]:
    r = _session.get(
        f"{EUTILS}/esearch.fcgi",
        params={"db": "pubmed", "term": term, "retmode": "json",
                "retmax": retmax, "tool": TOOL, "email": EMAIL,
                "mindate": mindate, "maxdate": maxdate, "datetype": "pdat",
                "sort": "relevance"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def search_pubmed(query: str, retmax: int = 8, recent_years: int = 7,
                  doc_type: str = "all") -> list[dict]:
    """关键词 -> 真实文献列表（限定近 N 年，默认 7 年内）。失败/无结果返回空列表。

    doc_type：all=全部 / research=排除综述、指南、社论、病例报道等 / review=只要综述与荟萃分析。
    检索策略：所有词 AND → 前两词 AND → 首词（疾病/主题）单独检索，均按相关性排序；
    不再退化成“任一单词命中”的 OR（那会把登革热、足球之类的无关综述也抓进来）。
    代理隧道坏掉时（Clash 常见）自动改直连重试一次，避免静默返回空表。
    """
    global _LAST_ERROR, _LAST_NOTE
    _LAST_ERROR = ""
    _LAST_NOTE = ""
    if not query.strip():
        return []
    net.ensure_network()
    # Windows 上 requests 读注册表代理且不识别 NO_PROXY="*"，需显式切换
    _session.trust_env = not net.direct_mode()
    import datetime
    year = datetime.date.today().year
    mindate, maxdate = str(year - recent_years), str(year + 1)

    terms = _build_terms(query)
    cores: list[str] = []
    if len(terms) > 1:
        cores.append(" AND ".join(terms))       # 全词求准
        cores.append(" AND ".join(terms[:2]))   # 前两词，丢掉后面的泛词
    if terms:
        cores.append(terms[0])                  # 首词通常是疾病/主题
    cores.append(query.strip())
    clause = _type_clause(doc_type)
    candidates = [f"({c}) {clause}".strip() for c in dict.fromkeys(cores)]
    fetch_n = retmax if doc_type == "all" else max(retmax * 2, 12)

    for attempt in range(2):
        try:
            for term in candidates:
                ids = _esearch(term, fetch_n, mindate, maxdate)
                if not ids:
                    continue
                r2 = _session.get(
                    f"{EUTILS}/efetch.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml",
                            "tool": TOOL, "email": EMAIL},
                    timeout=TIMEOUT,
                )
                r2.raise_for_status()
                refs = _parse_efetch(r2.content)
                kept = [r for r in refs if _keep(r, doc_type)]
                if kept:
                    _LAST_ERROR = ""
                    _LAST_NOTE = ""
                    return kept[:retmax]
                if refs:
                    _LAST_NOTE = (f"PubMed 有 {len(refs)} 条结果，但没有符合"
                                  f"“{'综述/荟萃分析' if doc_type == 'review' else '研究型'}”类型的文献")
            _LAST_ERROR = ""
            return []
        except Exception as e:
            _LAST_ERROR = f"{type(e).__name__}: {str(e)[:160]}"
            print(f"[pubmed] 检索失败（{'代理' if _session.trust_env else '直连'}）: {e}")
            if attempt == 0 and _session.trust_env:
                _session.trust_env = False      # 代理在但隧道坏：本次直接改直连重试
                continue
            return []
    return []


def _parse_efetch(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    out = []
    for art in root.iter("PubmedArticle"):
        cit = art.find("MedlineCitation")
        a = cit.find("Article") if cit is not None else None
        if a is None:
            continue
        at = a.find("ArticleTitle")
        title = " ".join("".join(at.itertext()).split()) if at is not None else ""
        abstract = " ".join(
            "".join(t.itertext()).strip()
            for t in a.findall("Abstract/AbstractText")
        )
        journal = (a.findtext("Journal/Title") or a.findtext("Journal/ISOAbbreviation") or "").strip()
        year = (a.findtext("Journal/JournalIssue/PubDate/Year")
                or a.findtext("Journal/JournalIssue/PubDate/MedlineDate")
                or a.findtext("ArticleDate/Year") or "").strip()
        volume = (a.findtext("Journal/JournalIssue/Volume") or "").strip()
        issue = (a.findtext("Journal/JournalIssue/Issue") or "").strip()
        pages = (a.findtext("Pagination/MedlinePgn") or "").strip()
        # 作者著录格式：姓 + 首字母缩写（Wunderink RG）
        authors = []
        for au in a.findall("AuthorList/Author"):
            collective = (au.findtext("CollectiveName") or "").strip()
            if collective:
                authors.append(collective)
                continue
            last = (au.findtext("LastName") or "").strip()
            fore = (au.findtext("ForeName") or "").strip()
            if last:
                initials = "".join(p[0].upper() for p in fore.split() if p)
                authors.append(f"{last} {initials}".strip())
        pmid = (cit.findtext("PMID") or "").strip() if cit is not None else ""
        pub_types = [(p.text or "").strip()
                     for p in a.findall("PublicationTypeList/PublicationType")]
        doi = ""
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip()
        out.append({
            "title": title, "authors": authors[:6], "journal": journal,
            "year": year, "volume": volume, "issue": issue, "pages": pages,
            "pmid": pmid, "doi": doi, "abstract": abstract[:1200],
            "pub_types": pub_types,
        })
    return out


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def format_reference(ref: dict) -> str:
    """GB/T 7714-2015 著录格式。

    期刊 [J]: 作者1, 作者2, 作者3, 等/et al. 题名[J]. 刊名, 年, 卷(期): 页码.
    专著 [M]: 作者. 书名[M]. 版本. 出版地: 出版社, 出版年: 引文页码.
    """
    if hasattr(ref, "model_dump"):
        ref = ref.model_dump()
    authors = ref.get("authors") or []
    if isinstance(authors, str):   # 防御：手填/目录来源可能是 "名1, 名2" 字符串
        authors = [a.strip() for a in re.split(r"[,，;；]", authors) if a.strip()]
    seg_author = ""
    if authors:
        head = ", ".join(authors[:3])
        if len(authors) > 3:
            head += ", 等" if _has_cjk(head) else ", et al"
        seg_author = head + ". "
    title = ref.get("title", "")

    if ref.get("ref_type") == "M":
        s = f"{seg_author}{title}[M]."
        if ref.get("edition"):
            s += f" {ref['edition']}."
        place, publisher = ref.get("place", ""), ref.get("publisher", "")
        pub = f"{place}: {publisher}" if place and publisher else (place or publisher)
        year, pages = ref.get("year", ""), ref.get("pages", "")
        tail = f"{year}: {pages}" if year and pages else (year or pages)
        if pub and tail:
            s += f" {pub}, {tail}."
        elif pub:
            s += f" {pub}."
        elif tail:
            s += f" {tail}."
        return s

    core = f"{seg_author}{title}[J]. {ref.get('journal', '')}".strip()
    tail = ref.get("year", "")
    if ref.get("volume"):
        tail += f", {ref['volume']}" + (f"({ref['issue']})" if ref.get("issue") else "")
    if ref.get("pages"):
        tail += f": {ref['pages']}"
    if tail:
        core += f", {tail}."
    else:
        core += "."
    if ref.get("doi"):
        core += f" DOI: {ref['doi']}."
    return core
