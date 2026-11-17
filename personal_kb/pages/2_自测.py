# -*- coding: utf-8 -*-
"""学习通式自测：新建自测、模拟测验/刷题自测、答题卡、判分、错题重练。"""
from __future__ import annotations

import html
import hashlib
import json
import random
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kb import _chat, display_source, get_kb  # noqa: E402
from quiz import generate_quiz  # noqa: E402

st.set_page_config(page_title="自测", page_icon="🧪", layout="wide")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BANK_FILE = DATA / "platform_bank.json"
FOLDERS_FILE = DATA / "platform_folders.json"
TESTS_FILE = DATA / "selftests.json"
WRONG_FILE = DATA / "self_wrong.json"
LEGACY_WRONG = DATA / "wrong_book.json"
MARKS_FILE = DATA / "question_marks.json"
AI_CACHE_FILE = DATA / "ai_solutions.json"
BACKUP_DIR = DATA / "backups"

DATA.mkdir(parents=True, exist_ok=True)

TYPE_LABEL = {
    "single": "单选题", "multiple": "多选题", "tf": "判断题",
    "fill": "填空题", "short": "简答题", "case": "病例题",
}
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------- data

@st.cache_data(show_spinner=False)
def load_bank() -> list[dict]:
    if not BANK_FILE.exists():
        return []
    payload = json.loads(BANK_FILE.read_text(encoding="utf-8"))
    return payload.get("questions", [])


@st.cache_data(show_spinner=False)
def load_folders() -> list[dict]:
    if not FOLDERS_FILE.exists():
        return []
    payload = json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
    return payload.get("folders", [])


def load_tests() -> list[dict]:
    try:
        data = json.loads(TESTS_FILE.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []


def _backup(path: Path, keep: int = 40, min_interval: int = 60) -> None:
    """写盘前自动备份，最多保留 keep 份，60 秒内不重复备份。"""
    try:
        if not path.exists():
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(BACKUP_DIR.glob(f"{path.stem}_*{path.suffix}"))
        if files:
            age = datetime.now().timestamp() - files[-1].stat().st_mtime
            if age < min_interval:
                return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, BACKUP_DIR / f"{path.stem}_{stamp}{path.suffix}")
        files = sorted(BACKUP_DIR.glob(f"{path.stem}_*{path.suffix}"))
        for f in files[:-keep]:
            f.unlink()
    except Exception:
        pass


def save_tests(tests: list[dict]) -> None:
    _backup(TESTS_FILE)
    TESTS_FILE.write_text(json.dumps(tests[-100:], ensure_ascii=False, indent=1), encoding="utf-8")


def load_wrong() -> list[dict]:
    if WRONG_FILE.exists():
        try:
            data = json.loads(WRONG_FILE.read_text(encoding="utf-8"))
            return [x for x in data if isinstance(x, dict)]
        except Exception:
            return []
    # 兼容旧版做题页的错题本
    if LEGACY_WRONG.exists():
        try:
            old = json.loads(LEGACY_WRONG.read_text(encoding="utf-8"))
        except Exception:
            return []
        out = []
        for w in old:
            it = w.get("item") if isinstance(w, dict) else None
            if not isinstance(it, dict):
                continue
            qtype = w.get("qtype", "single")
            if qtype in ("single", "case"):
                ans = it.get("answer", 0)
                answer = [int(ans)] if isinstance(ans, int) else []
            elif qtype == "tf":
                answer = [bool(it.get("answer"))]
            else:
                answer = []
            out.append({
                "id": f"legacy_{len(out)}",
                "qtype": qtype if qtype in TYPE_LABEL else "single",
                "stem": it.get("question", ""),
                "options": [{"key": LETTERS[i], "text": o} for i, o in enumerate(it.get("options", []))],
                "answer": answer,
                "answer_text": str(it.get("answer", "")),
                "explanation": it.get("explanation", ""),
                "folder": ["教材错题", display_source(w.get("book", ""))],
                "source": f"教材错题 · {display_source(w.get('book', ''))} 第{it.get('source_page', '')}页",
                "difficulty": "",
                "knowledge": "",
                "wrong_count": 1,
            })
        save_wrong(out)
        return out
    return []


