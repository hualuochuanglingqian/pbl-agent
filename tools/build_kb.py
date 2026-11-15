"""后台建库脚本：python tools/build_kb.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pbl import textbook


def cb(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


n, books = textbook.build_index(progress_cb=cb)
print(f"DONE: {len(books)} 本教材, {n} 个知识块", flush=True)
