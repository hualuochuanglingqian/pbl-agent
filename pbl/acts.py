"""病例分幕存档：同一病例按「第一幕 / 第二幕 / 第三幕」推进，每一幕完整存一份。

文件放在 `cases/acts/{系列}__act{n}.json`，内容是整份 Case（含问题、学习内容、目录、
导图、文献分享），因此后续幕可以：

- 随时回到任意一幕继续改（载入即替换工作区的当前案例）；
- 生成 Word 时把前几幕的「问题解答」并入作为附录（`Case.word_prev_acts`）；
- 老师要求把上一幕的病例信息再讲一遍时，直接取存档原文。

命名规则：系列标识为空时用标题生成；同一系列的每一幕共用同一系列标识。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .schema import Case

CASES = Path(__file__).resolve().parent.parent / "cases"
ACTS = CASES / "acts"


def slug(text: str, limit: int = 60) -> str:
    """标题 -> 安全的文件名字段。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    s = s.strip("._")
    return (s or "未命名")[:limit]


def series_key(case: Case) -> str:
    """同一病例的各幕共用的系列标识（缺省按标题生成）。"""
    return case.series or slug(case.title)


def set_current_act(case: Case) -> None:
    """确保 series 有值、act_no 合法（就地修正）。"""
    if not case.series:
        case.series = slug(case.title)
    if not isinstance(case.act_no, int) or case.act_no < 1:
        case.act_no = 1


def act_path(case: Case, act_no: int | None = None) -> Path:
    n = act_no if act_no is not None else case.act_no
    return ACTS / f"{series_key(case)}__act{int(n)}.json"


def save_act(case: Case) -> Path:
    """把当前案例整份存为该系列的第 n 幕；返回文件路径。"""
    set_current_act(case)
    case.touch()
    ACTS.mkdir(parents=True, exist_ok=True)
    p = act_path(case)
    p.write_text(json.dumps(case.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_act(path: str | Path) -> Case | None:
    """读回某一幕的存档；失败返回 None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return Case.model_validate(data)
    except Exception:
        return None


def _meta(path: Path) -> dict | None:
    """从存档里取列表用的摘要信息（不构造整个 Case）。"""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    m = re.match(r"^(?P<series>.+)__act(?P<n>\d+)$", path.stem)
    return {
        "path": str(path),
        "series": (m.group("series") if m else d.get("series") or ""),
        "act_no": int(m.group("n")) if m else int(d.get("act_no") or 1),
        "act_title": d.get("act_title") or "",
        "title": d.get("title") or "",
        "note": d.get("act_note") or "",
        "n_questions": len(d.get("questions") or []),
        "saved_at": d.get("updated") or "",
    }


def list_acts(series: str | None = None) -> list[dict]:
    """列出各幕存档（按系列、幕号排序）；series 为空则列出全部系列。"""
    if not ACTS.exists():
        return []
    out = []
    for p in sorted(ACTS.glob("*.json")):
        meta = _meta(p)
        if meta is None:
            continue
        if series and meta["series"] != series:
            continue
        out.append(meta)
    return sorted(out, key=lambda m: (m["series"], m["act_no"]))


def all_series() -> list[str]:
    """所有已存档的系列名（去重、保持出现顺序）。"""
    seen: list[str] = []
    for m in list_acts():
        if m["series"] not in seen:
            seen.append(m["series"])
    return seen


def next_act_no(case: Case) -> int:
    """该系列下一个可用的幕号（已有存档的最大幕号 + 1）。"""
    acts = list_acts(series_key(case))
    return (max((m["act_no"] for m in acts), default=case.act_no) + 1)


def make_next_act(case: Case, copy_case_text: bool = True) -> Case:
    """以当前幕为蓝本生成下一幕：病例原文沿用（便于往上追加新信息），问题清空。

    保留：标题/课程/对象、系列、关键词、PPT 模板与署名、Word 相关偏好。
    清空：问题、个人总结目录、导图、文献候选与分享内容（换幕后通常要重新选）。
    """
    set_current_act(case)
    new = Case(
        title=case.title,
        course=case.course,
        audience=case.audience,
        case_text=case.case_text if copy_case_text else "",
        series=series_key(case),
        act_no=next_act_no(case),
        act_title="",
        act_note="",
        keywords=list(case.keywords),
        word_follow_summary=case.word_follow_summary,
        mm_requirement=case.mm_requirement,
        mm_branches=list(case.mm_branches),
    )
    new.ppt.template = case.ppt.template
    new.ppt.author = case.ppt.author
    new.ppt.direction = case.ppt.direction
    new.touch()
    return new


def prev_cases(case: Case) -> list[Case]:
    """取 Case.word_prev_acts 指定的前幕存档（按幕号升序，缺文件自动跳过）。"""
    set_current_act(case)
    out: list[Case] = []
    for n in sorted(set(int(x) for x in case.word_prev_acts if int(x) < case.act_no)):
        p = act_path(case, n)
        c = load_act(p) if p.exists() else None
        if c is not None:
            out.append(c)
    return out


def available_prev_acts(case: Case) -> list[dict]:
    """该系列里幕号小于当前幕的存档（供界面勾选并入 Word）。"""
    set_current_act(case)
    return [m for m in list_acts(series_key(case)) if m["act_no"] < case.act_no]


def act_label(case: Case) -> str:
    """「第二幕 · 病理回报」这样的展示名。"""
    cn = "一二三四五六七八九十"
    n = min(max(case.act_no, 1), len(cn))
    name = f"第{cn[n - 1]}幕"
    return f"{name} · {case.act_title}" if case.act_title else name


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
