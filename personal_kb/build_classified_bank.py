# -*- coding: utf-8 -*-
"""把 91 个章节文件夹的扫描结果 + 答案合并成“按平台目录分类”的题库。

输出：
- D:\\医学知识库\\题库导出\\平台题库分类\\<学科>\\...\\<章节>.md / .json
- data/platform_bank.json：章节级分类题库（替换旧的 6 个学科模块）
"""
from __future__ import annotations

import glob
import json
import re
import shutil
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SWEEP = ROOT / "data" / "folder_sweep"
REFILL = ROOT / "data" / "refill_out"
EXPORT = Path(r"D:\医学知识库\题库导出\平台题库分类")
BANK_FILE = ROOT / "data" / "platform_bank.json"
EXPORT.mkdir(parents=True, exist_ok=True)

TYPE_MAP = {"单选题": "single", "多选题": "multiple", "判断题": "tf", "填空题": "fill", "简答题": "short"}
TYPE_TAG_RE = re.compile(r"^[（(]\s*(?:单选题|多选题|判断题|填空题|简答题|名词解释|论述题|A3A4型|共用题干|案例分析题)\s*[）)]\s*")
# 跨文件夹去重优先级：专科章节优先，西综306年份卷最后
PRIORITY = ["生理学", "病理学", "病理生理学", "诊断学", "执业医师真题库", "西综306"]


def norm(s: str) -> str:
    x = (s or "").lower().replace("\u00a0", "")
    x = re.sub(r"[（(]\s*\d+\s*分\s*[）)]", "", x)
    while True:
        m = TYPE_TAG_RE.match(x)
        if not m:
            break
        x = x[m.end():]
    x = re.sub(r"^\d{1,3}[.、．]\s*", "", x)
    return re.sub(r"[\s。；;,.、:：()（）\[\]【】]", "", x)


def norm_type(t: str) -> str:
    t = (t or "").strip()
    if t.startswith("单选"):
        return "单选题"
    if t.startswith("多选"):
        return "多选题"
    if t.startswith("判断"):
        return "判断题"
    if t.startswith("填空"):
        return "填空题"
    if t.startswith("简答") or t.startswith("名词解释") or t.startswith("论述"):
        return "简答题"
    return "单选题" if len(t) > 6 or not t else t


def load_answers() -> dict[str, dict]:
    """题干+选项 -> 答案（来自22套自测抓取 + 补缺收藏）"""
    amap: dict[str, dict] = {}
    for f in glob.glob(str(ROOT / "data" / "papers" / "*.json")):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for q in d.get("questions", []):
            if not (q.get("answer") or q.get("analysis")):
                continue
            key = norm(q.get("stem")) + "|" + "|".join(norm(o.get("text", "")) for o in (q.get("options") or []))
            amap.setdefault(key, {"answer": q.get("answer"), "analysis": q.get("analysis"), "difficulty": q.get("difficulty")})
    for f in glob.glob(str(REFILL / "*.json")):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for q in d.get("collected", []):
            if not (q.get("answer") or q.get("analysis")):
                continue
            key = norm(q.get("stem")) + "|" + "|".join(norm(o.get("text", "")) for o in (q.get("options") or []))
            amap.setdefault(key, {"answer": q.get("answer"), "analysis": q.get("analysis"), "difficulty": q.get("difficulty")})
    return amap


def convert(q: dict, path: list[str], ans: dict | None) -> dict | None:
    t = norm_type(q.get("type", ""))
    stem = (q.get("stem") or "").strip()
    if not stem:
        return None
    options = [{"key": o.get("key", ""), "text": (o.get("text") or "").strip()} for o in (q.get("options") or [])]
    item = {
        "id": "",
        "qtype": TYPE_MAP.get(t, "single" if options else "short"),
        "type_label": t,
        "stem": stem,
        "options": options,
        "answer": [],
        "answer_text": "",
        "explanation": (ans or {}).get("analysis", "") or "",
        "difficulty": (ans or {}).get("difficulty", "") or (q.get("difficulty") or ""),
        "knowledge": "",
        "folder": path,
        "source": "医学智慧教学平台 · 题库（按章节）",
        "question_id": "",
        "number": 0,
    }
    raw = ((ans or {}).get("answer") or "").strip() if ans else ""
    keys = [o["key"].upper() for o in item["options"]]
    if item["qtype"] in ("single", "multiple"):
        letters = [c for c in raw.upper() if c in "ABCDEFGH"]
        idx = sorted({keys.index(c) for c in letters if c in keys})
        if not idx:
            return None
        if item["qtype"] == "single" and len(idx) != 1:
            item["qtype"] = "multiple"
            item["type_label"] = "多选题"
        item["answer"] = idx
        item["answer_text"] = "".join(letters)
    elif item["qtype"] == "tf":
        if raw in ("对", "正确", "是", "true", "T", "√"):
            item["answer"] = [True]
        elif raw in ("错", "错误", "否", "false", "F", "×"):
            item["answer"] = [False]
        else:
            return None
        if not item["options"]:
            item["options"] = [{"key": "对", "text": "正确"}, {"key": "错", "text": "错误"}]
        item["answer_text"] = "正确" if item["answer"][0] else "错误"
    else:
        if not raw:
            return None
        item["answer_text"] = raw
    # 稳定 id：用题干指纹，保证跨批次稳定
    import hashlib
    item["id"] = "c_" + hashlib.md5((norm(stem) + "|" + "|".join(norm(o["text"]) for o in options)).encode("utf-8")).hexdigest()[:12]
    return item


