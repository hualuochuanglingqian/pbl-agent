# -*- coding: utf-8 -*-
"""预计算 BM25 分词缓存（只需跑一次，之后 app 启动直接加载）。"""
import time
from kb import KnowledgeBase


def main():
    t0 = time.time()
    kb = KnowledgeBase()
    print(f"chunks={len(kb.meta)} books={len(kb.sources)} load={time.time()-t0:.1f}s")
    t1 = time.time()
    tokens = kb._load_tokens()
    print(f"tokenized={len(tokens)} time={time.time()-t1:.1f}s")
    t2 = time.time()
    _ = kb.bm25
    print(f"bm25 ready time={time.time()-t2:.1f}s")


if __name__ == "__main__":
    main()
