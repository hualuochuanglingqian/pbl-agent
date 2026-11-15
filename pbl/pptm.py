"""PPT Master（ppt-master-comfyui）集成。

分工说明：
- 本模块负责「项目准备」——把案例内容导出为技能项目（素材 Markdown、图片、
  Stage-1 沟通契约），并可一键启动它的确认页；
- 「设计规格 + 逐页排版 + 质检 + 导出」由智能体按技能流程在对话中完成
  （技能要求页面设计由主智能体执行，Streamlit 内无法代跑）。

技能目录可用环境变量 PBL_PPTM_SKILL 覆盖；未安装时 available() 返回 False。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .schema import Case, PaperShare, Reference

SKILL_DIR = Path(os.getenv(
    "PBL_PPTM_SKILL", r"C:\Users\38311\.zcode\skills\ppt-master-comfyui"))
PROJECTS_ROOT = Path(__file__).resolve().parent.parent / "pptm_projects"
CANVAS = "ppt169"          # 16:9 1280x720


def available() -> bool:
    return (SKILL_DIR / "scripts" / "project_manager.py").exists()


def _run(args: list, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="ignore", timeout=timeout)


def _ref_line(r: Reference) -> str:
    if r.ref_type == "M":
        authors = ", ".join(r.authors)
        head = f"{authors}. " if authors else ""
        ed = f"{r.edition}. " if r.edition else ""
        pub = f"{r.place}: {r.publisher}" if r.publisher else r.place
        tail = f"{r.year}" + (f": {r.pages}" if r.pages else "")
        body = f"{pub}, {tail}." if pub else f"{tail}."
        return f"{head}{r.title}[M]. {ed}{body}"
    authors = ", ".join(r.authors[:3]) + (", et al" if len(r.authors) > 3 else "")
    vol = f", {r.volume}" if r.volume else ""
    iss = f"({r.issue})" if r.issue else ""
    pg = f": {r.pages}" if r.pages else ""
    doi = f" DOI: {r.doi}." if r.doi else ""
    return (f"{authors}. {r.title}[J]. {r.journal}, {r.year}{vol}{iss}{pg}." + doi)


def _question_markdown(case: Case, author: str = "") -> str:
    lines = [f"# PBL 问题 PPT 素材：{case.title}", ""]
    lines.append(f"课程：{case.course}　教学对象：{case.audience}")
    if author:
        lines.append(f"制作人：{author}")
    lines += ["", "## 病例摘要", case.case_text, ""]
    for q in case.questions:
        lines += [f"## 问题 {q.qid}：{q.text}", f"类型：{q.qtype}", "", "### 学习内容"]
        for b in q.learning:
            if b.heading:
                lines.append(f"**{b.heading}**")
            if b.body:
                lines.append(b.body)
            if b.table:
                head = b.table[0]
                lines.append("| " + " | ".join(head) + " |")
                lines.append("|" + "---|" * len(head))
                for row in b.table[1:]:
                    lines.append("| " + " | ".join(row) + " |")
            if b.source:
                lines.append(f"（出处：{b.source}）")
            lines.append("")
        lines += ["### 临床应用", q.clinical or "（无）", "", "### 参考文献"]
        for i, r in enumerate(q.references, 1):
            lines.append(f"[{i}] {_ref_line(r)}")
        lines.append("")
    return "\n".join(lines)


def _literature_markdown(paper: PaperShare) -> str:
    lines = [f"# 文献分享 PPT 素材：{paper.title_cn}", ""]
    lines.append(f"英文标题：{paper.title_en}")
    lines.append(f"期刊：{paper.journal} {paper.year}（IF≈{paper.if_value}）")
    lines.append("分享时长：5 分钟")
    lines += ["", "## 一、研究背景", paper.background or "（无）", ""]
    lines += ["## 二、研究思路与过程", paper.approach, ""]
    lines += ["## 三、研究结论", paper.conclusion or "（无）", ""]
    lines += ["## 四、与病例的联系", paper.case_link or "（无）", ""]
    lines += ["## 关键图（用户提供图片）"]
    for f in paper.figures:
        if f.selected and Path(f.file).exists():
            lines.append(f"- {f.label}：{f.note}（图片：{Path(f.file).name}）")
    lines += ["", "## 参考文献", f"[1] {paper.ref_text}"]
    return "\n".join(lines)


def _stage1(canvas_title: str, audience: str, intent: str, outcome: str,
            message: str, delivery: str, afterlife: str) -> dict:
    return {
        "stage": "stage1", "lang": "zh", "primary_language": "zh-CN",
        "recommend": {"canvas": CANVAS},
        "audience": {"value": audience},
        "communication_intent": {"value": intent},
        "audience_outcome": {"value": outcome},
        "core_message": {"value": message},
        "delivery_context": {"value": delivery},
        "artifact_afterlife": {"value": afterlife},
        "content_divergence": {"value": ""},
        "template_options": {"schema_version": 1, "phase": "template", "lang": "zh",
                             "explicit_workspace_roots": [], "default_mode": "free_design"},
    }


def _unique_name(base: str) -> str:
    """避免同日重复导出互相覆盖：已存在同日项目时追加 v2/v3…"""
    stamp = datetime.now().strftime("%Y%m%d")
    name, k = base, 2
    while (PROJECTS_ROOT / f"{name}_{CANVAS}_{stamp}").exists():
        name = f"{base}_v{k}"
        k += 1
    return name


def _prepare(name: str, markdown: str, stage1: dict) -> Path:
    """初始化项目 -> 导入素材 -> 写 Stage-1 契约，返回项目路径。"""
    if not available():
        raise RuntimeError(f"未找到 ppt-master-comfyui 技能目录：{SKILL_DIR}")
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    name = _unique_name(name)
    r = _run([str(SKILL_DIR / "scripts" / "project_manager.py"), "init", name,
              "--format", CANVAS, "--dir", str(PROJECTS_ROOT)])
    m = re.search(r"Project initialized:\s*(.+)", r.stdout or "")
    if not m:
        raise RuntimeError(f"项目初始化失败：{(r.stderr or r.stdout)[:300]}")
    proj = Path(m.group(1).strip())
    # 素材：写入临时 md 后导入（导入器会做规范化）
    tmp = PROJECTS_ROOT / "_tmp_sources"
    tmp.mkdir(exist_ok=True)
    src = tmp / f"{name}.md"
    src.write_text(markdown, encoding="utf-8")
    try:
        _run([str(SKILL_DIR / "scripts" / "project_manager.py"), "import-sources",
              str(proj), str(src)])
    finally:
        src.unlink(missing_ok=True)
    # Stage-1 契约（确认页读取的位置）
    cu = proj / "confirm_ui"
    cu.mkdir(exist_ok=True)
    (cu / "recommendations.stage1.json").write_text(
        json.dumps(stage1, ensure_ascii=False, indent=2), encoding="utf-8")
    return proj


def export_question_project(case: Case, author: str = "") -> Path:
    """导出「问题 PPT」项目（素材 + 沟通契约）。"""
    from . import pipeline
    pipeline.sync_source_refs(case)
    stage1 = _stage1(
        "问题 PPT", f"{case.audience}与带教老师（PBL 课堂）",
        f"按分工汇报本人负责的 {len(case.questions)} 个问题：给出有条理的知识要点与可靠出处，并落到本病例的应用",
        "听众能快速理解每个问题的答案、依据，以及它对本例诊疗判断的意义",
        "每个问题都能用教材与文献证据回答，并与本例的具体临床信息对应",
        "课堂现场主讲，本人约 5~8 分钟，投影为主",
        "课后复习材料，与 Word 教案一并留存")
    return _prepare("PBL问题PPT", _question_markdown(case, author), stage1)


def export_literature_project(case: Case, paper: PaperShare) -> Path:
    """导出「文献分享 PPT」项目（素材 + 图片 + 沟通契约）。"""
    stage1 = _stage1(
        "文献分享", f"{case.audience}与带教老师（PBL 课堂）",
        "用 5 分钟讲清一篇研究文献：研究问题、思路、关键证据与结论，并联系本病例",
        "听众能说出这篇文献证明了什么、证据来自哪几张图、对本例有何启发",
        "该文献的核心结论及其对本案机制/诊疗判断的解释价值",
        "课堂现场主讲，限时 5 分钟，投影为主",
        "文献分享存档，作为小组作业的一部分")
    proj = _prepare("文献分享PPT", _literature_markdown(paper), stage1)
    imgs = proj / "images"
    imgs.mkdir(exist_ok=True)
    for f in paper.figures:
        if f.selected and Path(f.file).exists():
            shutil.copy2(f.file, imgs / Path(f.file).name)
    if any(imgs.iterdir()):
        _run([str(SKILL_DIR / "scripts" / "analyze_images.py"), str(imgs)])
    return proj


def launch_confirm(proj: Path) -> str:
    """启动技能自带的确认页（后台常驻），返回访问 URL；已在运行则直接复用。"""
    alive, url = server_status(proj)
    if alive and url:
        return url
    r = _run([str(SKILL_DIR / "scripts" / "confirm_ui" / "server.py"), str(proj),
              "--daemon"], timeout=120)
    m = re.search(r"(https?://127\.0\.0\.1:\d+)", (r.stdout or "") + (r.stderr or ""))
    if not m:
        raise RuntimeError(f"确认页启动失败：{(r.stderr or r.stdout)[:300]}")
    return m.group(1)


def _probe(url: str, timeout: float = 1.5) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= getattr(r, "status", 200) < 500
    except Exception:
        return False


def server_status(proj: Path) -> tuple[bool, str | None]:
    """确认页是否在运行：返回 (是否运行, URL)。URL 取自 server.log 的最后一次启动。"""
    log = Path(proj) / "confirm_ui" / "server.log"
    url = None
    if log.exists():
        try:
            text = log.read_text(encoding="utf-8", errors="ignore")
            hits = re.findall(r"Running on (https?://127\.0\.0\.1:\d+)", text)
            if hits:
                url = hits[-1]
        except OSError:
            pass
    if url and _probe(url):
        return True, url
    return False, url


def _port_of(url: str | None) -> int | None:
    if not url:
        return None
    m = re.search(r":(\d+)/?$", url.strip())
    return int(m.group(1)) if m else None


def _kill_port(port: int) -> bool:
    """兜底：按端口结束占用进程（技能自己的 pid 记录丢失时使用）。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
             f"-ErrorAction SilentlyContinue | Select-Object -First 1 OwningProcess; "
             f"if ($c) {{ Stop-Process -Id $c.OwningProcess -Force; Write-Output 'KILLED' }}"],
            capture_output=True, text=True, timeout=30)
        return "KILLED" in (r.stdout or "")
    except Exception:
        return False


def shutdown_confirm(proj: Path) -> None:
    """关闭该项目的确认页服务（幂等）；进程记录丢失时按端口兜底结束。"""
    alive, url = server_status(proj)
    _run([str(SKILL_DIR / "scripts" / "confirm_ui" / "server.py"), str(proj),
          "--shutdown"], timeout=60)
    if alive and url:
        port = _port_of(url)
        if port:
            _kill_port(port)
