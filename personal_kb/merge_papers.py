# -*- coding: utf-8 -*-
"""合并 12 套平台自测试卷：去重、按模块分类、导出 Markdown/JSON，并入本地题库。

输入：data/papers/*.json（自测列表页抓取的 12 套试卷）
输出：
- D:\\医学知识库\\题库导出\\自测题库\\<模块>.md / <模块>.json
- data/platform_bank.json 追加新题（与已有 1,044 题去重）
"""
from __future__ import annotations

import glob
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAPERS = ROOT / "data" / "papers"
EXPORT = Path(r"D:\医学知识库\题库导出\自测题库")
BANK_FILE = ROOT / "data" / "platform_bank.json"
EXPORT.mkdir(parents=True, exist_ok=True)

MODULES = {
    "生理": "生理学",
    "执业医师2025": "2025年临床执业医师真题",
    "西综306": "西综306",
    "病理": "病理学",
    "病生": "病理生理学",
    "诊断": "诊断学",
}


def module_of(paper_title: str) -> str:
    for prefix, name in MODULES.items():
        if paper_title.startswith(prefix):
            return name
    return "其他"


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
    return t


def norm_key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[（(]\s*\d+\s*分\s*[）)]", "", s)
    s = re.sub(r"^\s*[（(]\s*(?:单选题|多选题|判断题|填空题|简答题|名词解释|论述题|A3A4型|共用题干|案例分析题)\s*[）)]", "", s)
    s = re.sub(r"[\s\u00a0。；;,.、:：()（）\[\]【】]", "", s)
    return s


TYPE_MAP = {"单选题": "single", "多选题": "multiple", "判断题": "tf", "填空题": "fill", "简答题": "short"}

TYPE_TAG_RE = re.compile(r"^[（(]\s*(?:单选题|多选题|判断题|填空题|简答题|名词解释|论述题|A3A4型|共用题干|案例分析题)\s*[）)]\s*")


def clean_stem(s: str) -> str:
    x = (s or "").strip()
    while True:
        m = TYPE_TAG_RE.match(x)
        if not m:
            break
        x = x[m.end():]
    return x.strip()


def convert(q: dict, module: str) -> dict | None:
    t = norm_type(q.get("type", ""))
    if not t or len(t) > 6:
        # 乱码题型：按选项/答案形态推断
        t = "单选题" if len(q.get("options") or []) >= 2 else "简答题"
    stem = clean_stem(q.get("stem") or "")
    options = q.get("options") or []
    raw = (q.get("answer") or "").strip()
    if not stem:
        return None

    item = {
        "id": "p_" + str(q.get("qid") or stem[:20]),
        "qtype": TYPE_MAP.get(t, "single" if options else "short"),
        "type_label": t,
        "stem": stem,
        "options": [{"key": o.get("key", ""), "text": (o.get("text") or "").strip()} for o in options],
        "answer": [],
        "answer_text": raw,
        "explanation": (q.get("analysis") or "").strip(),
        "difficulty": (q.get("difficulty") or "").strip(),
        "knowledge": "",
        "folder": ["平台自测题库", module],
        "source": "医学智慧教学平台 · 自测题库",
        "question_id": str(q.get("qid") or ""),
        "number": 0,
    }

    keys = [o.get("key", "").upper() for o in item["options"]]
    if item["qtype"] in ("single", "multiple"):
        letters = [c for c in raw.upper() if c in "ABCDEFGH"]
        idx = sorted({keys.index(c) for c in letters if c in keys})
        if not idx:
            return None
        if item["qtype"] == "single" and len(idx) != 1:
            item["qtype"] = "multiple"
        item["answer"] = idx
        item["answer_text"] = "".join(letters)
        return item
    if item["qtype"] == "tf":
        if raw in ("对", "正确", "是", "true", "T", "√"):
            item["answer"] = [True]
        elif raw in ("错", "错误", "否", "false", "F", "×"):
            item["answer"] = [False]
        else:
            return None
        if not item["options"]:
            item["options"] = [{"key": "对", "text": "正确"}, {"key": "错", "text": "错误"}]
        return item
    # fill / short
    if not raw:
        return None
    item["answer_text"] = raw
    return item


