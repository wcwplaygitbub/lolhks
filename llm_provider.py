# -*- coding: utf-8 -*-
"""统一的 LLM Provider 抽象层

支持：
- gemini   : Google Gemini (google-genai)
- glm      : 智谱 GLM (OpenAI 兼容)
- minimax  : MiniMax (OpenAI 兼容 chat/completions)
- openai   : 任意 OpenAI 兼容端点 (DeepSeek / Qwen / Ollama / OpenAI 等)

只有 Gemini 支持 Vision（图片输入）。其它 Provider 的图片调用会抛 NotImplementedError，
调用方应在无 Vision 支持时回退到纯文本逻辑。
"""
from __future__ import annotations

import logging
import time as _time
import ssl  # noqa: F401
import concurrent.futures
from typing import List, Optional

import config

log = logging.getLogger("ARAM")

# ==================== 通用重试封装 ====================
_RETRY_DELAY = 1.0


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "unexpected_eof" in msg
        or "ssleoferror" in msg
        or "eof occurred" in msg
        or "timeout" in msg
        or "timed out" in msg
        or isinstance(exc, concurrent.futures.TimeoutError)
    )


def _run_with_timeout(fn, *, hard_timeout: Optional[float], **kwargs):
    if not hard_timeout:
        return fn(**kwargs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: fn(**kwargs))
        return future.result(timeout=hard_timeout)


def _with_retry(fn, *, label: str, hard_timeout: Optional[float], max_retries: int, **kwargs):
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            return _run_with_timeout(fn, hard_timeout=hard_timeout, **kwargs)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if _is_retryable(e) and attempt < max_retries:
                reason = "超时" if isinstance(e, (concurrent.futures.TimeoutError, TimeoutError)) else "瞬态错误"
                log.warning(f"[{label}] {reason} ({attempt + 1}/{max_retries})，{_RETRY_DELAY}s 后重试...")
                _time.sleep(_RETRY_DELAY)
            else:
                raise
    raise last_exc  # type: ignore[misc]


# ==================== Provider 基类 ====================
class LLMProvider:
    name: str = "base"
    supports_vision: bool = False

    def generate_text(
        self,
        parts: List[str],
        *,
        temperature: float = 0.4,
        hard_timeout: Optional[float] = None,
        max_retries: int = 2,
        label: str = "LLM",
    ) -> str:
        raise NotImplementedError

    def generate_with_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        parts: List[str],
        *,
        temperature: float = 0.2,
        hard_timeout: Optional[float] = None,
        max_retries: int = 1,
        label: str = "LLM-Vision",
    ) -> str:
        raise NotImplementedError(f"Provider {self.name} 不支持 Vision，请使用 Gemini 或改走纯文本流程")


# ==================== Gemini ====================
class GeminiProvider(LLMProvider):
    name = "gemini"
    supports_vision = True

    def __init__(self, api_key: str, model: str):
        from google import genai  # lazy import
        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def _call(self, contents, temperature):
        from google.genai import types
        return self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(temperature=temperature),
        )

    def generate_text(self, parts, *, temperature=0.4, hard_timeout=None, max_retries=2, label="Gemini"):
        resp = _with_retry(
            self._call, label=label, hard_timeout=hard_timeout, max_retries=max_retries,
            contents=list(parts), temperature=temperature,
        )
        return resp.text

    def generate_with_image(self, image_bytes, mime_type, parts, *, temperature=0.2, hard_timeout=None, max_retries=1, label="Gemini-Vision"):
        from google.genai import types
        contents = [types.Part.from_bytes(data=image_bytes, mime_type=mime_type)] + list(parts)
        resp = _with_retry(
            self._call, label=label, hard_timeout=hard_timeout, max_retries=max_retries,
            contents=contents, temperature=temperature,
        )
        return resp.text


# ==================== OpenAI 兼容（GLM / MiniMax / 通用） ====================
class OpenAICompatProvider(LLMProvider):
    """用原始 requests 调用 OpenAI 兼容 /chat/completions 端点。"""
    supports_vision = False

    def __init__(self, name: str, api_key: str, model: str, base_url: str):
        self.name = name
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    def _call(self, messages, temperature):
        import requests
        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload = {"model": self._model, "messages": messages, "temperature": temperature}
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"{self.name} 响应结构异常: {data}") from e

    def generate_text(self, parts, *, temperature=0.4, hard_timeout=None, max_retries=2, label=None):
        label = label or self.name
        # 把多段内容拼成 system + user
        user_content = "\n\n".join(p for p in parts if p)
        messages = [{"role": "user", "content": user_content}]
        return _with_retry(
            self._call, label=label, hard_timeout=hard_timeout, max_retries=max_retries,
            messages=messages, temperature=temperature,
        )


# ==================== 工厂 ====================
_provider_cache: dict[str, LLMProvider] = {}


def _cfg(name: str, default=None):
    """优先 config.py 的属性，其次环境变量，最后默认值。"""
    import os
    val = getattr(config, name, None)
    if val is None or val == "":
        val = os.environ.get(name, None)
    return default if (val is None or val == "") else val


def get_provider(name: Optional[str] = None) -> LLMProvider:
    """按名称获取 Provider；不传则取 config.LLM_PROVIDER 或环境变量 LLM_PROVIDER。"""
    name = (name or _cfg("LLM_PROVIDER", "gemini") or "gemini").lower()
    if name in _provider_cache:
        return _provider_cache[name]

    if name == "gemini":
        key = _cfg("GEMINI_API_KEY", "")
        model = _cfg("GEMINI_MODEL", "gemini-2.0-flash")
        if not key or key == "YOUR_API_KEY_HERE":
            raise RuntimeError("GEMINI_API_KEY 未配置")
        p = GeminiProvider(key, model)

    elif name == "glm":
        key = _cfg("GLM_API_KEY", "")
        model = _cfg("GLM_MODEL", "glm-4-flash")
        base = _cfg("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
        if not key:
            raise RuntimeError("GLM_API_KEY 未配置")
        p = OpenAICompatProvider("glm", key, model, base)

    elif name == "minimax":
        key = _cfg("MINIMAX_API_KEY", "")
        model = _cfg("MINIMAX_MODEL", "abab6.5s-chat")
        base = _cfg("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
        if not key:
            raise RuntimeError("MINIMAX_API_KEY 未配置")
        p = OpenAICompatProvider("minimax", key, model, base)

    elif name == "openai":
        key = _cfg("OPENAI_API_KEY", "")
        model = _cfg("OPENAI_MODEL", "gpt-4o-mini")
        base = _cfg("OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not key:
            raise RuntimeError("OPENAI_API_KEY 未配置")
        p = OpenAICompatProvider("openai", key, model, base)

    else:
        raise RuntimeError(f"未知 LLM_PROVIDER: {name}")

    _provider_cache[name] = p
    log.info(f"[LLM] Provider 初始化: {p.name}")
    return p


def get_vision_provider() -> LLMProvider:
    """Vision 强制走 Gemini（由用户决策）。"""
    return get_provider("gemini")
