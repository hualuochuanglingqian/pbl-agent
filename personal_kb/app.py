# -*- coding: utf-8 -*-
"""个人医学知识库 · Streamlit 界面（混合检索 + LLM 重排 + 页码引用）。"""
import time

import streamlit as st

from kb import get_kb, display_source

st.set_page_config(page_title="个人医学知识库", page_icon="📚", layout="wide")


with st.spinner("正在加载 2.9 万块教材索引与 BM25…"):
    kb = get_kb()

st.title("📚 个人医学知识库")
n_books = sum(1 for x in kb.sources if "题库" not in x)
n_banks = len(kb.sources) - n_books
st.caption(
    f"语料：{n_books} 本教材 + {n_banks} 套自测题库 / {len(kb.meta):,} 个知识块　|　"
    "混合检索（向量 + BM25）→ LLM 重排 → 带页码引用的回答"
)
display_to_raw = {display_source(s): s for s in kb.sources}

with st.sidebar:
    st.header("检索设置")
    picked = st.multiselect("限定来源（不选=全部）", list(display_to_raw), default=[])
    books = [display_to_raw[x] for x in picked]
    picked_chaps = []
    if len(books) == 1:
        opts = kb.chapters(books[0])
        if opts:
            picked_chaps = st.multiselect("限定章节（不选=全书）", opts, default=[])
    st.caption("不选=跨教材综合；问具体标准/数值时，建议先选中对应教材。")
    mode = st.radio(
        "检索模式",
        ["hybrid", "dense", "bm25"],
        format_func=lambda x: {"hybrid": "混合（推荐）", "dense": "仅向量", "bm25": "仅关键词"}[x],
        horizontal=True,
    )
    candidate_k = st.slider("候选块数", 10, 50, 30, 5)
    top_n = st.slider("最终引用块数", 3, 10, 6)
    use_rerank = st.checkbox("LLM 重排", value=True,
                             help="把候选块交给大模型按相关性重排；关掉则按混合分数排序")
    use_rewrite = st.checkbox("多查询改写（较慢）", value=False,
                              help="把问题改写成多个检索式再合并；DeepSeek 不可用时自动走智谱")
    retrieve_only = st.checkbox("只检索，不生成回答", value=False)
    show_scores = st.checkbox("显示检索分数", value=False)

    st.divider()
    if st.button("清空对话", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

    with st.expander("示例问题"):
        st.markdown(
            "- IgA 肾病的牛津分型包括哪些指标？\n"
            "- 肾病综合征的诊断标准是什么？\n"
            "- 急性肾损伤的 KDIGO 分期？\n"
            "- 糖尿病肾脏病的首选降压药？\n"
            "- 社区获得性肺炎的 CURB-65 评分？"
        )

def render_sources(hits):
    seen, idx = set(), 0
    for h in hits:
        key = (h["source"], h["page"])
        if key in seen:
            continue
        seen.add(key)
        idx += 1
        ch = h.get("chapter") or ""
        suffix = f" · {ch}" if ch else ""
        st.markdown(f"**[{idx}] 《{display_source(h['source'])}》第 {h['page']} 页{suffix}**")
        st.write(h["text"])
        full = kb.page_text(h["source"], h["page"])
        if full and len(full) > len(h["text"]):
            st.caption("该页整页原文（截取，便于核对上下文）：")
            st.code(full[:2500], language=None)
        if show_scores:
            st.caption(
                f"融合 {h['score']:.3f}　向量 {h['dense']:.3f}　BM25 {h['bm25']:.3f}"
            )


if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("sources"):
            with st.expander(f"查看 {len(msg['sources'])} 条原文依据"):
                render_sources(msg["sources"])

query = st.chat_input("问一个医学问题，例如：IgA 肾病的诊断依据是什么？")
if query:
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        t0 = time.time()
        with st.spinner("检索中…"):
            hits = kb.search(query, books=books, mode=mode, candidate_k=candidate_k,
                             rewrite=use_rewrite, chapters=picked_chaps)
        t_search = time.time() - t0

        t1 = time.time()
        if use_rerank and hits:
            with st.spinner("重排中…"):
                hits = kb.rerank(query, hits, top_n=min(12, len(hits)))
                hits = kb.diversify(hits, top_n=top_n)
        else:
            hits = kb.diversify(hits, top_n=top_n)
        t_rerank = time.time() - t1

        if not hits:
            answer = "知识库中未检索到相关内容。可以换一个问法，或放宽教材筛选。"
            st.markdown(answer)
        elif retrieve_only:
            answer = f"检索到 {len(hits)} 条相关片段（只检索模式）。"
            st.markdown(answer)
        else:
            with st.spinner("生成回答中…"):
                answer = kb.answer(query, hits)
            st.markdown(answer)

        if hits:
            with st.expander(f"查看 {len(hits)} 条原文依据"):
                render_sources(hits)
            st.caption(
                f"检索 {t_search:.1f}s　重排 {t_rerank:.1f}s　"
                f"共 {len(kb.meta):,} 块中取 {len(hits)} 块"
            )

    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "sources": hits}
    )
