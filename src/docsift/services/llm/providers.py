"""Реализации LLM-провайдеров: Ollama и OpenAI-совместимый API."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

import httpx

from docsift.schemas.llm import LLMMessage, LLMRequest, LLMResponse
from docsift.services.llm.schema_compat import to_ollama_schema

logger = logging.getLogger(__name__)

#: Количество повторов при ретраемых ошибках (таймауты сети).
_RETRYABLE_RETRIES = 2
#: Паузы между повторами, секунды.
_RETRY_BACKOFF_SECONDS = (2.0, 8.0)


class LLMProviderError(RuntimeError):
    pass


class LLMProviderProtocol(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def supports_json_schema(self) -> bool: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


def _message_payload(request: LLMRequest) -> list[dict[str, str]]:
    return [
        {"role": message.role, "content": message.content}
        for message in request.messages
    ]


def _content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    raise LLMProviderError("LLM response does not contain textual JSON")


def _prompt_chars(request: LLMRequest) -> int:
    return sum(len(message.content) for message in request.messages)


class OllamaProvider:
    """Провайдер локального Ollama.

    Каждый запрос выполняется под семафором, ограничивающим конкурентность:
    локальный инференс на CPU плохо переносит параллельные вызовы, поэтому
    по умолчанию пускаем один запрос за раз (см. ``DOCSIFT_LLM_MAX_CONCURRENCY``).

    Таймаут задаётся гранулярно через ``httpx.Timeout``: connect короче read,
    потому что установление соединения к локальному Ollama — быстрая операция,
    а вот генерация может идти десятки секунд.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float,
        native_structured_output: bool = True,
        *,
        num_predict: int = 1024,
        num_ctx: int = 4096,
        max_concurrency: int = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=timeout_seconds,
            write=60.0,
            pool=60.0,
        )
        self._native_structured_output = native_structured_output
        self._num_predict = num_predict
        self._num_ctx = num_ctx
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    @property
    def provider_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_json_schema(self) -> bool:
        return self._native_structured_output

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/api/chat"

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": _message_payload(request),
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0,
                "num_predict": self._num_predict,
                "num_ctx": self._num_ctx,
            },
        }
        if self.supports_json_schema:
            payload["format"] = to_ollama_schema(request.json_schema)
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request)
        prompt_chars = _prompt_chars(request)
        last_exc: Exception | None = None
        response: httpx.Response | None = None

        for attempt in range(_RETRYABLE_RETRIES + 1):
            async with self._semaphore:
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(self.endpoint, json=payload)
                        response.raise_for_status()
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt < _RETRYABLE_RETRIES:
                        logger.warning(
                            "Ollama таймаут (%s), повтор %d/%d через %.1fs",
                            type(exc).__name__,
                            attempt + 1,
                            _RETRYABLE_RETRIES,
                            _RETRY_BACKOFF_SECONDS[attempt],
                        )
                        await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                        continue
                    raise self._timeout_error(exc, request, prompt_chars) from exc
                except httpx.HTTPStatusError as exc:
                    detail = (exc.response.text or "")[:500]
                    raise LLMProviderError(
                        f"Ollama HTTP {exc.response.status_code}: {detail}"
                    ) from exc
                except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                    response = getattr(exc, "response", None)
                    detail = ""
                    if response is not None and getattr(response, "text", ""):
                        detail = f": {response.text[:500]}"
                    raise LLMProviderError(
                        f"Ollama request failed: {type(exc).__name__}: {exc}{detail}"
                    ) from exc

            # Успех — парсим ответ вне sem, чтобы не держать слот дольше нужного.
            assert response is not None
            data = response.json()
            return LLMResponse(
                content=_content_as_text(data.get("message", {}).get("content")),
                input_tokens=data.get("prompt_eval_count"),
                output_tokens=data.get("eval_count"),
            )

        # Не должны сюда попасть, но для mypy.
        raise LLMProviderError("Ollama request exhausted retries") from last_exc

    def _timeout_error(
        self, exc: Exception, request: LLMRequest, prompt_chars: int
    ) -> LLMProviderError:
        read_timeout = self._timeout.read
        return LLMProviderError(
            f"{type(exc).__name__} after {read_timeout}s | "
            f"model={self._model} | prompt_chars={prompt_chars} | "
            f"num_predict={self._num_predict} | timeout_cfg={read_timeout} | "
            f"url={self.endpoint}"
        )


class OpenAICompatibleProvider:
    """Провайдер для OpenAI-совместимых API (облачные модели)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float,
        api_key: str | None = None,
        native_structured_output: bool = True,
        *,
        max_concurrency: int = 1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(
            connect=10.0,
            read=timeout_seconds,
            write=60.0,
            pool=60.0,
        )
        self._api_key = api_key
        self._native_structured_output = native_structured_output
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_json_schema(self) -> bool:
        return self._native_structured_output

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": _message_payload(request),
            "temperature": 0,
        }
        if self.supports_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request)
        prompt_chars = _prompt_chars(request)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        last_exc: Exception | None = None
        response: httpx.Response | None = None

        for attempt in range(_RETRYABLE_RETRIES + 1):
            async with self._semaphore:
                try:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(
                            self.endpoint, json=payload, headers=headers
                        )
                        response.raise_for_status()
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt < _RETRYABLE_RETRIES:
                        await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                        continue
                    read_timeout = self._timeout.read
                    raise LLMProviderError(
                        f"{type(exc).__name__} after {read_timeout}s | "
                        f"model={self._model} | prompt_chars={prompt_chars} | "
                        f"timeout_cfg={read_timeout} | url={self.endpoint}"
                    ) from exc
                except httpx.HTTPStatusError as exc:
                    detail = (exc.response.text or "")[:500]
                    raise LLMProviderError(
                        f"OpenAI-compatible HTTP {exc.response.status_code}: {detail}"
                    ) from exc
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                    raise LLMProviderError(
                        "OpenAI-compatible request failed"
                    ) from exc

            assert response is not None
            data = response.json()
            usage = data.get("usage", {})
            return LLMResponse(
                content=_content_as_text(data["choices"][0]["message"]["content"]),
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )

        raise LLMProviderError("OpenAI request exhausted retries") from last_exc


async def warmup_ollama_model(provider: OllamaProvider, *, num_predict: int = 1) -> None:
    """Прогреть модель Ollama одним минимальным запросом.

    Первый запрос к модели всегда медленнее: Ollama грузит веса в память.
    Этот вызов делается на старте приложения, чтобы первый реальный документ
    не попал на холодный старт. Ошибка только логируется — старт не ломаем.
    """
    warmup_request = LLMRequest(
        messages=(LLMMessage(role="user", content="ping"),),
        json_schema={},
    )
    payload = provider._build_payload(warmup_request)  # noqa: SLF001
    payload["options"]["num_predict"] = num_predict
    payload["stream"] = False
    try:
        async with httpx.AsyncClient(timeout=provider._timeout) as client:  # noqa: SLF001
            response = await client.post(provider.endpoint, json=payload)
            response.raise_for_status()
        logger.info("Ollama модель %s прогрета", provider.model_name)
    except Exception:
        logger.warning(
            "Прогрев Ollama модели %s не удался — первый запрос может быть медленным",
            provider.model_name,
            exc_info=True,
        )