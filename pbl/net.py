"""网络自愈：检测到系统代理已配置但代理隧道不可用时，自动绕过代理直连。

背景：Windows 上 Clash/V2Ray 等工具会写入系统代理；代理进程可能"端口还在听、
隧道全坏"（上游节点失效），表现为所有 HTTPS 请求 ProxyError / Connection error。
因此用真实 HTTPS 请求探测，坏则设 NO_PROXY 直连（智谱与 PubMed 均可直连）。
"""
from __future__ import annotations

import os
import time
import urllib.request

import requests

_last_check = 0.0
_bypassed = False


def _proxy_works(timeout: float = 5.0) -> bool:
    """经当前代理设置发起真实请求：ProxyError 判为代理坏，其余视为代理可用。"""
    try:
        requests.head("https://www.baidu.com", timeout=timeout)
        return True
    except requests.exceptions.ProxyError:
        return False
    except requests.exceptions.RequestException:
        return True  # 出网了但目标异常（超时/4xx），说明代理本身能通
    except Exception:
        return True


def direct_mode() -> bool:
    """当前是否处于「绕过代理直连」状态。

    注意：requests 在 Windows 上读注册表代理，NO_PROXY="*" 对它无效，
    因此 requests 调用方需用 Session.trust_env = not direct_mode() 显式绕行。
    httpx（zhipuai/openai SDK）则能识别 NO_PROXY 环境变量。
    """
    return _bypassed


def ensure_network() -> None:
    """进程内每 60 秒最多探测一次；代理隧道坏→设 NO_PROXY 直连，恢复→自动撤销。"""
    global _last_check, _bypassed
    if time.time() - _last_check < 60:
        return
    _last_check = time.time()
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        return
    target = proxies.get("https") or proxies.get("http")
    if not target:
        if _bypassed:  # 代理设置已被移除，恢复默认
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            _bypassed = False
        return
    if _proxy_works():
        if _bypassed:  # 代理恢复，撤掉直连标记
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            _bypassed = False
        return
    if not _bypassed:
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        _bypassed = True
        print("[net] 系统代理隧道不可用，已自动改为直连")
