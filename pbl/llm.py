"""多供应商 LLM 封装：智谱（默认）/ DeepSeek / OpenAI 及任意 OpenAI 兼容端点。

.env 配置：
  PBL_PROVIDER=zhipu | deepseek | openai | custom   （默认 zhipu）
  PBL_MODEL=...        缺省值：glm-4-flash / deepseek-chat / gpt-4o-mini
  DEEPSEEK_API_KEY=... 或 OPENAI_API_KEY=...（custom 用 PBL_API_KEY + PBL_BASE_URL）

对话接口统一为 OpenAI 兼容形态；Embedding（教材 RAG 向量化）固定使用智谱
embedding-3——DeepSeek/OpenAI 不作为向量来源，因此 ZHIPUAI_API_KEY 仍需配置。

网络策略：国内端点（zhipu/deepseek）强制直连不受系统代理影响；
openai/custom 默认跟随系统代理（可用 PBL_TRUST_ENV=0 强制直连）。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from . import net

load_dotenv()  # 读取项目根目录 .env

PROVIDER = os.getenv("PBL_PROVIDER", "zhipu").strip().lower() or "zhipu"
EMBED_MODEL = os.getenv("PBL_EMBED_MODEL", "embedding-3")
TEMPERATURE = float(os.getenv("PBL_TEMPERATURE", "0.6"))

_DEFAULT_MODEL = {"zhipu": "glm-4-flash", "deepseek": "deepseek-chat", "openai": "gpt-4o-mini"}
_DEFAULT_BASE = {"deepseek": "https://api.deepseek.com", "openai": "https://api.openai.com/v1"}
_KEY_ENV = {"zhipu": "ZHIPUAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY"}

MODEL = os.getenv("PBL_MODEL", "").strip() or _DEFAULT_MODEL.get(PROVIDER, "glm-4-flash")

# 最近一次失败的真实原因：界面上的「模型未返回有效内容」只有配上它才有诊断价值。
_LAST_ERROR = ""
_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "pbl.log"


def last_error() -> str:
    return _LAST_ERROR


def error_hint() -> str:
    """给调用方的可读原因，直接拼到失败提示后面；无错误时返回空串。"""
    return f"（原因：{_LAST_ERROR}）" if _LAST_ERROR else ""


def _classify(e: Exception, provider: str | None = None,
              model: str | None = None) -> tuple[str, bool]:
    """异常 -> (可读原因, 是否值得重试)。鉴权/余额/模型不存在属于确定性失败，重试无意义。"""
    prov = provider or PROVIDER
    mdl = model or MODEL
    status = getattr(e, "status_code", None)
    body = str(e)
    low = body.lower()
    if status == 402 or "insufficient balance" in low:
        return (f"API 账户余额不足（{prov} 402），请充值或在 .env 改用其他供应商"
                "（如 PBL_PROVIDER=zhipu、PBL_MODEL=glm-4-flash）"), False
    if status == 401:
        return "API Key 无效或已过期（401），请检查 .env 中的 Key", False
    if status == 403:
        return "API 拒绝访问（403），请检查 Key 权限或该模型是否已开通", False
    if status == 404:
        return f"模型或接口不存在（404）：{mdl}", False
    if status == 429:
        return "请求过于频繁或额度用尽（429），请稍后重试", True
    if type(e).__name__ in ("APIConnectionError", "ConnectError", "ReadTimeout",
                            "ConnectTimeout", "RemoteProtocolError"):
        hint = ""
        if "EOF occurred in violation" in body or "RemoteProtocolError" in type(e).__name__:
            hint = "（TLS 被中断：若本机开着 Clash/FlClash 等 TUN 代理，请切回「规则模式」或退出代理后重试）"
        return "无法连接 API 端点（网络或代理问题）" + hint, True
    return f"{type(e).__name__}: {body[:200]}", True


def _record_error(kind: str, e: Exception, provider: str | None = None,
                  model: str | None = None) -> bool:
    """记下原因并写日志，返回是否值得重试。"""
    global _LAST_ERROR
    msg, retryable = _classify(e, provider, model)
    _LAST_ERROR = msg
    try:
        _LOG_FILE.parent.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _LOG_FILE.open("a", encoding="utf-8").write(f"[{stamp}] llm {kind} 失败：{msg}\n")
    except OSError:
        pass
    return retryable


def _api_key() -> str:
    key = os.getenv(_KEY_ENV.get(PROVIDER, "PBL_API_KEY"), "").strip()
    return key or os.getenv("PBL_API_KEY", "").strip()


def _base_url() -> str | None:
    return os.getenv("PBL_BASE_URL", "").strip() or _DEFAULT_BASE.get(PROVIDER)


def has_key() -> bool:
    return bool(_api_key())


def has_key_for(provider: str | None = None) -> bool:
    """指定 provider（缺省=全局）是否配置了 API Key——UI 提示用。"""
    prov = (provider or PROVIDER).strip().lower()
    if prov == "zhipu":
        return bool(os.getenv("ZHIPUAI_API_KEY", "").strip())
    return bool(os.getenv(_KEY_ENV.get(prov, "PBL_API_KEY"), "").strip()
                or os.getenv("PBL_API_KEY", "").strip())


_zhipu = None   # zhipuai SDK 实例（对话 zhipu 模式 + 全部 embedding）
_oai = None     # openai SDK 实例（deepseek/openai/custom 对话）


def _reset_client() -> None:
    """Drop a stale cached client after a retryable connection failure."""
    global _zhipu, _oai
    for cached in (_zhipu, _oai):
        if cached is not None:
            try:
                http = getattr(cached, "_client", None)
                if http is not None:
                    http.close()
            except Exception:
                pass
    _zhipu = None
    _oai = None


def _zhipu_client():
    global _zhipu
    if _zhipu is None:
        key = os.getenv("ZHIPUAI_API_KEY", "").strip()
        if not key:
            return None
        import httpx
        from zhipuai import ZhipuAI
        # 智谱是国内服务：强制直连，不受本机系统代理（Clash等）状态影响
        _zhipu = ZhipuAI(
            api_key=key,
            http_client=httpx.Client(trust_env=False, timeout=httpx.Timeout(180.0, connect=10.0)),
        )
    return _zhipu


def client():
    """对话客户端。zhipu 返回 zhipuai SDK，其余返回 openai SDK——两者 chat.completions 接口同形。"""
    global _oai
    if PROVIDER == "zhipu":
        return _zhipu_client()
    if _oai is None:
        key = _api_key()
        if not key:
            return None
        from openai import OpenAI
        import httpx
        # 本机系统代理可能中断某些中转站的 TLS 握手（EOF in protocol）；
        # 在 .env 设 PBL_TRUST_ENV=0 可强制直连，不再读取系统代理。
        trust_env = os.getenv("PBL_TRUST_ENV", "").strip() != "0" and (
            PROVIDER == "openai" or PROVIDER == "custom")
        http = httpx.Client(trust_env=trust_env, timeout=httpx.Timeout(180.0, connect=15.0))
        kw = {"api_key": key, "http_client": http}
        base = _base_url()
        if base:
            kw["base_url"] = base
        _oai = OpenAI(**kw)
    return _oai


def _resolve(provider: str | None, model: str | None) -> tuple[str, str, bool]:
    """解析按次指定：任一显式传入即走 override 路径；返回 (provider, model, overridden)。"""
    if provider is None and model is None:
        if _ACTIVE_OVERRIDE[0] or _ACTIVE_OVERRIDE[1]:      # ②区等面板选定的活动模型
            prov = _ACTIVE_OVERRIDE[0] or PROVIDER
            mdl = _ACTIVE_OVERRIDE[1] or _DEFAULT_MODEL.get(prov, MODEL)
            return prov, mdl, True
        return PROVIDER, MODEL, False
    prov = (provider or PROVIDER).strip().lower()
    mdl = (model or "").strip()
    if not mdl:
        mdl = _DEFAULT_MODEL.get(prov, "") if provider else MODEL
    return prov, mdl or MODEL, True


_ACTIVE_OVERRIDE = (None, None)     # 界面选定的活动模型（如 ②区内容生成用 DeepSeek）


def set_active_model(provider: str | None, model: str | None) -> None:
    """设定后续所有未显式指定 provider/model 的调用的生效模型；传 (None, None) 恢复 .env 默认。"""
    global _ACTIVE_OVERRIDE
    _ACTIVE_OVERRIDE = ((provider or "").strip().lower() or None,
                        (model or "").strip() or None)


def active_model_label() -> str:
    """当前生效模型的可读标签（UI 提示用）。"""
    prov, mdl, overridden = _resolve(None, None)
    short = mdl.rsplit("/", 1)[-1]
    return f"{prov}/{short}" + ("（界面选定）" if overridden else "")


_override_clients: dict = {}    # (provider, model) -> openai SDK 实例


def _client_for(provider: str, model: str):
    """按次指定 provider/model 的独立客户端；zhipu 复用全局实例（Key 同源）。"""
    if provider == "zhipu":
        return _zhipu_client()
    cli = _override_clients.get((provider, model))
    if cli is not None:
        return cli
    key = os.getenv(_KEY_ENV.get(provider, "PBL_API_KEY"), "").strip() \
        or os.getenv("PBL_API_KEY", "").strip()
    if not key:
        return None
    from openai import OpenAI
    import httpx
    trust_env = os.getenv("PBL_TRUST_ENV", "").strip() != "0" and (
        provider == "openai" or provider == "custom")
    http = httpx.Client(trust_env=trust_env, timeout=httpx.Timeout(180.0, connect=15.0))
    kw = {"api_key": key, "http_client": http}
    base = os.getenv("PBL_BASE_URL", "").strip() or _DEFAULT_BASE.get(provider)
    if base:
        kw["base_url"] = base
    cli = OpenAI(**kw)
    _override_clients[(provider, model)] = cli
    return cli


def _drop_client(provider: str, model: str) -> None:
    cli = _override_clients.pop((provider, model), None)
    if cli is not None:
        try:
            http = getattr(cli, "_client", None)
            if http is not None:
                http.close()
        except Exception:
            pass


def chat(prompt: str, system: str | None = None, temperature: float | None = None,
         provider: str | None = None, model: str | None = None) -> str | None:
    """普通对话。无 Key 或调用失败返回 None，由调用方降级。含一次网络重试。

    provider/model 可按次覆盖全局 .env 配置（如海报 PPT 单独走 DeepSeek）。
    """
    global _LAST_ERROR
    _LAST_ERROR = ""
    prov, mdl, overridden = _resolve(provider, model)
    cli = _client_for(prov, mdl) if overridden else client()
    if cli is None:
        _LAST_ERROR = "未配置 API Key"
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    net.ensure_network()
    for attempt in range(2):
        try:
            resp = cli.chat.completions.create(
                model=mdl,
                messages=messages,
                temperature=TEMPERATURE if temperature is None else temperature,
            )
            _LAST_ERROR = ""
            return resp.choices[0].message.content
        except Exception as e:  # 网络/限流等，重试一次后降级
            print(f"[llm] chat 失败: {e}")
            retryable = _record_error("chat", e, prov, mdl)
            if attempt == 0 and retryable:
                _drop_client(prov, mdl) if overridden else _reset_client()
                cli = _client_for(prov, mdl) if overridden else client()
                time.sleep(1.5)
                net.ensure_network()
                continue
            break
    return None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _extract_json(text: str) -> str:
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text


def chat_json(prompt: str, schema_model, retries: int = 2, system: str | None = None,
              temperature: float | None = None,
              provider: str | None = None, model: str | None = None):
    """要求模型输出 JSON 并用 Pydantic 校验；校验失败把错误信息喂回去重试。

    provider/model 可按次覆盖全局 .env 配置。
    返回 schema_model 实例，失败（无 Key/解析失败）返回 None。
    """
    global _LAST_ERROR
    _LAST_ERROR = ""
    prov, mdl, overridden = _resolve(provider, model)
    full = prompt + (
        "\n\n【输出要求】只输出一个 JSON 对象，不要任何解释文字、"
        "不要使用 Markdown 代码块以外的多余内容。JSON 字段必须用双引号。"
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": full})
    error_note = ""
    for _ in range(retries + 1):
        cli = _client_for(prov, mdl) if overridden else client()
        if cli is None:
            _LAST_ERROR = "未配置 API Key"
            return None
        net.ensure_network()
        try:
            resp = cli.chat.completions.create(
                model=mdl, messages=messages,
                temperature=TEMPERATURE if temperature is None else temperature,
            )
            content = resp.choices[0].message.content
        except Exception as e:
            print(f"[llm] chat_json 调用失败: {e}")
            retryable = _record_error("chat_json", e, prov, mdl)
            if not retryable:
                return None
            _drop_client(prov, mdl) if overridden else _reset_client()
            time.sleep(1.5)
            net.ensure_network()
            continue
        if not (content or "").strip():
            _LAST_ERROR = "模型返回了空内容（finish_reason=" \
                          f"{getattr(resp.choices[0], 'finish_reason', '?')}）"
            continue
        try:
            # strict=False：容忍模型在字符串值里输出裸换行（否则触发整段重试，慢 2~3 倍）
            data = json.loads(_extract_json(content), strict=False)
            parsed = schema_model.model_validate(data)
            _LAST_ERROR = ""
            return parsed
        except Exception as e:
            error_note = f"上一次输出不是合法 JSON 或字段不符合要求（错误：{e}）。请严格按字段要求重新输出。"
            _LAST_ERROR = f"模型输出无法解析为要求的 JSON（{type(e).__name__}）"
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": error_note})
    return None


def embed(texts: list[str]) -> list[list[float]] | None:
    """批量向量化（教材 RAG 专用，固定智谱 embedding-3）。失败返回 None。"""
    cli = _zhipu_client()
    if cli is None:
        return None
    net.ensure_network()
    for attempt in range(2):
        try:
            resp = cli.embeddings.create(model=EMBED_MODEL, input=texts)
            return [d.embedding for d in resp.data]
        except Exception as e:
            print(f"[llm] embedding 失败: {e}")
            if attempt == 0:
                time.sleep(1.5)
                net.ensure_network()
    return None
