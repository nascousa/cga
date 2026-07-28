import re
from pathlib import Path

from backend.indexer.language_catalog import PARSER_LANGUAGE_IDS


FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


def test_indexing_settings_exposes_parser_language_controls() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert 'id="settings-indexing-language-search"' in html
    assert 'id="settings-indexing-language-groups"' in html
    assert 'id="settings-indexing-language-status"' in html
    assert 'onclick="setAllParserLanguages(true)"' in html
    assert 'onclick="setAllParserLanguages(false)"' in html
    assert 'type="checkbox"' in html
    assert "function renderParserLanguageSettings" in html
    assert "function toggleParserLanguage" in html
    assert "disabled_parser_languages:" in html


def test_indexing_settings_spells_out_emoji_extensions() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    assert "'.🔥': '.🔥 (emoji extension)'" in html
    assert ".map(file => PARSER_LANGUAGE_FILE_LABELS[file] || file)" in html


def test_indexing_settings_assigns_unique_svg_icon_to_every_parser_language() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    icon_map = re.search(
        r"const PARSER_LANGUAGE_ICONS = Object\.freeze\(\{\n"
        r"(?P<body>.*?)\n\}\);\nconst PARSER_LANGUAGE_FILE_LABELS",
        html,
        re.DOTALL,
    )
    assert icon_map is not None
    entries = re.findall(
        r"^\s{2}([a-z][a-z0-9_]*): \{ mark: '([^']+)', "
        r"color: '(#[0-9a-f]{6})', ink: '(#[0-9a-f]{6})' \},$",
        icon_map.group("body"),
        re.MULTILINE,
    )
    language_ids = {language_id for language_id, _, _, _ in entries}
    marks = [mark for _, mark, _, _ in entries]

    assert language_ids == PARSER_LANGUAGE_IDS
    assert len(marks) == len(set(marks))
    assert 'class="parser-language-icon"' in html
    assert "function createParserLanguageIconSvg(iconConfig)" in html
    assert "document.createElementNS(PARSER_LANGUAGE_ICON_SVG_NS, 'svg')" in html
    assert "icon.replaceChildren(createParserLanguageIconSvg(iconConfig))" in html
    assert "icon.textContent = iconConfig.mark" not in html


def test_settings_subtabs_restore_from_their_deep_links() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    expected_routes = {
        "general": "/admin/settings",
        "graphdb": "/admin/settings/graphdb",
        "indexing": "/admin/settings/indexing",
        "integrations": "/admin/settings/integrations",
        "delegation": "/admin/settings/delegation",
        "security": "/admin/settings/security",
        "upgrade": "/admin/settings/upgrade",
        "backup": "/admin/settings/backup",
        "report": "/admin/settings/report",
    }
    for subtab, route in expected_routes.items():
        assert f"{subtab}: '{route}'" in html

    assert "function settingsSubtabFromLocation()" in html
    assert "setSettingsSubtabRoute(nextName" in html
    assert "switchSettingsSubtab(settingsSubtabFromLocation(), null, { updateUrl: false })" in html


def test_every_settings_section_has_persistent_per_user_collapse_state() -> None:
    html = FRONTEND.read_text(encoding="utf-8")

    expected_sections = {
        "general",
        "graphdb",
        "indexing",
        "integrations",
        "delegation",
        "security",
        "upgrade",
        "backup",
        "report",
    }
    actual_sections = set(
        re.findall(r'<section class="settings-section(?: active)?" id="settings-section-([a-z-]+)">', html)
    )

    assert actual_sections == expected_sections
    assert "const SETTINGS_COLLAPSE_STORAGE_KEY = 'cga_settings_collapsed_sections'" in html
    assert "function settingsCollapseStorageKey()" in html
    assert "encodeURIComponent(String(_me?.username || 'anonymous'))" in html
    assert "function enhanceSettingsSections()" in html
    assert "data-settings-section-collapse" in html
    assert "data-settings-section-content" in html
    assert "localStorage.setItem(settingsCollapseStorageKey()" in html
    assert "enhanceSettingsSections();" in html
