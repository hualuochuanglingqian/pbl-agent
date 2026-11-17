# -*- coding: utf-8 -*-
"""从教材原文出题、判分。"""
from __future__ import annotations

import json
import re

from kb import _chat, display_source


def _chapter_range(kb, book: str, chapter: str | None):
    if not chapter:
        return None
    for r in kb.chapter_map.get(book, []):
        if r.get("title") == chapter:
            return int(r.get("start", 1)), int(r.get("end", 1))
    return None


def _context(kb, book: str, chapter: str | None, topic: str, max_pages: int = 6):
    pages = []
    if topic:
        hits = kb.search(topic, books=[book], chapters=[chapter] if chapter else None,
                         candidate_k=max_pages + 2)
        pages = [(h["page"], kb.page_text(book, h["page"]) or h["text"]) for h in hits]
    if not pages:
        rng = _chapter_range(kb, book, chapter)
        if rng:
            start, end = rng
            cand = list(range(start, end + 1))
            if len(cand) > max_pages:
                idx = [round(i * (len(cand) - 1) / (max_pages - 1)) for i in range(max_pages)]
                cand = [cand[i] for i in idx]
            pages = [(p, kb.page_text(book, p)) for p in cand]
    if not pages:
        hits = kb.search(topic or "重点内容", books=[book], candidate_k=max_pages)
        pages = [(h["page"], kb.page_text(book, h["page"]) or h["text"]) for h in hits]

    seen, blocks = set(), []
    for p, t in pages:
        if p is None or p in seen or not t:
            continue
        seen.add(int(p))
        blocks.append(f"《{display_source(book)}》第{int(p)}页\n{t[:2200]}")
    return "\n\n".join(blocks), {int(p) for p, _ in pages if p is not None}


def _parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    raw = m.group(0)
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(raw)
    except Exception:
        return []


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    return re.sub(r"[\s。；;,.、:：()（）\[\]【】]", "", s)


