from pathlib import Path


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def test_project_incremental_index_action_uses_explicit_label() -> None:
    markup = FRONTEND.read_text(encoding="utf-8")

    assert ">Incremental Index</button>" in markup
    assert "btn.textContent = 'Incremental Index'" in markup
    assert ">Reindex</button>" not in markup