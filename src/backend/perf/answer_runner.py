"""Controlled model execution for answer-level context-quality benchmarks."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


SYSTEM_PROMPT = (
    "You are evaluating code context. Answer only from the supplied evidence. "
    "Return JSON with exactly two keys: answer (string) and citations (array of evidence IDs). "
    "Do not cite evidence IDs that are not present in the context."
)

TASK_TEMPLATE = """Task:
{query}

Evidence:
{evidence}

Return JSON only:
{{"answer":"...","citations":["evidence-id"]}}
"""

RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class AnswerRunnerError(RuntimeError):
    """Raised when a controlled model run fails or returns invalid output."""


@dataclass(frozen=True)
class ModelRunConfig:
    """Configuration held constant across baseline and CG calls."""

    model: str
    base_url: str
    temperature: float = 0.0
    seed: int = 7
    timeout_seconds: float = 60.0
    tool_budget: int = 0
    requests_per_minute: float = 0.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    max_retry_delay_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests_per_minute < 0:
            raise AnswerRunnerError("requests_per_minute must be non-negative")
        if self.max_retries < 0:
            raise AnswerRunnerError("max_retries must be non-negative")
        if self.retry_backoff_seconds < 0:
            raise AnswerRunnerError("retry_backoff_seconds must be non-negative")
        if self.max_retry_delay_seconds < 0:
            raise AnswerRunnerError("max_retry_delay_seconds must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "baseUrl": self.base_url,
            "temperature": self.temperature,
            "seed": self.seed,
            "timeoutSeconds": self.timeout_seconds,
            "toolBudget": self.tool_budget,
            "requestsPerMinute": self.requests_per_minute,
            "maxRetries": self.max_retries,
            "retryBackoffSeconds": self.retry_backoff_seconds,
            "maxRetryDelaySeconds": self.max_retry_delay_seconds,
            "systemPromptSha256": _sha256(SYSTEM_PROMPT),
            "taskTemplateSha256": _sha256(TASK_TEMPLATE),
        }


class OpenAICompatibleAnswerRunner:
    """Run paired evidence prompts through an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        config: ModelRunConfig,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.tool_budget != 0:
            raise AnswerRunnerError("answer benchmark currently enforces a zero tool budget")
        if not api_key:
            raise AnswerRunnerError("an API key is required for answer-level model runs")
        self._config = config
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=config.timeout_seconds,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleAnswerRunner:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def run_case(self, case: dict[str, Any], mode: str) -> dict[str, Any]:
        """Run one frozen case and return parsed answer JSON plus reproducibility metadata."""
        prompt = render_task_prompt(case, mode)
        request = {
            "model": self._config.model,
            "temperature": self._config.temperature,
            "seed": self._config.seed,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        response, request_attempts = self._post_with_retry(request)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AnswerRunnerError(
                f"model request failed with HTTP {response.status_code}: {response.text[:500]}"
            ) from exc

        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AnswerRunnerError("model response did not contain choices[0].message.content") from exc

        parsed = parse_structured_answer(content)
        return {
            **parsed,
            "mode": mode,
            "promptSha256": _sha256(prompt),
            "usage": payload.get("usage") or {},
            "requestAttempts": request_attempts,
        }

    def _post_with_retry(self, request: dict[str, Any]) -> tuple[httpx.Response, int]:
        attempts = 0
        while True:
            attempts += 1
            self._throttle()
            try:
                response = self._client.post("/chat/completions", json=request)
            except httpx.TransportError as exc:
                if attempts > self._config.max_retries:
                    raise AnswerRunnerError(
                        f"model request failed after {attempts} attempts: {exc}"
                    ) from exc
                self._sleep(self._retry_delay(attempts, retry_after=None))
                continue

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempts <= self._config.max_retries
            ):
                self._sleep(
                    self._retry_delay(
                        attempts,
                        retry_after=_retry_after_seconds(response.headers.get("Retry-After")),
                    )
                )
                continue
            return response, attempts

    def _throttle(self) -> None:
        if self._config.requests_per_minute <= 0:
            return
        interval = 60.0 / self._config.requests_per_minute
        now = self._monotonic()
        if self._last_request_at is not None:
            delay = self._last_request_at + interval - now
            if delay > 0:
                self._sleep(delay)
                now = self._monotonic()
        self._last_request_at = now

    def _retry_delay(self, attempts: int, *, retry_after: float | None) -> float:
        if retry_after is not None:
            if retry_after > self._config.max_retry_delay_seconds:
                raise AnswerRunnerError(
                    "server Retry-After delay "
                    f"({retry_after:.1f}s) exceeds max_retry_delay_seconds "
                    f"({self._config.max_retry_delay_seconds:.1f}s)"
                )
            return retry_after
        delay = self._config.retry_backoff_seconds * (2 ** (attempts - 1))
        return min(delay, self._config.max_retry_delay_seconds)


def render_task_prompt(case: dict[str, Any], mode: str) -> str:
    """Render the common task template with only the evidence bundle varied by mode."""
    if mode not in {"baseline", "cg"}:
        raise AnswerRunnerError("mode must be 'baseline' or 'cg'")
    context = case.get(mode)
    if not isinstance(context, dict):
        raise AnswerRunnerError(f"case.{mode} must be an object")

    rendered_chunks: list[str] = []
    for chunk in context.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        evidence_ids = chunk.get("evidence") or []
        rendered_chunks.append(
            "\n".join(
                [
                    f"Evidence IDs: {json.dumps(evidence_ids, ensure_ascii=True)}",
                    str(chunk.get("text") or ""),
                ]
            )
        )
    return TASK_TEMPLATE.format(
        query=str(case.get("query") or ""),
        evidence="\n\n---\n\n".join(rendered_chunks),
    )


def parse_structured_answer(content: Any) -> dict[str, Any]:
    """Parse the strict answer/citations response, accepting fenced JSON defensively."""
    if not isinstance(content, str):
        raise AnswerRunnerError("model answer content must be a string")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnswerRunnerError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AnswerRunnerError("model answer must be a JSON object")
    answer = parsed.get("answer")
    citations = parsed.get("citations")
    if not isinstance(answer, str):
        raise AnswerRunnerError("model answer field must be a string")
    if not isinstance(citations, list) or any(not isinstance(item, str) for item in citations):
        raise AnswerRunnerError("model citations field must be a list of strings")
    return {"answer": answer.strip(), "citations": citations}


def _retry_after_seconds(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current_time = now or datetime.now(timezone.utc)
        return max(0.0, (retry_at - current_time).total_seconds())


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()