import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from backend.perf.answer_runner import (
    AnswerRunnerError,
    ModelRunConfig,
    OpenAICompatibleAnswerRunner,
    _retry_after_seconds,
    parse_structured_answer,
    render_task_prompt,
)


def _case() -> dict:
    return {
        "query": "Where is run implemented?",
        "baseline": {
            "chunks": [
                {
                    "text": "def run(): pass",
                    "evidence": ["file:src/service.py#run"],
                }
            ]
        },
        "cg": {
            "chunks": [
                {
                    "text": "def run(): pass",
                    "evidence": ["file:src/service.py#run"],
                }
            ]
        },
    }


def test_render_task_prompt_changes_only_evidence_bundle() -> None:
    case = _case()
    case["cg"]["chunks"][0]["text"] = "def run(): return 1"

    baseline = render_task_prompt(case, "baseline")
    cg = render_task_prompt(case, "cg")

    assert "Where is run implemented?" in baseline
    assert "Where is run implemented?" in cg
    assert "def run(): pass" in baseline
    assert "def run(): return 1" in cg


def test_runner_uses_controlled_config_and_parses_answer() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "run is in src/service.py",
                                    "citations": ["file:src/service.py#run"],
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://model.example/v1",
    )
    config = ModelRunConfig(model="test-model", base_url="https://model.example/v1")
    runner = OpenAICompatibleAnswerRunner(api_key="test-key", config=config, client=client)

    result = runner.run_case(_case(), "cg")

    assert result["answer"] == "run is in src/service.py"
    assert result["citations"] == ["file:src/service.py#run"]
    assert result["usage"] == {"prompt_tokens": 10, "completion_tokens": 8}
    assert result["requestAttempts"] == 1
    assert captured["model"] == "test-model"
    assert captured["temperature"] == 0.0
    assert captured["seed"] == 7
    assert "tools" not in captured


def test_parse_structured_answer_accepts_fenced_json() -> None:
    parsed = parse_structured_answer(
        '```json\n{"answer":"ok","citations":["evidence-1"]}\n```'
    )

    assert parsed == {"answer": "ok", "citations": ["evidence-1"]}


def test_runner_retries_rate_limit_using_retry_after() -> None:
    request_count = 0
    sleep_delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"answer":"ok","citations":[]}'}}
                ]
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://model.example/v1",
    )
    config = ModelRunConfig(
        model="test-model",
        base_url="https://model.example/v1",
        max_retries=1,
    )
    runner = OpenAICompatibleAnswerRunner(
        api_key="test-key",
        config=config,
        client=client,
        sleep=sleep_delays.append,
    )

    result = runner.run_case(_case(), "cg")

    assert result["requestAttempts"] == 2
    assert request_count == 2
    assert sleep_delays == [2.0]


def test_runner_throttles_every_request() -> None:
    current_time = 0.0
    sleep_delays: list[float] = []

    def sleep(delay: float) -> None:
        nonlocal current_time
        sleep_delays.append(delay)
        current_time += delay

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"answer":"ok","citations":[]}'}}
                ]
            },
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://model.example/v1",
    )
    config = ModelRunConfig(
        model="test-model",
        base_url="https://model.example/v1",
        requests_per_minute=60,
    )
    runner = OpenAICompatibleAnswerRunner(
        api_key="test-key",
        config=config,
        client=client,
        sleep=sleep,
        monotonic=lambda: current_time,
    )

    runner.run_case(_case(), "baseline")
    runner.run_case(_case(), "cg")

    assert sleep_delays == [1.0]


def test_retry_after_accepts_http_date() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    retry_at = format_datetime(now + timedelta(seconds=90), usegmt=True)

    assert _retry_after_seconds(retry_at, now=now) == 90.0


def test_runner_stops_when_retry_after_exceeds_wait_ceiling() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, text="quota")

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://model.example/v1",
    )
    config = ModelRunConfig(
        model="test-model",
        base_url="https://model.example/v1",
        max_retry_delay_seconds=60,
    )
    runner = OpenAICompatibleAnswerRunner(
        api_key="test-key",
        config=config,
        client=client,
    )

    with pytest.raises(AnswerRunnerError, match="exceeds max_retry_delay_seconds"):
        runner.run_case(_case(), "cg")