"""十版教材本地知识库（RAG）。

把 knowledge_base/books/ 下的 PDF / docx / txt / md 教材
抽取 -> 分块 -> 智谱 embedding -> 本地向量索引（npz + jsonl）。
查询返回教材原文段落与出处（书名、页码），供学习内容引用溯源。

增量建库：每处理完一本即原子保存，可随时中断重跑（自动跳过已入库书目）；
索引部分可用时查询即可命中已入库教材。无索引时 query() 返回空列表。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np

from . import llm

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"
BOOKS_DIR = KB_DIR / "books"
INDEX_DIR = KB_DIR / "index"
VEC_FILE = INDEX_DIR / "vectors.npz"
META_FILE = INDEX_DIR / "chunks.jsonl"

CHUNK_CHARS = 700      # 目标块长
CHUNK_MAX = 1200       # 块长上限
EMBED_BATCH = 32

# 检索相关性阈值：embedding-3 上相关命中约 0.62+，无关约 0.53-，低于阈值视为未命中
MIN_SCORE = float(os.getenv("PBL_MIN_SCORE", "0.55"))
OFFSET_FILE = INDEX_DIR / "page_offsets.json"   # 各书 PDF页码→印刷页码 偏移记录


def _extract_pdf(path: Path) -> list[tuple[int | None, str]]:
    """返回 [(页码或None, 文本), ...]"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        import pymupdf as fitz
    pages = []
    with fitz.open(path) as doc:
        for page in doc:
            text = page.get_text("text").strip()
            if text:
                pages.append((page.number + 1, text))
    return pages


def _extract_docx(path: Path) -> list[tuple[int | None, str]]:
    from docx import Document
    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return [(None, text)]


def _extract_plain(path: Path) -> list[tuple[int | None, str]]:
    return [(None, path.read_text(encoding="utf-8", errors="ignore"))]


