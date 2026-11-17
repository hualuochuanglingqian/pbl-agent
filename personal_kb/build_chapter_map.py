# -*- coding: utf-8 -*-
"""为 43 本教材建立“印刷页 -> 篇/章”映射。

不同 PDF 的目录页码口径不同：有的写 PDF 物理页，有的写印刷页。
这里先用章节标题在正文里回找，判断本书属于哪种，再统一换算成印刷页。
"""
import json
import re
import sys
from pathlib import Path

import pymupdf

PBL_ROOT = Path(r"D:\智能体开发\PBL智能体")
sys.path.insert(0, str(PBL_ROOT))
from pbl import textbook  # noqa: E402

OUT = Path(__file__).resolve().parent / "index" / "chapter_map.json"
CH = re.compile(r"^第[一二三四五六七八九十百]+章")


def clean(t: str) -> str:
    t = (t or "").replace("\x00", "")
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)
    return re.sub(r"\s+", " ", t.replace("|", " ").replace("\u3000", " ")).strip()


def key_of(title: str) -> str:
    k = re.sub(r"\s+", "", clean(title))
    k = re.sub(r"[|｜/、，。；：（）()\[\]【】]", "", k)
    return k[:12]


def is_toc(text: str) -> bool:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        return True
    digit = sum(1 for x in lines if re.search(r"\d{1,4}$", x))
    return digit / len(lines) > 0.35 or "目录" in text[:300]


def page_has(doc, pno: int, key: str) -> bool:
    if not (1 <= pno <= doc.page_count):
        return False
    t = doc[pno - 1].get_text("text")
    if is_toc(t):
        return False
    return key in re.sub(r"\s+", "", t)


def main():
    offsets = textbook._load_offsets()
    result = {}
    books = sorted(textbook.BOOKS_DIR.glob("*.pdf"))
    for i, pdf in enumerate(books, 1):
        source = textbook._book_title(pdf.name)
        off = int(offsets.get(source, 0) or 0)
        doc = pymupdf.open(pdf)
        toc = []
        for lvl, title, page in doc.get_toc(simple=True):
            t = clean(title)
            if not t or page is None:
                continue
            try:
                page = int(page)
            except Exception:
                continue
            if 1 <= page <= doc.page_count:
                toc.append((lvl, t, page))
        if not toc:
            doc.close()
            continue

        parents = [(p, t) for lvl, t, p in toc if lvl == 1]
        chapters = [(lvl, t, p) for lvl, t, p in toc if lvl == 2 and CH.match(t)]
        if len(chapters) < 5:
            chapters = [(lvl, t, p) for lvl, t, p in toc if lvl == 1 and CH.match(t)]
        if len(chapters) < 3:
            chapters = [(lvl, t, p) for lvl, t, p in toc if lvl == 1]
        if not chapters:
            doc.close()
            continue

        # 判断目录页码是 PDF 页还是印刷页
        sample = [c for c in chapters if len(key_of(c[1])) >= 6][:3]
        pdf_hits = sum(1 for _, t, p in sample if page_has(doc, p, key_of(t)))
        printed_hits = sum(1 for _, t, p in sample if page_has(doc, p + off, key_of(t)))
        toc_is_pdf = pdf_hits >= printed_hits
        if pdf_hits == 0 and printed_hits == 0:
            toc_is_pdf = True

        rows = []
        for lvl, title, page in chapters:
            parent = ""
            for pp, pt in parents:
                if pp <= page:
                    parent = pt
                else:
                    break
            if parent and not title.startswith(parent):
                title = f"{parent} · {title}"
            printed = page - off if toc_is_pdf else page
            rows.append({"title": title, "start": max(1, printed), "_toc_page": page})

        # 去重 + 排序 + 补 end
        seen, uniq = set(), []
        for r in rows:
            k = (r["title"], r["start"])
            if k in seen:
                continue
            seen.add(k)
            uniq.append(r)
        uniq.sort(key=lambda x: x["start"])
        for j, r in enumerate(uniq):
            r["end"] = (uniq[j + 1]["start"] - 1) if j + 1 < len(uniq) else 10 ** 6
            r.pop("_toc_page", None)

        valid = [r for r in uniq if r["end"] >= r["start"]]
        if len(valid) < max(3, int(len(uniq) * 0.6)):
            print(f"[{i}/{len(books)}] {source}: 目录页码口径混杂，跳过章映射", flush=True)
            doc.close()
            continue
        result[source] = valid
        doc.close()
        print(f"[{i}/{len(books)}] {source}: {len(uniq)} 章/篇, TOC={'PDF' if toc_is_pdf else '印刷页'}",
              flush=True)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("saved", OUT, len(result))


if __name__ == "__main__":
    main()
