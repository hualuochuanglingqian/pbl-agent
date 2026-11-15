"""用本机 PowerPoint（COM）把 pptx 导出为 PNG 预览图。

通过 PowerShell 调用，无需 pywin32。未装 Office / 调用失败时返回空列表，界面优雅降级。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


def export_pngs(pptx_path: str, out_dir: str, width: int = 1600, height: int = 900) -> list[str]:
    """把 pptx 每页导出 PNG 到 out_dir，返回按页码排序的文件路径列表。"""
    src = Path(pptx_path).resolve()
    dest = Path(out_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    ps = f"""
$ErrorActionPreference = "Stop"
try {{ $app = New-Object -ComObject PowerPoint.Application }} catch {{ Write-Output "NO_OFFICE"; exit 0 }}
$pres = $app.Presentations.Open("{src}", -1, 0, 0)
$pres.Export("{dest}", "PNG", {width}, {height})
$pres.Close()
$app.Quit()
Write-Output "OK"
"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=180, encoding="utf-8", errors="ignore",
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if "OK" not in (r.stdout or ""):
        return []

    def page_no(p: Path) -> int:
        m = re.search(r"(\d+)", p.stem)
        return int(m.group(1)) if m else 0

    return [str(p) for p in sorted(dest.glob("*.png"), key=page_no)]
