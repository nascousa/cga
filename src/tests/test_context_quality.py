from pathlib import Path

import pytest

from backend.perf.context_quality import (
    ContextQualityInputError,
    benchmark_context_quality,
    score_context_mode,
)


def _text(tokens: int) -> str:
    return "x" * (tokens * 4)


def test_score_context_mode_computes_hps_components() -> None:
    score = score_context_mode(
        mode="baseline",
        segment={
            "chunks": [
                {"id": "gold", "text": _text(100), "evidence": ["file:client.ts#timeout"]},
                {"id": "noise", "text": _text(300), "evidence": []},
                {"id": "dup1", "text": _text(50), "evidence": []},
                {"id": "dup2", "text": _text(50), "evidence": []},
            ],
            "symbols": [
                "src.services.mcp.client.Session",
                "src.protocol.Session",
            ],
        },
        gold_items=["file:client.ts#timeout", "file:client.ts#getMcpToolTimeoutMs"],
        supporting_items=[],
        target_symbols=["src.services.mcp.client.Session"],
        repo_root=Path("."),
    )

    assert score.total_tokens == 500
    assert score.useful_tokens == 100
    assert score.useless_tokens == 400
    assert score.gold_coverage == 0.5
    assert score.context_precision == 0.2
    assert score.duplicate_tokens == 50
    assert score.ambiguous_symbol_hits == 1
    assert score.hallucination_pressure_score > 50


def test_benchmark_context_quality_compares_baseline_and_cg() -> None:
    payload = {
        "cases": [
            {
                "id": "claudecli-mcp-timeout",
                "project": "ClaudeCLI",
                "query": "Where is MCP tool timeout configured?",
                "goldItems": [
                    "file:src/services/mcp/client.ts#DEFAULT_MCP_TOOL_TIMEOUT_MS",
                    "file:src/services/mcp/client.ts#getMcpToolTimeoutMs",
                ],
                "targetSymbols": ["src.services.mcp.client.getMcpToolTimeoutMs"],
                "baseline": {
                    "chunks": [
                        {
                            "id": "client-timeout",
                            "text": _text(160),
                            "evidence": [
                                "file:src/services/mcp/client.ts#DEFAULT_MCP_TOOL_TIMEOUT_MS"
                            ],
                        },
                        {"id": "unrelated-auth", "text": _text(500), "evidence": []},
                    ],
                    "symbols": [
                        "src.services.mcp.client.getMcpToolTimeoutMs",
                        "src.services.mcp.auth.getMcpToolTimeoutMs",
                    ],
                },
                "cg": {
                    "chunks": [
                        {
                            "id": "client-timeout",
                            "text": _text(160),
                            "evidence": [
                                "file:src/services/mcp/client.ts#DEFAULT_MCP_TOOL_TIMEOUT_MS",
                                "file:src/services/mcp/client.ts#getMcpToolTimeoutMs",
                            ],
                        },
                        {"id": "direct-callsite", "text": _text(80), "evidence": []},
                    ],
                    "symbols": ["src.services.mcp.client.getMcpToolTimeoutMs"],
                },
            }
        ]
    }

    report = benchmark_context_quality(payload=payload, repo_root=Path("."))
    case = report["cases"][0]

    assert report["metric"] == "hallucination-pressure-score"
    assert case["baseline"]["hallucinationPressureScore"] > case["cg"]["hallucinationPressureScore"]
    assert case["comparison"]["hpsReductionPercent"] > 0
    assert case["comparison"]["tokenReductionPercent"] > 0
    assert report["summary"]["avgHpsReductionPercent"] > 0


def test_benchmark_context_quality_requires_cases() -> None:
    with pytest.raises(ContextQualityInputError):
        benchmark_context_quality(payload={"cases": []}, repo_root=Path("."))


def test_benchmark_context_quality_requires_gold_items() -> None:
    payload = {
        "cases": [
            {
                "id": "bad",
                "baseline": {"chunks": [{"text": "baseline"}]},
                "cg": {"chunks": [{"text": "cg"}]},
            }
        ]
    }

    with pytest.raises(ContextQualityInputError, match="goldItems"):
        benchmark_context_quality(payload=payload, repo_root=Path("."))


@pytest.fixture
def mcp_project_scope(tmp_path):
    from backend.auth.context import ProjectScope, bind_project_scope

    with bind_project_scope(ProjectScope("QUALITY-TEST", 1, "quality-test", str(tmp_path))):
        yield


def test_mcp_benchmark_context_quality_tool(mcp_project_scope) -> None:
    from backend.tools import server as mcp_srv

    result = mcp_srv.benchmark_context_quality(
        cases=[
            {
                "id": "codexcli-mcp-owner",
                "project": "CodexCLI",
                "query": "Where should MCP tool call mutation logic live?",
                "goldItems": ["file:AGENTS.md#mcp-tool-call-mutation"],
                "baseline": {
                    "chunks": [
                        {"id": "noise", "text": _text(300), "evidence": []},
                        {
                            "id": "gold",
                            "text": _text(100),
                            "evidence": ["file:AGENTS.md#mcp-tool-call-mutation"],
                        },
                    ]
                },
                "cg": {
                    "chunks": [
                        {
                            "id": "gold",
                            "text": _text(100),
                            "evidence": ["file:AGENTS.md#mcp-tool-call-mutation"],
                        }
                    ]
                },
            }
        ]
    )

    assert result["metric"] == "hallucination-pressure-score"
    assert result["cases"][0]["comparison"]["hpsReductionPercent"] > 0


def test_context_quality_api_endpoint() -> None:
    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)
    response = client.post(
        "/api/benchmark/context-quality",
        json={
            "cases": [
                {
                    "id": "claudecli-mcp-timeout",
                    "project": "ClaudeCLI",
                    "query": "Where is MCP timeout configured?",
                    "goldItems": ["file:client.ts#timeout"],
                    "baseline": {
                        "chunks": [
                            {"id": "noise", "text": _text(100), "evidence": []},
                            {
                                "id": "gold",
                                "text": _text(50),
                                "evidence": ["file:client.ts#timeout"],
                            },
                        ]
                    },
                    "cg": {
                        "chunks": [
                            {
                                "id": "gold",
                                "text": _text(50),
                                "evidence": ["file:client.ts#timeout"],
                            }
                        ]
                    },
                }
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "hallucination-pressure-score"
    assert body["cases"][0]["comparison"]["hpsReductionPercent"] > 0