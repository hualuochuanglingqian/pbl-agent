"""PBL 医学教案工作台 —— Streamlit 界面。

六个工作区：① 病例与问题 ② 内容生成与修改 ③ 思维导图 ④ 文献检索 ⑤ 导出 Word ⑥ 生成 PPT
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from pbl import acts, demo, docx_preview, literature, llm, mindmap, pipeline, pptm, pptx_preview, pubmed, render_docx, render_pptx, textbook  # noqa: E402
from pbl.schema import (QUESTION_TYPES, REF_SOURCE_MANUAL, Case, LearningBlock, LitCandidate, Question,  # noqa: E402
                        Reference, SlideOutline, SummaryNode, heal_tables)

ROOT = Path(__file__).parent
OUT = ROOT / "output"
CASES = ROOT / "cases"
OUT.mkdir(exist_ok=True)
CASES.mkdir(exist_ok=True)

st.set_page_config(page_title="PBL 医学教案工作台", page_icon="🩺", layout="wide")

QTYPE_LABELS = {"cover": "封面", "case": "病例展示", "overview": "问题总览",
                "question": "学习要点", "clinical": "临床应用", "refs": "参考文献",
                "summary": "总结反思", "bullets": "通用列表"}


# ---------------------------------------------------------------- 工具
def cver() -> int:
    """案例控件版本号：程序性更新内容后 +1，迫使前端控件以新值重建。"""
    return st.session_state.get("case_ver", 0)


def dkey(base: str) -> str:
    """版本化控件 key。注意：仅 del session_state 键无法刷新前端控件旧值，
    必须更换 key 才能让控件重建。"""
    return f"cv{cver()}_{base}"


def bump_case() -> None:
    """程序性修改案例后调用：清理旧版本控件状态并递增版本。"""
    v = st.session_state.get("case_ver", 0)
    old = f"cv{v}_"
    for k in list(st.session_state.keys()):
        if k.startswith(old):
            del st.session_state[k]
    st.session_state["case_ver"] = v + 1
    snapshot_case()


# ---------------------------------------------------------------- 自动存档
AUTOSAVE = CASES / "_autosave.json"
HISTORY = CASES / "history"
HISTORY_KEEP = 30


def _dump_case(c: Case) -> str:
    return c.model_dump_json(indent=2, ensure_ascii=False)


def autosave_latest() -> None:
    """每次界面刷新都把当前案例写一份最新存档——关标签页/浏览器崩溃后可一键恢复。

    自动化测试/脚本化调用时设 PBL_NO_AUTOSAVE=1，避免把测试用的假案例写成本人存档。
    """
    if os.environ.get("PBL_NO_AUTOSAVE"):
        return
    c = get_case()
    if c is None:
        return
    try:
        AUTOSAVE.write_text(_dump_case(c), encoding="utf-8")
    except OSError:
        pass


def snapshot_case() -> None:
    """程序性修改（整理/生成/审校/载入）后：更新最新存档并落一份时间戳快照，保留最近 30 份。"""
    if os.environ.get("PBL_NO_AUTOSAVE"):
        return
    c = get_case()
    if c is None:
        return
    try:
        AUTOSAVE.write_text(_dump_case(c), encoding="utf-8")
        HISTORY.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (HISTORY / f"{slug(c.title)}_{ts}.json").write_text(_dump_case(c), encoding="utf-8")
        for old in sorted(HISTORY.glob("*.json"),
                          key=lambda p: p.stat().st_mtime)[:-HISTORY_KEEP]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def slug(text: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (text or "案例").strip())[:40]
    return s or "案例"


def get_case() -> Case | None:
    c = st.session_state.get("case")
    if c is not None:
        heal_tables(c)  # 旧会话里的双重编码表格就地自愈（幂等）
    return c


def need_case() -> bool:
    if get_case() is None:
        st.info("请先在 ① 病例与问题 中输入病例，或在左侧载入演示案例。")
        return True
    return False


def _md_to_summary(md: str, case: Case) -> list:
    """Markdown 目录 -> SummaryNode 树；按叶子顺序保留稳定题目 ID。"""
    flat: list[tuple[int, str]] = []
    for raw in (md or "").splitlines():
        if not raw.strip():
            continue
        level = (len(raw) - len(raw.lstrip(" "))) // 2
        txt = re.sub(r"^\s*\d+(?:\.\d+)*\s*[.、．]?\s*", "", raw.strip()).strip()
        if txt:
            flat.append((level, txt[:40]))

    qorder: list[str] = []
    for node in _walk_summary(case.summary_nodes):
        if not node.children:
            qorder.extend(node.question_ids)

    root: list[SummaryNode] = []
    stack: list[tuple[int, SummaryNode | None]] = [(-1, None)]
    created: list[SummaryNode] = []
    for level, txt in flat:
        node = SummaryNode(text=txt)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]
        (root if parent is None else parent.children).append(node)
        stack.append((level, node))
        created.append(node)

    leaf_nodes = [n for n in created if not n.children]
    remaining = list(qorder)
    for node in leaf_nodes:
        idx = next((i for i, uid in enumerate(remaining)
                    if (q := case.question_by_uid(uid)) is not None
                    and _norm_summary_title(node.text) in _norm_summary_title(q.text)), None)
        if idx is None and remaining:
            idx = 0
        if idx is not None:
            node.question_ids = [remaining.pop(idx)]
    for node in reversed(created):
        if node.children:
            node.question_ids = []
            node.qids = list(dict.fromkeys(
                q.qid for child in node.children for uid in child.question_ids
                for q in case.questions if q.uid == uid))
    return root


def _norm_summary_title(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").lower(), flags=re.UNICODE)


def _walk_summary(nodes) -> list:
    out = []
    for n in nodes or []:
        out.append(n)
        out.extend(_walk_summary(n.children))
    return out


# ---------------------------------------------------------------- 侧边栏
with st.sidebar:
    st.header("🩺 PBL 医学教案工作台")
    st.caption("工作台版本 v20261021.1 — 若此行不存在说明浏览器/进程仍是旧版，请彻底重启。")

    with st.expander("⚙️ 系统状态", expanded=False):
        st.write("供应商：", llm.PROVIDER, "　模型：", llm.MODEL if llm.has_key() else "**未配置 Key（演示模式）**")
        st.write("教材检索：", "✅ 已建库" if textbook.has_index() else "❌ 未建库")
        books = textbook.list_books()
        st.write(f"教材文件：{len(books)} 本", "、".join(books[:5]) if books else "")
        if not llm.has_key():
            st.caption("按 .env.example 配置所选供应商的 API Key（deepseek → DEEPSEEK_API_KEY 等）后重启。"
                       "教材向量化固定使用智谱（ZHIPUAI_API_KEY）。")

    with st.expander("📚 教材知识库", expanded=False):
        ups = st.file_uploader("上传十版教材（PDF/Word/TXT）", type=["pdf", "docx", "txt", "md"],
                               accept_multiple_files=True)
        if ups and st.button("保存到教材库", key="save_books"):
            textbook.BOOKS_DIR.mkdir(parents=True, exist_ok=True)
            for up in ups:
                (textbook.BOOKS_DIR / up.name).write_bytes(up.getvalue())
            st.success(f"已保存 {len(ups)} 个文件")
            st.rerun()
        if st.button("建立 / 重建索引", key="build_idx", disabled=not books):
            ph = st.empty()
            try:
                n, names = textbook.build_index(progress_cb=ph.info)
                ph.success(f"建库完成：{len(names)} 本教材，{n} 个知识块")
            except Exception as e:
                ph.error(str(e))
        if st.button("📐 校准页码并清理前言/目录", key="calib_pages",
                     disabled=not textbook.has_index(),
                     help="推断每本教材 PDF 页码与印刷页码的偏移，把引用页码改写为印刷页码，"
                          "并删除封面/编委/前言/目录知识块。不重新向量化，秒级完成；建库时对新书自动执行。"):
            ph2 = st.empty()
            try:
                info = textbook.recalibrate_pages(progress_cb=ph2.info)
                calibrated = sum(1 for v in info.values() if v["offset"] > 0)
                dropped = sum(v["dropped"] for v in info.values())
                ph2.success(f"校准完成：{calibrated}/{len(info)} 本书应用了页码偏移，"
                            f"清理前置部分 {dropped} 块（此后引用页码=印刷页码）")
            except Exception as e:
                ph2.error(str(e))

    with st.expander("💾 案例存档", expanded=False):
        if st.button("载入演示案例", key="load_demo", width="stretch"):
            st.session_state.case = demo.demo_case()
            bump_case()
            st.rerun()
        c = get_case()
        if c is None and AUTOSAVE.exists():
            try:
                meta = json.loads(AUTOSAVE.read_text(encoding="utf-8"))
                if st.button(f"⏪ 恢复上次案例：{meta.get('title', '未命名')}"
                             f"（{meta.get('updated') or '时间未知'}）",
                             width="stretch", key="restore_auto"):
                    st.session_state.case = Case.model_validate(meta)
                    bump_case()
                    st.rerun()
            except Exception:
                pass
        c = get_case()
        if c is not None:
            st.download_button("下载案例 JSON", data=c.model_dump_json(indent=2, ensure_ascii=False),
                               file_name=f"{slug(c.title)}.json", mime="application/json",
                               width="stretch")
        up_json = st.file_uploader("载入案例 JSON", type=["json"], key="load_case")
        if up_json is not None:
            try:
                st.session_state.case = Case.model_validate(json.loads(up_json.getvalue()))
                bump_case()
                st.rerun()
            except Exception as e:
                st.error(f"案例文件解析失败：{e}")
        snaps = sorted(HISTORY.glob("*.json"), reverse=True) if HISTORY.exists() else []
        if snaps:
            pick = st.selectbox(f"🕘 历史快照（自动保留最近 {HISTORY_KEEP} 份）",
                                snaps[:10], format_func=lambda f: f.stem, key="hist_pick")
            if st.button("载入选中快照", key="hist_load"):
                try:
                    st.session_state.case = Case.model_validate(
                        json.loads(pick.read_text(encoding="utf-8")))
                    bump_case()
                    st.rerun()
                except Exception as e:
                    st.error(f"快照解析失败：{e}")

# ---------------------------------------------------------------- 主界面
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["① 病例与问题", "② 内容生成与修改", "③ 思维导图", "④ 文献检索",
     "⑤ 导出 Word", "⑥ 生成 PPT"])

# ================================================================ ① 输入
with tab1:
    st.subheader("病例与问题草稿")
    c = get_case()
    b1, b2, b3 = st.columns(3)
    title = b1.text_input("案例标题", value=c.title if c else "", key=dkey("in_title"))
    course = b2.text_input("学科 / 课程", value=c.course if c else "临床医学", key=dkey("in_course"))
    audience = b3.text_input("教学对象", value=c.audience if c else "本科生", key=dkey("in_aud"))
    case_text = st.text_area("病例内容", value=c.case_text if c else "", height=220,
                             key=dkey("in_case"), placeholder="粘贴完整病例：主诉、现病史、既往史、查体、辅助检查……")
    # 把输入框的值写回当前案例：否则自动存档保存的永远是上一次提交的旧病例，
    # 刷新/重启后刚改的内容会丢（分幕存档也会存成旧文本）。
    if c is not None:
        c.title = title or c.title
        c.course = course
        c.audience = audience
        c.case_text = case_text

    # ---- 病例分幕（第一幕 / 第二幕 / 第三幕）
    if c is None:
        st.caption("🎬 病例分幕存档：先填写病例并整理问题生成案例，之后即可按幕存档、随时载入，"
                   "生成 Word 时还能把前几幕的问题解答并入附录。")
    else:
        acts.set_current_act(c)
        with st.expander(f"🎬 病例分幕存档（当前：{acts.act_label(c)}）", expanded=False):
            st.caption("同一病例分几幕推进：每幕**整份存档**到 `cases/acts/`，可随时载入任意一幕继续改；"
                       "做 Word 时还能把前几幕的「问题解答」并入文末作为回顾附录。")
            k1, k2, k3 = st.columns([0.8, 1.2, 1.6])
            c.act_no = int(k1.number_input("第几幕", min_value=1, max_value=9,
                                           value=int(c.act_no), step=1, key=dkey("act_no")))
            c.act_title = k2.text_input("本幕标题（可选）", value=c.act_title,
                                        key=dkey("act_title"), placeholder="如：首次就诊／病理回报")
            c.series = k3.text_input("系列名（同一病例各幕共用）", value=c.series,
                                     key=dkey("act_series"))
            c.act_note = st.text_input("本幕新增信息摘要（可选）", value=c.act_note,
                                       key=dkey("act_note"),
                                       placeholder="如：第二幕补充 24h 尿蛋白 3.2 g 与肾活检结果")
            b1, b2, _b3 = st.columns([1, 1, 2])
            if b1.button("💾 保存本幕存档", key="act_save"):
                p = acts.save_act(c)
                snapshot_case()
                st.success(f"已保存：{p.name}")
                st.rerun()
            if b2.button("＋ 新建下一幕", key="act_next"):
                acts.save_act(c)                    # 先把本幕存好，再开下一幕
                nxt = acts.make_next_act(c)
                st.session_state.case = nxt
                bump_case()
                st.success(f"已新建{acts.act_label(nxt)}：病例原文沿用，问题清空——"
                           "往上追加本幕新信息后，重新整理问题即可。")
                st.rerun()

            saved = acts.list_acts(acts.series_key(c))
            if saved:
                st.markdown(f"**「{acts.series_key(c)}」已有 {len(saved)} 幕存档**")
                for m in saved:
                    is_cur = m["act_no"] == c.act_no
                    r1, r2, r3 = st.columns([3.2, 0.8, 0.8])
                    r1.write(("▶ " if is_cur else "") + f"第{m['act_no']}幕"
                             + (f" · {m['act_title']}" if m["act_title"] else "")
                             + f" — {m['n_questions']} 题 · {m['saved_at'] or '—'}")
                    if r2.button("载入", key=f"act_load_{m['act_no']}", disabled=is_cur):
                        loaded = acts.load_act(m["path"])
                        if loaded is None:
                            st.error("存档解析失败，文件可能被改动过")
                        else:
                            st.session_state.case = loaded
                            bump_case()
                            st.rerun()
                    if r3.button("删除", key=f"act_del_{m['act_no']}"):
                        Path(m["path"]).unlink(missing_ok=True)
                        st.rerun()

                prev = acts.available_prev_acts(c)
                if prev:
                    opts = {m["act_no"]: f"第{m['act_no']}幕"
                            + (f" · {m['act_title']}" if m["act_title"] else "") for m in prev}
                    c.word_prev_acts = st.multiselect(
                        "生成 Word 时，把哪几幕的「问题解答」并入文末（作为附录）",
                        options=list(opts), format_func=lambda n: opts.get(n, f"第{n}幕"),
                        default=[n for n in c.word_prev_acts if n in opts],
                        key=dkey("word_prev_acts"))
            else:
                st.caption("还没有存档——点「💾 保存本幕存档」即可留下第一幕。")
    draft = st.text_area("问题草稿（先据此生成个人问题总结）",
                         value=c.question_draft if c else "", height=120, key=dkey("in_draft"))
    if c is not None:
        c.question_draft = draft

    def _current_case() -> Case:
        base = get_case()
        case = base.model_copy(deep=True) if base else Case()
        case.title = title or (base.title if base else "未命名 PBL 案例")
        case.course, case.audience, case.case_text = course, audience, case_text
        case.question_draft = draft
        return case

    # Stage 1: summarize the draft, independently from the editable question bank.
    st.divider()
    st.subheader("第一步：个人问题总结（基于草稿）")
    st.caption("先归纳草稿覆盖的全部知识主题；后续题库的删减和添加不会改动这份总结。")
    if st.button("🗂 从草稿生成个人问题总结", type="primary", disabled=not draft.strip()):
        case = _current_case()
        with st.spinner("正在从草稿归纳个人问题总结……"):
            ok, msg = pipeline.generate_draft_summary(case, draft)
        if ok:
            st.session_state.case = case
            bump_case()
            st.toast(msg, icon="🗂")
            st.rerun()
        else:
            st.error(msg)

    c = get_case()
    if c is not None:
        if c.draft_summary_nodes:
            st.divider()
            st.subheader("第一步：个人问题总结（基于草稿）")
            if c.draft_summary_is_stale():
                st.warning("草稿或病例已修改，当前总结对应旧输入；请先重新生成总结。")
            sm1, sm2 = st.columns([1, 3])
            if sm1.button("↻ 重新归纳草稿总结", key="draft_summary_refresh",
                          disabled=not draft.strip()):
                case = _current_case()
                with st.spinner("正在更新个人问题总结……"):
                    ok, msg = pipeline.generate_draft_summary(case, draft)
                if ok:
                    st.session_state.case = case
                    bump_case()
                    st.toast(msg, icon="🗂")
                    st.rerun()
                else:
                    st.error(msg)
            sm2.caption(f"{len(c.draft_summary_nodes)} 个一级主题；题库增删不会改变这份总结。")
            with st.expander("查看/编辑个人问题总结（Markdown 缩进，两空格一层）", expanded=True):
                summary_md = pipeline.summary_nodes_to_markdown(c.draft_summary_nodes)
                edited_summary = st.text_area("个人问题总结", value=summary_md, height=260,
                                              key=dkey("draft_summary_md"))
                if st.button("应用总结修改", key="draft_summary_apply"):
                    c.draft_summary_nodes = pipeline.markdown_to_summary_nodes(edited_summary)
                    c.draft_summary_signature = c.draft_summary_source_signature()
                    c.touch()
                    bump_case()
                    st.rerun()

            # Stage 2: generate the full question bank from the independent summary.
            st.divider()
            st.subheader("第二步：依据个人问题总结生成全量问题清单")
            st.caption("题目逐条取自总结叶子，保持总结原文和顺序；仅题号及检索词由系统补充。生成前会自动读取当前编辑框。")
            if st.button("生成/重建全量问题清单", type="primary",
                         disabled=c.draft_summary_is_stale(), key="questions_from_summary"):
                # The widget value is the user's latest summary, while c.draft_summary_nodes
                # may still contain the previous saved version. Apply it before generation.
                if edited_summary.strip() != summary_md.strip():
                    c.draft_summary_nodes = pipeline.markdown_to_summary_nodes(edited_summary)
                    c.draft_summary_signature = c.draft_summary_source_signature()
                with st.spinner("正在按总结叶子原文生成问题清单……"):
                    ok, msg = pipeline.generate_questions_from_draft_summary(c)
                if ok:
                    if llm.has_key():
                        try:
                            pipeline.fill_question_keywords(c)
                        except Exception:
                            pass
                    c.touch()
                    bump_case()
                    st.toast(msg, icon="✅")
                    st.rerun()
                else:
                    st.error(msg)
        else:
            st.info("先填写问题草稿并生成个人问题总结，再生成全量问题清单。")

        # Stage 3: the editable bank is independent from the draft summary.
        st.divider()
        st.subheader(f"第三步：全量问题清单（{len(c.questions)} 个）")
        for i, q in enumerate(c.questions):
            with st.container(border=True):
                col1, col2, col3, col4 = st.columns([5, 2, 1, 1])
                q.text = col1.text_input("问题", value=q.text, key=dkey(f"q_{q.qid}_text"))
                q.qtype = col2.selectbox("类型", QUESTION_TYPES,
                                         index=QUESTION_TYPES.index(q.qtype)
                                         if q.qtype in QUESTION_TYPES else len(QUESTION_TYPES) - 1,
                                         key=dkey(f"q_{q.qid}_type"))
                if col3.button("↑", key=f"q_{q.qid}_up", disabled=i == 0):
                    c.questions[i - 1], c.questions[i] = c.questions[i], c.questions[i - 1]
                    c.rename_qids()
                    bump_case()
                    st.rerun()
                if col4.button("✕", key=f"q_{q.qid}_del"):
                    c.questions.pop(i)
                    c.rename_qids()
                    bump_case()
                    st.rerun()
                ec1, ec2 = st.columns(2)
                q.en_keywords = ec1.text_input("PubMed 英文检索词", value=q.en_keywords,
                                               key=dkey(f"q_{q.qid}_en"))
                q.cn_keywords = ec2.text_input("知网中文检索词（复制去知网检索）", value=q.cn_keywords,
                                               key=dkey(f"q_{q.qid}_cn"))
        if st.button("＋ 添加问题", key="question_add"):
            new_q = Question(qid=f"Q{len(c.questions) + 1}", text="新问题")
            c.questions.append(new_q)
            c.answer_question_ids = list(c.answer_question_ids or []) + [new_q.uid]
            c.rename_qids()
            c.touch()
            bump_case()
            st.rerun()

        st.divider()
        st.subheader("实际作答题")
        st.caption("个人问题总结和全量问题清单保持不变；此选择仅控制批量生成和 Word 答案。默认全选。")
        selected_ids = set(c.answer_question_ids or [])
        selected_qids = st.multiselect(
            "选择需要生成/导出答案的问题",
            options=[q.uid for q in c.questions],
            default=[q.uid for q in c.questions if q.uid in selected_ids],
            format_func=lambda uid: next((f"{q.qid}：{q.text}" for q in c.questions if q.uid == uid), uid),
            key=dkey("answer_question_ids")) if c.questions else []
        c.answer_question_ids = selected_qids
        sc1, sc2, _sc3 = st.columns([1, 1, 4])
        if sc1.button("全选", key="answer_select_all", disabled=len(selected_qids) == len(c.questions)):
            c.answer_question_ids = [q.uid for q in c.questions]
            bump_case(); st.rerun()
        if sc2.button("清空", key="answer_select_none", disabled=not selected_qids):
            c.answer_question_ids = []
            bump_case(); st.rerun()
        st.caption(f"已选 {len(selected_qids)} / {len(c.questions)} 题作答。未选题仍在全量题库和个人总结中，但不会出现在②内容生成区或 Word 答案中。")

        st.divider()
        kw_str = st.text_input("关键词（填入模板「关键词」栏，；分隔）",
                               value="；".join(c.keywords), key=dkey("in_kw"))
        c.keywords = [x.strip() for x in re.split(r"[;；,，]", kw_str) if x.strip()]
        if st.button("🧠 AI 提炼关键词", disabled=not llm.has_key()):
            with st.spinner("提炼中……"):
                c.keywords = pipeline.generate_keywords(c)
            bump_case()
            st.rerun()

# ================================================================ ② 内容生成
with tab2:
    if need_case():
        st.stop()
    c = get_case()
    if c.questions and not pipeline.selected_questions(c):
        st.warning("当前没有选择实际作答题；请回到①区点击“全选”或勾选具体题目。")

    # 上次批量生成的结果汇总（失败/注意事项）
    report = st.session_state.pop("gen_report", None)
    if report:
        if report["fails"]:
            st.error("以下问题**生成失败**（内容保留原样，可在下方逐题重试）：\n\n"
                     + "\n".join(f"- {x}" for x in report["fails"]))
        if report["warns"]:
            st.warning("部分问题有注意信息：\n\n"
                       + "\n".join(f"- {x}" for x in report["warns"]))
    if len(c.case_text) > pipeline.CASE_TEXT_LIMIT:
        st.warning(f"病例较长（{len(c.case_text)} 字）：生成时只取前 {pipeline.CASE_TEXT_LIMIT} 字。"
                   "请精简病例，或把关键检验/用药信息前移，否则模型可能看不到后半段内容。")

    _REF_DOC_TYPES = {"all": "全部类型",
                      "research": "只要研究型（排除综述/指南/社论/病例报道）",
                      "review": "只要综述/荟萃分析"}
    _rd_keys = list(_REF_DOC_TYPES)
    _rd_vals = list(_REF_DOC_TYPES.values())
    if c.ref_doc_type not in _rd_keys:
        c.ref_doc_type = "all"
    r0, r1 = st.columns([2, 3])
    _rd_pick = r0.selectbox(
        "学习内容参考文献类型", _rd_vals,
        index=_rd_keys.index(c.ref_doc_type), key=dkey("ref_doc_type"),
        help="只影响 ② 生成/重生成学习内容时从 PubMed 取哪类文献；"
             "④ 的「文献类型」只筛选文献分享的候选文献。")
    c.ref_doc_type = _rd_keys[_rd_vals.index(_rd_pick)]
    r1.caption("改了类型后，点「检索生成」或逐题「重新生成学习内容」才会重新检索文献；"
               "在修改指令里写「综述」也会自动按综述检索。")

    # 内容生成模型（对 ② 区所有生成/重写/审校/润色生效，直到改选或离开应用）
    m1, m2 = st.columns([2, 3])
    _mm_pick = m1.selectbox(
        "内容生成模型",
        ["DeepSeek V4.1 Flash Ultrafast（极速，默认）", "DeepSeek V4.1 Flash（均衡）",
         "DeepSeek V4 Pro（质量优先，慢）", "智谱 GLM-4.6", "当前默认模型（.env）"],
        index=0, key=dkey("gen_model"),
        help="实测同题生成：Ultrafast 约 10~15 秒/题、Flash 约 1 分钟、V4 Pro 约 2~4 分钟"
             "（其思考 token 不回传但耗时计费）。质量硬门槛提示词对全部模型生效；"
             "特别难的题可切 V4 Pro。")
    m2.caption("当前生效：" + llm.active_model_label())
    _gen_map = {"DeepSeek V4.1 Flash Ultrafast（极速，默认）":
                    ("deepseek", "deepseek/deepseek-v4.1-flash-ultrafast"),
                "DeepSeek V4.1 Flash（均衡）": ("deepseek", "deepseek/deepseek-v4.1-flash"),
                "DeepSeek V4 Pro（质量优先，慢）": ("deepseek", "deepseek/deepseek-v4-pro"),
                "智谱 GLM-4.6": ("zhipu", "glm-4.6"),
                "当前默认模型（.env）": (None, None)}
    llm.set_active_model(*_gen_map[_mm_pick])

    colA, colB, colC = st.columns([2, 1, 1])
    colA.markdown("每个字段可编辑或**带指令重新生成**；右上选生成模式。")

    def _gen_all(gen_one) -> None:
        """并发逐题生成（默认 3 路，PBL_GEN_WORKERS 可调）：单题失败不中断整批；
        失败/注意汇总在生成后展示。"""
        targets = pipeline.selected_questions(c)
        if not targets:
            st.warning("请先在 ① 区选择至少一道实际作答题。")
            return
        import os as _os
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            workers = int(_os.getenv("PBL_GEN_WORKERS", "3"))
        except ValueError:
            workers = 3
        workers = max(1, min(4, workers, len(targets)))
        fails: list[str] = []
        warns: list[str] = []
        prog = st.progress(0.0, text=f"开始生成（{workers} 路并发）……")
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(gen_one, q): q for q in targets}
            for fut in as_completed(futs):
                q = futs[fut]
                done += 1
                try:
                    ok, msg = fut.result()
                    if not ok:
                        fails.append(f"{q.qid}：{msg}")
                    elif msg:
                        warns.append(f"{q.qid}：{msg}")
                except Exception as e:
                    fails.append(f"{q.qid}：{type(e).__name__} {e}")
                prog.progress(done / len(targets),
                              text=f"已完成 {done}/{len(targets)}（{q.qid}）")
        if not c.keywords:
            c.keywords = pipeline.generate_keywords(c)
        c.touch(); bump_case()
        if fails or warns:
            st.session_state["gen_report"] = {"fails": fails, "warns": warns}
        st.rerun()

    if colB.button("🚀 检索生成", type="primary", width="stretch",
                   disabled=not pipeline.selected_questions(c),
                   help="严格依据教材检索结果生成已选题的学习内容 → 单独生成临床应用；引用标注自动转论文格式。"):
        def _rag(q):
            ok, msg = pipeline.generate_question(c, q)
            if not ok:
                return ok, msg
            return pipeline.generate_clinical(c, q)
        _gen_all(_rag)
    if colC.button("💬 直接生成＋审校", width="stretch",
                   disabled=not pipeline.selected_questions(c),
                   help="模型先凭医学知识作答已选题的学习内容 → 审校智能体对照教材修订补出处 → 单独生成临床应用。"):
        def _direct(q):
            ok, msg = pipeline.generate_question_direct(c, q)
            if not ok:
                return ok, msg
            notes = [msg] if msg else []
            review_note = pipeline.review_question(c, q)
            if review_note.startswith("（"):
                notes.append(f"审校未生效：{review_note}")
            ok2, msg2 = pipeline.generate_clinical(c, q)
            if msg2:
                notes.append(msg2)
            return ok2, "；".join(notes)
        _gen_all(_direct)

    nat1, _ = st.columns([1, 3])
    if nat1.button("🖋 全部自然化润色（降低 AI 文风）", width="stretch",
                   help="对已选作答题做语言层重写：去套话、句长错落、术语统一；数据与引用标注不动。"
                        "并发润色（PBL_GEN_WORKERS 可调）；每题最多两次生成——改写度不足或引用标注不符会重来"):
        targets = pipeline.selected_questions(c)
        if not targets:
            st.warning("请先在 ① 区选择至少一道实际作答题。")
            st.stop()
        import os as _os2
        from concurrent.futures import ThreadPoolExecutor, as_completed
        try:
            _w2 = int(_os2.getenv("PBL_GEN_WORKERS", "3"))
        except ValueError:
            _w2 = 3
        _w2 = max(1, min(4, _w2, len(targets)))
        prog2 = st.progress(0.0, text=f"润色中（{_w2} 路并发）……")
        ok_n = fail_n = 0
        _done2 = 0
        with ThreadPoolExecutor(max_workers=_w2) as ex2:
            futs2 = {ex2.submit(pipeline.naturalize_question, c, q): q for q in targets}
            for fut2 in as_completed(futs2):
                _done2 += 1
                q2 = futs2[fut2]
                try:
                    okk, _m = fut2.result()
                except Exception:
                    okk = False
                ok_n += int(bool(okk))
                fail_n += int(not okk)
                prog2.progress(_done2 / len(targets),
                               text=f"已完成 {_done2}/{len(targets)}（{q2.qid}）")
        c.touch(); bump_case()
        st.success(f"润色完成：成功 {ok_n} 题" + (f"，失败 {fail_n} 题" if fail_n else ""))
        st.rerun()

    for q in pipeline.ordered_questions(c, selected_only=True):
        with st.container(border=True):
            st.markdown(f"### {q.qid}　{q.text}")
            st.caption(f"类型：{q.qtype}")
            if q.review_note:
                st.caption(f"🕵️ 上次审校：{q.review_note}")

            # ---- 学习内容
            st.markdown("**📖 学习内容**")
            for j, blk in enumerate(q.learning):
                b1, b2 = st.columns([1, 2])
                blk.heading = b1.text_input("小标题", value=blk.heading, key=dkey(f"q_{q.qid}_lh_{j}"))
                blk.source = b2.text_input("教材出处", value=blk.source, key=dkey(f"q_{q.qid}_ls_{j}"))
                blk.body = st.text_area("正文", value=blk.body, height=140, key=dkey(f"q_{q.qid}_lb_{j}"))
                if blk.table:
                    t_md = st.text_area(f"表格（Markdown，首行表头；删除全部内容即去掉表格）",
                                        value=pipeline.to_md_table(blk.table),
                                        height=120, key=dkey(f"q_{q.qid}_lt_{j}"))
                    blk.table = pipeline.parse_md_table(t_md)
            ci1, ci2, _ = st.columns([1, 3, 2])
            inst1 = ci1.text_input("修改指令（可选）", placeholder="如：补充药理机制", key=dkey(f"q_{q.qid}_inst_learning"))
            if ci2.button("♻️ 重新生成学习内容", key=f"q_{q.qid}_reg_learning"):
                with st.spinner("生成中……"):
                    ok = pipeline.regenerate_field(c, q.qid, "learning", inst1)
                if ok:
                    c.touch(); bump_case()
                    st.rerun()
                else:
                    st.error("生成失败（检查 API Key / 网络）")

            # ---- 参考文献
            st.markdown("**📚 参考文献**（PubMed 自动检索 + 手动导入）")
            if q.citation_warning:
                st.warning(q.citation_warning)
            _rd_label = {"all": "全部类型", "research": "研究型",
                         "review": "综述/荟萃分析"}.get(c.ref_doc_type, "全部类型")
            if st.button(f"🔎 {q.qid} 重新检索文献（{_rd_label}）", key=f"q_{q.qid}_ref_search",
                         help="按 ② 顶部选择的文献类型重新检索 PubMed，只补充参考文献表和 [n] 标注，"
                              "不改写学习内容"):
                with st.spinner("检索 PubMed……"):
                    n, msg = pipeline.refresh_references(c, q)
                if n:
                    c.touch(); bump_case()
                    st.toast(f"{q.qid} 已补充 {n} 篇文献", icon="✅")
                    st.rerun()
                else:
                    st.warning(msg or "没有新增文献")
            if not q.references:
                st.caption("暂无文献。重新生成整题、或手动添加中文文献。")
            for j, ref in enumerate(q.references):
                r1, r2 = st.columns([9, 1])
                r1.markdown(f"[{j + 1}] {pubmed.format_reference(ref)}"
                            + (f"　<span style='color:gray'>↳ {ref.relevance}</span>" if ref.relevance else ""),
                            unsafe_allow_html=True)
                if r2.button("✕", key=f"q_{q.qid}_rdel_{j}"):
                    _old_refs = list(q.references)
                    q.references.pop(j)
                    pipeline.renumber_citations(q, _old_refs)
                    c.touch(); st.rerun()
            with st.expander("＋ 手动添加文献（知网检索结果 / 书籍）"):
                m1, m2 = st.columns(2)
                m_type = m1.selectbox("类型", ["期刊[J]", "书籍[M]"], key=f"q_{q.qid}_m_type")
                m_title = m1.text_input("标题 / 书名", key=f"q_{q.qid}_m_title")
                m_authors = m1.text_input("作者（逗号分隔）", key=f"q_{q.qid}_m_au")
                m_journal = m2.text_input("期刊（类型=期刊时填）", key=f"q_{q.qid}_m_jour")
                m_year = m2.text_input("年份", key=f"q_{q.qid}_m_year")
                m_place = m_pub = m_ed = m_pages = ""
                if m_type == "书籍[M]":
                    n1, n2, n3 = st.columns(3)
                    m_place = n1.text_input("出版地", value="北京", key=f"q_{q.qid}_m_place")
                    m_pub = n2.text_input("出版社", value="人民卫生出版社", key=f"q_{q.qid}_m_pub")
                    m_ed = n3.text_input("版次（如 10版）", key=f"q_{q.qid}_m_ed")
                m_pages = st.text_input("页码（期刊如 1007-1015；书籍如 125-128）",
                                        key=f"q_{q.qid}_m_pages")
                if st.button("添加这条文献", key=f"q_{q.qid}_m_add"):
                    if not m_title.strip():
                        st.error("请先填写标题 / 书名。")
                    elif len(q.references) >= pipeline.MAX_REFS:
                        st.error(f"参考文献已达上限 {pipeline.MAX_REFS} 条——请先删除不需要的条目再添加。")
                    else:
                        q.references.append(Reference(
                            ref_type="M" if m_type == "书籍[M]" else "J",
                            title=m_title, authors=[a.strip() for a in m_authors.split(",") if a.strip()],
                            journal=m_journal, place=m_place, publisher=m_pub, edition=m_ed,
                            year=m_year, pages=m_pages, source=REF_SOURCE_MANUAL))
                        c.touch(); st.rerun()

            # ---- 临床应用
            st.markdown("**🩺 临床应用**")
            st.caption("AI 基于学习内容与病例生成**初稿**，可直接在下方编辑成自己的表述；"
                       "也可用修改指令让 AI 重写。")
            q.clinical = st.text_area("结合病例的分析", value=q.clinical, height=140,
                                      key=dkey(f"q_{q.qid}_clin"))
            cc1, cc2, _ = st.columns([1, 3, 2])
            inst2 = cc1.text_input("修改指令（可选）", placeholder="如：结合患者用药方案展开",
                                   key=dkey(f"q_{q.qid}_inst_clinical"))
            if cc2.button("🩺 生成/重写临床应用", key=f"q_{q.qid}_reg_clinical",
                          help="把病例+问题+学习内容交给模型撰写，将知识与病例联系起来"):
                if not q.learning:
                    st.error("该问题还没有学习内容，请先在上方生成或填写学习内容。")
                else:
                    try:
                        with st.spinner("正在生成临床应用……"):
                            ok, msg = pipeline.generate_clinical(c, q, inst2)
                        if ok:
                            c.touch(); bump_case()
                            st.toast(msg, icon="✅")
                            st.rerun()
                        else:
                            st.error(f"生成失败：{msg}")
                    except Exception as e:
                        st.error(f"生成出错：{type(e).__name__}: {e}（详见 logs/pbl.log）")

            # ---- 智能体审校
            rv1, rv2, _ = st.columns([1, 3, 2])
            if rv1.button("🕵️ 智能体审校修订", key=f"q_{q.qid}_review",
                          help="审校智能体对照教材检索结果核对修订本问题内容，并补充教材出处"):
                with st.spinner("审校中……"):
                    note = pipeline.review_question(c, q)
                c.touch(); bump_case()
                st.rerun()
            if rv2.button("🖋 自然化润色", key=f"q_{q.qid}_nat",
                          help="语言层重写：去机器文风套话、句长有落差、术语统一；数据与 [n] 引用标注一律不动"):
                with st.spinner("润色中……"):
                    ok_nat, msg_nat = pipeline.naturalize_question(c, q)
                if ok_nat:
                    c.touch(); bump_case()
                    st.toast(msg_nat, icon="🖋")
                    st.rerun()
                else:
                    st.error(msg_nat)

# ================================================================ ③ 思维导图
with tab3:
    if need_case():
        st.stop()
    c = get_case()
    st.subheader("疾病中心知识导图")
    st.caption("PBL 病例跨多次课：建议**最后一节课结束后**再生成。以疾病为中心组织知识框架，"
               "**并把本病例的具体内容就地标黄**（不单列「结合病例」分支）。"
               "可直接编辑 Markdown；导图默认不进 Word，单独导出（.xmind 可在 XMind 中继续编辑）。")

    # ---- 老师要求与一级分支框架（每位老师要求不同）
    with st.expander("🎯 老师要求与一级分支框架（每位老师不一样，改这里）", expanded=False):
        st.caption("**一级分支一行一个**，顺序即导图顺序。老师要求不单列的分支（如治疗、病因学）"
                   "直接删掉即可——模型会把它要讲的内容并入最贴合的上级/相邻分支，不留空壳。")
        br_text = st.text_area("一级分支（一行一个）",
                               value="\n".join(c.mm_branches or pipeline.DEFAULT_MM_BRANCHES),
                               height=150, key=dkey("mm_branches"))
        c.mm_branches = [x.strip() for x in br_text.splitlines() if x.strip()]
        req_text = st.text_area("老师对导图的其他要求（可为空）",
                                value=c.mm_requirement, height=110, key=dkey("mm_req"),
                                placeholder="如：不单列治疗，并入诊断与鉴别；病因学要含诱因、危险因素；"
                                            "每个分支不超过 4 个要点；必须保留预后与预防；"
                                            "本病例的检验结果要写具体数值……")
        c.mm_requirement = req_text
        _CONN = {"xmind": "XMind 默认（照 XMind 内置 Dawn 主题：黑字中心无底框、实色一级块黑字、圆角肘形连线）",
                 "spine": "主干式（竖线 + 短横桩）",
                 "curve": "曲线（每个分点各一条等宽弧线）",
                 "straight": "直线",
                 "none": "不画连线（纯缩进）"}
        _ck = st.radio("连线样式", options=list(_CONN), format_func=lambda k: _CONN[k],
                       index=list(_CONN).index(c.mm_connector) if c.mm_connector in _CONN else 0,
                       horizontal=True, key=dkey("mm_conn"))
        c.mm_connector = _ck
        st.caption("默认框架：病因学（病因、诱因、危险因素）｜发病机制｜诊断及鉴别诊断｜治疗｜预后。"
                   "**「预防」默认不单列**（内容落到治疗的二级预防/患者教育与病因学的诱因控制里）；"
                   "需要单列就自己加一行。**病例内容不单列**，由模型就地用 `==…==` 标注（渲染为黄底+下划线）。")

    # ---- 补充资料（多文档）
    with st.expander("📎 补充资料（可选：上传 Word / PDF，导图将结合这些资料生成）"):
        mats = textbook.list_materials()
        if mats:
            st.write(f"已上传 {len(mats)} 份：", "、".join(mats))
            if st.button("清空补充资料", key="mat_clear"):
                for name in mats:
                    try:
                        (textbook.MATERIALS_DIR / name).unlink()
                    except OSError:
                        pass
                bump_case()
                st.rerun()
        ups = st.file_uploader("上传资料（可多选，支持 docx / pdf / txt）",
                               type=["docx", "pdf", "txt", "md"], accept_multiple_files=True,
                               key="mat_up")
        if ups and st.button("保存资料", key="mat_save"):
            textbook.MATERIALS_DIR.mkdir(parents=True, exist_ok=True)
            for up in ups:
                (textbook.MATERIALS_DIR / up.name).write_bytes(up.getvalue())
            st.success(f"已保存 {len(ups)} 份资料")
            st.rerun()
        if mats:
            with st.expander("预览提取文本（前 800 字）", expanded=False):
                st.text(textbook.extract_materials_text()[:800] or "（未能提取文本，扫描版 PDF 不支持）")

    mm_choice = st.selectbox(
        "导图生成模型",
        ["DeepSeek V4 Pro（质量优先）", "DeepSeek V4.1 Flash（快）",
         "DeepSeek V4.1 Flash Ultrafast（极速）", "智谱 GLM-4.6", "当前默认模型（.env）"],
        index=0, key=dkey("mm_model"),
        help="总结整合类任务知识密度高，推荐 DeepSeek V4 Pro；赶时间可用 Ultrafast。")
    _mm_map = {"DeepSeek V4 Pro（质量优先）": ("deepseek", "deepseek/deepseek-v4-pro"),
               "DeepSeek V4.1 Flash（快）": ("deepseek", "deepseek/deepseek-v4.1-flash"),
               "DeepSeek V4.1 Flash Ultrafast（极速）":
                   ("deepseek", "deepseek/deepseek-v4.1-flash-ultrafast"),
               "智谱 GLM-4.6": ("zhipu", "glm-4.6"),
               "当前默认模型（.env）": (None, None)}

    def _gen_mm():
        mats_text = textbook.extract_materials_text() if textbook.list_materials() else ""
        if mats_text:
            st.caption(f"本次生成结合了 {len(textbook.list_materials())} 份补充资料")
        pv, md = _mm_map[mm_choice]
        return pipeline.generate_disease_mindmap(
            c, materials_text=mats_text,
            requirement=c.mm_requirement,
            branches=c.mm_branches or pipeline.DEFAULT_MM_BRANCHES,
            provider=pv, model=md)

    if c.disease_mindmap is None:
        if st.button("🧠 AI 生成疾病导图", type="primary"):
            with st.spinner("正在汇总全部问题的学习内容、病例与补充资料……"):
                c.disease_mindmap = _gen_mm()
            c.touch(); bump_case()
            st.rerun()
        if not llm.has_key():
            st.caption("未配置 API Key：将按你的分支框架生成演示骨架导图。")
        st.info("尚未生成。AI 会按你设定的分支框架，汇总所有问题的学习内容、病例与补充资料，"
                "并把本病例特有的表现/结果就地标黄。")
    else:
        root = c.disease_mindmap
        n_case = mindmap.count_case_nodes(root)
        g1, g2 = st.columns([1, 3])
        g1.markdown(f"**中心：{root.text}**")
        g1.caption(f"病例内容已标黄 **{n_case}** 处")
        if g2.button("🔄 重新生成", key="dm_regen"):
            with st.spinner("生成中……"):
                c.disease_mindmap = _gen_mm()
            c.touch(); bump_case()
            st.rerun()
        if n_case == 0:
            st.caption("⚠️ 这次没标出病例内容。可以在下面 Markdown 里给本病例特有的要点加"
                       "`==…==`（如 `- ==本例：尿蛋白 2+，血压 160/100==`），应用后即变黄。")
        md = st.text_area("导图内容（Markdown，两空格一层；`==要点==` 表示病例内容，会标黄）",
                          value=mindmap.node_to_markdown(root), height=320, key=dkey("dm_md"))
        if st.button("✅ 应用修改", key="dm_apply"):
            c.disease_mindmap = mindmap.markdown_to_node(md, root_text=root.text)
            c.touch()
            st.rerun()
        # 出图方式：内置绘制（可调连线样式）／浏览器渲染（markmap，d3 真曲线）
        _MMMODE = {"builtin": "内置绘制（按上面选的连线样式）",
                   "browser": "浏览器渲染（markmap，d3 曲线，最像导图软件）"}
        if mindmap.find_browser() is None:
            _MMMODE = {k: v for k, v in _MMMODE.items() if k != "browser"}
        if len(_MMMODE) > 1:
            _mm_mode = st.radio("PNG 出图方式", options=list(_MMMODE),
                                format_func=lambda k: _MMMODE[k], horizontal=True,
                                key=dkey("mm_mode"))
        else:
            _mm_mode = "builtin"
        try:
            pv = OUT / "_pv_disease.png"
            _png = None
            if _mm_mode == "browser":
                with st.spinner("正在用浏览器渲染导图……"):
                    _png = mindmap.render_png_browser(c.disease_mindmap, str(pv))
                if not _png:
                    st.caption("⚠️ 浏览器渲染失败（可能未联网加载 CDN），已退回内置绘制。")
            if not _png:
                _png = mindmap.render_png(c.disease_mindmap, str(pv), connector=c.mm_connector)
            st.image(_png, width="stretch")
            d1, d2, d3, d4 = st.columns(4)
            d1.download_button("⬇️ 下载 PNG", data=Path(_png).read_bytes(),
                               file_name=f"{root.text}_知识导图.png", mime="image/png")
            d2.download_button("⬇️ 下载 .xmind（XMind 直接打开/编辑）",
                               data=mindmap.node_to_xmind(c.disease_mindmap, root.text),
                               file_name=f"{root.text}_知识导图.xmind",
                               mime="application/octet-stream")
            d3.download_button("⬇️ 下载交互 HTML",
                               data=mindmap.node_to_markmap_html(c.disease_mindmap).encode("utf-8"),
                               file_name=f"{root.text}_知识导图.html", mime="text/html")
            d4.download_button("⬇️ 下载 FreeMind .mm",
                               data=mindmap.node_to_freemind(c.disease_mindmap).encode("utf-8"),
                               file_name=f"{root.text}_知识导图.mm", mime="text/xml")
        except Exception as e:
            st.warning(f"导图预览失败：{e}")

# ================================================================ ④ 文献检索
with tab4:
    if need_case():
        st.stop()
    c = get_case()

    st.subheader("文献检索（先检索文献，再去 ⑤ 导出 Word）")
    st.caption("检索 PubMed **真实文献**，可按年份范围、影响因子范围、文献类型（研究型 / 综述）筛选。"
               "此处的「文献类型」只筛选**文献分享**的候选；学习内容引用的文献类型请在 ② 设置。"
               "选定一篇后生成分享内容：**⑤ 的 Word 自动带上「文献分享」章节**，"
               "⑥ 可导出单页速览 PPT。")

    flt = c.ppt.lit_filter
    with st.expander("🔎 检索条件与提示词", expanded=True):
        f1, f2, f3 = st.columns([2.2, 1.2, 1.2])
        _YMODES = {"recent": "近 N 年", "range": "自定义年份区间", "all": "不限年份"}
        _ylist = list(_YMODES.values())
        _ykeys = list(_YMODES.keys())
        ypick = f1.radio("时间范围", _ylist, horizontal=True,
                         index=_ykeys.index(flt.year_mode) if flt.year_mode in _ykeys else 0,
                         key=dkey("lit_ymode"))
        flt.year_mode = _ykeys[_ylist.index(ypick)]
        import datetime as _dt
        _this_year = _dt.date.today().year
        if flt.year_mode == "recent":
            flt.recent_years = int(f2.number_input(
                "近 N 年", min_value=1, max_value=60, value=int(flt.recent_years or 10),
                step=1, key=dkey("lit_years")))
            f3.caption("")
        elif flt.year_mode == "range":
            flt.year_from = int(f2.number_input(
                "起始年", min_value=1900, max_value=_this_year,
                value=int(flt.year_from or _this_year - 10), step=1, key=dkey("lit_yfrom")))
            flt.year_to = int(f3.number_input(
                "结束年", min_value=1900, max_value=_this_year,
                value=int(flt.year_to or _this_year), step=1, key=dkey("lit_yto")))
        else:
            f2.caption("不限年份（库内全部）")

        g1, g2, g3 = st.columns([1.2, 1.2, 2.2])
        flt.if_min = float(g1.number_input("影响因子下限（0=不限）", min_value=0.0,
                                           max_value=200.0, value=float(flt.if_min),
                                           step=0.5, key=dkey("lit_ifmin")))
        flt.if_max = float(g2.number_input("影响因子上限（0=不限）", min_value=0.0,
                                           max_value=200.0, value=float(flt.if_max),
                                           step=0.5, key=dkey("lit_ifmax")))
        _DTYPES = {"research": "只要研究型（排除综述/指南/社论/病例报道）",
                   "review": "只要综述/荟萃分析",
                   "all": "全部类型"}
        _dlist = list(_DTYPES.values())
        _dkeys = list(_DTYPES.keys())
        dpick = g3.radio("文献类型", _dlist, index=_dkeys.index(flt.doc_type)
                         if flt.doc_type in _dkeys else 0, key=dkey("lit_doctype"))

        def _doc_type_of(label: str) -> str:
            """取下标再映射，避免用选项文案反查键（文案里含有其他选项的字样）。"""
            return _dkeys[_dlist.index(label)] if label in _dlist else "research"

        flt.doc_type = _doc_type_of(dpick)
        if flt.if_min and flt.if_max and flt.if_min > flt.if_max:
            st.warning("影响因子下限大于上限，请调整。")
        st.caption("⚠️ 设了影响因子下限时，未收录进内置影响因子表的期刊会被一并排除（其 IF 记为 0）。")

        h1, h2, h3 = st.columns([1.6, 3.0, 1.0])
        _dirs = ["发病机制", "诊断", "治疗", "预后", "流行病学", "其他"]
        direction = h1.selectbox("研究方向", _dirs,
                                 index=_dirs.index(c.ppt.direction)
                                 if c.ppt.direction in _dirs else 0, key=dkey("lit_dir"))
        prompt = h2.text_input("提示词（可写中文，系统自动转英文检索式）", value=c.ppt.lit_prompt,
                               key=dkey("lit_prompt"),
                               placeholder="如：IgA肾病 系膜细胞 半乳糖缺陷 IgA1")
        flt.oa_only = st.checkbox(
            "只检索可自动取图的开放获取文献（保证速览页有原文图片，推荐）",
            value=flt.oa_only, key=dkey("lit_oa_only"))
        st.caption(f"本次将检索：**{flt.window()[2]}** · **{flt.if_text()}** · "
                   f"**{_DTYPES[flt.doc_type]}**" + (" · 仅开放获取" if flt.oa_only else ""))

        if h3.button("🔎 检索文献", type="primary"):
            with st.spinner("正在生成检索式并检索……"):
                query = literature.build_query(c, direction, prompt)
                raw = literature.search_research(query, retmax=30, flt=flt)
                if not raw and flt.oa_only:
                    st.warning("开放获取限定下没有命中，已自动放宽为全部同类文献。")
                    flt.oa_only = False
                    raw = literature.search_research(query, retmax=30, flt=flt)
                cands = [LitCandidate(**{k: v for k, v in r.items()
                                         if k in LitCandidate.model_fields}) for r in raw]
                with st.spinner(f"检索到 {len(cands)} 篇，正在生成简介与创新点……"):
                    literature.annotate_candidates(c, cands, direction)
            c.ppt.direction = direction
            c.ppt.lit_prompt = prompt
            c.ppt.candidates = cands
            bump_case()
            if not cands:
                st.warning("没有检索到符合条件的文献，请放宽筛选条件或调整提示词后重试。")
            st.rerun()

    # ---- 手动导入：粘贴引用格式 + 上传 PDF
    with st.expander("＋ 手动导入文献（粘贴引用格式 + 上传 PDF）", expanded=False):
        st.caption("粘贴 GB/T 7714 格式引用（或 DOI / 任意格式），上传文献 PDF，"
                   "系统读取正文和图表自动生成分享内容；不需要通过 PubMed 检索。")
        manual_ref = st.text_area("引用格式（GB/T 7714 / DOI / 任意格式）",
                                  value="", height=100, key=dkey("manual_ref"),
                                  placeholder="Si M, Fu J, et al. Lysosome-mediated aggregation…[J]. Nature communications, 2025. DOI: 10.1038/…")
        manual_pdf = st.file_uploader("上传文献 PDF（可选；上传后自动提取关键图和正文）",
                                      type=["pdf"], key=dkey("manual_pdf"))
        if st.button("📎 生成文献分享内容", type="primary", key="manual_lit_gen",
                     disabled=not manual_ref.strip()):
            pdf_bytes = manual_pdf.getvalue() if manual_pdf else None
            fname = manual_pdf.name if manual_pdf else ""
            with st.spinner("正在解析引用、提取图表并生成解读……"):
                paper, note = literature.build_paper_share_from_manual(
                    c, manual_ref, pdf_bytes, fname)
            if paper is None:
                st.error(note or "生成失败，请重试")
            else:
                c.ppt.paper = paper
                bump_case()
                if note:
                    st.warning(note)
                st.rerun()

    cands = c.ppt.candidates
    if cands:
        st.markdown(f"**候选文献（{len(cands)} 篇，按影响因子排序）**——选一篇生成分享内容")
        for i, cand in enumerate(cands, 1):
            with st.container(border=True):
                st.markdown(f"**{i}. {cand.title_cn or cand.title}**")
                tag = "可提取图 ✓" if cand.oa else "本文暂不支持自动取图"
                ifv = f"IF≈{cand.if_value:.1f}" if cand.if_value else "IF 未收录"
                types = "/".join(t for t in cand.pub_types[:2]) or "—"
                st.caption(f"{cand.journal} · {cand.year} · {ifv} · {types} · {tag}")
                if cand.intro:
                    st.markdown(f"**简介**：{cand.intro}")
                if cand.innovation:
                    st.markdown(f"**创新点**：{cand.innovation}")
                if st.button("📌 选这篇，生成分享内容", key=f"lit_pick_{i}"):
                    with st.spinner("正在提取关键图并生成解读（约 1~2 分钟）……"):
                        paper, note = literature.build_paper_share(c, cand)
                    if paper is None:
                        st.error(note or "生成失败，请重试")
                    else:
                        c.ppt.paper = paper
                        bump_case()
                        if note:
                            st.warning(note)
                        st.rerun()

    paper = c.ppt.paper
    if paper is not None:
        st.divider()
        st.markdown(f"### ✏️ 文献分享内容：{paper.title_cn}")
        st.caption("研究过程会被画成 PPT 底部的 **A→B→C→D 流程条**，也写进 Word 的「文献分享」。"
                   "可逐项修改，或输入提示词让 AI 重写。")
        p1, p2 = st.columns([3, 2])
        paper.title_cn = p1.text_input("论文中文标题（PPT 页首 / Word 标题）",
                                      value=paper.title_cn, key=dkey("paper_title"))
        paper.section_label = p2.text_input("小标签", value=paper.section_label,
                                            key=dkey("paper_label"))
        st.markdown("**研究过程（A→B→C→D，每步 16~26 字）**")
        _cur_steps = list(paper.steps or [])
        while len(_cur_steps) < 4:
            _cur_steps.append({"tag": "ABCD"[len(_cur_steps)], "text": ""})
        _new_steps = []
        _scols = st.columns(4)
        for _si, _s in enumerate(_cur_steps[:4]):
            _tag = _s.get("tag") or "ABCD"[_si]
            _val = _scols[_si].text_input(f"{_tag}. 步骤", value=str(_s.get("text", "")),
                                          key=dkey(f"step_{_si}"))
            if _val.strip():
                _new_steps.append({"tag": _tag, "text": _val.strip()})
        paper.steps = _new_steps
        c1, c2 = st.columns(2)
        paper.conclusion = c1.text_area("核心结论（PPT 左下框）", value=paper.conclusion,
                                        height=90, key=dkey("paper_cc"))
        paper.case_link = c2.text_area("与病例的联系（PPT 右下框）", value=paper.case_link,
                                       height=90, key=dkey("paper_link"))
        with st.expander("研究思路与过程（写进 Word 的整段文字）／局限性与背景", expanded=False):
            paper.approach = st.text_area("研究思路与过程", value=paper.approach,
                                          height=110, key=dkey("paper_ap"))
            paper.limitations = st.text_input("局限性（可留空）", value=paper.limitations,
                                              key=dkey("paper_lim"))
            paper.background = st.text_area("研究背景（1 句，显示在速览 PPT 标题下方与 Word 文献章节）",
                                            value=paper.background, height=70,
                                            key=dkey("paper_bg"))
        u1, u2, _u3 = st.columns([2, 1, 2])
        inst = u1.text_input("修改提示词（可选）", key=dkey("paper_inst"),
                             placeholder="如：研究过程再压缩；病例联系写具体一点")
        if u2.button("🪄 按提示词修改", disabled=not inst):
            with st.spinner("修改中……"):
                ok, msg = literature.revise_paper(c, paper, inst)
            if ok:
                bump_case()
                st.rerun()
            else:
                st.error(msg)

        if paper.figures:
            st.markdown(f"**关键图（{len(paper.figures)} 张；建议勾选 2~4 张，每张配一句解读）**")
            for fi, fig in enumerate(paper.figures):
                fc1, fc2 = st.columns([1.2, 3])
                if fig.panels:
                    opts = ["整图"] + [f"分图 {p['panel']}" for p in fig.panels]
                    cur_idx = 0
                    for oi, pn in enumerate(fig.panels, 1):
                        if fig.file == pn["file"]:
                            cur_idx = oi
                    pick = fc2.selectbox("使用整图还是某个分图（小图更好讲）", opts,
                                         index=cur_idx, key=dkey(f"fig_pick_{fi}"))
                    if pick == "整图":
                        fig.file = fig.whole_file or fig.file
                    else:
                        fig.file = fig.panels[opts.index(pick) - 1]["file"]
                if Path(fig.file).exists():
                    fc1.image(fig.file, width="stretch", caption=fig.label)
                else:
                    fc1.warning(f"{fig.label} 图片缺失")
                fig.selected = fc2.checkbox("纳入速览页", value=fig.selected,
                                            key=dkey(f"fig_sel_{fi}"))
                fig.note = fc2.text_area("图中讲了什么、证明了什么（1~2 句，40 字内）",
                                         value=fig.note, height=80, key=dkey(f"fig_note_{fi}"))
        else:
            st.info("本文未提取到图片（非开放期刊或提取失败）；速览页将只排文字与流程条。"
                    "想配图请优先选择标注「可提取图 ✓」的文献。")

        _tpls = [t for t in render_pptx.list_ppt_templates() if "参考样例" not in t]
        if _tpls:
            _cur = c.ppt.template if c.ppt.template in _tpls else render_pptx.default_ppt_template()
            g4, g5 = st.columns([2, 1])
            picked_tpl = g4.selectbox("PPT 模板", _tpls, index=_tpls.index(_cur) if _cur in _tpls else 0,
                                      key=dkey("lit_tpl"))
            c.ppt.template = picked_tpl
            _author = g5.text_input("汇报人署名", value=c.ppt.author, key=dkey("lit_author"))
            c.ppt.author = _author
            if st.button("🎬 生成文献分享 PPT（一页速览）", type="primary"):
                with st.spinner("生成中……"):
                    path = render_pptx.build_literature_onepage(
                        paper, picked_tpl, str(OUT / f"{slug(paper.title_cn)}_文献分享.pptx"),
                        author=_author)
                st.session_state["litppt_path"] = path
                st.success(f"已生成：{path}")
        if st.session_state.get("litppt_path"):
            d1, d2, _d3 = st.columns([1, 1, 2])
            d1.download_button("⬇️ 下载文献分享 PPT",
                               data=Path(st.session_state["litppt_path"]).read_bytes(),
                               file_name=Path(st.session_state["litppt_path"]).name,
                               mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                               width="stretch")
            if d2.button("👁️ 预览", key="litppt_pv", width="stretch"):
                with st.spinner("调用 PowerPoint 导出预览……"):
                    pngs = pptx_preview.export_pngs(st.session_state["litppt_path"],
                                                     str(OUT / "_litppt_pv"))
                if not pngs:
                    st.warning("预览失败（本机可能未装 PowerPoint），可直接下载查看。")
                else:
                    for k in range(0, len(pngs), 2):
                        cols = st.columns(2)
                        for j, col in enumerate(cols):
                            if k + j < len(pngs):
                                col.image(pngs[k + j], width="stretch",
                                          caption=f"第 {k + j + 1} 页")

# ================================================================ ⑤ Word
with tab5:
    if need_case():
        st.stop()
    c = get_case()
    st.subheader("导出 Word 教案")
    st.caption("按《PBL作业word模板》填充：**小组问题**=你输入的原始清单，"
               "**个人总结**=① 区生成的分层目录（1. / 1.2 / 1.3），"
               "**问题解答**默认按个人总结顺序排列，**文献分享**章节来自 ④ 区选定的文献。"
               "标题、姓名/班级/学号留空，由你自行填写。")

    # ---- ✏️ 导出前编辑面板（暂存式：点「应用修改」或「生成 Word」时统一写回案例，
    #      避免与 ①② 区同字段控件在同一 rerun 里互相覆盖）
    if cver() != st.session_state.get("we_stage_ver"):
        # 版本变化（bump_case：重新生成/载入/应用后）→ 面板控件随新版本 key 重建，
        # 自然从案例最新内容出发；旧版本 we_ 键已由 bump_case 清理。
        st.session_state["we_stage_ver"] = cver()
    with st.expander("✏️ 导出前编辑（此处修改将写入 Word）", expanded=False):
        st.caption("按 Word 输出顺序编辑：全局信息 → 个人总结目录 → 各题内容。"
                   "改完点【✅ 应用修改】，或直接点下方【📄 生成 Word】（自动带上这里的修改）。"
                   "表格首行为表头（Word 中加粗），网格里可直接改字、加行、删行。")
        edits: dict = {"questions": {}}
        g1, g2 = st.columns(2)
        edits["title"] = g1.text_input("教案标题", value=c.title, key=dkey("we_title"))
        edits["keywords"] = g2.text_input("关键词（；或,分隔）", value="；".join(c.keywords),
                                          key=dkey("we_kw"))
        sum_nodes = c.draft_summary_nodes or c.summary_nodes
        if sum_nodes:
            edits["summary_md"] = st.text_area(
                "个人总结目录（Markdown 缩进，两空格一层）",
                value=pipeline.summary_nodes_to_markdown(sum_nodes),
                height=180, key=dkey("we_summary"))
        for q in pipeline.ordered_questions(c, selected_only=True):
            qe: dict = {"blocks": []}
            edits["questions"][q.qid] = qe
            st.markdown(f"**{q.qid}　{q.text}**")
            qe["text"] = st.text_input("题目", value=q.text, key=dkey(f"we_{q.qid}_text"))
            for j, blk in enumerate(q.learning):
                we1, we2 = st.columns(2)
                qe["blocks"].append({
                    "heading": we1.text_input(f"块{j + 1} 小标题", value=blk.heading,
                                              key=dkey(f"we_{q.qid}_lh_{j}")),
                    "source": we2.text_input(f"块{j + 1} 教材出处", value=blk.source,
                                             key=dkey(f"we_{q.qid}_ls_{j}")),
                })
                qe["blocks"][-1]["body"] = st.text_area(
                    f"块{j + 1} 正文", value=blk.body, height=120,
                    key=dkey(f"we_{q.qid}_lb_{j}"))
                b1, b2, _rest = st.columns([1, 1, 8])
                if blk.table:
                    grid = st.data_editor(pd.DataFrame(blk.table), num_rows="dynamic",
                                          hide_index=True, key=dkey(f"we_{q.qid}_lt_{j}"))
                    qe["blocks"][-1]["table"] = grid.values.tolist() if grid is not None else []
                    if b1.button("🗑 删除表格", key=dkey(f"we_{q.qid}_delt_{j}")):
                        pipeline.apply_export_edits(c, edits)
                        blk.table = []
                        c.touch()
                        bump_case()
                        st.rerun()
                else:
                    if b1.button("➕ 添加表格", key=dkey(f"we_{q.qid}_addt_{j}")):
                        pipeline.apply_export_edits(c, edits)
                        blk.table = [["表头1", "表头2"], ["", ""]]
                        c.touch()
                        bump_case()
                        st.rerun()
                if b2.button("🗑 删除本块", key=dkey(f"we_{q.qid}_delb_{j}")):
                    pipeline.apply_export_edits(c, edits)
                    q.learning.pop(j)
                    c.touch()
                    bump_case()
                    st.rerun()
            if st.button("➕ 新增学习块", key=dkey(f"we_{q.qid}_addb")):
                pipeline.apply_export_edits(c, edits)
                q.learning.append(LearningBlock(heading="", body=""))
                c.touch()
                bump_case()
                st.rerun()
            qe["clinical"] = st.text_area(f"{q.qid} 临床应用（实际应用）", value=q.clinical,
                                          height=90, key=dkey(f"we_{q.qid}_clinical"))
        if st.button("✅ 应用修改", type="primary", key="we_apply"):
            pipeline.apply_export_edits(c, edits)
            c.touch()
            bump_case()
            st.rerun()

    has_paper = c.ppt.paper is not None
    prev_cases = acts.prev_cases(c)          # ① 区勾选的前幕存档（缺文件自动跳过）
    w1, w2, w3, w4, _w5 = st.columns([1, 1, 1.3, 1.3, 0.9])
    include_mm = w1.checkbox("文末附疾病导图", value=False, key="w_mm",
                             help="需先在 ③ 区生成疾病导图；默认不进 Word。")
    include_refs = w2.checkbox("包含参考文献", value=True, key="w_refs")
    include_lit = w3.checkbox("含文献分享章节", value=has_paper, key="w_lit",
                              disabled=not has_paper,
                              help="内容取自 ④ 区选定的那篇文献；未选文献时不可用。")
    include_prev = w4.checkbox(f"并入前幕解答（{len(prev_cases)} 幕）", value=bool(prev_cases),
                               key="w_prev", disabled=not prev_cases,
                               help="在 ① 区「病例分幕存档」里勾选要并入哪几幕，"
                                    "会把那几幕的问题解答整段放进文末附录。")
    _ls_opts = {"紧凑 1.0": 1.0, "适中 1.25": 1.25, "标准 1.5": 1.5, "宽松 1.75": 1.75}
    _ls_pick = _w5.selectbox("正文行距", list(_ls_opts), index=1, key="w_line_spacing",
                             help="默认「适中 1.25」；原来的 1.5 偏大。作用于正文、文献、"
                                  "目录等全部插入内容（覆盖模板行距）。")
    _line_spacing = _ls_opts[_ls_pick]
    if prev_cases and include_prev:
        w5.markdown("　".join(acts.act_label(pc).strip() for pc in prev_cases))
        w5.caption("附于文末")
    if c.word_prev_acts and not prev_cases:
        st.caption("⚠️ 已勾选前幕，但 `cases/acts/` 里找不到对应存档（可能被删除），本次不并入。")
    if not has_paper:
        st.caption("💡 想在 Word 里加「文献分享」，先到 ④ 文献检索选一篇文献。")
    if not c.summary_nodes and c.questions:
        st.caption("💡 还没生成「个人总结」目录：到 ① 区点「🗂 生成/重排个人总结」。")

    # ---- 自定义模板
    user_tpl = Path(__file__).parent / "pbl" / "templates" / "用户模板.docx"
    st.markdown("**📄 Word 模板**")
    if user_tpl.exists():
        st.info("当前使用：你上传的自定义模板")
        if st.button("恢复内置模板", key="tpl_reset"):
            user_tpl.unlink()
            bump_case()
            st.rerun()
    else:
        st.caption("当前使用：内置《PBL作业word模板》")
    up_tpl = st.file_uploader("上传自定义模板（结构类似即可：标题行 / 姓名·学号行 / 全部问题 / "
                              "关键词 / 问题解答＋学习内容·参考文献·实际应用；找不到标准结构时自动改为追加模式）",
                              type=["docx"], key="tpl_up")
    if up_tpl is not None:
        data = up_tpl.getvalue()
        if not user_tpl.exists() or user_tpl.read_bytes() != data:
            user_tpl.parent.mkdir(parents=True, exist_ok=True)
            user_tpl.write_bytes(data)
            st.success("自定义模板已启用")
            st.rerun()
    tpl_arg = str(user_tpl) if user_tpl.exists() else None

    if st.button("📄 生成 Word", type="primary"):
        # 导出前自动应用编辑面板的暂存修改（用户没点「应用修改」也不会漏）
        pipeline.apply_export_edits(c, edits)
        c.touch()
        if not include_refs and any(q.references for q in c.questions):
            st.warning("「包含参考文献」未勾选：本次 Word 的参考文献表会是空的。需要的话勾上再重新导出。")
        if not c.keywords and llm.has_key():
            with st.spinner("AI 正在根据病例与问题生成关键词……"):
                try:
                    c.keywords = pipeline.generate_keywords(c)
                    bump_case()
                except Exception:
                    pass
        fmt = render_docx.WordFormat(include_mindmap=include_mm, include_refs=include_refs,
                                     include_literature=include_lit,
                                     include_prev_acts=include_prev,
                                     line_spacing=_line_spacing)
        mm_png = None
        if include_mm:
            if c.disease_mindmap is None:
                st.warning("尚未生成疾病导图（③ 区），本次 Word 不含导图。")
            else:
                try:
                    mm_png = mindmap.render_png(c.disease_mindmap, str(OUT / "_word_mm.png"),
                                                connector=c.mm_connector)
                except Exception as e:
                    st.warning(f"思维导图渲染失败，Word 将不含导图：{e}")
        path = render_docx.build_docx(c, fmt, str(OUT / f"{slug(c.title)}.docx"), mm_png,
                                      template_path=tpl_arg,
                                      prev_cases=prev_cases if include_prev else None)
        st.session_state["docx_path"] = path
        st.session_state.pop("pdf_path", None)   # Word 重新生成后旧 PDF 失效
        st.success(f"已生成：{path}")

    if st.session_state.get("docx_path"):
        st.download_button("⬇️ 下载 Word", data=Path(st.session_state["docx_path"]).read_bytes(),
                           file_name=f"{slug(c.title)}.docx",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        if st.button("📄 转成 PDF"):
            with st.spinner("正在调用本机 Word 转换 PDF……"):
                st.session_state["pdf_path"] = docx_preview.export_pdf(st.session_state["docx_path"])
            if not st.session_state.get("pdf_path"):
                st.error("转换失败：请确认本机已安装 Microsoft Word，且目标 PDF 未被其他程序占用。")
        pdf_path = st.session_state.get("pdf_path")
        if pdf_path and Path(pdf_path).exists():
            st.download_button("⬇️ 下载 PDF", data=Path(pdf_path).read_bytes(),
                               file_name=Path(pdf_path).name, mime="application/pdf")
        elif pdf_path:
            st.caption("PDF 文件已不存在，请重新转换。")
        if st.button("👁️ 用 Word 导出预览图"):
            with st.spinner("正在调用本机 Word 导出预览……"):
                pngs = docx_preview.export_pngs(st.session_state["docx_path"], str(OUT / "_doc_pv"))
            if not pngs:
                st.warning("导出失败：本机可能未安装 Word，或 COM 调用被拒绝。可直接下载 docx 查看。")
            else:
                for k in range(0, len(pngs), 2):
                    cols = st.columns(2)
                    for j, col in enumerate(cols):
                        if k + j < len(pngs):
                            col.image(pngs[k + j], width="stretch",
                                      caption=f"第 {k + j + 1} 页")

# ================================================================ ⑥ PPT 制作
with tab6:
    if need_case():
        st.stop()
    c = get_case()

    st.subheader("PPT 制作：问题 PPT")
    st.caption("问题 PPT 沿用所选模板的版式与设计；上传自己的模板即可切换风格。"
               "**文献分享 PPT 已移到 ④ 文献检索**（先检索文献，再生成单页速览）。")

    # ---------------- 模板库
    with st.expander("📁 PPT 模板库（默认天医大模板；可上传自己的模板）", expanded=False):
        all_tpls = render_pptx.list_ppt_templates()
        usable = [t for t in all_tpls if "参考样例" not in t]
        default_tpl = render_pptx.default_ppt_template()
        cur_tpl = c.ppt.template if c.ppt.template in usable else default_tpl
        idx = usable.index(cur_tpl) if cur_tpl in usable else 0
        picked = st.selectbox("当前模板", usable, index=idx, key=dkey("ppt_tpl"),
                              help="生成 PPT 时沿用该模板的母版与版式（标题页、内容页、图片页）。")
        if picked != c.ppt.template:
            c.ppt.template = picked
        up = st.file_uploader("上传模板（.pptx，加入模板库后可选）", type=["pptx"],
                              key=dkey("ppt_up"))
        if up is not None:
            dest = render_pptx.PPT_DIR / up.name
            if not dest.exists() or dest.read_bytes() != up.getvalue():
                render_pptx.PPT_DIR.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(up.getvalue())
                c.ppt.template = up.name
                bump_case()
                st.success(f"模板已入库：{up.name}")
                st.rerun()
    tpl_now = c.ppt.template or render_pptx.default_ppt_template()

    # ---------------- A. 问题 PPT
    st.markdown("### A. 问题 PPT（分工：每人 1~2 个问题）")
    st.caption("版式：**问题在上 → 学习内容居中（自动 1~2 页，含表格）→ 参考文献在底部**。"
               "内容取自 ② 区学习内容，改完回来重新生成即可。")
    a1, a2 = st.columns([2, 3])
    author = a1.text_input("署名（可选）", value=c.ppt.author, key=dkey("ppt_author"),
                           placeholder="如：张三（显示在问题页标题后）")
    c.ppt.author = author
    qids = [q.qid for q in c.questions]
    sel = a2.multiselect("制作哪些问题（默认第一个；通常只选自己负责的 1~2 个）", qids,
                         default=qids[:1], key=dkey("ppt_qsel"))
    digest_on = st.checkbox("AI 精简为一页（推荐：内容压缩为幻灯片要点版，保留引用标注）",
                            value=True, key=dkey("ppt_digest"),
                            help="不勾选则按完整学习内容渲染（内容多时会自动多页并缩小字号）。")
    if st.button("🎬 生成问题 PPT", type="primary", disabled=not sel):
        sub = c.model_copy(deep=True)
        sub.questions = [q for q in sub.questions if q.qid in sel]
        content_map, notes = {}, []
        # Reconcile canonical textbook sources before AI digesting; the rendered digest
        # must contain the same citation markers as the bibliography footer.
        pipeline.sync_source_refs(sub)
        if digest_on and sel:
            prog = st.progress(0.0, text="正在精简各问题内容……")
            for k, q in enumerate(sub.questions):
                prog.progress(k / max(len(sub.questions), 1), text=f"精简 {q.qid}……")
                blocks, note = pipeline.digest_for_slides(sub, q)
                content_map[q.qid] = blocks
                if note and "失败" in note:
                    notes.append(f"{q.qid}：{note}")
            prog.progress(1.0, text="精简完成")
        with st.spinner("生成中……"):
            path = render_pptx.build_question_pptx(
                sub, tpl_now, str(OUT / f"{slug(c.title)}_问题PPT.pptx"), author=author,
                content_map=content_map or None)
        st.session_state["qppt_path"] = path
        st.success(f"已生成：{path}")
        for n in notes:
            st.warning(n)
    if st.session_state.get("qppt_path"):
        d1, d2, _ = st.columns([1, 1, 2])
        d1.download_button("⬇️ 下载问题 PPT",
                           data=Path(st.session_state["qppt_path"]).read_bytes(),
                           file_name=Path(st.session_state["qppt_path"]).name,
                           mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                           width="stretch")
        if d2.button("👁️ 预览", key="qppt_pv", width="stretch"):
            with st.spinner("调用 PowerPoint 导出预览……"):
                pngs = pptx_preview.export_pngs(st.session_state["qppt_path"], str(OUT / "_qppt_pv"))
            if not pngs:
                st.warning("预览失败（本机可能未装 PowerPoint），可直接下载查看。")
            else:
                for k in range(0, len(pngs), 2):
                    cols = st.columns(2)
                    for j, col in enumerate(cols):
                        if k + j < len(pngs):
                            col.image(pngs[k + j], width="stretch",
                                      caption=f"第 {k + j + 1} 页")

    st.markdown("### B. 海报式问题 PPT（单页：机制流程链 + 对比表格 + 核心要点）")
    st.caption("版式参照学术海报：机制链在上、密集对比表居中、要点收底、参考文献脚注；"
               "每题一页合成一份。内容结构由模型设计，**推荐 DeepSeek**（知识密度高）；"
               "表格中的研究数据由模型生成，汇报前请人工核对。")
    pb1, pb2 = st.columns([2, 3])
    poster_model = pb1.selectbox(
        "内容生成模型",
        ["DeepSeek V4 Pro（质量优先）", "DeepSeek V4.1 Flash（快）",
         "DeepSeek V4.1 Flash Ultrafast（极速）", "智谱 GLM-4.6", "当前默认模型（.env）"],
        index=0, key=dkey("poster_model"),
        help="海报内容知识密度高，推荐 DeepSeek V4 Pro；赶时间可用 Ultrafast。")
    _poster_map = {"DeepSeek V4 Pro（质量优先）": ("deepseek", "deepseek/deepseek-v4-pro"),
                   "DeepSeek V4.1 Flash（快）": ("deepseek", "deepseek/deepseek-v4.1-flash"),
                   "DeepSeek V4.1 Flash Ultrafast（极速）":
                       ("deepseek", "deepseek/deepseek-v4.1-flash-ultrafast"),
                   "智谱 GLM-4.6": ("zhipu", "glm-4.6"),
                   "当前默认模型（.env）": (None, None)}
    p_sel = pb2.multiselect("制作哪些问题的海报", qids, default=qids[:1],
                            key=dkey("poster_qsel"))
    if st.button("🎬 生成海报 PPT", type="primary", disabled=not p_sel, key="poster_gen"):
        _pv, _pm = _poster_map[poster_model]
        items, errs = [], []
        prog = st.progress(0.0, text="正在生成海报内容……")
        for k, qid in enumerate(p_sel):
            q = next((x for x in c.questions if x.qid == qid), None)
            if q is None:
                continue
            prog.progress(k / max(len(p_sel), 1), text=f"生成 {qid} 海报内容……")
            spec, err = pipeline.generate_question_poster(c, q, provider=_pv, model=_pm)
            if spec is None:
                errs.append(f"{qid}：{err}")
                continue
            qi = qids.index(qid) + 1
            refs_text = "  ".join(f"[{i + 1}] {pubmed.format_reference(r)}"
                                  for i, r in enumerate(q.references))
            items.append({"title": f"问题{qi}：{q.text}", "spec": spec,
                          "refs_text": refs_text})
        prog.progress(1.0, text="排版海报……")
        if items:
            with st.spinner("排版中……"):
                path = render_pptx.build_poster_pptx(
                    items, author, tpl_now, str(OUT / f"{slug(c.title)}_海报PPT.pptx"))
            st.session_state["poster_path"] = path
            st.success(f"已生成：{path}")
        for e in errs:
            st.error(e)
    if st.session_state.get("poster_path"):
        pd1, pd2, _ = st.columns([1, 1, 2])
        pd1.download_button("⬇️ 下载海报 PPT",
                            data=Path(st.session_state["poster_path"]).read_bytes(),
                            file_name=Path(st.session_state["poster_path"]).name,
                            mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                            width="stretch")
        if pd2.button("👁️ 预览", key="poster_pv", width="stretch"):
            with st.spinner("调用 PowerPoint 导出预览……"):
                pngs = pptx_preview.export_pngs(st.session_state["poster_path"],
                                                str(OUT / "_poster_pv"))
            if not pngs:
                st.warning("预览失败（本机可能未装 PowerPoint），可直接下载查看。")
            else:
                for k in range(0, len(pngs), 2):
                    cols = st.columns(2)
                    for j, col in enumerate(cols):
                        if k + j < len(pngs):
                            col.image(pngs[k + j], width="stretch",
                                      caption=f"第 {k + j + 1} 页")

    st.divider()
    with st.expander("🧪 进阶：PPT Master 设计版（更强版式；导出后在对话中继续生成）", expanded=False):
        if not pptm.available():
            st.warning("未找到 ppt-master-comfyui 技能目录（可用环境变量 PBL_PPTM_SKILL 指定路径）。")
        else:
            st.caption("把当前案例打包为 PPT Master 项目（素材 + 图片 + Stage-1 沟通契约），并可启动确认页。"
                       "确认页提交后回到对话说「继续生成 <项目名>」，由智能体按技能流程完成设计排版与导出。")
            pp1, pp2 = st.columns(2)
            if pp1.button("📦 导出「问题 PPT」项目", key="pptm_q"):
                try:
                    with st.spinner("正在初始化项目并导入素材……"):
                        proj_q = pptm.export_question_project(c, c.ppt.author)
                    st.session_state["pptm_last"] = str(proj_q)
                    st.success(f"已创建：{proj_q.name}")
                except Exception as e:
                    st.error(f"导出失败：{e}")
            if pp2.button("📦 导出「文献分享」项目", key="pptm_l", disabled=c.ppt.paper is None):
                try:
                    with st.spinner("正在初始化项目并导入素材与图片……"):
                        proj_l = pptm.export_literature_project(c, c.ppt.paper)
                    st.session_state["pptm_last"] = str(proj_l)
                    st.success(f"已创建：{proj_l.name}")
                except Exception as e:
                    st.error(f"导出失败：{e}")
            last_proj = st.session_state.get("pptm_last")
            if last_proj and Path(last_proj).exists():
                st.markdown(f"**最近导出的项目**：`{Path(last_proj).name}`")
                st.caption("确认页提交后，复制下面这句话，回到与智能体的对话窗口粘贴发送：")
                st.code(f"继续生成 {Path(last_proj).name}", language=None)
                alive, live_url = pptm.server_status(Path(last_proj))
                st.caption("确认页状态：" + ("🟢 运行中" if alive else "⚪ 未启动"))
                pb1, pb2 = st.columns(2)
                if pb1.button("🌐 启动 PPT Master 确认页", key="pptm_launch"):
                    if alive:
                        st.session_state["pptm_url"] = live_url
                        st.toast("确认页已在运行，直接点下方链接打开", icon="🌐")
                        st.rerun()
                    else:
                        with st.spinner("启动确认页……"):
                            try:
                                st.session_state["pptm_url"] = pptm.launch_confirm(Path(last_proj))
                                st.toast("确认页已启动", icon="🌐")
                                st.rerun()
                            except Exception as e:
                                st.error(f"启动失败：{e}")
                if pb2.button("⏹ 关闭确认页", key="pptm_shutdown", disabled=not alive):
                    pptm.shutdown_confirm(Path(last_proj))
                    st.session_state.pop("pptm_url", None)
                    st.toast("确认页已关闭", icon="⏹")
                    st.rerun()
                show_url = st.session_state.get("pptm_url") or (live_url if alive else None)
                if show_url:
                    st.link_button("打开确认页 →", show_url, width="content")
                    st.code(show_url, language=None)
                st.caption("在页面提交后，回到对话说「继续生成 <项目名>」即可继续设计与导出；"
                           "确认页闲置 15 分钟会自动关闭。")

# 每次界面刷新：当前案例写入最新存档（历史快照在程序性修改时保存）
autosave_latest()