def generate_quiz(kb, book: str, chapter: str | None, topic: str,
                  n: int = 5, qtype: str = "single",
                  avoid: str = "") -> tuple[list[dict], str]:
    context, pages = _context(kb, book, chapter, topic)
    if not context:
        return [], "没有取到教材原文，请换一个章节或关键词。"

    labels = {
        "single": "单选题（4 个选项，只有一个正确答案）",
        "fill": "填空题",
        "tf": "判断题（只判断正误，不要求改错）",
        "case": "病例题（先给 2-3 句简短病例，再给一道单选题）",
    }
    label = labels.get(qtype, labels["single"])
    if qtype == "single":
        schema = ('[{"question":"...","options":["...","...","...","..."],"answer":0,'
                  '"explanation":"...","source_page":480}]')
    elif qtype == "tf":
        schema = ('[{"statement":"一句完整陈述句，不是问句","answer":true,'
                  '"explanation":"...","source_page":480}]')
    elif qtype == "case":
        schema = ('[{"case":"2-3 句虚构病例：年龄、主诉、关键检查",'
                  '"question":"基于病例的单选题","options":["...","...","...","..."],'
                  '"answer":0,"explanation":"...","source_page":480}]')
    else:
        schema = ('[{"question":"...（用 ____ 表示空）","answer":"...",'
                  '"explanation":"...","source_page":480}]')
    avoid_line = (f"8. 不要与下面的原题重复：{avoid}" + chr(10)) if avoid else ""
    prompt = f"""你是医学考试命题老师。只依据下面的教材原文出题，不要使用原文之外的知识。

教材：{display_source(book)}
章节：{chapter or "全书"}
题型：{label}
数量：{n} 道

要求：
1. 单选题：options 给 4 个选项，不要带 A/B/C/D 前缀；answer 用 0~3 的整数表示正确选项下标。
2. 填空题：题干用 ____ 表示空；answer 为 1~3 个词或数字。
3. 判断题：用 statement 字段给出一句完整陈述句，以句号结尾，学生只需判断“正确/错误”。
   - 不能是问句，不能出现“哪种/哪个/哪些/如何/为什么/哪项/哪一项/是什么/下列/以下哪”。
   - 正确示例：急性肾小球肾炎的典型临床表现包括血尿、蛋白尿、水肿和高血压。
   - 错误示例：IgA肾病的确诊主要依靠血清学检查。
   - 不要给 options。
4. 病例题：case 写 2~3 句虚构病例（年龄、主诉、关键检查），必须能对应下面的教材知识点；question 是基于该病例的单选题；options 给 4 个选项，answer 用 0~3。
4. 每题 explanation 写 1~2 句解析；source_page 必须是下面片段里出现过的页码。
5. 只输出 JSON 数组，不要 markdown 代码块、不要额外解释。
6. 题目要覆盖不同知识点，不要重复；干扰项要合理，但不能是原文没有的事实。
7. 判断题至少要有 1 道答案为“错误”；错误项应把原文的关键点改错（如数值、首选、分型），不要编造无关事实；正确/错误尽量均衡。
{avoid_line}
格式示例：
{schema}

教材片段：
{context}
"""
    r = _chat(prompt, system="你是严谨的医学考试命题老师，只输出 JSON 数组。", temperature=0.4)
    data = _parse_json(r or "")

    items, seen = [], set()
    for x in data:
        if not isinstance(x, dict):
            continue
        q = str(x.get("statement") or x.get("question", "")).strip() if qtype == "tf"             else str(x.get("question", "")).strip()
        case = str(x.get("case", "")).strip() if qtype == "case" else ""
        if not q or q in seen:
            continue
        if qtype == "tf" and (q.endswith("？") or q.endswith("?") or
                              re.search(r"哪种|哪个|哪些|如何|为什么|哪项|哪一项|是什么|下列|以下哪", q)):
            continue
        if qtype == "fill" and not re.search(r"_{2,}", q):
            continue
        try:
            sp = int(x.get("source_page"))
        except Exception:
            sp = sorted(pages)[0] if pages else 1
        if pages and sp not in pages:
            sp = sorted(pages)[0]
        exp = str(x.get("explanation", "")).strip()
        if qtype in ("single", "case"):
            if qtype == "case" and not case:
                continue
            opts = x.get("options") or []
            if not isinstance(opts, list) or len(opts) < 4:
                continue
            opts = [re.sub(r"^[A-Da-d][.、)]\s*", "", str(o)).strip() for o in opts[:4]]
            if any(not o for o in opts):
                continue
            ans = x.get("answer", 0)
            if isinstance(ans, str):
                s = ans.strip().upper()
                ans = ord(s[0]) - 65 if s[:1] in "ABCD" else int(re.sub(r"\D", "", s) or 0)
            try:
                ans = int(ans)
            except Exception:
                continue
            if not (0 <= ans <= 3):
                continue
            item = {"question": q, "options": opts, "answer": ans,
                    "explanation": exp, "source_page": sp}
            if case:
                item["case"] = case
        elif qtype == "tf":
            ans = x.get("answer")
            if isinstance(ans, str):
                ans = ans.strip().lower() in ("true", "正确", "对", "1", "是", "t")
            else:
                ans = bool(ans)
            item = {"question": q, "answer": ans,
                    "explanation": exp, "source_page": sp}
        else:
            ans = str(x.get("answer", "")).strip()
            if not ans:
                continue
            item = {"question": q, "answer": ans,
                    "explanation": exp, "source_page": sp}
        seen.add(q)
        items.append(item)
        if len(items) >= n:
            break
    if not items:
        return [], "没有生成合格题目（可能模型格式不对），请重试或换一个章节/主题。"
    return items, ""


def generate_similar(kb, wrong_items: list[dict], n: int = 3,
                     qtype: str = "single") -> tuple[list[dict], str, str]:
    """基于最近一道错题的知识点，出同类新题。"""
    if not wrong_items:
        return [], "错题本是空的。", ""
    w = wrong_items[-1]
    it = w.get("item") or {}
    book = w.get("book") or ""
    if not book:
        return [], "这道错题没有记录教材来源。", ""
    q = str(it.get("question", "")).strip()
    case = str(it.get("case", "")).strip()
    topic = (case + " " + q).strip()[:200] or q
    items, err = generate_quiz(kb, book, None, topic, n, qtype, avoid=q)
    for x in items:
        x["_book"] = book
    return items, err, book


def grade_quiz(items: list[dict], answers: list, qtype: str) -> tuple[int, list[dict]]:
    results, score = [], 0
    for i, it in enumerate(items):
        ua = answers[i] if i < len(answers) else None
        if qtype in ("single", "case"):
            ok = ua is not None and int(ua) == int(it["answer"])
            user = "-" if ua is None else "ABCD"[int(ua)]
            correct = "ABCD"[int(it["answer"])]
        elif qtype == "tf":
            ok = ua is not None and bool(int(ua)) == bool(it["answer"])
            user = "-" if ua is None else ("正确" if int(ua) == 1 else "错误")
            correct = "正确" if it["answer"] else "错误"
        else:
            user = (ua or "").strip()
            a, b = _norm(user), _norm(it["answer"])
            ok = bool(a) and (a == b or (len(a) >= 2 and (a in b or b in a)))
            correct = it["answer"]
        score += 1 if ok else 0
        results.append({"correct": ok, "user": user, "answer": correct, "item": it})
    return score, results