def main() -> None:
    # 1) 读取全部试卷，按模块聚合（同模块内先按 qid 去重）
    by_module: dict[str, dict[str, dict]] = {}
    paper_stats = []
    for f in sorted(glob.glob(str(PAPERS / "*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        title = Path(f).stem
        module = module_of(title)
        bucket = by_module.setdefault(module, {})
        kept = dropped = 0
        for q in d.get("questions", []):
            qid = str(q.get("qid") or "")
            if qid and qid in bucket:
                dropped += 1
                continue
            conv = convert(q, module)
            if conv is None:
                dropped += 1
                continue
            if qid:
                bucket[qid] = conv
            else:
                bucket["stem:" + norm_key(conv["stem"]) + "|" + "|".join(o["text"] for o in conv["options"])] = conv
            kept += 1
        paper_stats.append((title, module, kept, dropped))

    # 2) 跨模块去重（按 题干+选项 文本）
    seen: set[str] = set()
    modules_out: dict[str, list[dict]] = {}
    for module, bucket in by_module.items():
        out = []
        for q in bucket.values():
            key = norm_key(q["stem"]) + "|" + "|".join(norm_key(o["text"]) for o in q["options"])
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
        modules_out[module] = out

    # 3) 与已有题库去重后并入 platform_bank.json（新模块可重入：先移除旧的同模块题再重加）
    bank = json.loads(BANK_FILE.read_text(encoding="utf-8"))
    new_modules = set(MODULES.values())
    existing = [q for q in bank.get("questions", [])
                if not (len(q.get("folder", [])) > 1 and q["folder"][1] in new_modules)]
    ex_seen = {
        norm_key(q.get("stem", "")) + "|" + "|".join(norm_key(o.get("text", "")) for o in (q.get("options") or []))
        for q in existing
    }
    added = []
    for module, qs in modules_out.items():
        for q in qs:
            key = norm_key(q["stem"]) + "|" + "|".join(norm_key(o["text"]) for o in q["options"])
            if key in ex_seen:
                continue
            ex_seen.add(key)
            added.append(q)
    bank["questions"] = existing + added
    bank["count"] = len(bank["questions"])
    stats = bank.setdefault("stats", {})
    for q in added:
        k = q["folder"][1] if len(q.get("folder", [])) > 1 else "?"
        stats[k] = stats.get(k, 0) + 1
    BANK_FILE.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")

    # 4) 导出 Markdown / JSON
    label = {"single": "单选题", "multiple": "多选题", "tf": "判断题", "fill": "填空题", "short": "简答题"}
    summary = []
    for module, qs in sorted(modules_out.items()):
        qs.sort(key=lambda x: x.get("question_id", ""))
        payload = {"source": "医学智慧教学平台 · 创建的自测", "module": module, "count": len(qs), "questions": qs}
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
                lines.append(f"- {o.get('key','')}. {o.get('text','')}{mark}")
            lines.append("")
            if q["qtype"] == "tf":
                lines.append(f"**正确答案：** {'正确' if q['answer'] and q['answer'][0] else '错误'}")
            elif q["qtype"] in ("fill", "short"):
                lines.append(f"**参考答案：** {q['answer_text']}")
            else:
                lines.append(f"**正确答案：** {q['answer_text']}")
            if q.get("explanation"):
                lines.append(f"**解析：** {q['explanation']}")
            if q.get("difficulty"):
                lines.append(f"**难度：** {q['difficulty']}")
            lines.append("")
        (EXPORT / f"{module}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary.append((module, len(qs), dict(tcount)))

    # 5) 汇总报告
    print("== 每套试卷 ==")
    for title, module, kept, dropped in paper_stats:
        print(f"  {title} -> {module}: 保留 {kept}, 剔除/重复 {dropped}")
    print("== 模块汇总（跨模块去重后）==")
    for module, n, tcount in summary:
        print(f"  {module}: {n} 题 {tcount}")
    print(f"== 并入本地题库：新增 {len(added)} 题，总题量 {bank['count']} ==")


if __name__ == "__main__":
    main()
