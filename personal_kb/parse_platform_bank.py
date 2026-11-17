# -*- coding: utf-8 -*-
"""解析已导出的平台自测题库 Markdown，合并错题集，生成统一 JSON 题库。"""
from __future__ import annotations

import json
import re
from pathlib import Path

BOOKS = Path(r"D:\智能体开发\PBL智能体\knowledge_base\books")
OUT = Path(r"D:\医学知识库\personal_kb\data\platform_bank.json")
WRONG = Path(r"D:\医学知识库\personal_kb\data\platform_wrongbook.json")

FILES = [
    ("医学智慧教学平台自测题库-医学遗传学.md", ["平台自测题库", "医学遗传学"]),
    ("医学智慧教学平台自测题库-循环药病理.md", ["平台自测题库", "循环药病理"]),
    ("医学智慧教学平台自测题库-消化病理药理.md", ["平台自测题库", "消化病理药理"]),
    ("医学智慧教学平台自测题库-自测202607072125.md", ["平台自测题库", "自测202607072125"]),
]

HEAD = re.compile(r"^## 第(\d+)题[（(](.+?)[）)]·\s*详解\s*$")
FIELD = re.compile(r"^\*\*(题干|选项|正确答案|答案解析|难度|题目ID|知识点)\*\*[:：]\s*(.*)$")


def clean(s: str) -> str:
    s = (s or "").replace("\u00a0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def parse_file(path: Path, folder: list[str]) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    blocks: list[tuple[int, str, list[str]]] = []
    cur = None
    for line in lines:
        m = HEAD.match(line.strip())
        if m:
            if cur:
                blocks.append(cur)
            cur = (int(m.group(1)), m.group(2), [])
        elif cur is not None:
            cur[2].append(line)
    if cur:
        blocks.append(cur)

    out = []
    for num, type_label, body in blocks:
        fields = {"题干": [], "选项": [], "正确答案": [], "答案解析": [], "难度": [], "题目ID": [], "知识点": []}
        cur_field = None
        for line in body:
            m = FIELD.match(line.strip())
            if m:
                cur_field = m.group(1)
                fields[cur_field].append(m.group(2))
            elif cur_field:
                fields[cur_field].append(line)
        stem = clean("\n".join(fields["题干"]))
        if not stem:
            continue
        options = []
        for line in fields["选项"]:
            line = line.strip()
            m = re.match(r"^[-*]\s*([A-Za-z0-9_]+)\s*[.、．]\s*(.*)$", line)
            if m:
                options.append({"key": m.group(1), "text": clean(m.group(2))})
        answer_raw = clean(" ".join(fields["正确答案"]))
        explanation = clean(" ".join(fields["答案解析"]))
        diff_raw = clean(" ".join(fields["难度"]))
        qid = clean(" ".join(fields["题目ID"]))
        knowledge = clean(" ".join(fields["知识点"]))

        diff = ""
        if "易" in diff_raw:
            diff = "易"
        elif "中" in diff_raw:
            diff = "中"
        elif "难" in diff_raw:
            diff = "难"
        elif diff_raw:
            diff = diff_raw[:20]

        t = type_label.strip()
        qtype = {
            "单选题": "single", "多选题": "multiple", "判断题": "tf",
            "填空题": "fill", "简答题": "short", "名词解释": "short",
            "论述题": "short", "A3A4型": "single", "案例分析题": "case",
        }.get(t, "single" if options else "short")

        item = {
            "id": f"p_{qid or num}",
            "qtype": qtype,
            "type_label": t,
            "stem": stem,
            "options": options,
            "answer": [],
            "answer_text": answer_raw,
            "explanation": explanation,
            "difficulty": diff,
            "knowledge": knowledge,
            "folder": folder,
            "source": "医学智慧教学平台 · 自测题库",
            "question_id": qid,
            "number": num,
        }

        if qtype in ("single", "multiple", "case") and options:
            letters = re.findall(r"[A-Za-z]", answer_raw.upper())
            keys = [o["key"].upper() for o in options]
            ans = [keys.index(x) for x in letters if x in keys]
            if not ans:
                # 有些答案是 "B" 之外的文字，尝试按选项文本匹配
                for i, o in enumerate(options):
                    if o["text"] and o["text"] in answer_raw:
                        ans.append(i)
            if not ans:
                continue
            item["answer"] = sorted(set(ans))
        elif qtype == "tf":
            ok = answer_raw.strip() in ("对", "正确", "是", "true", "True", "√", "T")
            bad = answer_raw.strip() in ("错", "错误", "否", "false", "False", "×", "F")
            if not (ok or bad):
                continue
            item["qtype"] = "tf"
            item["answer"] = [ok]
        elif qtype in ("fill", "short"):
            if not answer_raw:
                continue
            item["answer_text"] = answer_raw
        else:
            continue

        out.append(item)
    return out


def norm(s: str) -> str:
    s = (s or "").lower()
    return re.sub(r"[\s\u00a0。；;,.、:：()（）\[\]【】]", "", s)


def main() -> None:
    items: list[dict] = []
    for name, folder in FILES:
        p = BOOKS / name
        if not p.exists():
            print(f"skip missing: {name}")
            continue
        parsed = parse_file(p, folder)
        print(f"{name}: {len(parsed)}")
        items.extend(parsed)

    # 合并错题集（正确答案在 JSON 里）
    if WRONG.exists():
        wrong = json.loads(WRONG.read_text(encoding="utf-8")).get("questions", [])
        for q in wrong:
            opts = q.get("options") or []
            letters = q.get("answer") or []
            keys = [o.get("key", "").upper() for o in opts]
            ans = [keys.index(x) for x in letters if x in keys]
            if q.get("type") in ("single", "multiple") and opts and not ans:
                continue
            item = {
                "id": f"w_{q.get('record_id') or len(items)}",
                "qtype": "multiple" if q.get("type") == "multiple" else "single",
                "type_label": q.get("type_label", "单选题"),
                "stem": q.get("stem", ""),
                "options": opts,
                "answer": ans,
                "answer_text": "".join(letters),
                "explanation": q.get("explanation", ""),
                "difficulty": "",
                "knowledge": q.get("knowledge", ""),
                "folder": ["平台错题集", q.get("knowledge") or "未分类"],
                "source": "医学智慧教学平台 · 错题集",
                "question_id": q.get("record_id", ""),
                "number": 0,
            }
            items.append(item)

    # 去重：同一题干 + 选项
    seen, uniq = set(), []
    for it in items:
        key = (norm(it.get("stem", "")), tuple(norm(o.get("text", "")) for o in it.get("options", [])))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    # 按来源/题型统计
    stats: dict[str, int] = {}
    for it in uniq:
        k = it.get("folder", ["?"])[1] if len(it.get("folder", [])) > 1 else "?"
        stats[k] = stats.get(k, 0) + 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "医学智慧教学平台 · 自测题库 + 错题集",
        "count": len(uniq),
        "stats": stats,
        "questions": uniq,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"total={len(uniq)}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"output={OUT}")


if __name__ == "__main__":
    main()