def main() -> None:
    amap = load_answers()
    print(f"answer bank: {len(amap)}")

    # 1) 读取扫描结果，转成统一题目，附章节路径
    entries: list[tuple[list[str], dict]] = []
    no_answer = 0
    for f in sorted(glob.glob(str(SWEEP / "*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        for q in d.get("questions", []):
            key = norm(q.get("stem")) + "|" + "|".join(norm(o.get("text", "")) for o in (q.get("options") or []))
            ans = amap.get(key)
            conv = convert(q, d["path"], ans)
            if conv is None:
                no_answer += 1
                continue
            entries.append((d["path"], conv))
    print(f"sweep converted: {len(entries)}, dropped(no answer/stem): {no_answer}")

    # 2) 跨文件夹去重（按优先级）
    order = {name: i for i, name in enumerate(PRIORITY)}
    entries.sort(key=lambda x: order.get(x[0][0], 99))
    seen: set[str] = set()
    classified: list[tuple[list[str], dict]] = []
    for path, q in entries:
        key = norm(q["stem"]) + "|" + "|".join(norm(o["text"]) for o in q["options"])
        if key in seen:
            continue
        seen.add(key)
        classified.append((path, q))

    # 3) 导出到 平台题库分类/<学科>/...
    by_tree: dict[tuple, list[dict]] = {}
    for path, q in classified:
        by_tree.setdefault(tuple(path), []).append(q)

    type_label = {"single": "单选题", "multiple": "多选题", "tf": "判断题", "fill": "填空题", "short": "简答题"}
    total = 0
    index_rows = []
    for tree_key in sorted(by_tree):
        qs = by_tree[tree_key]
        chapter = tree_key[-1]
        subject = tree_key[0]
        folder = EXPORT.joinpath(*tree_key)
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"source": "医学智慧教学平台 · 题库", "path": list(tree_key), "count": len(qs), "questions": qs}
        (folder / f"{chapter}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        lines = [f"# {chapter}", "", f"> 路径：{' / '.join(tree_key)}　共 {len(qs)} 题", ""]
        tc = Counter(q["type_label"] for q in qs)
        lines.append("> 题型：" + "、".join(f"{k} {v} 题" for k, v in tc.most_common()))
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
            lines.append(f"**正确答案：** {q['answer_text']}")
            if q.get("explanation"):
                lines.append(f"**解析：** {q['explanation']}")
            if q.get("difficulty"):
                lines.append(f"**难度：** {q['difficulty']}")
            lines.append("")
        (folder / f"{chapter}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        total += len(qs)
        index_rows.append({"学科": subject, "路径": " / ".join(tree_key), "题数": len(qs)})

    # 分类索引 CSV
    import csv
    with (EXPORT / "分类索引.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["学科", "路径", "题数"])
        w.writeheader()
        w.writerows(sorted(index_rows, key=lambda r: (r["学科"], r["路径"])))
    print(f"exported chapters: {len(by_tree)}, questions: {total}")

    # 4) 更新 platform_bank.json：旧6个学科模块 -> 章节级分类
    bank = json.loads(BANK_FILE.read_text(encoding="utf-8"))
    superseded = {"生理学", "病理学", "病理生理学", "诊断学", "2025年临床执业医师真题", "西综306"}
    kept = [q for q in bank.get("questions", [])
            if not (len(q.get("folder", [])) > 1 and q["folder"][1] in superseded)]
    old_seen = {
        norm(q.get("stem", "")) + "|" + "|".join(norm(o.get("text", "")) for o in (q.get("options") or []))
        for q in kept
    }
    final = list(kept)
    added = 0
    for path, q in classified:
        key = norm(q["stem"]) + "|" + "|".join(norm(o["text"]) for o in q["options"])
        if key in old_seen:
            continue
        old_seen.add(key)
        final.append(q)
        added += 1
    bank["questions"] = final
    bank["count"] = len(final)
    bank["stats"] = dict(Counter("/".join(q.get("folder", [])) for q in final))
    BANK_FILE.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"bank: kept {len(kept)}, added {added}, total {bank['count']}")
    print(f"output: {EXPORT}")


if __name__ == "__main__":
    main()
