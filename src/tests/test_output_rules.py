import pytest

from backend.auth.output_rules import DEFAULT_RULES, render_markdown, resolve_rules, validate_rules


def test_project_overrides_global_and_inherits_unset_fields():
    resolved, provenance = resolve_rules(DEFAULT_RULES, {"summary": "none", "directives": ["Answer directly"]})
    assert resolved["summary"] == "none"
    assert resolved["show_diff_only"] is True
    assert provenance["summary"] == "project"
    assert provenance["show_diff_only"] == "global"


def test_unknown_rule_is_rejected():
    with pytest.raises(ValueError, match="Unknown output rule"):
        validate_rules({"made_up_rule": True})


def test_rendered_rules_are_marked_as_server_managed():
    markdown = render_markdown(DEFAULT_RULES, base_profile="concise", version=2, project_id=7)
    assert "CGA-MANAGED" in markdown
    assert "project 7" in markdown


def test_partial_global_profile_is_completed_without_changing_partial_overrides():
    resolved, provenance = resolve_rules({"summary": "none"}, {"tool_updates": "all"})
    assert set(resolved) == set(DEFAULT_RULES)
    assert resolved["summary"] == "none"
    assert provenance["preamble"] == "global"
    assert provenance["tool_updates"] == "project"
    assert validate_rules({"summary": "none"}) == {"summary": "none"}


def test_legacy_partial_profile_can_be_rendered():
    markdown = render_markdown({"summary": "none"}, base_profile="concise", version=1, project_id=None)
    assert "Summary: `none`" in markdown
    assert "Preamble: `none`" in markdown
