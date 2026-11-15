"""回填 book_catalog.json 的教材主编（authors）信息。

GB/T 7714-2015 的 [M] 专著条目必须有作者。主编名单写在每本教材的
封面/版权/编委页里，本工具抽取每本书 PDF 的前 12 页文本，交给 LLM
提取主编名单，写回 pbl/book_catalog.json。

用法（在项目根目录，用 .venv 的 Python）：
  python tools/fill_catalog_authors.py           # 预览抽取结果，不写文件
  python tools/fill_catalog_authors.py --apply   # 确认无误后写回
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbl import llm, textbook  # noqa: E402

CATALOG = Path(__file__).resolve().parent.parent / "pbl" / "book_catalog.json"
FRONT_PAGES = 12   # 封面/版权/编委页一般在最前面


class _Authors(BaseModel):
    authors: str = ""


PROMPT = """下面是一本中文医学教材 PDF 前 {n} 页提取的文本（封面、版权页、编委名单等）。

【教材】{title}

【文本】
{text}

请找出本书的**主编**名单（不是副主编、不是编委、不是出版社人员）：
- 按版权页/封面署名填写，通常形如「王建枝、钱睿哲」或「王卫平」；
- 人数以署名为准（一般 1~3 人）；
- 用中文逗号分隔；文本中确实没有主编信息时 authors 留空字符串，不要猜。
只输出 JSON：{{"authors": "名1, 名2"}}"""


def _front_text(fname: str) -> str:
    pages = textbook._extract(textbook.BOOKS_DIR / fname)[:FRONT_PAGES]
    return "\n".join(t for _, t in pages)[:6000]


def main() -> None:
    apply = "--apply" in sys.argv
    if not llm.has_key():
        print("未配置 API Key（.env），无法调用模型抽取主编。")
        return
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    # 书名键 -> 教材文件名
    file_by_title = {}
    for fname in textbook.list_books():
        file_by_title.setdefault(textbook._book_title(fname), fname)

    changed, failed = 0, 0
    for key, info in catalog.items():
        if info.get("authors"):
            continue
        fname = file_by_title.get(key)
        if not fname:
            print(f"[跳过] {key}：books/ 下找不到对应教材文件")
            failed += 1
            continue
        try:
            text = _front_text(fname)
        except Exception as e:
            print(f"[失败] {key}：抽取 PDF 文本出错 {e}")
            failed += 1
            continue
        result = llm.chat_json(PROMPT.format(n=FRONT_PAGES, title=info.get("title", key),
                                             text=text), _Authors)
        authors = (result.authors if result else "").strip()
        if not authors:
            print(f"[未识别] {key}：前 {FRONT_PAGES} 页未找到主编署名，请手动补填")
            failed += 1
            continue
        print(f"[{'写入' if apply else '预览'}] {key} -> {authors}")
        info["authors"] = authors
        changed += 1

    if apply and changed:
        CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
        print(f"\n已写回 {CATALOG}（{changed} 本）。")
    else:
        print(f"\n预览完成：{changed} 本可回填，{failed} 本需手动处理。"
              "确认无误后加 --apply 写回。")


if __name__ == "__main__":
    main()
