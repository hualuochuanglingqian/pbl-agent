# -*- coding: utf-8 -*-
"""检索自检：从索引里抽样知识块，用块首句反查原页，算 Hit@1/3/5 与 MRR。

用法：python eval.py --n 30
"""
import argparse
import json
import random
import re
from pathlib import Path

from kb import KnowledgeBase

BASE = Path(__file__).resolve().parent


def first_sentence(text: str, max_len: int = 100) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[。！？；])", text)
    q = (parts[0] if parts else text).strip()
    return q[:max_len]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    kb = KnowledgeBase()
    random.seed(args.seed)

    # 每个 (书, 页) 只取一个块，避免同一页重复
    by_page = {}
    for i, m in enumerate(kb.meta):
        if "题库" in m.get("source", ""):
            continue
        t = m.get("text", "")
        if len(t) < 120:
            continue
        by_page.setdefault((m["source"], m["page"]), i)
    keys = list(by_page)
    random.shuffle(keys)
    keys = keys[: args.n]

    rows, hit1, hit3, hit5, mrr = [], 0, 0, 0, 0.0
    for source, page in keys:
        i = by_page[(source, page)]
        q = first_sentence(kb.meta[i]["text"])
        if len(q) < 20:
            q = re.sub(r"\s+", " ", kb.meta[i]["text"]).strip()[:100]
        if len(q) < 20:
            continue
        hits = kb.search(q, books=[source], mode="hybrid", candidate_k=10)
        ranks = [j + 1 for j, h in enumerate(hits) if h["page"] == page]
        rank = min(ranks) if ranks else None
        hit1 += rank == 1
        hit3 += bool(rank and rank <= 3)
        hit5 += bool(rank and rank <= 5)
        mrr += (1.0 / rank) if rank else 0.0
        rows.append({
            "query": q,
            "source": source,
            "expected_page": page,
            "rank": rank,
            "top_pages": [h["page"] for h in hits[:5]],
        })

    n = len(rows)
    report = {
        "n": n,
        "hit@1": round(hit1 / n, 3),
        "hit@3": round(hit3 / n, 3),
        "hit@5": round(hit5 / n, 3),
        "mrr": round(mrr / n, 3),
        "failures": [r for r in rows if not r["rank"] or r["rank"] > 3][:15],
    }
    (BASE / "eval").mkdir(exist_ok=True)
    (BASE / "eval" / "report.json").write_text(
        json.dumps({"summary": report, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
