# -*- coding: utf-8 -*-
"""XMind 模板文件带 10 字节私有前缀，且中央目录偏移是绝对偏移 —— 直接按本地头扫描解析。

用法: python tools/xmind_zip.py <file.xmind> [list|dump 名称]
"""
import struct
import sys
import zlib
from pathlib import Path


def entries(raw: bytes):
    out = []
    off = 0
    while True:
        i = raw.find(b"PK\x03\x04", off)
        if i < 0:
            break
        (sig, _ver, flag, method, _t, _d, _crc, csize, usize,
         nlen, elen) = struct.unpack("<IHHHHHIIIHH", raw[i:i + 30])
        name = raw[i + 30:i + 30 + nlen].decode("utf-8", "replace")
        data = raw[i + 30 + nlen + elen: i + 30 + nlen + elen + csize]
        out.append((name, method, csize, usize, data))
        off = i + 30 + nlen + elen + max(csize, 0)
        if csize == 0 and usize == 0:
            off = i + 30 + nlen + elen
    return out


def main() -> None:
    p = Path(sys.argv[1])
    raw = p.read_bytes()
    es = entries(raw)
    cmd = sys.argv[2] if len(sys.argv) > 2 else "list"
    print(f"{p.name}: {len(es)} 个条目")
    for name, method, csize, usize, data in es:
        print(f"  {name:20s} method={method} 压缩={csize} 原始={usize}")
    if cmd == "dump":
        target = sys.argv[3]
        for name, method, csize, usize, data in es:
            if name == target:
                blob = zlib.decompress(data, -15) if method == 8 else data
                print(blob.decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
