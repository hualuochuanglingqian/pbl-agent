# -*- coding: utf-8 -*-
"""把从医学智慧教学平台导出的题库目录、错题集整理成 CSV/Markdown。

数据来源：
- data/platform_folders.json    平台自测「从文件夹选题」的目录树（578 个目录，41,729 题）
- data/platform_wrongbook.json  平台错题集（18 题，含正确答案、解析、知识点）
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = Path(r"D:\医学知识库\题库导出")
OUT.mkdir(parents=True, exist_ok=True)


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def folder_rows(folders: list[dict]) -> list[dict]:
    rows = []
    for f in folders:
        path = f.get("path") or [f.get("name", "")]
        rows.append({
            "一级": path[0] if len(path) > 0 else "",
            "二级": path[1] if len(path) > 1 else "",
            "三级": path[2] if len(path) > 2 else "",
            "四级": path[3] if len(path) > 3 else "",
            "名称": f.get("name", ""),
            "题目数": f.get("count") if f.get("count") is not None else "",
            "目录ID": ",".join(str(x) for x in (f.get("dir_ids") or [])),
            "完整路径": " / ".join(path),
        })
    return rows


def write_folders_csv(rows: list[dict]) -> None:
    with (OUT / "平台题库_文件夹.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_folders_md(rows: list[dict]) -> None:
    lines = [
        "# 医学智慧教学平台 · 题库文件夹",
        "",
        "> 课程：医学智慧教学平台（courseId 221645549）",
        f"> 目录数：{len(rows)}；有题目数的叶子目录：" + str(sum(1 for r in rows if r["题目数"] != "")),
        f"> 题目总数：{sum(int(r['题目数']) for r in rows if r['题目数'] != '')}",
        "",
        "说明：这是平台“新建自测 → 从文件夹选题”的目录树，题目正文未导出。",
        "",
    ]
    for r in rows:
        depth = sum(1 for k in ("一级", "二级", "三级", "四级") if r[k])
        indent = "  " * max(0, depth - 1)
        count = f"（{r['题目数']} 题）" if r["题目数"] != "" else ""
        lines.append(f"{indent}- {r['名称']}{count}")
    (OUT / "平台题库_文件夹.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\s\u00a0。；;,.、:：()（）\[\]【】]", "", s)
    return s


def dedupe_questions(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for q in items:
        key = (norm(q.get("stem", "")), tuple(o.get("text", "") for o in q.get("options", [])))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def write_wrongbook(items: list[dict]) -> None:
    items = dedupe_questions(items)
    payload = {
        "source": "医学智慧教学平台 · 错题集",
        "count": len(items),
        "questions": items,
    }
    (OUT / "错题集_题目.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    label = {"single": "单选题", "multiple": "多选题", "tf": "判断题", "fill": "填空题", "short": "简答题"}
    lines = [
        "# 医学智慧教学平台 · 错题集",
        "",
        f"> 共 {len(items)} 题，含题干、选项、正确答案、解析、知识点。",
        "",
    ]
    for i, q in enumerate(items, 1):
        lines.append(f"## {i}. [{label.get(q.get('type'), q.get('type'))}] {q.get('stem', '')}")
        lines.append("")
        for o in q.get("options", []):
            lines.append(f"- {o.get('key', '')}. {o.get('text', '')}")
        lines.append("")
        lines.append(f"**正确答案：** {'、'.join(q.get('answer') or [])}")
        if q.get("my_answer"):
            lines.append(f"**我的答案：** {q['my_answer']}")
        if q.get("explanation"):
            lines.append(f"**解析：** {q['explanation']}")
        if q.get("knowledge"):
            lines.append(f"**知识点：** {q['knowledge']}")
        lines.append("")
    (OUT / "错题集_题目.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    folders = load("platform_folders.json")["folders"]
    rows = folder_rows(folders)
    write_folders_csv(rows)
    write_folders_md(rows)

    wrong = load("platform_wrongbook.json")["questions"]
    write_wrongbook(wrong)

    total = sum(int(r["题目数"]) for r in rows if r["题目数"] != "")
    readme = f"""# 平台题库导出

数据来自“医学智慧教学平台”（超星课程，courseId 221645549）。

## 已导出

| 内容 | 文件 | 数量 |
|---|---|---|
| 题库文件夹目录 | `平台题库_文件夹.csv` / `平台题库_文件夹.md` | 578 个目录，{total} 题 |
| 错题集题目（含答案解析） | `错题集_题目.json` / `错题集_题目.md` | {len(dedupe_questions(wrong))} 题 |

## 未导出

平台题库共 {total} 题，但题目正文只能通过“新建自测 → 进入考试”查看。进入考试需要平台的人机验证（滑块/验证码），
自动批量进入会被判定为异常。因此本次只导出了：

1. 题库的完整文件夹分类和每个目录的题量；
2. 当前账号错题集里的 {len(dedupe_questions(wrong))} 道题（平台直接展示答案、解析、知识点）。

其余题目可以在平台里手动进入自测后查看，或在本地知识库的“自测”页用教材 AI 出题替代。

## 本地使用

`D:\\医学知识库\\personal_kb` 的“自测”页会读取：

- `personal_kb/data/platform_folders.json`
- `personal_kb/data/platform_wrongbook.json`

支持按文件夹、题型、知识点抽题，以及“模拟测验 / 刷题自测”两种模式。
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")

    print(f"folders={len(rows)} total_questions={total} wrongbook={len(dedupe_questions(wrong))}")
    print(f"output={OUT}")


if __name__ == "__main__":
    main()
