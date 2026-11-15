# -*- coding: utf-8 -*-
"""在 XMind 的 app.asar 里找内置主题定义（按关键字扫二进制，打印上下文）。"""
import re
import sys
from pathlib import Path

ASAR = Path(sys.argv[1])
NEEDLES = sys.argv[2].split(",") if len(sys.argv) > 2 else ["svg:fill"]
WINDOW = 500

paths: set = set()


def scan(needle: str) -> int:
    b = needle.encode("utf-8")
    hits = 0
    with ASAR.open("rb") as f:
        prev = b""
        off = 0
        while True:
            chunk = f.read(1 << 22)
            if not chunk:
                break
            buf = prev + chunk
            start = 0
            while True:
                i = buf.find(b, start)
                if i < 0:
                    break
                hits += 1
                if hits <= 3:
                    lo = max(0, i - WINDOW // 2)
                    hi = min(len(buf), i + WINDOW)
                    frag = buf[lo:hi].decode("utf-8", "replace")
                    print(f"\n=== 命中 @{off + i - len(prev)} (共第 {hits} 处) ===")
                    print(frag.replace("\n", " ")[:WINDOW + 200])
                start = i + 1
            prev = buf[-64:]
            off += len(buf)
    return hits


for n in NEEDLES:
    print(f"\n######## 关键字: {n} ########")
    c = scan(n)
    print(f"[{n}] 命中总数: {c}")
