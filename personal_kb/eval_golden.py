# -*- coding: utf-8 -*-
"""人工黄金问答集评估：检索命中页 + 关键点覆盖。"""
import argparse
import json
import re
from pathlib import Path

from kb import KnowledgeBase

BASE = Path(__file__).resolve().parent
GOLDEN = BASE / "eval" / "golden.jsonl"


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rewrite", action="store_true")
    args = ap.parse_args()
    kb = KnowledgeBase()
    items = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows, h1, h3, h5, mrr, covered, total_kp = [], 0, 0, 0, 0.0, 0, 0

    for it in items:
        q = it["question"]
        book = it["book"]
        expected = set(it.get("expected_pages") or [])
        hits = kb.search(q, books=[book], mode="hybrid", candidate_k=10, rewrite=args.rewrite)
        ranks = [j + 1 for j, h in enumerate(hits) if h.get("page") in expected]
        rank = min(ranks) if ranks else None
        h1 += rank == 1
        h3 += bool(rank and rank <= 3)
        h5 += bool(rank and rank <= 5)
        mrr += (1.0 / rank) if rank else 0.0

        top_text = norm(" ".join(kb.page_text(h["source"], h["page"]) for h in hits[:5]))
        kps = it.get("key_points") or []
        missing = [k for k in kps if norm(k) not in top_text]
        covered += len(kps) - len(missing)
        total_kp += len(kps)

        rows.append({
            "question": q,
            "book": book,
            "expected_pages": sorted(expected),
            "rank": rank,
            "top_pages": [h.get("page") for h in hits[:5]],
            "missing_key_points": missing,
        })

    n = len(rows)
    report = {
        "n": n,
        "hit@1": round(h1 / n, 3),
        "hit@3": round(h3 / n, 3),
        "hit@5": round(h5 / n, 3),
        "mrr": round(mrr / n, 3),
        "key_point_coverage@5": round(covered / max(1, total_kp), 3),
        "failures": [r for r in rows if not r["rank"] or r["rank"] > 3],
    }
    (BASE / "eval" / "golden_report.json").write_text(
        json.dumps({"summary": report, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for r in rows:
        print(f"  {r['rank']}  {r['question'][:38]:40s}  top={r['top_pages']}")


if __name__ == "__main__":
    main()
