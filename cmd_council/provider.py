"""Command Code Provider API adapter (dual protocol).

Routes each model to the right endpoint on api.commandcode.ai:
  - protocol "openai"    -> POST /chat/completions  (OpenAI Chat Completions)
  - protocol "anthropic" -> POST /messages          (Anthropic Messages)

Responsibilities: auth header, optional ZDR header, per-call timeout,
one retry with backoff on 429/5xx/network errors, a global concurrency
semaphore, response normalization to (text, TokenUsage), and SSE
streaming for both protocols.
"""
from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator

import httpx

from .config import LimitsConfig, ModelRef, ProviderConfig
from .models import TokenUsage

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CommandCodeProvider:
    def __init__(self, provider_cfg: ProviderConfig, limits: LimitsConfig) -> None:
        headers = {
            "Authorization": f"Bearer {provider_cfg.api_key}",
            "Content-Type": "application/json",
        }
        if provider_cfg.zdr:
            headers["x-cmd-zdr"] = "1"
        self._client = httpx.AsyncClient(
            base_url=provider_cfg.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(limits.timeout_seconds),
        )
        self._sem = asyncio.Semaphore(limits.max_concurrency)
        self._retries = max(0, limits.retries)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------- request building ----------------

    @staticmethod
    def _payload(
        ref: ModelRef,
        system: str | None,
        user: str,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> tuple[str, dict, dict]:
        if ref.protocol == "anthropic":
            body: dict[str, Any] = {
                "model": ref.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": [{"role": "user", "content": user}],
                "stream": stream,
            }
            if system:
                body["system"] = system
            return "/messages", body, {"anthropic-version": "2023-06-01"}

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = {
            "model": ref.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        return "/chat/completions", body, {}

    # ---------------- response normalization ----------------

    @staticmethod
    def _extract(ref: ModelRef, data: dict) -> tuple[str, TokenUsage]:
        if ref.protocol == "anthropic":
            blocks = data.get("content") or []
            text = "".join(
                b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
            u = data.get("usage") or {}
            usage = TokenUsage(
                int(u.get("input_tokens", 0) or 0),
                int(u.get("output_tokens", 0) or 0),
            )
            return text, usage

        choices = data.get("choices") or []
        text = ""
        if choices:
            msg = choices[0].get("message") or {}
            text = msg.get("content") or ""
        u = data.get("usage") or {}
        usage = TokenUsage(
            int(u.get("prompt_tokens", 0) or 0),
            int(u.get("completion_tokens", 0) or 0),
        )
        return text, usage

    # ---------------- calls ----------------

    async def chat(
        self,
        ref: ModelRef,
        *,
        system: str | None,
        user: str,
        max_tokens: int,
        temperature: float = 0.7,
    ) -> tuple[str, TokenUsage]:
        path, body, extra = self._payload(ref, system, user, max_tokens, temperature, False)
        last_err: ProviderError | None = None

        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(1.5 * attempt + random.random())
            try:
                async with self._sem:
                    resp = await self._client.post(path, json=body, headers=extra)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = ProviderError(f"{ref.model}: network error: {e}")
                continue

            if resp.status_code in RETRYABLE_STATUS and attempt < self._retries:
                retry_after = resp.headers.get("retry-after")
                if retry_after:
                    try:
                        await asyncio.sleep(min(float(retry_after), 30.0))
                    except ValueError:
                        pass
                last_err = ProviderError(
                    f"{ref.model}: HTTP {resp.status_code}", resp.status_code
                )
                continue

            if resp.status_code >= 400:
                raise ProviderError(
                    f"{ref.model}: HTTP {resp.status_code}: {resp.text[:300]}",
                    resp.status_code,
                )

            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise ProviderError(f"{ref.model}: invalid JSON response: {e}") from e
            return self._extract(ref, data)

        raise last_err or ProviderError(f"{ref.model}: exhausted retries")

    async def chat_raw(
        self,
        ref: ModelRef,
        *,
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
    ) -> tuple[str, TokenUsage]:
        """Multi-turn passthrough (OpenAI Chat format, openai protocol only).

        Backs the gateway's `chat` / `chat-eco` facade modes so everyday
        persona conversations cost ONE cheap model call instead of a full
        council session (the council stays for /council moments).
        """
        if ref.protocol != "openai":
            raise ProviderError(f"{ref.model}: chat_raw requires openai protocol")
        body = {
            "model": ref.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_err: ProviderError | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(1.5 * attempt + random.random())
            try:
                async with self._sem:
                    resp = await self._client.post("/chat/completions", json=body)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = ProviderError(f"{ref.model}: network error: {e}")
                continue
            if resp.status_code in RETRYABLE_STATUS and attempt < self._retries:
                last_err = ProviderError(
                    f"{ref.model}: HTTP {resp.status_code}", resp.status_code
                )
                continue
            if resp.status_code >= 400:
                raise ProviderError(
                    f"{ref.model}: HTTP {resp.status_code}: {resp.text[:300]}",
                    resp.status_code,
                )
            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise ProviderError(f"{ref.model}: invalid JSON response: {e}") from e
            return self._extract(ref, data)
        raise last_err or ProviderError(f"{ref.model}: exhausted retries")

    async def stream_raw(
        self,
        ref: ModelRef,
        *,
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Streaming chat_raw. Yields ("token", str) then one ("usage", ...)."""
        if ref.protocol != "openai":
            raise ProviderError(f"{ref.model}: stream_raw requires openai protocol")
        body = {
            "model": ref.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        usage = TokenUsage()
        emitted_chars = 0
        async with self._sem:
            try:
                async with self._client.stream(
                    "POST", "/chat/completions", json=body
                ) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise ProviderError(
                            f"{ref.model}: HTTP {resp.status_code}: {detail}",
                            resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        u = obj.get("usage")
                        if u:
                            usage = TokenUsage(
                                int(u.get("prompt_tokens", 0) or 0),
                                int(u.get("completion_tokens", 0) or 0),
                            )
                        for ch in obj.get("choices") or []:
                            tok = (ch.get("delta") or {}).get("content") or ""
                            if tok:
                                emitted_chars += len(tok)
                                yield ("token", tok)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise ProviderError(f"{ref.model}: network error: {e}") from e
        if usage.input_tokens == 0 and usage.output_tokens == 0:
            approx_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
            usage = TokenUsage(approx_in, max(1, emitted_chars // 4))
        yield ("usage", usage)

    async def stream_chat(
        self,
        ref: ModelRef,
        *,
        system: str | None,
        user: str,
        max_tokens: int,
        temperature: float = 0.7,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Yields ("token", str) events, then exactly one ("usage", TokenUsage)."""
        path, body, extra = self._payload(ref, system, user, max_tokens, temperature, True)
        usage = TokenUsage()
        emitted_chars = 0

        async with self._sem:
            try:
                async with self._client.stream("POST", path, json=body, headers=extra) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise ProviderError(
                            f"{ref.model}: HTTP {resp.status_code}: {detail}",
                            resp.status_code,
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            obj = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        if ref.protocol == "anthropic":
                            etype = obj.get("type")
                            if etype == "message_start":
                                u = (obj.get("message") or {}).get("usage") or {}
                                usage.input_tokens = int(u.get("input_tokens", 0) or 0)
                            elif etype == "content_block_delta":
                                delta = obj.get("delta") or {}
                                text = delta.get("text")
                                if text:
                                    emitted_chars += len(text)
                                    yield ("token", text)
                            elif etype == "message_delta":
                                u = obj.get("usage") or {}
                                if u.get("output_tokens"):
                                    usage.output_tokens = int(u["output_tokens"])
                        else:
                            for ch in obj.get("choices") or []:
                                delta = ch.get("delta") or {}
                                text = delta.get("content")
                                if text:
                                    emitted_chars += len(text)
                                    yield ("token", text)
                            u = obj.get("usage")
                            if u:
                                usage = TokenUsage(
                                    int(u.get("prompt_tokens", 0) or 0),
                                    int(u.get("completion_tokens", 0) or 0),
                                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise ProviderError(f"{ref.model}: network error during stream: {e}") from e

        # Rough estimates if the provider didn't send usage on the stream.
        if usage.output_tokens == 0 and emitted_chars:
            usage.output_tokens = max(1, emitted_chars // 4)
        if usage.input_tokens == 0:
            usage.input_tokens = max(1, (len(system or "") + len(user)) // 4)
        yield ("usage", usage)

    async def get_models(self) -> Any:
        try:
            resp = await self._client.get("/models")
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ProviderError(f"GET /models: network error: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(
                f"GET /models: HTTP {resp.status_code}: {resp.text[:300]}",
                resp.status_code,
            )
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise ProviderError(f"GET /models: invalid JSON: {e}") from e
