"""思维导图：MindmapNode <-> 多种形态。

- PNG：XMind 风格两翼均衡布局（根居中、一级分支对半分到左右、分支各自着色、贝塞尔连线）
- .xmind：XMind（2020+）可直接打开的 ZEN 格式文件，可继续在 XMind 中编辑
- HTML：markmap 交互网页（可缩放折叠）
- .mm：FreeMind / XMind 8 可打开的格式
- Markdown 缩进列表：界面内直接编辑的格式

导图以疾病为中心（病因/发病机制/诊断/治疗等），在 PBL 最后一节课结束后
由 pipeline.generate_disease_mindmap 单独生成，不随每份 Word 输出。

**病例内容不单列分支**：来自本病例的要点用 `==要点==` 标记（Markdown 里可见、可手改），
渲染时标黄并加下划线，让人一眼看出「这部分是这个病人的」。
"""
from __future__ import annotations

import re
import xml.sax.saxutils as sx
from pathlib import Path

from .schema import MindmapNode

# ---------------------------------------------------------------- Markdown 往返
_CASE_MARK = re.compile(r"==(.+?)==")


def node_to_markdown(node: MindmapNode) -> str:
    lines: list[str] = []

    def walk(n: MindmapNode, depth: int) -> None:
        text = n.text.replace("\n", " ")
        if n.in_case and "==" not in text:
            text = f"=={text}=="
        lines.append("  " * depth + "- " + text)
        for c in n.children:
            walk(c, depth + 1)

    for c in node.children:          # 根节点本身是中心词，不重复输出
        walk(c, 0)
    return "\n".join(lines)


def markdown_to_node(text: str, root_text: str) -> MindmapNode:
    """Markdown 缩进列表 -> 以 root_text 为根的树。`==要点==` 解析为病例内容。"""
    root = MindmapNode(text=root_text)
    stack: list[tuple[int, MindmapNode]] = [(-1, root)]
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.lstrip(" ")
        content = re.sub(r"^[-*•]\s*", "", content).strip()
        if not content:
            continue
        in_case = bool(_CASE_MARK.search(content))
        content = _CASE_MARK.sub(r"\1", content).strip()
        if not content:
            continue
        level = indent // 2
        node = MindmapNode(text=content, in_case=in_case)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack[-1][1].children.append(node)
        stack.append((level, node))
    return root


def count_case_nodes(node: MindmapNode) -> int:
    """统计被标记为病例内容的节点数（界面提示用）。"""
    return (1 if node.in_case else 0) + sum(count_case_nodes(c) for c in node.children)


def has_case_nodes(node: MindmapNode) -> bool:
    return count_case_nodes(node) > 0


def update_node_from_markdown(node: MindmapNode, text: str) -> MindmapNode:
    """界面编辑 Markdown 后回写树（保留根文本）。"""
    return markdown_to_node(text, node.text)


# ---------------------------------------------------------------- PNG（照 XMind 默认主题）
# 数值取自 XMind 软件自带的「基础思维导图」模板（content.json 里的 theme，Dawn 主题）：
#   根 = 无底框、纯黑粗体文字 30pt/800；分支线 curve、2pt
#   一级 = roundedRect + 该支实色填充 + **黑字** 18pt/500；分支线 roundedElbow、2pt
#   二级及以下 = 纯文字 14pt/400；分支线 roundedElbow、2pt
#   调色板恰好 6 色，line-tapered: none（不做楔形渐变）
# 注意：XMind 默认主题里 centralTopic 的 fill-pattern 是 none，所以中心词没有黑底框；
# 一级分支的 fo:color 是 inherited（黑），不是白字。这两点是最容易被画错的地方。
_S = 2                       # 超采样：先画 2 倍图再缩小，曲线与文字更细腻
_CASE_FILL, _CASE_LINE, _CASE_TXT = "#FFF3C4", "#C99700", "#7A4F00"