def save_wrong(items: list[dict]) -> None:
    _backup(WRONG_FILE)
    WRONG_FILE.write_text(json.dumps(items[-500:], ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- marks / AI

def qkey(q: dict) -> str:
    raw = (q.get("stem") or "") + "|" + "|".join(o.get("text", "") for o in (q.get("options") or []))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def load_marks() -> list[dict]:
    try:
        data = json.loads(MARKS_FILE.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []


def save_marks(items: list[dict]) -> None:
    _backup(MARKS_FILE)
    MARKS_FILE.write_text(json.dumps(items[-1000:], ensure_ascii=False, indent=1), encoding="utf-8")


def is_marked(q: dict) -> bool:
    k = qkey(q)
    return any(m.get("key") == k for m in st.session_state.get("marks", []))


def toggle_mark(q: dict) -> None:
    k = qkey(q)
    marks = st.session_state.setdefault("marks", [])
    if any(m.get("key") == k for m in marks):
        st.session_state.marks = [m for m in marks if m.get("key") != k]
    else:
        snapshot = {kk: v for kk, v in q.items()}
        snapshot["key"] = k
        snapshot["marked_at"] = iso()
        st.session_state.marks.append(snapshot)
    save_marks(st.session_state.marks)


def load_ai_cache() -> dict:
    try:
        return json.loads(AI_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ai_cache(cache: dict) -> None:
    AI_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def ai_solution(q: dict) -> str:
    """按题生成 AI 解答（答案+解析+知识点），结果落盘缓存。"""
    k = qkey(q)
    cache = st.session_state.setdefault("ai_cache", load_ai_cache())
    if cache.get(k):
        return cache[k]
    opts = "\n".join(f"{o.get('key', LETTERS[j])}. {o.get('text', '')}" for j, o in enumerate(q.get("options") or []))
    platform = ""
    if q.get("qtype") in ("single", "multiple", "case") and q.get("answer"):
        platform = f"\n平台给出的参考答案：{'、'.join(LETTERS[int(i)] for i in q['answer'])}（仅供参考，可能是错的）"
    elif q.get("answer_text"):
        platform = f"\n平台给出的参考答案：{q['answer_text']}（仅供参考，可能是错的）"
    prompt = f"""你是医学考试辅导老师。请解答下面这道题，只依据公认的医学教材知识，不要编造。

题型：{q.get('type_label') or TYPE_LABEL.get(q.get('qtype'), '题目')}
题干：{q.get('stem', '')}
选项：
{opts or '（无选项）'}{platform}

请严格按以下格式输出（中文）：
1. 正确答案：<答案；选择题写选项字母并附选项内容>
2. 解析：<逐项说明为什么对/错；2-5 句>
3. 知识点：<这题考的核心知识点>
如果题目信息不足以确定答案，直接说“无法确定”，并说明缺少什么信息，不要猜测。"""
    r = _chat(prompt, system="你是严谨的医学考试辅导老师，只依据教材共识作答。", temperature=0.2)
    text = (r or "").strip() or "AI 暂时没有返回内容，请稍后重试。"
    cache[k] = text
    save_ai_cache(cache)
    return text


def wrong_analysis(test: dict) -> str:
    """交卷后按错题生成知识点缺漏分析，结果缓存在 test 里。"""
    results = {r.get("index"): r for r in test.get("results", [])}
    wrong = []
    for i, q in enumerate(test.get("questions", [])):
        r = results.get(i, {})
        if r.get("correct") is False:
            wrong.append((i + 1, q, r))
    if not wrong:
        return ""
    lines = []
    for n, q, r in wrong[:40]:
        lines.append(
            f"第{n}题（{folder_name(q)}）｜{q.get('stem','')[:120]}\n"
            f"  正确答案：{correct_text(q)[:120]}｜学生答案：{r.get('user', '-')}"
        )
    prompt = f"""下面是一位医学生在自测中做错的题（共 {len(wrong)} 题，最多列出 40 题）。请分析其知识点的缺漏。

{chr(10).join(lines)}

请用 Markdown 输出：
## 总体薄弱点
（2-4 条，按失分集中度排序）

## 分知识点分析
（按学科/章节聚类，说明每类的典型错误和原因，不要逐题复述）

## 复习建议
（3-5 条，具体到章节或知识点，可执行的顺序）

要求：只依据题目和正确答案推断，不要编造学生没做过的内容；语言简洁。"""
    r = _chat(prompt, system="你是医学教育的学习分析师，擅长根据错题诊断知识薄弱点。", temperature=0.3)
    return (r or "").strip()


# ---------------------------------------------------------------- helpers

def now() -> datetime:
    return datetime.now()


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None


def new_id() -> str:
    return f"st_{now().strftime('%Y%m%d%H%M%S')}_{random.randint(100, 999)}"


def folder_name(q: dict) -> str:
    parts = [p for p in (q.get("folder") or []) if p]
    return " / ".join(parts) if parts else "未分类"


def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    return re.sub(r"[\s。；;,.、:：()（）\[\]【】]", "", s)


def option_texts(q: dict) -> list[str]:
    return [o.get("text", "") for o in (q.get("options") or [])]


def correct_text(q: dict) -> str:
    if q.get("qtype") in ("single", "multiple", "case"):
        parts = []
        for i in q.get("answer") or []:
            opts = q.get("options") or []
            if 0 <= int(i) < len(opts):
                parts.append(f"{opts[int(i)].get('key', LETTERS[i])}. {opts[int(i)].get('text', '')}")
        return "；".join(parts) or q.get("answer_text", "")
    if q.get("qtype") == "tf":
        return "正确" if (q.get("answer") or [False])[0] else "错误"
    return q.get("answer_text", "")


def answered_count(test: dict) -> int:
    n = 0
    for i, q in enumerate(test.get("questions", [])):
        if q.get("qtype") in ("single", "case"):
            if st.session_state.get(f"ans_{test['id']}_{i}") is not None:
                n += 1
        elif q.get("qtype") == "multiple":
            if any(st.session_state.get(f"ans_{test['id']}_{i}_{j}") for j in range(len(q.get("options") or []))):
                n += 1
        elif q.get("qtype") == "tf":
            if st.session_state.get(f"ans_{test['id']}_{i}") is not None:
                n += 1
        else:
            if (st.session_state.get(f"ans_{test['id']}_{i}") or "").strip():
                n += 1
    return n


def collect_answers(test: dict) -> list:
    out = []
    for i, q in enumerate(test.get("questions", [])):
        qt = q.get("qtype")
        if qt in ("single", "case"):
            val = st.session_state.get(f"ans_{test['id']}_{i}")
            out.append(None if val is None else LETTERS.index(val[0]))
        elif qt == "multiple":
            picks = [
                j for j in range(len(q.get("options") or []))
                if st.session_state.get(f"ans_{test['id']}_{i}_{j}")
            ]
            out.append(picks)
        elif qt == "tf":
            val = st.session_state.get(f"ans_{test['id']}_{i}")
            out.append(None if val is None else (val == "正确"))
        else:
            out.append((st.session_state.get(f"ans_{test['id']}_{i}") or "").strip())
    return out


def clear_answer_keys(test_id: str) -> None:
    for k in list(st.session_state.keys()):
        if k.startswith(f"ans_{test_id}_") or k.startswith(f"rev_{test_id}_") or k.startswith(f"draft_restored_{test_id}"):
            del st.session_state[k]


def restore_draft(test: dict, qs: list[dict]) -> None:
    """把已保存的答题草稿恢复到控件状态（每次进入答题页一次）。"""
    draft = test.get("draft_answers")
    if not draft or st.session_state.get(f"draft_restored_{test['id']}"):
        return
    for i, a in enumerate(draft):
        if i >= len(qs):
            break
        q = qs[i]
        qt = q.get("qtype")
        key = f"ans_{test['id']}_{i}"
        if a is None or a == "" or a == []:
            continue
        if qt in ("single", "case") and isinstance(a, int):
            opts = option_texts(q)
            if 0 <= a < len(opts):
                st.session_state[key] = f"{LETTERS[a]}. {opts[a]}"
        elif qt == "multiple" and isinstance(a, list):
            for j in range(len(q.get("options") or [])):
                st.session_state[f"{key}_{j}"] = j in a
        elif qt == "tf" and isinstance(a, bool):
            st.session_state[key] = "正确" if a else "错误"
        elif isinstance(a, str):
            st.session_state[key] = a
    st.session_state[f"draft_restored_{test['id']}"] = True


def save_draft(test: dict, answers: list) -> None:
    """作答后自动落盘草稿，刷新或重启后可恢复。"""
    ans = [None if a == "" else a for a in answers]
    if ans == test.get("draft_answers"):
        return
    test["draft_answers"] = ans
    test["draft_saved_at"] = iso()
    for t in st.session_state.tests:
        if t.get("id") == test.get("id"):
            t.update(test)
            break
    save_tests(st.session_state.tests)


def remaining_seconds(test: dict) -> int | None:
    limit = int(test.get("limit_min") or 0)
    if limit <= 0:
        return None
    start = parse_dt(test.get("started_at")) or parse_dt(test.get("created_at"))
    if not start:
        return None
    return int(limit * 60 - (now() - start).total_seconds())


def fmt_seconds(sec: int) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def add_wrong(q: dict) -> None:
    items = st.session_state.wrong
    key = norm_text(q.get("stem", ""))
    for w in items:
        if norm_text(w.get("stem", "")) == key:
            w["wrong_count"] = int(w.get("wrong_count", 1)) + 1
            w["last_wrong_at"] = iso()
            save_wrong(items)
            return
    item = {k: v for k, v in q.items() if not k.startswith("_")}
    item["wrong_count"] = 1
    item["last_wrong_at"] = iso()
    items.append(item)
    save_wrong(items)


# ---------------------------------------------------------------- grading

def finish_test(test: dict, answers: list) -> None:
    results, score, total = [], 0, 0
    for i, q in enumerate(test.get("questions", [])):
        ua = answers[i] if i < len(answers) else None
        qt = q.get("qtype")
        if qt in ("single", "case"):
            correct = ua is not None and int(ua) == int((q.get("answer") or [0])[0])
            user = "-" if ua is None else LETTERS[int(ua)]
        elif qt == "multiple":
            correct = set(ua or []) == set(q.get("answer") or [])
            user = "-" if not ua else "".join(LETTERS[j] for j in sorted(ua))
        elif qt == "tf":
            correct = ua is not None and bool(ua) == bool((q.get("answer") or [False])[0])
            user = "-" if ua is None else ("正确" if ua else "错误")
        elif qt == "fill":
            a, b = norm_text(str(ua or "")), norm_text(q.get("answer_text", ""))
            correct = bool(a) and (a == b or (len(a) >= 2 and (a in b or b in a)))
            user = str(ua or "-")
        else:
            correct = None
            user = str(ua or "-")

        if correct is not None:
            total += 1
            if correct:
                score += 1
        results.append({"correct": correct, "user": user, "index": i})
        if correct is False:
            add_wrong(q)

    test["results"] = results
    test["score"] = score
    test["total"] = total
    test["status"] = "done"
    test["submitted_at"] = iso()
    test["answers"] = answers
    test.pop("draft_answers", None)
    test.pop("draft_saved_at", None)
    for t in st.session_state.tests:
        if t.get("id") == test.get("id"):
            t.update(test)
            break
    else:
        st.session_state.tests.append(test)
    save_tests(st.session_state.tests)


# ---------------------------------------------------------------- create

def make_test(title: str, mode: str, limit_min: int, questions: list[dict], selection: dict) -> dict:
    return {
        "id": new_id(),
        "title": title.strip() or f"自测 {now().strftime('%m-%d %H:%M')}",
        "mode": mode,
        "limit_min": max(0, int(limit_min)),
        "created_at": iso(),
        "started_at": iso(),
        "status": "todo",
        "selection": selection,
        "questions": questions,
        "score": 0,
        "total": 0,
    }


def pick_by_groups(pool: list[dict], group_key, wants: dict[str, int]) -> list[dict]:
    picked = []
    for g, n in wants.items():
        if n <= 0:
            continue
        cand = [q for q in pool if group_key(q) == g]
        if len(cand) <= n:
            picked.extend(cand)
        else:
            picked.extend(random.sample(cand, n))
    return picked


# ---------------------------------------------------------------- UI

st.markdown("""
<style>
.stApp { background: #f4f6f9; }
header[data-testid="stHeader"] { background: #f4f6f9; }
.block-container { max-width: 1120px; padding-top: 4.4rem; padding-bottom: 3rem; }
div[class*="st-key-cx_"] {
    background: #fff; border: 1px solid #eef0f4; border-radius: 12px;
    box-shadow: 0 2px 10px rgba(31, 41, 55, .04); padding: 18px 20px;
}
div[class*="st-key-cx_sheet"] { position: sticky; top: 4.2rem; }
.cx-h1 { font-size: 24px; font-weight: 700; color: #1f2329; margin: 0; }
.cx-sub { color: #8a919f; font-size: 13px; margin-top: 4px; }
.cx-badge { display:inline-block; padding:2px 9px; border-radius:4px; font-size:12px;
    background:#eef3ff; color:#3b6ef5; margin-right:6px; }
.cx-badge.gray { background:#f1f3f5; color:#6b7280; }
.cx-badge.green { background:#e9f8f0; color:#18a058; }
.cx-badge.orange { background:#fff4e6; color:#d97706; }
.cx-title { font-size:17px; font-weight:600; color:#1f2329; }
.cx-meta { color:#8a919f; font-size:12px; }
.cx-num { display:inline-flex; align-items:center; justify-content:center;
    width:30px; height:30px; border-radius:6px; margin:3px; font-size:13px;
    background:#f4f6f9; color:#6b7280; border:1px solid #eef0f4; }
.cx-num.done { background:#eef3ff; color:#3b6ef5; border-color:#c9d8ff; font-weight:600; }
.cx-num.marked { background:#fff3bf; color:#8a6d00; border-color:#ffd43b; }
.cx-num.done.marked { background:#fff3bf; color:#3b6ef5; border-color:#3b6ef5; }
a.cx-num { text-decoration:none; cursor:pointer; }
.cx-q { scroll-margin-top: 90px; }
.cx-q { border-top: 1px solid #f0f2f5; padding-top: 16px; margin-top: 16px; }
.cx-q:first-child { border-top: 0; padding-top: 0; margin-top: 0; }
.cx-stem { font-size:15px; color:#1f2329; line-height:1.7; margin:6px 0 10px; }
.cx-opt-correct { color:#18a058; font-weight:600; }
.cx-opt-wrong { color:#e5484d; }
[data-testid="stCaptionContainer"] { color:#6b7280 !important; }
[data-testid="stExpander"] details { background:#fff; border:1px solid #eef0f4; border-radius:10px; }
[data-testid="stExpander"] summary { color:#1f2329 !important; }
.stButton > button { border-radius: 8px; }
.stButton > button[kind="secondary"] { background:#fff; border:1px solid #d9dee7; color:#3b6ef5; }
.stButton > button[kind="primary"] { background:#3b6ef5; border-color:#3b6ef5; }
div[data-testid="stMetricValue"] { font-size: 30px; }
</style>
""", unsafe_allow_html=True)

bank = load_bank()
folders = load_folders()

if "tests" not in st.session_state:
    st.session_state.tests = load_tests()
if "wrong" not in st.session_state:
    st.session_state.wrong = load_wrong()
if "marks" not in st.session_state:
    st.session_state.marks = load_marks()
if "ai_cache" not in st.session_state:
    st.session_state.ai_cache = load_ai_cache()
if "view" not in st.session_state:
    st.session_state.view = "list"
if "test_id" not in st.session_state:
    st.session_state.test_id = None
if "ai_questions" not in st.session_state:
    st.session_state.ai_questions = []


def goto(view: str, test_id: str | None = None) -> None:
    st.session_state.view = view
    if test_id is not None:
        st.session_state.test_id = test_id
    st.rerun()


def find_test(test_id: str | None) -> dict | None:
    for t in st.session_state.tests:
        if t.get("id") == test_id:
            return t
    return None


def header(title: str, sub: str = "") -> None:
    st.markdown(f'<p class="cx-h1">{html.escape(title)}</p>', unsafe_allow_html=True)
    if sub:
        st.markdown(f'<p class="cx-sub">{html.escape(sub)}</p>', unsafe_allow_html=True)


# ================================================================ list

def render_list() -> None:
    tests = sorted(st.session_state.tests, key=lambda x: x.get("created_at", ""), reverse=True)
    done = [t for t in tests if t.get("status") == "done"]

    top = st.columns([6, 2, 2])
    with top[0]:
        header("全部自测", f"本地题库 {len(bank):,} 题　平台目录 {sum(int(f.get('count') or 0) for f in folders):,} 题　已完成 {len(done)} 套")
    with top[1]:
        if st.button("＋ 新建自测", type="primary", use_container_width=True):
            goto("create")
    with top[2]:
        if st.button("AI 出题", use_container_width=True):
            goto("ai")

    with st.container(border=True, key="cx_my_tests"):
        st.markdown('<span class="cx-badge">我的自测</span>', unsafe_allow_html=True)
        if not tests:
            st.markdown(
                '<div style="text-align:center;color:#a0a6b1;padding:64px 0 56px;">暂无自测</div>',
                unsafe_allow_html=True,
            )
            c1, c2, c3 = st.columns([2, 1, 2])
            with c2:
                if st.button("新建一套", type="primary", use_container_width=True):
                    goto("create")
        else:
            for t in tests:
                total_q = len(t.get("questions", []))
                status = t.get("status")
                badge = "模拟测验" if t.get("mode") == "exam" else "刷题自测"
                badge_cls = "" if t.get("mode") == "exam" else "green"
                if status == "done":
                    status_txt = f"已完成　得分 {t.get('score', 0)}/{t.get('total', 0)}"
                    status_cls = "green"
                else:
                    status_txt = "待做"
                    status_cls = "orange"
                rem = remaining_seconds(t)
                rem_txt = ""
                if status != "done" and rem is not None:
                    rem_txt = f"　剩余 {fmt_seconds(rem)}" if rem > 0 else "　已超时"

                cols = st.columns([6, 2, 2])
                with cols[0]:
                    draft = t.get("draft_answers") or []
                    done_n = sum(1 for a in draft if a not in (None, "", []))
                    progress = f"　已答 {done_n}/{total_q}" if done_n else ""
                    st.markdown(
                        f'<span class="cx-badge {badge_cls}">{badge}</span>'
                        f'<span class="cx-badge {status_cls}">{status_txt}{rem_txt}</span><br>'
                        f'<span class="cx-title">{html.escape(t.get("title", "未命名"))}</span>'
                        f'<div class="cx-meta">{total_q} 题{progress}　{html.escape(t.get("created_at", "")[:16])}</div>',
                        unsafe_allow_html=True,
                    )
                with cols[1]:
                    if status == "done":
                        if st.button("查看结果", key=f"res_{t['id']}", use_container_width=True):
                            goto("result", t["id"])
                    else:
                        if st.button("继续作答", key=f"go_{t['id']}", type="primary", use_container_width=True):
                            goto("exam", t["id"])
                with cols[2]:
                    if st.button("删除", key=f"del_{t['id']}", use_container_width=True):
                        st.session_state.tests = [x for x in st.session_state.tests if x.get("id") != t["id"]]
                        save_tests(st.session_state.tests)
                        st.rerun()
                st.divider()

    with st.container(border=True, key="cx_wrong"):
        c1, c2 = st.columns([5, 2])
        with c1:
            st.markdown('<span class="cx-badge gray">错题本</span>', unsafe_allow_html=True)
            st.caption(f"本地错题 {len(st.session_state.wrong)} 题；答错会自动收进这里。")
        with c2:
            if st.session_state.wrong and st.button("错题重练", use_container_width=True):
                qs = st.session_state.wrong[-50:]
                test = make_test(
                    f"错题重练 {now().strftime('%m-%d %H:%M')}", "practice", 0, qs,
                    {"source": "wrong", "method": "random"},
                )
                st.session_state.tests.append(test)
                save_tests(st.session_state.tests)
                goto("exam", test["id"])

        if st.session_state.wrong:
            groups: dict[str, int] = {}
            for w in st.session_state.wrong:
                groups[folder_name(w)] = groups.get(folder_name(w), 0) + 1
            st.dataframe(
                [{"来源": k, "错题数": v} for k, v in sorted(groups.items(), key=lambda x: -x[1])],
                hide_index=True, use_container_width=True,
            )
            with st.expander("查看错题"):
                for w in st.session_state.wrong[-30:]:
                    st.markdown(f"- {w.get('stem', '')[:120]}　`{correct_text(w)[:60]}`")
            if st.button("清空错题本", use_container_width=True):
                st.session_state.wrong = []
                save_wrong([])
                st.rerun()

    with st.container(border=True, key="cx_marks"):
        c1, c2 = st.columns([5, 2])
        with c1:
            st.markdown('<span class="cx-badge">题目标记</span>', unsafe_allow_html=True)
            st.caption(f"已标记 {len(st.session_state.marks)} 题；做题时点「☆ 标记」，可在这里回溯和重练。")
        with c2:
            if st.session_state.marks and st.button("标记重练", use_container_width=True):
                qs = list(st.session_state.marks[-500:])
                test = make_test(
                    f"标记重练 {now().strftime('%m-%d %H:%M')}", "practice", 0, qs,
                    {"source": "marks", "method": "random"},
                )
                st.session_state.tests.append(test)
                save_tests(st.session_state.tests)
                goto("exam", test["id"])

        marks = st.session_state.marks
        if marks:
            with st.expander(f"查看标记题目（{len(marks)}）"):
                for m in marks[-50:]:
                    mc1, mc2 = st.columns([8, 2])
                    with mc1:
                        st.markdown(f"- {m.get('stem', '')[:110]}")
                        st.caption(f"答案：{correct_text(m)[:80]}　来源：{folder_name(m)}")
                    with mc2:
                        if st.button("取消", key=f"unmark_{m.get('key', '')}", use_container_width=True):
                            st.session_state.marks = [x for x in st.session_state.marks if x.get("key") != m.get("key")]
                            save_marks(st.session_state.marks)
                            st.rerun()
            if st.button("清空标记", use_container_width=True):
                st.session_state.marks = []
                save_marks([])
                st.rerun()

    with st.expander("平台题库完整目录（578 个文件夹 / 41,729 题）"):
        st.caption("目录来自平台“新建自测 → 从文件夹选题”；题目正文仅导出了本地题库中的 1,044 题。")
        if folders:
            st.dataframe(
                [{"完整路径": " / ".join(f.get("path") or [f.get("name", "")]),
                  "题目数": f.get("count")} for f in folders],
                hide_index=True, use_container_width=True, height=340,
            )


# ================================================================ create

def render_create() -> None:
    header("新建自测", "按平台题库 / 错题本抽题，支持模拟测验和刷题自测。")
    if st.button("← 返回我的自测"):
        goto("list")

    pool_source = st.radio(
        "题目来源", ["平台题库", "错题本"], horizontal=True,
        captions=[f"{len(bank):,} 题，含答案解析", f"{len(st.session_state.wrong)} 题"],
    )
    source_pool = bank if pool_source == "平台题库" else st.session_state.wrong
    # 随机/文件夹/主题默认排除主观题，保证能自动判分；按题型选题里仍可手动选简答题。
    pool = [q for q in source_pool if q.get("qtype") != "short"]
    if not source_pool:
        st.warning("这个来源暂时没有题目。")
        return

    title = st.text_input(
        "自测标题（可随意修改，创建后显示在“我的自测”里）",
        value=f"自测 {now().strftime('%m-%d %H:%M')}",
        key="new_test_title",
    )
    c1, c2 = st.columns(2)
    with c1:
        mode_label = st.radio(
            "自测模式", ["模拟测验", "刷题自测"], horizontal=True,
            captions=["交卷后才能看答案", "每题可查看答案"],
        )
    with c2:
        limit = st.number_input(
            "自测时间（分钟，0 = 不限时）", min_value=0, max_value=999,
            value=10 if mode_label == "模拟测验" else 0, step=5,
        )
    mode = "exam" if mode_label == "模拟测验" else "practice"

    method = st.radio(
        "选题方式", ["随机抽题", "从文件夹选题", "按题型选题", "按主题选题"],
        horizontal=True,
    )

    picked: list[dict] = []
    selection: dict = {"source": "bank" if pool_source == "平台题库" else "wrong", "method": method}

    if method == "随机抽题":
        if not pool:
            st.warning("这个来源只有主观题，请改用「按题型选题」或换一个来源。")
            return
        max_n = min(len(pool), 500)
        n = st.number_input("抽题数量", min_value=1, max_value=max_n, value=min(10, max_n))
        picked = random.sample(pool, int(n))
        selection["n"] = int(n)

    elif method == "从文件夹选题":
        st.caption("按平台题库目录分类（学科 → 章节），与“新建自测”里的文件夹树一致。")
        tree: dict[str, dict[str, int]] = {}
        for q in pool:
            parts = [p for p in (q.get("folder") or []) if p]
            if not parts:
                continue
            full = " / ".join(parts)
            tree.setdefault(parts[0], {})
            tree[parts[0]][full] = tree[parts[0]].get(full, 0) + 1
        subjects = sorted(tree)
        if not subjects:
            st.warning("这个来源没有章节分类。")
            return
        subject = st.selectbox("学科", subjects)
        chapters = sorted(tree[subject])
        chapter = st.selectbox(
            "章节", chapters,
            format_func=lambda p: f"{p}（{tree[subject][p]} 题）",
        )
        avail = tree[subject][chapter]
        n = st.number_input("抽题数量（单套最多 500 题）", min_value=1, max_value=min(avail, 500), value=min(10, avail))
        selection["folder"] = chapter
        picked = random.sample(
            [q for q in pool if " / ".join([p for p in (q.get("folder") or []) if p]) == chapter],
            int(n),
        )

    elif method == "按题型选题":
        labels = {"single": "单选题", "multiple": "多选题", "tf": "判断题", "fill": "填空题", "short": "简答题"}
        counts: dict[str, int] = {}
        for q in source_pool:
            counts[q.get("qtype", "single")] = counts.get(q.get("qtype", "single"), 0) + 1
        rows = [{"题型": labels.get(k, k), "本地题量": v, "抽题数": 0} for k, v in counts.items() if v > 0]
        edited = st.data_editor(
            pd.DataFrame(rows), hide_index=True, use_container_width=True, num_rows="fixed",
            column_config={
                "题型": st.column_config.TextColumn(disabled=True),
                "本地题量": st.column_config.NumberColumn(disabled=True),
                "抽题数": st.column_config.NumberColumn(min_value=0, max_value=500, step=1),
            },
        )
        rev = {v: k for k, v in labels.items()}
        wants = {rev.get(str(r["题型"]), "single"): int(r["抽题数"] or 0) for _, r in edited.iterrows()}
        selection["groups"] = {k: v for k, v in wants.items() if v > 0}
        picked = pick_by_groups(source_pool, lambda q: q.get("qtype", "single"), wants)

    else:
        def topic_of(q: dict) -> str:
            if q.get("knowledge"):
                return q["knowledge"]
            parts = [p for p in (q.get("folder") or []) if p]
            return parts[-1] if parts else "未分类"

        groups: dict[str, int] = {}
        for q in pool:
            g = topic_of(q)
            groups[g] = groups.get(g, 0) + 1
        df = pd.DataFrame([{"主题": k, "本地题量": v, "抽题数": 0} for k, v in sorted(groups.items())])
        edited = st.data_editor(
            df, hide_index=True, use_container_width=True, num_rows="fixed",
            column_config={
                "主题": st.column_config.TextColumn(disabled=True),
                "本地题量": st.column_config.NumberColumn(disabled=True),
                "抽题数": st.column_config.NumberColumn(min_value=0, max_value=500, step=1),
            },
        )
        wants = {str(r["主题"]): int(r["抽题数"] or 0) for _, r in edited.iterrows()}
        selection["groups"] = {k: v for k, v in wants.items() if v > 0}
        picked = pick_by_groups(pool, topic_of, wants)

    if picked:
        random.shuffle(picked)
        st.info(f"本次共 {len(picked)} 题。" + ("（已达到单套上限 500 题）" if len(picked) >= 500 else ""))
    else:
        st.caption("请填写抽题数量（单套最多 500 题）。")

    c1, c2, c3 = st.columns([4, 1, 1])
    with c2:
        if st.button("取消", use_container_width=True):
            goto("list")
    with c3:
        if st.button("确定", type="primary", use_container_width=True):
            if not picked:
                st.error("还没有选题。")
            elif len(picked) > 500:
                st.error(f"单套自测最多 500 题，当前选了 {len(picked)} 题，请减少后再创建。")
            else:
                test = make_test(title, mode, int(limit), picked, selection)
                st.session_state.tests.append(test)
                save_tests(st.session_state.tests)
                goto("exam", test["id"])


# ================================================================ ai

@st.cache_resource(show_spinner=False)
def kb_resource():
    return get_kb()


def render_ai() -> None:
    header("AI 出题", "按教材原文生成一组题，适合题库没有覆盖的章节。")
    if st.button("← 返回我的自测"):
        goto("list")

    with st.spinner("正在加载教材索引…"):
        kb = kb_resource()
    display_to_raw = {display_source(s): s for s in kb.sources}
    book_label = st.selectbox("教材", list(display_to_raw))
    book = display_to_raw[book_label]
    chapters = kb.chapters(book)
    chapter = st.selectbox("章节", ["（全书）"] + chapters) if chapters else "（全书）"
    topic = st.text_input("主题/关键词（可选）", placeholder="如：IgA肾病 诊断")
    c1, c2, c3 = st.columns(3)
    with c1:
        n = st.number_input("题目数量", 1, 20, 5)
    with c2:
        qtype_label = st.selectbox("题型", ["单选题", "填空题", "判断题", "病例题"])
    with c3:
        mode_label = st.selectbox("模式", ["刷题自测", "模拟测验"])
    qtype = {"单选题": "single", "填空题": "fill", "判断题": "tf", "病例题": "case"}[qtype_label]

    if st.button("生成并开始", type="primary"):
        with st.spinner("正在依据教材原文出题…"):
            items, err = generate_quiz(
                kb, book, None if chapter == "（全书）" else chapter, topic, int(n), qtype
            )
        if err or not items:
            st.error(err or "没有生成成功，请换一个章节或关键词。")
            return
        questions = []
        for it in items:
            if qtype == "tf":
                questions.append({
                    "id": f"ai_{len(questions)}",
                    "qtype": "tf",
                    "stem": it.get("question", ""),
                    "options": [{"key": "对", "text": "正确"}, {"key": "错", "text": "错误"}],
                    "answer": [bool(it.get("answer"))],
                    "answer_text": "正确" if it.get("answer") else "错误",
                    "explanation": it.get("explanation", ""),
                    "folder": ["教材AI", display_source(book)],
                    "source": f"教材AI · {display_source(book)} 第{it.get('source_page', '')}页",
                    "difficulty": "",
                    "knowledge": chapter if chapter != "（全书）" else topic,
                })
            elif qtype == "fill":
                questions.append({
                    "id": f"ai_{len(questions)}",
                    "qtype": "fill",
                    "stem": it.get("question", ""),
                    "options": [],
                    "answer": [],
                    "answer_text": str(it.get("answer", "")),
                    "explanation": it.get("explanation", ""),
                    "folder": ["教材AI", display_source(book)],
                    "source": f"教材AI · {display_source(book)} 第{it.get('source_page', '')}页",
                    "difficulty": "",
                    "knowledge": chapter if chapter != "（全书）" else topic,
                })
            else:
                stem = it.get("question", "")
                if it.get("case"):
                    stem = f"{it['case']}\n\n{stem}"
                questions.append({
                    "id": f"ai_{len(questions)}",
                    "qtype": "single",
                    "stem": stem,
                    "options": [{"key": LETTERS[i], "text": o} for i, o in enumerate(it.get("options", []))],
                    "answer": [int(it.get("answer", 0))],
                    "answer_text": LETTERS[int(it.get("answer", 0))],
                    "explanation": it.get("explanation", ""),
                    "folder": ["教材AI", display_source(book)],
                    "source": f"教材AI · {display_source(book)} 第{it.get('source_page', '')}页",
                    "difficulty": "",
                    "knowledge": chapter if chapter != "（全书）" else topic,
                })
        test = make_test(
            f"AI 出题 · {display_source(book)}", "practice" if mode_label == "刷题自测" else "exam",
            0 if mode_label == "刷题自测" else 10, questions,
            {"source": "ai", "method": "generated"},
        )
        st.session_state.tests.append(test)
        save_tests(st.session_state.tests)
        goto("exam", test["id"])


# ================================================================ exam

def render_question(q: dict, i: int, test: dict) -> None:
    qt = q.get("qtype", "single")
    label = TYPE_LABEL.get(qt, "题目")
    marked_badge = '<span class="cx-badge orange">★ 已标记</span>' if is_marked(q) else ""
    st.markdown(
        f'<div class="cx-q" id="q_{i}"><span class="cx-badge gray">{html.escape(label)}</span>'
        f'<span class="cx-meta">第 {i + 1} 题</span>{marked_badge}</div>',
        unsafe_allow_html=True,
    )
    stem = html.escape(q.get("stem", "")).replace("\n", "<br>")
    st.markdown(f'<div class="cx-stem">{stem}</div>', unsafe_allow_html=True)

    key = f"ans_{test['id']}_{i}"
    opts = option_texts(q)
    if qt in ("single", "case"):
        labels = [f"{LETTERS[j]}. {o}" for j, o in enumerate(opts)]
        st.radio("请选择", labels, index=None, key=key, label_visibility="collapsed")
    elif qt == "multiple":
        for j, o in enumerate(opts):
            st.checkbox(f"{LETTERS[j]}. {o}", key=f"{key}_{j}")
    elif qt == "tf":
        st.radio("请判断", ["正确", "错误"], index=None, key=key, label_visibility="collapsed")
    elif qt == "fill":
        st.text_input("答案", key=key, label_visibility="collapsed", placeholder="填写答案")
    else:
        st.text_area("答案", key=key, label_visibility="collapsed", height=90, placeholder="主观题，可留空")

    # 标记 / AI 解答
    k = qkey(q)
    act1, act2 = st.columns([1.2, 1.2])
    with act1:
        if st.button("★ 已标记" if is_marked(q) else "☆ 标记", key=f"mark_{test['id']}_{i}", use_container_width=True):
            toggle_mark(q)
            st.rerun()
    with act2:
        if st.button("🤖 AI 解答", key=f"ai_{test['id']}_{i}", use_container_width=True):
            with st.spinner("AI 正在解答…"):
                ai_solution(q)
            st.rerun()
    if st.session_state.get("ai_cache", {}).get(k):
        with st.expander("🤖 AI 解答（仅供参考）", expanded=True):
            st.markdown(st.session_state.ai_cache[k])


def reveal_answer(q: dict) -> None:
    st.success(f"正确答案：{correct_text(q)}")
    if q.get("explanation"):
        st.caption(f"解析：{q['explanation']}")


def render_exam() -> None:
    test = find_test(st.session_state.test_id)
    if not test:
        st.warning("没有找到这套自测。")
        if st.button("返回我的自测"):
            goto("list")
        return

    rem = remaining_seconds(test)
    if test.get("status") != "done" and rem is not None and rem <= 0:
        finish_test(test, collect_answers(test))
        st.warning("时间到，已自动交卷。")
        goto("result", test["id"])

    badge = "模拟测验" if test.get("mode") == "exam" else "刷题自测"
    c1, c2, c3 = st.columns([6, 2, 2])
    with c1:
        st.markdown(
            f'<span class="cx-badge">{badge}</span>'
            f'<span class="cx-title">{html.escape(test.get("title", "自测"))}</span>'
            f'<div class="cx-meta">{len(test.get("questions", []))} 题　'
            f'{html.escape(test.get("created_at", "")[:16])}</div>',
            unsafe_allow_html=True,
        )
    with c2:
        if rem is not None:
            st.metric("剩余时间", fmt_seconds(rem))
        else:
            st.metric("剩余时间", "不限时")
    with c3:
        if st.button("退出", use_container_width=True):
            save_draft(test, collect_answers(test))
            goto("list")

    qs = test.get("questions", [])
    restore_draft(test, qs)
    main, side = st.columns([3, 1])
    with main:
        with st.container(border=True, key="cx_questions"):
            for i, q in enumerate(qs):
                render_question(q, i, test)
                if test.get("mode") == "practice":
                    if st.button("查看答案", key=f"rev_{test['id']}_{i}"):
                        st.session_state[f"revealed_{test['id']}_{i}"] = True
                    if st.session_state.get(f"revealed_{test['id']}_{i}"):
                        reveal_answer(q)
    # 每次作答后自动保存草稿，刷新/重启后可恢复
    save_draft(test, collect_answers(test))

    with side:
        with st.container(border=True, key="cx_sheet"):
            total = len(qs)
            done = answered_count(test)
            st.markdown(f'<span class="cx-badge">答题卡</span>', unsafe_allow_html=True)
            cells = "".join(
                f'<a class="cx-num{" done" if _is_answered(test, i, qs[i]) else ""}{" marked" if is_marked(qs[i]) else ""}"'
                f' href="#q_{i}" title="跳转到第 {i + 1} 题">{i + 1}</a>'
                for i in range(total)
            )
            st.markdown(f'<div>{cells}</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="cx-meta"><span class="cx-num done" style="width:14px;height:14px;margin:2px;"></span>已答　'
                '<span class="cx-num marked" style="width:14px;height:14px;margin:2px;"></span>已标记</div>',
                unsafe_allow_html=True,
            )
            st.progress(done / total if total else 0)
            st.caption(f"已答 {done} / {total}")

            if st.button("交卷", type="primary", use_container_width=True):
                answers = collect_answers(test)
                finish_test(test, answers)
                goto("result", test["id"])
            st.caption("交卷后自动判分，错题会进入错题本。")


def _is_answered(test: dict, i: int, q: dict) -> bool:
    qt = q.get("qtype")
    if qt in ("single", "case", "tf"):
        return st.session_state.get(f"ans_{test['id']}_{i}") is not None
    if qt == "multiple":
        return any(st.session_state.get(f"ans_{test['id']}_{i}_{j}") for j in range(len(q.get("options") or [])))
    return bool((st.session_state.get(f"ans_{test['id']}_{i}") or "").strip())


# ================================================================ result

def render_result() -> None:
    test = find_test(st.session_state.test_id)
    if not test or test.get("status") != "done":
        st.warning("还没有交卷。")
        if st.button("返回我的自测"):
            goto("list")
        return

    header(test.get("title", "自测"), f"交卷时间 {test.get('submitted_at', '')[:16]}　共 {len(test.get('questions', []))} 题")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("得分", f"{test.get('score', 0)} / {test.get('total', 0)}")
    pct = (test.get("score", 0) / test["total"] * 100) if test.get("total") else 0
    c2.metric("正确率", f"{pct:.0f}%")
    c3.metric("答对", f"{test.get('score', 0)} 题")
    c4.metric("答错", f"{max(0, test.get('total', 0) - test.get('score', 0))} 题")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("← 返回我的自测", use_container_width=True):
            goto("list")
    with c2:
        if st.button("再做一遍", use_container_width=True):
            clear_answer_keys(test["id"])
            test["status"] = "todo"
            test["score"] = 0
            test["total"] = 0
            test.pop("results", None)
            test.pop("answers", None)
            test.pop("draft_answers", None)
            test.pop("draft_saved_at", None)
            test["started_at"] = iso()
            save_tests(st.session_state.tests)
            goto("exam", test["id"])

    results = {r.get("index"): r for r in test.get("results", [])}
    wrong_items = [(i, q, results[i]) for i, q in enumerate(test.get("questions", [])) if results.get(i, {}).get("correct") is False]

    if wrong_items:
        with st.container(border=True, key="cx_analysis"):
            st.markdown('<span class="cx-badge">错题分析</span>', unsafe_allow_html=True)
            st.caption(f"本套共错 {len(wrong_items)} 题，按来源分布：")
            dist: dict[str, int] = {}
            for _, q, _r in wrong_items:
                dist[folder_name(q)] = dist.get(folder_name(q), 0) + 1
            st.dataframe(
                [{"来源": k, "错题数": v} for k, v in sorted(dist.items(), key=lambda x: -x[1])],
                hide_index=True, use_container_width=True,
            )

            if not test.get("ai_analysis") and not test.get("analysis_tried"):
                with st.spinner("AI 正在分析知识薄弱点…"):
                    test["ai_analysis"] = wrong_analysis(test)
                    test["analysis_tried"] = True
                    for t in st.session_state.tests:
                        if t.get("id") == test.get("id"):
                            t.update(test)
                            break
                    save_tests(st.session_state.tests)

            if test.get("ai_analysis"):
                st.markdown(test["ai_analysis"])
                if st.button("重新生成分析", key=f"regen_{test['id']}"):
                    test["ai_analysis"] = ""
                    test["analysis_tried"] = False
                    for t in st.session_state.tests:
                        if t.get("id") == test.get("id"):
                            t.update(test)
                            break
                    save_tests(st.session_state.tests)
                    st.rerun()
            else:
                st.info("AI 暂时没有返回分析结果。")
                if st.button("重新生成分析", key=f"regen2_{test['id']}"):
                    test["analysis_tried"] = False
                    st.rerun()

    for i, q in enumerate(test.get("questions", [])):
        r = results.get(i, {})
        correct = r.get("correct")
        mark = "✅" if correct else ("📝" if correct is None else "❌")
        with st.container(border=True, key=f"cx_result_{i}"):
            st.markdown(
                f'<span class="cx-badge gray">{TYPE_LABEL.get(q.get("qtype"), "题目")}</span>'
                f'<span class="cx-title">{mark} 第 {i + 1} 题</span>',
                unsafe_allow_html=True,
            )
            st.markdown(f'<div class="cx-stem">{html.escape(q.get("stem", "")).replace(chr(10), "<br>")}</div>', unsafe_allow_html=True)

            opts = q.get("options") or []
            ans = set(q.get("answer") or [])
            user_pick = set(str(r.get("user", ""))) if q.get("qtype") in ("single", "multiple", "case") else set()
            if opts:
                lines = []
                for j, o in enumerate(opts):
                    if j in ans:
                        cls = "cx-opt-correct"
                    elif LETTERS[j] in user_pick:
                        cls = "cx-opt-wrong"
                    else:
                        cls = ""
                    lines.append(f'<div class="{cls}">{LETTERS[j]}. {html.escape(o.get("text", ""))}</div>')
                st.markdown("".join(lines), unsafe_allow_html=True)

            if correct is None:
                st.info("主观题，请对照参考答案自评。")
            else:
                st.markdown(
                    f"**你的答案：** {html.escape(str(r.get('user', '-')))}　　"
                    f"**正确答案：** {html.escape(correct_text(q))}"
                )
            if q.get("explanation"):
                st.caption(f"解析：{q['explanation']}")
            st.caption(f"来源：{q.get('source', folder_name(q))}")

            k = qkey(q)
            ac1, ac2 = st.columns([1.2, 1.2])
            with ac1:
                if st.button("★ 已标记" if is_marked(q) else "☆ 标记", key=f"rmark_{test['id']}_{i}", use_container_width=True):
                    toggle_mark(q)
                    st.rerun()
            with ac2:
                if st.button("🤖 AI 解答", key=f"rai_{test['id']}_{i}", use_container_width=True):
                    with st.spinner("AI 正在解答…"):
                        ai_solution(q)
                    st.rerun()
            if st.session_state.get("ai_cache", {}).get(k):
                with st.expander("🤖 AI 解答（仅供参考）", expanded=True):
                    st.markdown(st.session_state.ai_cache[k])


# ================================================================ route

view = st.session_state.view
if view == "create":
    render_create()
elif view == "ai":
    render_ai()
elif view == "exam":
    render_exam()
elif view == "result":
    render_result()
else:
    render_list()
