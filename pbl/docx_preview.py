"""用本机 Word（COM）把 docx 导出为 PDF，再用 PyMuPDF 转成 PNG 预览图。

通过 PowerShell 调用 COM，无需 pywin32。未装 Office / 调用失败返回空列表或空串。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _word_to_pdf(src: Path, pdf: Path) -> bool:
    """用本机 Word（COM）把 docx 另存为 PDF；成功返回 True。"""
    ps = f"""
$ErrorActionPreference = "Stop"
try {{ $app = New-Object -ComObject Word.Application }} catch {{ Write-Output "NO_OFFICE"; exit 0 }}
$app.Visible = $false
$doc = $app.Documents.Open("{src}", $false, $true)
$doc.SaveAs2("{pdf}", 17)
$doc.Close($false)
Write-Output "OK"
try {{ $app.Quit() }} catch {{ }}
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="ignore",
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if "NO_OFFICE" in (r.stdout or ""):
        return False
    return pdf.exists()


def export_pdf(docx_path: str, out_path: str | None = None) -> str:
    """把 docx 转成 PDF，返回 PDF 路径；未装 Office / 转换失败返回空串。

    out_path 缺省时输出到 docx 同目录同名 .pdf（覆盖旧文件）。
    """
    src = Path(docx_path).resolve()
    if not src.exists():
        return ""
    pdf = Path(out_path).resolve() if out_path else src.with_suffix(".pdf")
    if pdf == src:
        return ""
    return str(pdf) if _word_to_pdf(src, pdf) else ""


def export_pngs(docx_path: str, out_dir: str, width: int = 1200) -> list[str]:
    src = Path(docx_path).resolve()
    dest = Path(out_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    pdf = dest / (src.stem + ".pdf")
    for old in dest.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    if not _word_to_pdf(src, pdf):
        return []

    import fitz
    out = []
    doc = fitz.open(str(pdf))
    for page in doc:
        zoom = width / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        png = dest / f"page{page.number + 1:02d}.png"
        pix.save(str(png))
        out.append(str(png))
    doc.close()
    return out