_XMIND_COLORS = ["#FF6B6B", "#FF9F69", "#97D3B6", "#88E2D7", "#6FD0F9", "#E18BEE"]

# 把 30/18/14pt 换算成像素（96dpi）后，相对当前字号约 1.7 倍；
# 同时放大内边距与层间距，避免「字大了但框还是胶囊」的违和感。
_XM_SCALE = 1.7
_XM_MAXW = [560, 300, 340, 380, 420]   # 各级正文最大宽度（成品像素）
_XM_TEXT = "#000000"
_XM_RADIUS = 14                        # 一级分支圆角（2 倍画布像素，约 7px 成品）


def _tint(hex_color: str, keep: float = 0.25) -> str:
    """XMind 的浅色档：主色留 25%，其余混白。实测 #FF6B6B -> #FFDADA。"""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    mix = [round(c * keep + 255 * (1 - keep)) for c in (r, g, b)]
    return "#%02X%02X%02X" % tuple(mix)


def _shade(hex_color: str, keep: float = 0.45) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02X" % tuple(round(c * keep) for c in (r, g, b))


# (主色, 浅底, 深字)：主色=一级分支条与连接线，浅底=浅色档，深字=深色文字
_BRANCH_COLORS = [(c, _tint(c), _shade(c)) for c in _XMIND_COLORS]
_FS = [45, 27, 21, 21, 19]            # 字号（已乘 _S）：根 / 一级 / 二级 / 三级 / 四级以上
_MAXC = [12, 10, 18, 22, 24]          # 每行最多字数
_LH = [1.32, 1.30, 1.34, 1.34, 1.34]  # 行高倍数
_PILL = {1: 66, 2: 48}                # 一级/二级圆角条高度（已乘 _S）
_PADX = {0: 0, 1: 26, 2: 20, 3: 7, 4: 7}
_GAPX, _GAPY, _MARGIN = 62, 26, 46
_PADY_PLAIN = 7
_SPINE = 18                  # 主干式连线：子节点外侧竖起的主干偏移
_SPINE_GRAY = "#C3CAD3"       # 根到一级分支的主干（共用一条，用中性色）


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    names = (("msyhbd.ttc", "msyh.ttc", "simhei.ttf") if bold
             else ("msyh.ttc", "simhei.ttf", "simsun.ttc"))
    for name in names:
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_png(root: MindmapNode, out_path: str, connector: str = "xmind") -> str:
    """XMind 导出风格辐射图，返回 out_path。

    connector：连线（版式）样式
      - "xmind"（默认）：照 XMind 内置 Dawn 主题——中心是纯黑粗体文字、**没有底框**，
        一级分支是实色圆角块 + **黑字**，二级以下纯文字，分支线等宽（不做楔形）；
      - "spine"：竖主干 + 短横桩，同层共用一条线，最干净；
      - "curve"：每个子节点各拉一条等宽贝塞尔曲线（节点多时偏乱）；
      - "straight"：直线；
      - "none"：不画连线，纯靠缩进分层。
    """
    from PIL import Image, ImageDraw

    xm = connector == "xmind"        # XMind 默认：根无框、一级实色块黑字、二级起纯文字
    sc = _XM_SCALE if xm else 1.0
    pill_h = {1: int(round(_PILL[1] * sc))} if xm else dict(_PILL)
    # 层间距：根→一级最长（长弧线才好看），越往下越紧
    _GAP_BY_D = {1: 124, 2: 78, 3: 50, 4: 42}

    def gap_of(depth: int) -> float:
        return _GAP_BY_D.get(depth, 42) * sc if xm else _GAPX

    # 纵向节奏：一级分支之间松一些，二级以下收紧（XMind 默认不是等距）
    def gapy_at(depth: int) -> float:
        if not xm:
            return _GAPY
        return {0: 90, 1: 58}.get(depth, 40) * sc

    padx_map = {k: v * sc for k, v in _PADX.items()}
    if xm:
        padx_map[2] = _PADX[3] * sc   # 二级退化为纯文字
        padx_map[0] = 0               # 根没有底框，不留内边距
    padx_map[0] = 0

    cache: dict = {}

    def font(depth: int, bold: bool = False):
        key = (depth, bold)
        if key not in cache:
            cache[key] = _load_font(int(round(_FS[min(depth, 4)] * sc)), bold=bold)
        return cache[key]

    def wrap(text: str, depth: int) -> list[str]:
        if not xm:
            maxc = _MAXC[min(depth, 4)]
            lines = []
            for seg in (text or "").split("\n"):
                cur = ""
                for ch in seg:
                    cur += ch
                    if len(cur) >= maxc:
                        lines.append(cur)
                        cur = ""
                if cur or not lines:
                    lines.append(cur or " ")
            return lines

        # XMind 模式下按**像素宽度**换行，并且西文/数字整词不拆（Gd-IgA1、C1GALT1 不被劈开）
        f = font(depth, depth <= 1)
        maxw = _XM_MAXW[min(depth, 4)] * _S
        lines: list[str] = []
        token_re = re.compile(
            r"\s+|[A-Za-z0-9][A-Za-z0-9\-_/\.%μ±+×·,;:()\[\]<>≈≥≤]*|[\u4e00-\u9fff]|[^\s]")
        closers = "，。、；：！？）》」』】,.;:!?)]}%"
        for seg in (text or "").split("\n"):
            seg_lines: list[str] = []
            cur = ""
            for tok in token_re.findall(seg):
                if tok.isspace():              # 保留英文词之间的空格，但不在行首留空
                    if cur:
                        cur += " "
                    continue
                if not cur:
                    if f.getlength(tok) <= maxw:
                        cur = tok
                    else:                      # 超长单词按字符拆
                        for ch in tok:
                            if cur and f.getlength(cur + ch) > maxw:
                                seg_lines.append(cur)
                                cur = ch
                            else:
                                cur += ch
                    continue
                cand = cur + tok
                if f.getlength(cand) <= maxw:
                    cur = cand
                elif tok in closers and seg_lines:
                    seg_lines[-1] += tok       # 标点不另起一行
                else:
                    seg_lines.append(cur.rstrip())
                    cur = tok
            if cur or not seg_lines:
                seg_lines.append(cur.rstrip() or " ")
            # 避免最后一行只剩一个汉字（把上一行末尾的字挪下来）
            while len(seg_lines) >= 2 and len(seg_lines[-1]) <= 2:
                prev, last = seg_lines[-2], seg_lines[-1]
                m = re.search(r"[\u4e00-\u9fff]$", prev)
                if len(prev) <= 3 or not m:
                    break
                seg_lines[-2] = prev[:-1]
                seg_lines[-1] = prev[-1] + last
            lines.extend(seg_lines)
        return lines

    def lh(depth: int) -> float:
        return _FS[min(depth, 4)] * sc * _LH[min(depth, 4)]

    def prep(n: MindmapNode, depth: int) -> None:
        n._lines = wrap(n.text, depth)
        # 宽度必须用**实际绘制时**的字重来量：根与一级分支是粗体，
        # 若按常规字重算宽度，加粗的字会顶出框外。
        n._tw = max(font(depth, depth <= 1).getlength(t) for t in n._lines)
        pad = padx_map[min(depth, 4)]
        n._w = n._tw + pad * 2
        if depth in pill_h:
            n._h = pill_h[depth]
        else:
            n._h = len(n._lines) * lh(depth) + _PADY_PLAIN * 2 * sc
            if xm and depth == 0:                     # 根只有文字，不加额外内边距
                n._h -= _PADY_PLAIN * 2 * sc
        for c in n.children:
            prep(c, depth + 1)
        block = sum(c._th for c in n.children) + gapy_at(depth) * (len(n.children) - 1) if n.children else 0
        n._th = max(n._h, block)

    def place(n: MindmapNode, ytop: float, depth: int) -> None:
        kids = n.children
        if kids:
            gy = gapy_at(depth)
            used = sum(k._th for k in kids) + gy * (len(kids) - 1)
            block = max(used, n._h)
            y = ytop + (block - used) / 2
            for k in kids:
                place(k, y, depth + 1)
                y += k._th + gy
            n._cy = (kids[0]._cy + kids[-1]._cy) / 2
            n._th = block
        else:
            n._cy = ytop + n._th / 2

    prep(root, 0)
    kids = root.children
    if xm and len(kids) > 2:
        # 按子树高度而不是条数分配左右两翼：医学导图往往一支特别长，
        # 单纯对半分会让一侧堆到几千像素、另一侧大片留白。
        heights = [k._th for k in kids]
        best_k, best_diff = 1, None
        for k in range(1, len(kids)):
            diff = abs(sum(heights[:k]) - sum(heights[k:]))
            if best_diff is None or diff < best_diff:
                best_k, best_diff = k, diff
        half = best_k
    else:
        half = (len(kids) + 1) // 2
    right_kids, left_kids = kids[:half], kids[half:]

    def col_widths(nodes) -> dict:
        cols: dict = {}

        def rec(n, d):
            cols[d] = max(cols.get(d, 0), n._w)
            for c in n.children:
                rec(c, d + 1)

        for k in nodes:
            rec(k, 1)
        return cols

    rcols, lcols = col_widths(right_kids), col_widths(left_kids)
    maxd = max([1] + list(rcols) + list(lcols))

    def prefix(cols) -> dict:
        acc, out = 0.0, {1: 0.0}
        for d in range(2, maxd + 1):
            acc += cols.get(d - 1, 0) + gap_of(d)
            out[d] = acc
        return out

    roff, loff = prefix(rcols), prefix(lcols)
    left_extent = sum(lcols.get(d, 0) + gap_of(d) for d in range(1, maxd + 1))
    right_extent = sum(rcols.get(d, 0) + gap_of(d) for d in range(1, maxd + 1))

    r_total = sum(k._th for k in right_kids) + gapy_at(0) * (len(right_kids) - 1) if right_kids else 0
    l_total = sum(k._th for k in left_kids) + gapy_at(0) * (len(left_kids) - 1) if left_kids else 0
    legend_h = 34 * sc if has_case_nodes(root) else 0   # 底部图例：说明黄底=本病例内容
    H = int(max(r_total, l_total, root._h) + _MARGIN * sc * 2 + 14 + legend_h)

    root_hw = root._w / 2
    W = int(_MARGIN * sc * 2 + left_extent + gap_of(1) + root._w + gap_of(1) + right_extent)
    cx = _MARGIN * sc + left_extent + gap_of(1) + root_hw
    root._x, root._cy, root._side, root._depth = cx - root_hw, H / 2, 0, 0
    root._col = (_XM_TEXT, "#FFFFFF", _XM_TEXT)

    def assign(nodes, side, start_idx: int = 0, reverse: bool = False):
        gy = gapy_at(0)
        y = (H - (sum(k._th for k in nodes) + gy * (len(nodes) - 1))) / 2 if nodes else 0
        for i, k in enumerate(nodes):
            place(k, y, 1)
            y += k._th + gy
            if reverse:          # 左翼从下往上配色，和 XMind 的顺时针轮转一致
                idx = start_idx + len(nodes) - 1 - i
            else:
                idx = start_idx + i

            def rec(n, depth, col):
                n._col, n._side, n._depth = col, side, depth
                if side == 1:
                    n._x = cx + root_hw + gap_of(depth) + roff.get(depth, 0)
                else:
                    n._x = cx - root_hw - gap_of(depth) - loff.get(depth, 0) - n._w
                for c in n.children:
                    rec(c, depth + 1, col)

            rec(k, 1, _BRANCH_COLORS[idx % len(_BRANCH_COLORS)])

    assign(right_kids, 1, 0, False)
    assign(left_kids, -1, len(right_kids) if xm else 0, bool(xm))

    img = Image.new("RGB", (W, H), "#FFFFFF")
    dr = ImageDraw.Draw(img)

    def bez(p0, p3, color, width):
        """贝塞尔钩形：出枝先横向走，再弯进子节点。"""
        x1 = p0[0] + (p3[0] - p0[0]) * 0.45
        x2 = p0[0] + (p3[0] - p0[0]) * 0.80
        pts = []
        for i in range(29):
            t = i / 28
            mt = 1 - t
            pts.append((mt ** 3 * p0[0] + 3 * mt * mt * t * x1 + 3 * mt * t * t * x2 + t ** 3 * p3[0],
                        mt ** 3 * p0[1] + 3 * mt * mt * t * p0[1] + 3 * mt * t * t * p3[1]
                        + t ** 3 * p3[1]))
        dr.line(pts, fill=color, width=width, joint="curve")
        dr.line([pts[-1], p3], fill=color, width=width)

    def edge(n, k, gap, side):
        """父节点到子节点的起止点：按**子节点所在翼**取父节点的对应边缘。"""
        if side == 1:
            return (n._x + n._w + gap, n._cy), (k._x - 2, k._cy)
        return (n._x - gap, n._cy), (k._x + k._w + 2, k._cy)

    def draw_conn(n, depth):
        kids = n.children
        if not kids or connector == "none":
            for k in kids:
                draw_conn(k, depth + 1)
            return
        gap = 4 * sc if xm else (6 if depth == 0 else 4)
        lw = max(3, int(round(4 * sc))) if xm else (4 if depth == 0 else (3 if depth == 1 else 2))
        # 根节点两侧都有子节点，必须按翼分组画（否则右翼会被当成左翼，连出穿图的斜线）
        for side in (1, -1):
            group = [k for k in kids if k._side == side]
            if not group:
                continue
            base = (_SPINE_GRAY if depth == 0 else n._col[0]) if not xm else n._col[0]
            if connector == "xmind":
                if depth == 0:
                    # 根 -> 一级：XMind 的 curve 连接线，2pt 等宽（line-tapered: none）
                    for k in group:
                        p0, p3 = edge(n, k, gap, side)
                        bez(p0, p3, k._col[0], lw)
                else:
                    # 一级以下：XMind 的 roundedElbow —— 直角肘形、转角带圆角（joint="curve"）
                    if side == 1:
                        spine = min(k._x for k in group) - _SPINE * sc
                        start = (n._x + n._w + gap, n._cy)
                    else:
                        spine = max(k._x + k._w for k in group) + _SPINE * sc
                        start = (n._x - gap, n._cy)
                    for k in group:
                        tip = k._x - 2 if side == 1 else k._x + k._w + 2
                        pts = [start, (spine, n._cy), (spine, k._cy), (tip, k._cy)]
                        dr.line(pts, fill=base, width=lw, joint="curve")
            elif connector == "spine":
                # 同一父节点的同侧子节点共用一条竖主干，再用短横桩接到各自节点
                if side == 1:
                    spine = min(k._x for k in group) - _SPINE
                    start = (n._x + n._w + gap, n._cy)
                else:
                    spine = max(k._x + k._w for k in group) + _SPINE
                    start = (n._x - gap, n._cy)
                dr.line([start, (spine, n._cy)], fill=base, width=lw)
                ys = [k._cy for k in group]
                if max(ys) - min(ys) > 1:
                    dr.line([(spine, min(ys)), (spine, max(ys))], fill=base, width=lw)
                for k in group:
                    tip = k._x - 1 if side == 1 else k._x + k._w + 1
                    dr.line([(spine, k._cy), (tip, k._cy)],
                            fill=(k._col[0] if depth == 0 else base), width=lw)
            else:
                for k in group:
                    p0, p3 = edge(n, k, gap, side)
                    col = k._col[0] if depth == 0 else base
                    if connector == "straight":
                        dr.line([p0, p3], fill=col, width=lw)
                    else:
                        bez(p0, p3, col, lw)
        for k in kids:
            draw_conn(k, depth + 1)

    def draw_node(n, depth):
        main, tint, dark = n._col
        # XMind 默认的根只有文字、没有底框；一级分支才有实色圆角块
        boxed = depth in pill_h or (depth == 0 and not xm)
        bold = depth <= 1 if xm else depth <= 2
        f, lhx = font(depth, bold), lh(depth)
        bx, by = n._x, n._cy - n._h / 2
        fill = line = None
        text_fill = _XM_TEXT if xm else dark      # XMind 默认：各级都是黑字
        if n.in_case:                                  # 病例内容：黄底 + 下划线
            fill, line, text_fill = _CASE_FILL, _CASE_LINE, _CASE_TXT
        elif depth == 0:
            text_fill = _XM_TEXT if xm else "#1F2329"
        elif depth == 1:                               # 一级分支：实色圆角块
            fill, text_fill = main, (_XM_TEXT if xm else "#FFFFFF")
        elif depth in pill_h:                          # 二级（非 XMind 模式）：浅色条
            fill, text_fill = tint, dark
        if fill:
            if xm and depth == 1:                      # 照 XMind 的 roundedRect，不做胶囊
                radius = min(_XM_RADIUS, n._h // 4)
            elif depth in pill_h:
                radius = pill_h[depth] // 2
            else:
                radius = 18 if depth == 0 else 9 * _S
            dr.rounded_rectangle([bx, by, bx + n._w, by + n._h], radius=radius,
                                 fill=fill, outline=line, width=2 if line else 1)
        ty = n._cy - len(n._lines) * lhx / 2 + (lhx - _FS[min(depth, 4)] * sc) * 0.5
        for i, t in enumerate(n._lines):
            tw = f.getlength(t)
            if depth == 0:                             # 根：文字居中
                tx = bx + (n._w - tw) / 2
            elif xm and depth == 1:                    # 一级：XMind 是左对齐
                tx = bx + padx_map[depth]
            elif boxed:
                tx = bx + (n._w - tw) / 2
            else:
                tx = bx + padx_map[min(depth, 4)]
            dr.text((tx, ty + i * lhx), t, font=f, fill=text_fill, anchor="la")
            if n.in_case and not boxed and t.strip():
                uy = ty + i * lhx + _FS[min(depth, 4)] * sc * 1.02
                dr.line([(tx, uy), (tx + tw, uy)], fill=_CASE_LINE, width=2)
        for k in n.children:
            draw_node(k, depth + 1)

    if kids:
        draw_conn(root, 0)
    draw_node(root, 0)

    if legend_h:                              # 图例：老师一眼看懂黄色代表什么
        ly = H - legend_h + 9 * sc
        dr.rounded_rectangle([_MARGIN * sc, ly, _MARGIN * sc + 20 * sc, ly + 15 * sc],
                             radius=4 * sc, fill=_CASE_FILL, outline=_CASE_LINE, width=2)
        dr.text((_MARGIN * sc + 28 * sc, ly - 3 * sc),
                "黄底 + 下划线 = 本病例内容（不单列「结合病例」分支）",
                font=_load_font(int(round(14 * sc))), fill=_CASE_TXT)
    img = img.resize((max(1, W // _S), max(1, H // _S)), Image.LANCZOS)
    img.save(out_path)
    return out_path


# ---------------------------------------------------------------- XMind (.xmind)
def _default_xmind_theme() -> dict:
    """XMind 内置「基础思维导图」默认主题（Dawn）的副本。

    写进 content.json 后，无论使用者本机 XMind 当前选的是什么主题，
    打开都会回到这套默认样式：中心黑字无框、一级实色块黑字、二级以下纯文字。
    """
    import uuid
    palette = " ".join(_XMIND_COLORS)

    def ent(props: dict) -> dict:
        return {"id": uuid.uuid4().hex, "properties": props}

    return {
        "map": ent({"svg:fill": "#ffffff", "multi-line-colors": palette,
                    "color-list": palette, "line-tapered": "none"}),
        "centralTopic": ent({
            "fo:font-family": "NeverMind", "fo:font-size": "30pt",
            "fo:font-weight": "800", "fo:font-style": "normal",
            "fo:color": "inherited", "fo:text-transform": "manual",
            "fo:text-decoration": "none", "fo:text-align": "center",
            "svg:fill": "#000000", "fill-pattern": "none",
            "line-width": "2pt", "line-color": "#ADADAD", "line-pattern": "solid",
            "border-line-color": "#000000", "border-line-width": "0pt",
            "border-line-pattern": "inherited",
            "shape-class": "org.xmind.topicShape.roundedRect",
            "line-class": "org.xmind.branchConnection.curve",
            "arrow-end-class": "org.xmind.arrowShape.none",
            "alignment-by-level": "inherited"}),
        "mainTopic": ent({
            "fo:font-family": "NeverMind", "fo:font-size": "18pt",
            "fo:font-weight": "500", "fo:font-style": "normal",
            "fo:color": "inherited", "fo:text-transform": "manual",
            "fo:text-decoration": "none", "fo:text-align": "left",
            "svg:fill": "inherited", "fill-pattern": "solid",
            "line-width": "2pt", "line-color": "inherited",
            "line-pattern": "inherited", "border-line-color": "inherited",
            "border-line-width": "0pt", "border-line-pattern": "inherited",
            "shape-class": "org.xmind.topicShape.roundedRect",
            "line-class": "org.xmind.branchConnection.roundedElbow",
            "arrow-end-class": "inherited", "alignment-by-level": "inherited"}),
        "subTopic": ent({
            "fo:font-family": "NeverMind", "fo:font-size": "14pt",
            "fo:font-weight": "400", "fo:font-style": "normal",
            "fo:color": "inherited", "fo:text-transform": "manual",
            "fo:text-decoration": "none", "fo:text-align": "left",
            "svg:fill": "inherited", "fill-pattern": "none",
            "line-width": "2pt", "line-color": "inherited",
            "line-pattern": "inherited", "border-line-color": "inherited",
            "border-line-width": "0pt", "border-line-pattern": "inherited",
            "shape-class": "org.xmind.topicShape.roundedRect",
            "line-class": "org.xmind.branchConnection.roundedElbow",
            "arrow-end-class": "inherited", "alignment-by-level": "inherited"}),
        "level3": ent({
            "fo:font-family": "NeverMind", "fo:font-size": "14pt",
            "fo:font-weight": "400", "fo:font-style": "normal",
            "fo:text-transform": "manual", "fo:text-decoration": "none",
            "fo:text-align": "left", "fill-pattern": "solid",
            "line-width": "2pt", "line-pattern": "inherited",
            "border-line-width": "0pt", "border-line-pattern": "inherited",
            "shape-class": "org.xmind.topicShape.roundedRect",
            "line-class": "org.xmind.branchConnection.roundedElbow",
            "arrow-end-class": "inherited", "alignment-by-level": "inherited"}),
    }


def node_to_xmind(root: MindmapNode, sheet_title: str = "PBL 思维导图") -> bytes:
    """导出 XMind（2020+/ZEN）可直接打开的 .xmind 文件（zip: content.json 等）。"""
    import io
    import json
    import uuid
    import zipfile

    def topic(n: MindmapNode) -> dict:
        t = {"id": uuid.uuid4().hex, "class": "topic", "title": n.text}
        if n.in_case:                      # 病例内容在 XMind 里也着色，继续编辑时仍能认出
            t["style"] = {"id": uuid.uuid4().hex,
                          "properties": {"svg:fill": _CASE_FILL,
                                         "border-line-color": _CASE_LINE,
                                         "fo:color": _CASE_TXT}}
        if n.children:
            t["children"] = {"attached": [topic(c) for c in n.children]}
        return t

    root_topic = topic(root)
    root_topic["structureClass"] = "org.xmind.ui.map.clockwise"
    sheet = {"id": uuid.uuid4().hex, "class": "sheet", "title": sheet_title,
             "rootTopic": root_topic, "topicOverlapping": "overlap",
             "extensions": [{"provider": "org.xmind.ui.skeleton.structure.style",
                             "content": {"centralTopic": "org.xmind.ui.map.clockwise",
                                         "mainTopic": "org.xmind.ui.logic.right"}}],
             "theme": _default_xmind_theme()}
    content = json.dumps([sheet], ensure_ascii=False)
    meta = json.dumps({"creator": {"name": "PBL 工作台", "version": "1.0"}}, ensure_ascii=False)
    manifest = json.dumps({"file-entries": {"content.json": {}, "metadata.json": {}}})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("content.json", content)
        z.writestr("metadata.json", meta)
        z.writestr("manifest.json", manifest)
    return buf.getvalue()


# ---------------------------------------------------------------- markmap HTML
_MARKMAP_TPL = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>mindmap</title>
<style>svg.markmap{{width:100%;height:96vh}}</style>
<script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.18"></script>
</head><body><div class="markmap"><script type="text/template">
{md}
</script></div></body></html>"""


def node_to_markmap_html(root: MindmapNode) -> str:
    md = f"# {root.text}\n" + node_to_markdown(root)
    md = _CASE_MARK.sub(r"<mark>\1</mark>", md)   # markmap 里也高亮病例内容
    md = md.replace("</script>", "<\\/script>")
    return _MARKMAP_TPL.format(md=md)


# ---------------------------------------------------------------- 浏览器渲染成 PNG
_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    """找本机可用于无头截图的浏览器（Edge 一般都有）。"""
    for p in _BROWSERS:
        if Path(p).exists():
            return p
    return None


def render_png_browser(root: MindmapNode, out_path: str,
                       width: int = 2000, height: int = 2800,
                       timeout: int = 90) -> str | None:
    """用无头浏览器把 markmap 渲染成 PNG（d3 真曲线，观感最接近思维导图软件）。

    需要：本机有 Edge/Chrome + 能联网加载 markmap 的 CDN。失败返回 None。
    """
    import subprocess
    import tempfile

    exe = find_browser()
    if not exe:
        return None
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out = out.resolve()          # 浏览器按自己的 CWD 写文件，必须给绝对路径
    if out.exists():
        try:
            out.unlink()
        except OSError:
            pass
    with tempfile.TemporaryDirectory(prefix="mm_html_") as td:
        hp = Path(td) / "mm.html"
        hp.write_text(node_to_markmap_html(root), encoding="utf-8")
        cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--disable-extensions",
               f"--user-data-dir={Path(td) / 'prof'}",
               f"--window-size={width},{height}",
               "--virtual-time-budget=15000",
               f"--screenshot={out}", hp.as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout)
        except Exception:
            return None
    if not out.exists() or out.stat().st_size < 20000:
        return None
    try:                                   # 裁掉四周空白，图更紧凑
        from PIL import Image, ImageChops
        im = Image.open(out).convert("RGB")
        bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
        if bbox:
            m = 40
            im.crop((max(0, bbox[0] - m), max(0, bbox[1] - m),
                     min(im.width, bbox[2] + m), min(im.height, bbox[3] + m))).save(out)
    except Exception:
        pass
    return str(out)


# ---------------------------------------------------------------- FreeMind (.mm)
def node_to_freemind(root: MindmapNode) -> str:
    def esc(t: str) -> str:
        return sx.escape(t, {'"': "&quot;", "\n": " "})

    def walk(n: MindmapNode) -> str:
        kids = "".join(walk(c) for c in n.children)
        if kids:
            return f'<node TEXT="{esc(n.text)}">{kids}</node>'
        return f'<node TEXT="{esc(n.text)}"/>'

    return f'<map version="1.0.1">{walk(root)}</map>'
