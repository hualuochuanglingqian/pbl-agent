# -*- coding: utf-8 -*-
"""极简 asar 读取器：只解出需要的文件，够用来翻 XMind 的内置主题定义。

asar 格式：8 字节头（4 字节 uint32 = 4，紧跟 4 字节 header size）+
JSON 索引 + 按偏移排布的文件内容。
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def read_header(asar: Path) -> tuple[dict, int]:
    with asar.open("rb") as f:
        head = f.read(16)
        _size, _ = struct.unpack("<II", head[:8])
        header_size = struct.unpack("<I", head[12:16])[0]
        raw = f.read(header_size)
    return json.loads(raw.decode("utf-8")), 8 + header_size


def walk(node: dict, prefix: str = ""):
    for name, meta in (node.get("files") or {}).items():
        path = f"{prefix}/{name}"
        if "files" in meta:
            yield from walk(meta, path)
        else:
            yield path, meta


def extract(asar: Path, out_dir: Path, pattern: str = "") -> list[str]:
    index, base = read_header(asar)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    with asar.open("rb") as f:
        for path, meta in walk(index):
            if pattern and pattern.lower() not in path.lower():
                continue
            if meta.get("unpacked"):
                continue
            f.seek(base + int(meta["offset"]))
            data = f.read(int(meta["size"]))
            dst = out_dir / path.lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            saved.append(str(dst))
    return saved


def list_paths(asar: Path, pattern: str = "") -> list[str]:
    index, _ = read_header(asar)
    return [p for p, m in walk(index)
            if (not pattern or pattern.lower() in p.lower()) and not m.get("unpacked")]


if __name__ == "__main__":
    asar_path = Path(sys.argv[1])
    if len(sys.argv) > 2 and sys.argv[2] == "list":
        pat = sys.argv[3] if len(sys.argv) > 3 else ""
        for p in list_paths(asar_path, pat)[:400]:
            print(p)
    else:
        pat = sys.argv[3] if len(sys.argv) > 3 else ""
        out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_asar_out")
        files = extract(asar_path, out, pat)
        print(f"解出 {len(files)} 个文件 -> {out}")
