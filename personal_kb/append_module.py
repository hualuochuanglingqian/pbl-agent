# -*- coding: utf-8 -*-
"""把新导出的自测卷（如药理 3 套）并入本地题库与导出目录。

用法：python append_module.py 药理 药理学
  参数1：试卷文件名前缀（data/papers/<前缀>*.json）
  参数2：模块名（导出为 题库导出/自测题库/<模块>.md/.json，bank 里 folder=[平台自测题库,<模块>]）
"""
from __future__ import annotations

import glob
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from merge_papers import convert, norm_key  # noqa: E402

PAPERS = ROOT / "data" / "papers"
EXPORT = Path(r"D:\医学知识库\题库导出\自测题库")
BANK_FILE = ROOT / "data" / "platform_bank.json"


def main() -> None:
    prefix, module = sys.argv[1], sys.argv[2]
    files = sorted(glob.glob(str(PAPERS / f"{prefix}*.json")))
    if not files:
        print(f"no papers matched: {prefix}")
        return

    bank = json.loads(BANK_FILE.read_text(encoding="utf-8"))
    existing = bank.get("questions", [])
    ex_seen = {
        norm_key(q.get("stem", "")) + "|" + "|".join(norm_key(o.get("text", "")) for o in (q.get("options") or []))
        for q in existing
    }

    # 同前缀内先按题干去重
    uniq: dict[str, dict] = {}
    per_file = []
    for f in files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        kept = 0
        for q in d.get("questions", []):
            conv = convert(q, module)
            if conv is None:
                continue
            key = norm_key(conv["stem"]) + "|" + "|".join(norm_key(o["text"]) for o in conv["options"])
            if key in uniq:
                continue
            uniq[key] = conv
            kept += 1
        per_file.append((Path(f).stem, len(d.get("questions", [])), kept))

    # 与现有题库去重后追加
    added = []
    for conv in uniq.values():
        key = norm_key(conv["stem"]) + "|" + "|".join(norm_key(o["text"]) for o in conv["options"])
        if key in ex_seen:
            continue
        ex_seen.add(key)
        added.append(conv)

    bank["questions"] = existing + added
    bank["count"] = len(bank["questions"])
    stats = bank.setdefault("stats", {})
    stats[module] = stats.get(module, 0) + len(added)
    BANK_FILE.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")

    # 导出 Markdown / JSON
    EXPORT.mkdir(parents=True, exist_ok=True)
    qs = sorted(added, key=lambda x: x.get("question_id", ""))
    payload = {"source": "医学智慧教学平台 · 自测题库", "module": module, "count": len(qs), "questions": qs}
    (EXPORT / f"{module}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# {module} · 平台自测题", "", f"> 共 {len(qs)} 题，来自学习通“医学智慧教学平台”创建的自测，含正确答案与解析。", ""]
    tcount = Counter(q["type_label"] for q in qs)
    lines.append("> 题型：" + "、".join(f"{k} {v} 题" for k, v in tcount.most_common()))
    lines.append("")
    for i, q in enumerate(qs, 1):
        lines.append(f"## {i}. [{q['type_label']}] {q['stem']}")
        lines.append("")
        for o in q["options"]:
            mark = ""
            if q["qtype"] in ("single", "multiple") and o.get("key", "").upper() in [chr(65 + j) for j in q["answer"]]:
                mark = " ✅"
            lines.append(f"- {o.get('key', '')}. {o.get('text', '')}{mark}")
        lines.append("")
        if q["qtype"] == "tf":
            lines.append(f"**正确答案：** {'正确' if q['answer'] and q['answer'][0] else '错误'}")
        else:
            lines.append(f"**正确答案：** {q['answer_text']}")
        if q.get("explanation"):
            lines.append(f"**解析：** {q['explanation']}")
        if q.get("difficulty"):
            lines.append(f"**难度：** {q['difficulty']}")
        lines.append("")
    (EXPORT / f"{module}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("== 每套试卷 ==")
    for title, total, kept in per_file:
        print(f"  {title}: 抓取 {total}, 去重后保留 {kept}")
    print(f"== 并入题库：新增 {len(added)} 题（与现有题库重复 {len(uniq) - len(added)}），总题量 {bank['count']} ==")
    print(f"== 导出：{EXPORT / (module + '.md')} / .json ==")


if __name__ == "__main__":
    main()
