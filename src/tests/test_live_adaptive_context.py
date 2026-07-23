from pathlib import Path
from types import SimpleNamespace

from scripts.run_live_context_quality_benchmark import (
    ProjectRecord,
    SymbolRecord,
    _natural_relation_fact,
    _natural_target_fact,
    _source_filename,
    build_cases,
    load_graph_expansion_candidates,
)


class FakeGraph:
    def query(self, cypher: str, _params: dict) -> SimpleNamespace:
        if "CALLS*1..2" in cypher:
            return SimpleNamespace(
                result_set=[
                    ["pkg.helper", "helper", "helper.py", 1, 2, 1],
                    ["pkg.deep", "deep", "deep.py", 1, 2, 2],
                ]
            )
        if "IMPORTS*1..2" in cypher:
            return SimpleNamespace(result_set=[["config.py", 1]])
        if "FLOWS_TO" in cypher:
            return SimpleNamespace(
                result_set=[["pkg.run:value", "pkg.run:result", "result", "service.py", 2, 1]]
            )
        raise AssertionError(f"unexpected query: {cypher}")


class FakeFalkorDB:
    def select_graph(self, name: str) -> FakeGraph:
        assert name == "repo"
        return FakeGraph()


def _project(repo_path: Path) -> ProjectRecord:
    return ProjectRecord(
        project_name="Repo",
        project_id="repo-1",
        repo_path=str(repo_path),
        graph_name="repo",
        host_repo_path=repo_path,
    )


def _symbol(repo_path: Path) -> SymbolRecord:
    return SymbolRecord(
        qualified_name="pkg.run",
        name="run",
        graph_file_path="service.py",
        host_file_path=repo_path / "service.py",
        line_start=1,
        line_end=3,
    )


def test_graph_expansion_candidates_are_real_relations_in_depth_order(tmp_path) -> None:
    for filename in ["service.py", "helper.py", "deep.py", "config.py"]:
        (tmp_path / filename).write_text(
            f"# {filename}\ndef value():\n    return 1\n",
            encoding="utf-8",
        )

    result = load_graph_expansion_candidates(
        project=_project(tmp_path),
        symbol=_symbol(tmp_path),
        graph_client=FakeFalkorDB(),
        repos_host_root=tmp_path,
    )

    traces = [candidate["trace"] for candidate in result["candidates"]]
    assert [(trace["depth"], trace["relationshipType"]) for trace in traces] == [
        (1, "CALLS"),
        (1, "IMPORTS"),
        (1, "FLOWS_TO"),
        (2, "CALLS"),
    ]
    assert {item["status"] for item in result["queryDiagnostics"]} == {"ok"}


def test_build_cases_holds_graph_evidence_for_adaptive_expansion(tmp_path) -> None:
    for filename in ["service.py", "helper.py", "deep.py", "config.py"]:
        (tmp_path / filename).write_text(
            f"# {filename}\ndef value():\n    return 1\n",
            encoding="utf-8",
        )
    symbol = _symbol(tmp_path)

    cases = build_cases(
        _project(tmp_path),
        [symbol],
        1,
        graph_client=FakeFalkorDB(),
        repos_host_root=tmp_path,
    )

    case = cases[0]
    assert len(case["goldItems"]) == 2
    assert case["goldItems"][1].startswith("relation:CALLS:")
    assert len(case["cg"]["chunks"]) == 1
    assert case["cg"]["retrievalTrace"][0]["relationshipType"] == "TARGET"
    assert len(case["cgExpansionPool"]) == 4
    assert case["requiredFacts"][:2] == ["run", "service.py"]
    assert case["requiredFacts"][-2:] == ["call", "deep"]
    assert "CALLS path from pkg.run to pkg.deep" in case["query"]


class ManyCandidateGraph(FakeGraph):
    def query(self, cypher: str, params: dict) -> SimpleNamespace:
        if "CALLS*1..2" in cypher:
            return SimpleNamespace(
                result_set=[
                    [f"pkg.target{index}", f"target{index}", f"target{index}.py", 1, 2, 1]
                    for index in range(1, 8)
                ]
            )
        return super().query(cypher, params)


class ManyCandidateFalkorDB:
    def select_graph(self, _name: str) -> ManyCandidateGraph:
        return ManyCandidateGraph()


def test_build_cases_selects_relation_gold_within_default_chunk_budget(tmp_path) -> None:
    for filename in [
        "service.py",
        "config.py",
        *(f"target{index}.py" for index in range(1, 8)),
    ]:
        (tmp_path / filename).write_text(
            f"# {filename}\ndef value():\n    return 1\n",
            encoding="utf-8",
        )

    case = build_cases(
        _project(tmp_path),
        [_symbol(tmp_path)],
        1,
        graph_client=ManyCandidateFalkorDB(),
        repos_host_root=tmp_path,
    )[0]

    assert len(case["cgExpansionPool"]) > 6
    assert case["goldItems"][-1].endswith("->pkg.target6")
    assert case["requiredFacts"][-1] == "target6"


def test_natural_relationship_facts_preserve_real_method_names() -> None:
    assert _natural_relation_fact("CALLS") == "call"
    assert _natural_relation_fact("IMPORTS") == "import"
    assert _natural_relation_fact("FLOWS_TO") == "flow"
    assert _natural_target_fact("__return__") == "return"
    assert _natural_target_fact("key = ''") == "key"
    assert _natural_target_fact("__aenter__") == "__aenter__"


def test_source_filename_accepts_both_path_separators() -> None:
    assert _source_filename("/repos/project/src/service.py") == "service.py"
    assert _source_filename(r"C:\repos\project\src\service.py") == "service.py"