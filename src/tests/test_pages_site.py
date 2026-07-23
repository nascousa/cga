from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_pages_workflow_publishes_promotional_site() -> None:
    workflow = _read(".github/workflows/deploy-pages.yml")

    assert "docs/site/index.html" in workflow
    assert "cp -R docs/site/. site/" in workflow
    assert "! -name site" in workflow
    assert "touch site/.nojekyll" in workflow


def test_promotional_site_keeps_author_credit_to_footer() -> None:
    index = _read("docs/site/index.html")

    assert index.count("Nate Scott") == 1
    assert 'class="footer-credit"' in index
    assert 'href="mailto:nate@ucia.us"' in index
    assert "Created and authored by <a href=\"mailto:nate@ucia.us\" aria-label=\"Email project author\"><strong>Nate Scott</strong></a>" in index
    assert "by Nate Scott" not in index
    assert "Author spotlight" not in index
    assert "#author" not in index


def test_author_attribution_lives_in_project_documents() -> None:
    readme = _read("README.md")
    notice = _read("NOTICE.md")
    open_source = _read("OPEN_SOURCE.md")

    assert "## Author And Attribution" in readme
    assert "created and authored by Nate Scott" in readme
    assert "Copyright (c) 2026 Nate Scott" in notice
    assert "originally created and" in notice
    assert "maintained by Nate Scott" in notice
    assert "Project author and creator: Nate Scott" in open_source


def test_readme_uses_live_multi_project_benchmark_results() -> None:
    readme = _read("README.md")
    live_benchmark = _read("docs/benchmarks/live-context-quality.md")

    assert "## Historical Benchmark Snapshot" in readme
    assert "docs/benchmarks/live-context-quality.md" in readme
    assert "34 deterministic symbol-level cases per project" in live_benchmark
    assert "| Total real-code cases | 102 |" in readme
    assert "**90.44%**" in readme
    assert "BrowserAgent (BA)" not in live_benchmark
    assert "OSAgent (OSA)" not in live_benchmark
    assert "**68.4%**" not in live_benchmark


def test_promotional_site_uses_vanta_net_and_project_links() -> None:
    index = _read("docs/site/index.html")
    script = _read("docs/site/site.js")
    styles = _read("docs/site/styles.css")

    assert "vanta@0.5.24/dist/vanta.net.min.js" in index
    assert "three.min.js" in index
    assert "VANTA" in script
    assert "NET" in script
    assert "integrity=\"sha384-" in index
    assert "crossorigin=\"anonymous\"" in index
    assert "--vanta-bg-opacity: 0.20" in styles
    assert "#vanta-net canvas" in styles
    assert "opacity: var(--vanta-bg-opacity)" in styles
    assert "https://github.com/nascousa/cga" in index
    assert "https://codespaces.new/nascousa/cga?quickstart=1" in index


def test_project_icons_use_promotional_site_logo() -> None:
    index = _read("docs/site/index.html")
    favicon = _read("docs/site/favicon.svg")

    assert '<link rel="icon" href="./favicon.svg" type="image/svg+xml" />' in index
    assert 'aria-label="CGA"' in favicon
    assert '<rect width="64" height="64" rx="10" fill="#05070d"/>' in favicon
    assert '>CGA</text>' in favicon
    assert 'font-size="22"' in favicon
    assert "M12 42V22" not in favicon
    assert _read("src/frontend/favicon.svg") == favicon
    assert _read("src/viewer/favicon.svg") == favicon


def test_promotional_site_topbar_links_to_linkedin() -> None:
    index = _read("docs/site/index.html")

    assert '<a class="nav-action" href="https://nate.ucia.us" target="_blank" rel="noopener noreferrer">' in index
    assert 'class="linkedin-icon"' in index
    assert 'viewBox="0 0 24 24" fill="currentColor"' in index
    assert 'data-lucide="external-link"' not in index
    assert "LinkedIn" in index
    assert '<a class="nav-action" href="https://github.com/nascousa/cga"' not in index
    assert "GitHub\n      </a>" not in index


def test_promotional_site_topbar_uses_full_brand_text() -> None:
    index = _read("docs/site/index.html")
    styles = _read("docs/site/styles.css")

    assert '<span class="brand-copy">CONTEXT GRAPH AGENT</span>' in index
    assert ('<span class="brand-copy">ContextGraph' + 'Agent</span>') not in index
    assert ".brand-copy { color: var(--muted); font-size: 14px; font-weight: 700; letter-spacing: 0; white-space: nowrap; }" in styles


def test_promotional_site_uses_context_graph_agent_name() -> None:
    index = _read("docs/site/index.html")

    assert "Context Graph Agent" in index
    assert "ContextGraph" + "Agent" not in index
    assert "ContextGraph" + "Admin" not in index


def test_promotional_site_highlights_cga_retrieval_model() -> None:
    index = _read("docs/site/index.html")

    assert "about 90% fewer prompt tokens" in index
    assert "lower hallucination pressure" in index
    assert "find the right files, symbols, and dependencies faster" in index
    assert "retrieves the right evidence before generation" in index
    assert "query repository relationships" in index
    assert "keyword search alone" in index
    assert "Evidence Before Generation" in index
    assert "Repository Relationships" in index
    assert "impact graph to optimized context to minimal code" in index
    assert "files, symbols, calls, and dependencies faster" in index