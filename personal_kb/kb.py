# -*- coding: utf-8 -*-
"""个人医学知识库：混合检索（向量 + BM25）+ LLM 重排 + 带页码引用的回答。

复用 PBL 智能体已建好的 43 本教材索引：
  D:\智能体开发\PBL智能体\knowledge_base\index\{vectors.npz,chunks.jsonl}
"""
from __future__ import annotations

import json
import os
import pickle
import re
import sys
from pathlib import Path

import jieba
import numpy as np
import pymupdf
from rank_bm25 import BM25Okapi

PBL_ROOT = Path(r"D:\智能体开发\PBL智能体")
INDEX_DIR = PBL_ROOT / "knowledge_base" / "index"
CACHE_DIR = Path(__file__).resolve().parent / "index"
CACHE_DIR.mkdir(exist_ok=True)
BM25_TOKENS = CACHE_DIR / "bm25_tokens.pkl"
CHAPTER_MAP = CACHE_DIR / "chapter_map.json"

sys.path.insert(0, str(PBL_ROOT))
from pbl import llm  # noqa: E402
from pbl import textbook  # noqa: E402

DISPLAY = {
    "内科": "内科学", "外科": "外科学", "病理": "病理学", "病生": "病理生理学",
    "生化": "生物化学与分子生物学", "药理": "药理学", "系解": "系统解剖学",
    "局解": "局部解剖学", "组胚": "组织学与胚胎学", "寄生虫": "人体寄生虫学",
    "妇产科": "妇产科学", "儿科": "儿科学", "麻醉": "麻醉学", "中医": "中医学",
}


def display_source(source: str) -> str:
    m = re.match(r"^(.+?)（(第\d+版)）$", source or "")
    if not m:
        return source
    base, edition = m.group(1), m.group(2)
    return f"{DISPLAY.get(base, base)}（{edition}）"


STOP = {"什么", "哪些", "如何", "怎么", "是否", "包括", "以及", "分别", "是什么", "为什么", "请", "问", "的", "了", "吗", "呢"}


def _keyword_query(query: str) -> str:
    toks = [t for t in jieba.lcut_for_search(query) if len(t) >= 2 and t not in STOP]
    return " ".join(toks) or query


def _chat(prompt: str, system: str | None = None, temperature: float = 0.2) -> str | None:
    """优先 DeepSeek；失败自动切智谱 GLM-4-Flash。"""
    r = llm.chat(prompt, system=system, temperature=temperature)
    if r:
        return r
    cli = llm._zhipu_client() if hasattr(llm, "_zhipu_client") else None
    if cli is None:
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        resp = cli.chat.completions.create(
            model=os.getenv("PBL_FALLBACK_MODEL", "glm-4-flash"),
            messages=messages,
            temperature=temperature,
        )
        return resp.choices[0].message.content
    except Exception:
        return None


