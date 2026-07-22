from pathlib import Path

from backend.agent.query_strategy import (
    CGFirstQueryStrategy,
    StrategyConfig,
    decide_fallback,
    estimate_tokens,
    evaluate_graph_quality,
    trim_items_to_budget,
    read_code_snippet,
    run_cg_first_strategy,
)


def test_estimate_tokens_empty_and_non_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2


def test_trim_items_to_budget_keeps_order_and_budget():
    items = [
        {"name": "a", "payload": "x" * 20},
        {"name": "b", "payload": "y" * 200},
        {"name": "c", "payload": "z" * 20},
    ]
    kept, used = trim_items_to_budget(items, budget_tokens=40)
    assert len(kept) >= 1
    assert used <= 40
    assert kept[0]["name"] == "a"


def test_read_code_snippet_respects_bounds(tmp_path: Path):
    content = "\n".join(
        [
            "line1",
            "line2",
            "line3",
            "line4",
            "line5",
            "line6",
        ]
    )
    file_path = tmp_path / "sample.py"
    file_path.write_text(content, encoding="utf-8")

    snippet = read_code_snippet(
        repo_root=tmp_path,
        relative_path="sample.py",
        line_start=3,
        line_end=4,
        context_lines=1,
        max_chars=200,
    )

    assert "line2" in snippet
    assert "line3" in snippet
    assert "line4" in snippet
    assert "line5" in snippet


def test_run_cg_first_strategy_without_fallback(tmp_path: Path):
    def retrieve_graph_hits(query: str, limit: int):
        return [
            {
                "qualified_name": "pkg.mod.fn",
                "symbol_type": "function",
                "file_path": "sample.py",
                "line_start": 1,
                "line_end": 3,
                "summary": "function pkg.mod.fn at sample.py:1-3 sample handler",
                "snippet": "def fn():\n    return 'sample'",
            }
        ]

    def get_call_graph(qualified_name: str, depth: int):
        return {"callers": ["caller.a"], "callees": ["callee.b"]}

    result = run_cg_first_strategy(
        query="sample",
        repo_root=tmp_path,
        retrieve_graph_hits=retrieve_graph_hits,
        get_call_graph=get_call_graph,
        graph_top_k=5,
        min_graph_hits=1,
        token_budget=500,
    )

    assert result["strategy"] == "cg-first"
    assert result["used_fallback"] is False
    assert len(result["graph_context"]) == 1
    assert result["quality_score"] >= result["quality_threshold"]


def test_run_cg_first_strategy_reuses_inline_relations(tmp_path: Path):
    def retrieve_graph_hits(query: str, limit: int):
        return [
            {
                "qualified_name": "pkg.mod.fn",
                "symbol_type": "function",
                "file_path": "sample.py",
                "line_start": 1,
                "line_end": 3,
                "summary": "function pkg.mod.fn at sample.py:1-3 sample handler",
                "snippet": "def fn():\n    return 'sample'",
                "callers": ["caller.a"],
                "callees": ["callee.b"],
            }
        ]

    def get_call_graph(qualified_name: str, depth: int):
        raise AssertionError("get_call_graph should not be called when inline relations exist")

    result = run_cg_first_strategy(
        query="sample",
        repo_root=tmp_path,
        retrieve_graph_hits=retrieve_graph_hits,
        get_call_graph=get_call_graph,
        graph_top_k=5,
        min_graph_hits=1,
        token_budget=500,
    )

    assert result["used_fallback"] is False
    assert result["graph_context"][0]["callers"] == ["caller.a"]


def test_decide_fallback_for_low_quality():
    items = [
        {
            "qualified_name": "pkg.mod.fn",
            "symbol_type": "function",
            "file_path": "a.py",
            "line_start": 1,
            "line_end": 2,
            "summary": "function pkg.mod.fn at a.py:1-2",
            "snippet": "",
            "callers": [],
            "callees": [],
        }
    ]

    should_fallback, reason, quality = decide_fallback(
        trimmed_items=items,
        query="totally unrelated search",
        min_graph_hits=1,
        quality_threshold=0.55,
    )

    assert should_fallback is True
    assert reason == "low_graph_quality"
    assert quality["quality_score"] < 0.55


def test_evaluate_graph_quality_with_snippet_and_relations():
    items = [
        {
            "qualified_name": "backend.indexer.pipeline.IndexPipeline.index_full",
            "symbol_type": "method",
            "summary": "method backend.indexer.pipeline.IndexPipeline.index_full at file.py:1-3",
            "snippet": "def index_full(self): pass",
            "callers": ["a"],
            "callees": ["b"],
        }
    ]
    quality = evaluate_graph_quality(items, "index pipeline")
    assert quality["quality_score"] > 0.55
    assert quality["matched_items"] == 1


def test_cg_first_query_strategy_class_run(monkeypatch, tmp_path: Path):
    strategy = CGFirstQueryStrategy(
        StrategyConfig(
            repo_root=tmp_path,
            base_url="http://127.0.0.1:8011",
            min_graph_hits=1,
        )
    )

    seen_query: list[str] = []

    async def fake_run_async(query: str):
        seen_query.append(query)
        return {
            "strategy": "cg-first",
            "source": "contextgraph-client",
            "used_fallback": False,
        }

    monkeypatch.setattr(strategy, "run_async", fake_run_async)

    result = strategy.run("pkg fn")
    assert seen_query == ["pkg fn"]
    assert result["source"] == "contextgraph-client"
    assert result["used_fallback"] is False


def test_cg_first_query_strategy_message_endpoint_compat(tmp_path: Path):
    strategy = CGFirstQueryStrategy(StrategyConfig(repo_root=tmp_path, base_url="http://127.0.0.1:8011/"))
    assert strategy._read_message_endpoint("http://127.0.0.1:8011/") == "http://127.0.0.1:8011/mcp/sse"