def _extract(path: Path) -> list[tuple[int | None, str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    return _extract_plain(path)


def _book_title(filename: str) -> str:
    """从文件名清洗书名：「21内科+第10版+ (1).pdf」->「内科（第10版）」。"""
    stem = Path(filename).stem
    stem = re.sub(r"\s*\(\d+\)$", "", stem)          # 去 " (1)" 重复后缀
    m = re.search(r"第(\d+)版", stem)
    edition = f"第{m.group(1)}版" if m else ""
    name = re.sub(r"第\d+版", "", stem)
    name = re.sub(r"^\d+\s*", "", name)               # 去序号
    name = name.replace("+", "").replace("＋", "").strip(" +_")
    return f"{name}（{edition}）" if edition else name


_CATALOG = Path(__file__).parent / "book_catalog.json"


def _page_offset(pages: list[tuple[int | None, str]]) -> int:
    """推断 PDF 物理页码与印刷页码的偏移（封面/编委/前言/目录造成）。

    取每页文本中第一个「独立数字行」当作印刷页码投票，众数且足够一致才采纳；
    返回 0 表示无法校准（页码按 PDF 物理页使用）。
    """
    from collections import Counter
    votes: Counter = Counter()
    for pdf_no, text in pages:
        if not pdf_no:
            continue
        for line in text.split("\n"):
            s = line.strip().replace(" ", "").replace("\u3000", "")
            if s.isdigit():
                printed = int(s)
                off = pdf_no - printed
                if 0 <= off <= 60 and 1 <= printed <= len(pages) + 60:
                    votes[off] += 1
                break
    if not votes:
        return 0
    off, n = votes.most_common(1)[0]
    return off if n >= 5 and n >= sum(votes.values()) * 0.5 else 0


def find_catalog_entry(name: str, edition: str = "") -> dict | None:
    """按书名（+版次）在著录目录中查找，返回著录信息；未收录返回 None。"""
    try:
        cat = json.loads(_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name_n = name.strip()
    ed_n = re.sub(r"版", "", edition or "").strip()
    for key, info in cat.items():
        i_title = info.get("title", "").strip()
        # 双向前缀匹配：「病理学」↔目录「病理」、「内科」↔目录「内科学」
        if not (i_title == name_n or i_title.startswith(name_n) or name_n.startswith(i_title)):
            continue
        key_ed = re.sub(r"版", "", (info.get("edition") or "").strip())
        if ed_n and key_ed and ed_n != key_ed:
            continue
        out = dict(info)
        out.setdefault("edition", f"{ed_n}版" if ed_n else "")
        return out
    return None


def book_ref_info(title: str) -> dict:
    """查书名著录信息（pbl/book_catalog.json，可手动编辑补充作者等）。

    返回 {title, authors, place, publisher, year, edition}；未收录时从书名解析兜底。
    """
    try:
        cat = json.loads(_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cat = {}
    info = cat.get(title) or cat.get(title.replace("（", "(").replace("）", ")")) or {}
    m = re.search(r"第(\d+)版", title)
    name = re.sub(r"（第\d+版）", "", title).strip()
    return {
        "title": info.get("title", name),
        "authors": info.get("authors", ""),
        "place": info.get("place", ""),
        "publisher": info.get("publisher", ""),
        "year": info.get("year", ""),
        "edition": info.get("edition", f"{m.group(1)}版" if m else ""),
    }


MATERIALS_DIR = KB_DIR / "materials"


def extract_materials_text(max_chars: int = 60000) -> str:
    """提取 knowledge_base/materials/ 下全部 Word/PDF/txt 资料的文本（导图生成用）。"""
    MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for f in sorted(MATERIALS_DIR.iterdir()):
        if f.suffix.lower() not in (".pdf", ".docx", ".txt", ".md") or f.name.startswith("_"):
            continue
        try:
            pages = _extract(f)
            text = chr(10).join(x[1] for x in pages)
        except Exception as e:
            print(f"[materials] 提取失败 {f.name}: {e}")
            continue
        if text.strip():
            parts.append(f"=== 资料：{f.name} ==={chr(10)}{text}")
        if sum(len(x) for x in parts) >= max_chars:
            break
    return (chr(10) + chr(10)).join(parts)[:max_chars]


def list_materials() -> list[str]:
    if not MATERIALS_DIR.exists():
        return []
    return sorted(f.name for f in MATERIALS_DIR.iterdir()
                  if f.suffix.lower() in (".pdf", ".docx", ".txt", ".md") and not f.name.startswith("_"))


# 目录条目行特征：六个以上点号引导符（……22）
_TOC_LINE = re.compile(r"[．.·…]{6,}")


def _chunk(pages: list[tuple[int | None, str]], source: str, offset: int = 0) -> list[dict]:
    """按段落聚成 ~700 字的块，保留页码。

    offset>0 表示已校准 PDF→印刷页偏移：丢弃前置部分（封面/编委/前言/目录），
    页码换算为印刷页码；目录引导行无论在哪一律跳过。source 传原始文件名。
    """
    title = _book_title(source)
    if offset > 0:
        pages = [(n - offset if n else n, t) for n, t in pages if n is None or n > offset]
    chunks: list[dict] = []
    buf: list[str] = []
    buf_len = 0
    page = pages[0][0] if pages else None

    def flush():
        nonlocal buf, buf_len
        text = "\n".join(buf).strip()
        if len(text) >= 40:
            chunks.append({"text": text, "source": title, "page": page})
        buf, buf_len = [], 0

    for pg, whole in pages:
        for para in whole.split("\n"):
            para = para.strip()
            if not para or _TOC_LINE.search(para):
                continue
            # Markdown 小节（如题库的「## 第N题」）单独成块，避免一题被拆散或多题挤在一块
            if para.startswith("## ") and buf:
                flush()
                page = pg
            buf.append(para)
            buf_len += len(para)
            if buf_len >= CHUNK_CHARS:
                flush()
                page = pg
    flush()
    # 超长块截断
    for c in chunks:
        if len(c["text"]) > CHUNK_MAX:
            c["text"] = c["text"][:CHUNK_MAX]
    return chunks


def list_books() -> list[str]:
    if not BOOKS_DIR.exists():
        return []
    return sorted(
        p.name for p in BOOKS_DIR.iterdir()
        if p.suffix.lower() in (".pdf", ".docx", ".txt", ".md")
    )


def has_index() -> bool:
    return VEC_FILE.exists() and META_FILE.exists()


def _load_offsets() -> dict:
    try:
        return json.loads(OFFSET_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_offsets(offsets: dict) -> None:
    try:
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(json.dumps(offsets, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except OSError:
        pass


def recalibrate_pages(progress_cb=None) -> dict[str, dict]:
    """存量索引免向量化校准：逐书推断页码偏移，丢弃前置部分块，页码改写为印刷页码。

    只重写 chunks.jsonl 并同步删除对应向量行（不做任何 embedding），幂等可重复执行。
    返回 {书名: {"offset": 偏移, "dropped": 丢弃块数}}。
    """
    if not has_index():
        raise RuntimeError("尚未建立索引，请先建库")
    old = _load_offsets()
    if old.get("_applied"):
        return old.get("_books") or {}
    chunks = _load_meta()
    vectors = np.load(VEC_FILE)["vectors"]
    if len(vectors) != len(chunks):
        raise RuntimeError(f"索引不一致：{len(vectors)} 条向量 vs {len(chunks)} 个知识块，请重建索引")

    name_by_title: dict[str, str] = {}
    for fname in list_books():
        name_by_title.setdefault(_book_title(fname), fname)

    info: dict[str, dict] = {}
    keep: list[int] = []
    for i, c in enumerate(chunks):
        title = c["source"]
        if title not in info:
            fname = name_by_title.get(title)
            off = 0
            if fname:
                if progress_cb:
                    progress_cb(f"推断页码偏移：{title}")
                try:
                    off = _page_offset(_extract(BOOKS_DIR / fname))
                except Exception as e:
                    print(f"[textbook] 偏移推断失败 {title}: {e}")
            info[title] = {"offset": off, "dropped": 0}
        off = info[title]["offset"]
        page = c.get("page")
        if isinstance(page, int) and off > 0:
            if page <= off:            # 封面/编委/前言/目录
                info[title]["dropped"] += 1
                continue
            c["page"] = page - off
        keep.append(i)

    _save_index(vectors[keep], [chunks[i] for i in keep])
    _save_offsets({**{k: v["offset"] for k, v in info.items()},
                   "_applied": True, "_books": info})
    return info


def build_index(progress_cb=None) -> tuple[int, list[str]]:
    """增量重建索引：跳过已入库书目，每完成一本原子保存。返回 (总块数, 已入库书名列表)。"""
    books = list_books()
    if not books:
        raise RuntimeError("knowledge_base/books/ 下没有找到教材文件（支持 pdf/docx/txt/md）")

    chunks: list[dict] = []
    vectors: list[np.ndarray] = []
    offsets = _load_offsets()
    if has_index():
        chunks = _load_meta()
        vectors = list(np.load(VEC_FILE)["vectors"])
    done_titles = {c["source"] for c in chunks}
    new_books = [b for b in books if _book_title(b) not in done_titles]
    if progress_cb and not new_books:
        progress_cb("全部书目已在索引中，无需重建")

    for i, name in enumerate(new_books):
        if progress_cb:
            progress_cb(f"抽取文本：{name}（{i + 1}/{len(new_books)}）")
        pages = _extract(BOOKS_DIR / name)
        offset = _page_offset(pages)
        offsets[_book_title(name)] = offset
        book_chunks = _chunk(pages, name, offset=offset)
        if not book_chunks:
            continue
        embs: list[list[float]] = []
        for start in range(0, len(book_chunks), EMBED_BATCH):
            batch = [c["text"] for c in book_chunks[start:start + EMBED_BATCH]]
            if progress_cb:
                progress_cb(f"向量化 {name}：{start + 1}/{len(book_chunks)} 块")
            emb = llm.embed(batch)
            if emb is None:
                raise RuntimeError("向量化失败：请检查 ZHIPUAI_API_KEY 是否有效"
                                   "（已完成的书目进度已保存，修复后重跑即可续建）")
            embs.extend(emb)
        chunks.extend(book_chunks)
        vectors.extend(np.asarray(embs, dtype=np.float32))
        _save_index(vectors, chunks)
        _save_offsets(offsets)

    return len(chunks), sorted({c["source"] for c in chunks})


def _save_index(vectors: list, chunks: list[dict]) -> None:
    """原子写入：先写临时文件再替换，保证建库途中查询不会读到半截索引。"""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp_v = VEC_FILE.with_name(VEC_FILE.name + ".tmp.npz")   # numpy 会自动补 .npz 后缀
    np.savez_compressed(tmp_v, vectors=np.asarray(vectors, dtype=np.float32))
    os.replace(tmp_v, VEC_FILE)
    tmp_m = META_FILE.with_suffix(".jsonl.tmp")
    with tmp_m.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp_m, META_FILE)


def _load_meta() -> list[dict]:
    return [json.loads(line) for line in META_FILE.read_text(encoding="utf-8").splitlines()]


_query_cache: dict = {"key": None, "vecs": None, "meta": None}


def _load_index() -> tuple[np.ndarray, list[dict]]:
    """加载并归一化向量（按文件改动时间缓存，建库途中查询免重复加载）。"""
    key = (VEC_FILE.stat().st_mtime_ns, META_FILE.stat().st_mtime_ns)
    if _query_cache["key"] != key:
        vecs = np.load(VEC_FILE)["vectors"]
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        _query_cache["key"] = key
        _query_cache["vecs"] = vecs / norms
        _query_cache["meta"] = _load_meta()
    return _query_cache["vecs"], _query_cache["meta"]


def query(text: str, k: int = 4, min_score: float | None = None) -> list[dict]:
    """检索教材段落，返回 [{text, source, page, score}]。无索引返回 []。

    低于相关性阈值（MIN_SCORE，默认 0.55，可用环境变量 PBL_MIN_SCORE 调整）的
    命中视为未命中——避免把不相干段落喂给模型诱发硬凑引用。
    """
    if not has_index():
        return []
    thr = MIN_SCORE if min_score is None else min_score
    q = llm.embed([text])
    if q is None:
        return []
    vecs, meta = _load_index()
    qv = np.asarray(q[0], dtype=np.float32)
    qv = qv / (np.linalg.norm(qv) or 1.0)
    scores = vecs @ qv
    top = np.argsort(-scores)[:k]
    hits = []
    for i in top:
        if float(scores[int(i)]) >= thr:
            m = meta[int(i)]
            hits.append({**m, "score": float(scores[int(i)])})
    return hits
