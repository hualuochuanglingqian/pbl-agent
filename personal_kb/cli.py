# -*- coding: utf-8 -*-
"""命令行问答：python cli.py "问题" [--books 内科学,病理学] [--no-rerank]"""
import argparse

from kb import KnowledgeBase, display_source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--books", default="", help="逗号分隔的书名（显示名或索引名均可）")
    ap.add_argument("--mode", default="hybrid", choices=["hybrid", "dense", "bm25"])
    ap.add_argument("--candidates", type=int, default=30)
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--rewrite", action="store_true")
    ap.add_argument("--chapters", default="", help="逗号分隔的章节名（需与索引中的章节标题一致）")
    ap.add_argument("--retrieve-only", action="store_true")
    args = ap.parse_args()

    kb = KnowledgeBase()
    display_to_raw = {display_source(s): s for s in kb.sources}
    books = []
    for x in [x.strip() for x in args.books.split(",") if x.strip()]:
        books.append(display_to_raw.get(x, x))

    chaps = [x.strip() for x in args.chapters.split(",") if x.strip()]
    hits = kb.search(args.query, books=books, mode=args.mode,
                     candidate_k=args.candidates, rewrite=args.rewrite, chapters=chaps)
    if args.no_rerank:
        hits = kb.diversify(hits, top_n=args.top)
    else:
        hits = kb.rerank(args.query, hits, top_n=min(12, len(hits)))
        hits = kb.diversify(hits, top_n=args.top)

    print(f"\n检索到 {len(hits)} 条：")
    for i, h in enumerate(hits, 1):
        ch = f" · {h['chapter']}" if h.get("chapter") else ""
        print(f"[{i}] 《{display_source(h['source'])}》第 {h['page']} 页{ch} "
              f"(融合 {h['score']:.3f} / 向量 {h['dense']:.3f} / BM25 {h['bm25']:.3f})")
        print("    " + h["text"][:180].replace("\n", " "))

    if not args.retrieve_only:
        print("\n回答：\n" + kb.answer(args.query, hits))


if __name__ == "__main__":
    main()