def _norm(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    lo, hi = float(x.min()), float(x.max())
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


class KnowledgeBase:
    def __init__(self):
        self.vecs, self.meta = textbook._load_index()
        self.sources = sorted({m["source"] for m in self.meta})
        self.pdf_map = {textbook._book_title(p.name): p
                        for p in textbook.BOOKS_DIR.glob("*.pdf")}
        try:
            self.chapter_map = json.loads(CHAPTER_MAP.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.chapter_map = {}
        self.offsets = textbook._load_offsets()
        self._page_cache: dict[tuple[str, int], str] = {}
        self._bm25 = None
        self._tokens = None

    def chapters(self, source: str) -> list[str]:
        return [r.get("title", "") for r in self.chapter_map.get(source, []) if r.get("title")]

    def chapter_of(self, source: str, page) -> str:
        if page is None:
            return ""
        try:
            page = int(page)
        except Exception:
            return ""
        for r in self.chapter_map.get(source, []):
            if r.get("start", 0) <= page <= r.get("end", 0):
                return r.get("title", "")
        return ""

    # ---------------- 命中页整页回填 ----------------
    def page_text(self, source: str, page) -> str:
        """按“印刷页 + 偏移”取回该页整页文本，弥补分块漏段。"""
        if page is None:
            return ""
        key = (source, int(page))
        if key in self._page_cache:
            return self._page_cache[key]
        pdf = self.pdf_map.get(source)
        if not pdf or not pdf.exists():
            return ""
        off = int(self.offsets.get(source, 0) or 0)
        pno = int(page) + off
        try:
            with pymupdf.open(pdf) as doc:
                if 1 <= pno <= doc.page_count:
                    t = re.sub(r"\s+", " ", doc[pno - 1].get_text("text")).strip()
                    self._page_cache[key] = t
                    return t
        except Exception:
            pass
        return ""

    # ---------------- BM25 ----------------
    def _load_tokens(self):
        if self._tokens is not None:
            return self._tokens
        if BM25_TOKENS.exists():
            with BM25_TOKENS.open("rb") as f:
                self._tokens = pickle.load(f)
        else:
            self._tokens = [jieba.lcut_for_search(m["text"]) for m in self.meta]
            with BM25_TOKENS.open("wb") as f:
                pickle.dump(self._tokens, f, protocol=pickle.HIGHEST_PROTOCOL)
        return self._tokens

    @property
    def bm25(self):
        if self._bm25 is None:
            self._bm25 = BM25Okapi(self._load_tokens())
        return self._bm25

    def rewrite_queries(self, query: str, n: int = 3) -> list[str]:
        """把问题改写成 n 个检索式（同义词/别名/章节关键词），失败则退回原问题。"""
        prompt = f"""请把下面的医学问题改写成 {n} 个用于教材检索的查询，每行一个，不要编号、不要解释。
可以补充同义词、别名和章节关键词（如“诊断标准”“分型”“病理”“机制”“治疗”），但不要编造新概念。

问题：{query}
"""
        base = [query, _keyword_query(query)]
        r = _chat(prompt, system="你是医学检索查询改写器，只输出查询，每行一个。", temperature=0.3)
        out = []
        for line in (r or "").splitlines():
            line = re.sub(r"^[-•\d.、\s]+", "", line.strip())
            if line and len(line) >= 4:
                out.append(line)
        return base + out[:n]

    # ---------------- 检索 ----------------
    def _dense_scores(self, query: str) -> np.ndarray:
        q = llm.embed([query])
        if not q:
            return np.zeros(len(self.meta), dtype=np.float32)
        qv = np.asarray(q[0], dtype=np.float32)
        qv = qv / (np.linalg.norm(qv) or 1.0)
        return self.vecs @ qv

    def _bm25_scores(self, query: str) -> np.ndarray:
        return np.asarray(self.bm25.get_scores(jieba.lcut_for_search(query)), dtype=np.float32)

    def search(
        self,
        query: str,
        books: list[str] | None = None,
        mode: str = "hybrid",
        candidate_k: int = 30,
        dense_weight: float = 0.65,
        rewrite: bool = False,
        chapters: list[str] | None = None,
    ) -> list[dict]:
        queries = self.rewrite_queries(query) if rewrite else [query]
        dense = np.zeros(len(self.meta), dtype=np.float32)
        bm25 = np.zeros(len(self.meta), dtype=np.float32)
        for q in queries:
            dense = np.maximum(dense, self._dense_scores(q))
            bm25 = np.maximum(bm25, self._bm25_scores(q))

        if mode == "dense":
            fused = _norm(dense)
        elif mode == "bm25":
            fused = _norm(bm25)
        else:
            fused = dense_weight * _norm(dense) + (1 - dense_weight) * _norm(bm25)

        if books:
            allowed = set(books)
            mask = np.array([m["source"] in allowed for m in self.meta])
            fused = np.where(mask, fused, -1.0)

        top = np.argsort(-fused)[: max(1, candidate_k)]
        hits = []
        for i in top:
            i = int(i)
            if fused[i] < 0:
                continue
            m = self.meta[i]
            hits.append({
                "id": i,
                "text": m.get("text", ""),
                "source": m.get("source", ""),
                "page": m.get("page"),
                "chapter": self.chapter_of(m.get("source", ""), m.get("page")),
                "dense": float(dense[i]),
                "bm25": float(bm25[i]),
                "score": float(fused[i]),
            })
        hits = self._page_boost(query, hits)[:candidate_k]
        if chapters:
            wanted = set(chapters)
            hits = [h for h in hits if h.get("chapter") in wanted]
        return hits

    # ---------------- LLM 重排 ----------------
    def rerank(self, query: str, hits: list[dict], top_n: int = 6) -> list[dict]:
        if not hits:
            return []
        lines = []
        for i, h in enumerate(hits, 1):
            text = re.sub(r"\s+", " ", h["text"])[:600]
            lines.append(f"[{i}] 《{h['source']}》第{h['page']}页\n{text}")
        prompt = f"""你是检索结果重排器。请判断下面每个片段能否直接回答该问题，按回答价值从高到低排序。
不要因为片段里出现相同的疾病名就给高分；要看它是否包含问题所问的定义、标准、分型或治疗。
只输出一个 JSON 数组，例如 [3,1,8]，输出 4~{top_n} 个最相关序号（候选不足 4 条时按实际数量）；不要输出解释、不要编造序号。
问题：{query}

片段：
{chr(10).join(lines)}
"""
        r = _chat(prompt, system="你是严谨的检索重排器，只输出 JSON 数组。", temperature=0.0)
        if not r:
            return hits[:top_n]
        m = re.search(r"\[[\d,\s]+\]", r)
        if not m:
            return hits[:top_n]
        try:
            idx = json.loads(m.group(0))
        except Exception:
            return hits[:top_n]
        out, seen = [], set()
        for x in idx:
            try:
                j = int(x) - 1
            except Exception:
                continue
            if 0 <= j < len(hits) and j not in seen:
                seen.add(j)
                out.append(hits[j])
            if len(out) >= top_n:
                break
        if len(out) < min(4, len(hits)):
            for h in hits:
                if h not in out:
                    out.append(h)
                if len(out) >= min(4, len(hits)):
                    break
        out.sort(key=lambda h: h.get("page_score", h["score"]), reverse=True)
        return out or hits[:top_n]

    def _page_boost(self, query: str, hits: list[dict]) -> list[dict]:
        """按命中页整页的关键词覆盖率给页面加分，弥补分块漏段。"""
        qtok = {t for t in jieba.lcut_for_search(query) if len(t) >= 2}
        best = {}
        for h in hits:
            key = (h["source"], h["page"])
            full = self.page_text(h["source"], h["page"])
            cov = 0.0
            if full:
                ptok = set(jieba.lcut_for_search(full))
                cov = len(qtok & ptok) / max(1, len(qtok))
            h["page_coverage"] = round(cov, 3)
            h["page_score"] = h["score"] + 0.15 * cov
            if key not in best or h["page_score"] > best[key]["page_score"]:
                best[key] = h
        return sorted(best.values(), key=lambda x: x["page_score"], reverse=True)

    def diversify(self, hits: list[dict], top_n: int = 6, max_per_source: int = 2) -> list[dict]:
        """跨教材时限制单一来源占比，避免一本书刷屏；不足再按顺序补齐。"""
        out, count = [], {}
        for h in hits:
            src = h.get("source", "")
            if count.get(src, 0) >= max_per_source:
                continue
            out.append(h)
            count[src] = count.get(src, 0) + 1
            if len(out) >= top_n:
                break
        if len(out) < top_n:
            for h in hits:
                if h not in out:
                    out.append(h)
                if len(out) >= top_n:
                    break
        return out[:top_n]

    # ---------------- 生成 ----------------
    def answer(self, query: str, hits: list[dict]) -> str:
        if not hits:
            return "知识库中未检索到相关内容。可以换一个问法，或放宽教材筛选。"
        ctx, used, seen = [], [], set()
        for h in hits:
            key = (h["source"], h["page"])
            if key in seen:
                continue
            seen.add(key)
            used.append(h)
            page = self.page_text(h["source"], h["page"])
            text = page if len(page) > len(h["text"]) else h["text"]
            ctx.append(f"《{display_source(h['source'])}》第{h['page']}页\n{text[:3500]}\n---")
        prompt = f"""请只依据下面的教材片段回答问题。

要求：
1. 先给结论，再分点说明。
2. 正文不要写 [1] 这类编号，也不要自己编参考来源；系统会在文末按真实检索结果列出。
3. 片段里没有的内容不要编造；证据不足时直接说“知识库中未找到足够依据”，并说明缺什么。
4. 数值和单位必须逐字照抄片段原文；g/d、g/L、mg/dl、ml/min 等不得改写或混用。
5. 如果不同教材给出不同标准，按教材分别列出，不要合并成一个标准。
6. 涉及分型、诊断标准、首选治疗时，尽量保留原文的术语。

问题：{query}

教材片段：
{chr(10).join(ctx)}
"""
        r = _chat(prompt, system="你是严谨的医学知识库助手，只依据给定片段回答。", temperature=0.2)
        r = (r or "").strip()
        if not r:
            excerpt = (chr(10) * 2).join(
                f"《{display_source(h['source'])}》第{h['page']}页{chr(10)}{h['text'][:500]}"
                for h in used[:3]
            )
            return ("模型生成暂不可用（DeepSeek 连接失败，智谱兜底也未成功）。"
                    "以下是最相关的原文依据：" + chr(10) * 2 + excerpt)
        r = re.sub(r"\[\d+\]", "", r)
        r = re.split(r"\n\s*参考来源\s*[:：]", r)[0].strip()
        refs = "\n".join(
            f"- 《{display_source(h['source'])}》第 {h['page']} 页" for h in used
        )
        return f"{r}\n\n参考来源（按检索命中页列出）：\n{refs}"


_KB = None


def get_kb() -> "KnowledgeBase":
    """进程内单例：主页面和“做题”页共用一份索引，避免重复加载 190MB 向量。"""
    global _KB
    if _KB is None:
        _KB = KnowledgeBase()
    return _KB


def format_sources(hits: list[dict]) -> str:
    lines = []
    for i, h in enumerate(hits, 1):
        lines.append(f"[{i}] 《{display_source(h['source'])}》第 {h['page']} 页")
    return "\n".join(lines)