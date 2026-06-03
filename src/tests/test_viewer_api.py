from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.auth.dependencies import get_registry, require_admin
from backend.main import app


class _FakeRegistry:
    def __init__(self, graph: MagicMock) -> None:
        self.graph = graph
        self.requested_projects: list[str] = []

    def get(self, project_name: str) -> MagicMock:
        self.requested_projects.append(project_name)
        return self.graph


def _result(rows: list[list]) -> MagicMock:
    result = MagicMock()
    result.result_set = rows
    return result


def test_viewer_stats_returns_known_node_and_edge_counts() -> None:
    graph = MagicMock()
    graph.query.side_effect = [
        _result([[2]]),
        _result([[3]]),
        _result([[5]]),
        _result([[0]]),
        _result([[7]]),
        _result([[11]]),
        _result([[13]]),
        _result([[17]]),
        _result([[19]]),
        _result([[23]]),
    ]
    registry = _FakeRegistry(graph)

    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        response = TestClient(app).get("/api/viewer/graphs/ContextGraph/stats")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "project_name": "contextgraph",
        "node_counts": {
            "Repository": 2,
            "File": 3,
            "Symbol": 5,
            "Variable": 0,
        },
        "edge_counts": {
            "CONTAINS": 7,
            "DEFINES": 11,
            "IMPORTS": 13,
            "CALLS": 17,
            "USES_VARIABLE": 19,
            "FLOWS_TO": 23,
        },
        "total_nodes": 10,
        "total_edges": 90,
        "default_chunk_limit": 50000,
        "max_chunk_limit": 500000,
    }
    assert registry.requested_projects == ["contextgraph"]


def test_viewer_chunk_uses_limit_as_node_count_and_returns_next_offset() -> None:
    graph = MagicMock()
    graph.query.side_effect = [
        _result(
            [
                [1, ["Symbol"], "pkg.source", None, "source", "function", None, "src/source.py", 10],
                [2, ["Symbol"], "pkg.target", None, "target", "function", None, "src/target.py", 20],
                [3, ["File"], None, "src/third.py", None, None, "python", "src/third.py", None],
            ]
        ),
        _result(
            [
                [
                    7,
                    "CALLS",
                    1,
                    ["Symbol"],
                    "pkg.source",
                    None,
                    "source",
                    "function",
                    None,
                    "src/source.py",
                    10,
                    2,
                    ["Symbol"],
                    "pkg.target",
                    None,
                    "target",
                    "function",
                    None,
                    "src/target.py",
                    20,
                ],
                [
                    8,
                    "CALLS",
                    1,
                    ["Symbol"],
                    "pkg.source",
                    None,
                    "source",
                    "function",
                    None,
                    "src/source.py",
                    10,
                    3,
                    ["File"],
                    None,
                    "src/third.py",
                    None,
                    None,
                    "python",
                    "src/third.py",
                    None,
                ],
            ]
        ),
    ]
    registry = _FakeRegistry(graph)

    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        response = TestClient(app).get("/api/viewer/graphs/contextgraph/chunk?offset=1&limit=2")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["project_name"] == "contextgraph"
    assert body["offset"] == 1
    assert body["limit"] == 2
    assert body["next_offset"] == 3
    assert body["links"] == [
        {"id": "CALLS:7", "source": "Symbol:pkg.source", "target": "Symbol:pkg.target", "type": "CALLS"},
    ]
    assert body["points"] == [
        {
            "id": "Symbol:pkg.source",
            "label": "source",
            "kind": "Symbol",
            "subtitle": "function",
            "file_path": "src/source.py",
            "line_start": 10,
        },
        {
            "id": "Symbol:pkg.target",
            "label": "target",
            "kind": "Symbol",
            "subtitle": "function",
            "file_path": "src/target.py",
            "line_start": 20,
        },
    ]
    assert graph.query.call_count == 2
    assert graph.query.call_args_list[0].args[1]["node_fetch_limit"] == 3
    assert graph.query.call_args_list[1].args[1]["node_ids"] == [1, 2]


def test_viewer_chunk_returns_no_more_cursor_when_node_page_is_not_full() -> None:
    graph = MagicMock()
    graph.query.side_effect = [
        _result([[1, ["Symbol"], "pkg.source", None, "source", "function", None, "src/source.py", 10]]),
        _result([]),
    ]
    registry = _FakeRegistry(graph)

    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        response = TestClient(app).get("/api/viewer/graphs/contextgraph/chunk?offset=1&limit=2")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 2
    assert body["next_offset"] is None
    assert body["links"] == []
    assert body["points"] == [
        {
            "id": "Symbol:pkg.source",
            "label": "source",
            "kind": "Symbol",
            "subtitle": "function",
            "file_path": "src/source.py",
            "line_start": 10,
        },
    ]


def test_viewer_rejects_invalid_project_name() -> None:
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    try:
        response = TestClient(app).get("/api/viewer/graphs/bad%20name/stats")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"] == "Project name may only contain letters, numbers, dot, dash, and underscore"


def test_viewer_entrypoint_is_served() -> None:
    response = TestClient(app).get("/viewer/")

    assert response.status_code == 200
    assert "<title>CGA Viewer</title>" in response.text
    assert '"sigma"' in response.text
    assert '"graphology"' in response.text
    assert 'src="./main.js?v=1.30.44"' in response.text
    assert 'id="copy-falkor-url"' in response.text
    assert 'aria-label="Copy FalkorDB connection URL"' in response.text
    assert '<label for="chunk-limit">Display Nodes</label>' in response.text
    assert 'value="250"' in response.text
    assert '<div id="edge-grid" class="edge-grid" aria-label="Edge types"></div>' in response.text
    assert 'id="toggle-edges" class="btn secondary" type="button" aria-pressed="false">Show Edges</button>' in response.text
    assert 'id="open-layout-settings" class="btn secondary" type="button">Settings</button>' in response.text
    assert '<div class="filter-title">Rendering Node Types</div>' in response.text
    assert 'class="node-type-grid" aria-label="Rendering Node Types"' in response.text
    assert 'name="node-type" value="Repository" checked' in response.text
    assert 'name="node-type" value="Variable" checked' in response.text
    assert '<div id="fps-counter" class="fps-counter">FPS --</div>' in response.text
    assert '<div id="focus-layer-control" class="focus-layer-control" aria-label="Focused graph layers" hidden>' in response.text
    assert 'id="focus-layer-slider" type="range" min="1" max="2" step="1" value="1"' in response.text
    assert '<span>Layer</span><strong id="focus-layer-value">1</strong>' in response.text
    assert 'id="clear-focus" class="focus-clear-button" type="button" aria-label="Clear focused graph"' in response.text
    assert '<div id="layout-settings-modal" class="modal-backdrop" hidden role="dialog" aria-modal="true" aria-labelledby="layout-settings-title">' in response.text
    assert '<h2 id="layout-settings-title">Layout Settings</h2>' in response.text
    assert 'data-layout-setting="repulsion"' in response.text
    assert 'data-layout-setting="linkStrength"' in response.text
    assert 'id="apply-layout-settings" class="btn primary" type="button">Apply</button>' in response.text
    assert 'id="reset-layout-settings" class="btn secondary" type="button">Reset</button>' in response.text
    assert '<button id="toggle-sim" class="btn secondary" type="button">Play</button>' in response.text
    assert '>3D Rotate</button>' not in response.text
    assert 'id="toggle-performance" class="btn secondary" type="button" aria-pressed="true">Performance On</button>' in response.text
    assert '<div id="cluster-overlay" class="cluster-overlay" hidden></div>' in response.text
    normalized_html = response.text.replace("\r\n", "\n")
    assert '<button id="load-first" class="btn primary" type="button">Load</button>\n            <button id="clear-graph" class="btn secondary" type="button">Clear</button>' in normalized_html
    assert 'id="load-next"' not in response.text
    assert "Session Token" not in response.text
    assert 'id="token-input"' not in response.text
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"


def test_viewer_static_assets_are_not_cached() -> None:
    response = TestClient(app).get("/viewer/main.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert "Sigma" in response.text
    assert "DEFAULT_CHUNK_LIMIT = 250" in response.text
    assert "MAX_AUTO_CHUNK_FETCHES = 100" in response.text
    assert "VISIBLE_NODE_BATCH_LIMIT = 5000" in response.text
    assert "DEFAULT_EDGE_VISIBILITY = false" in response.text
    assert "FALKOR_CONNECTION_URL = 'falkor://cga-falkordb-dev:6379'" in response.text
    assert "EDGE_VISIBILITY_STORAGE_KEY = 'cg_viewer_edges_visible_v4'" in response.text
    assert "NODE_KIND_VISIBILITY_STORAGE_KEY" in response.text
    assert "PERFORMANCE_MODE_STORAGE_KEY = 'cg_viewer_performance_mode_v1'" in response.text
    assert "LAYOUT_SETTINGS_STORAGE_KEY = 'cg_viewer_layout_settings_v1'" in response.text
    assert "function normalizeLayoutSettings" in response.text
    assert "layoutSettings: defaultLayoutSettings()" in response.text
    assert "function openLayoutSettingsModal" in response.text
    assert "function applyLayoutSettingsToLoadedGraph" in response.text
    assert "layoutSettings: state.layoutSettings" in response.text
    assert "new Worker(workerUrl, { type: 'module' })" in response.text
    assert "Float32Array" in response.text
    assert "function reduceNode" in response.text
    assert "function reduceEdge" in response.text
    assert "focusRootNode: null" in response.text
    assert "focusLayerDepth: 1" in response.text
    assert "function setFocusLayerDepth" in response.text
    assert "function buildFocusScope" in response.text
    assert "function focusNodeNeighborhood" in response.text
    assert "rightClickNode" in response.text
    assert "attributes.sourceNodeId !== nodeId" in response.text
    assert "state.focusVisibleEdges.has(edgeId)" in response.text
    assert "function syncClusterNodes" in response.text
    assert "function preprocessBatch" in response.text
    assert "LAYOUT_BARNES_HUT_THETA = 0.72" in response.text
    assert "function relaxLayoutPositions" in response.text
    assert "function applyBarnesHutRepulsion" in response.text
    assert "function applyCollisionForce" in response.text
    assert "const linkStrength = settings.linkStrength / minDegree" in response.text
    assert "function refreshPerformanceView" in response.text
    assert "CAMERA_INTERACTION_IDLE_MS = 160" in response.text
    assert "function markCameraActivity" in response.text
    assert "state.edgeLevel === 'hidden'" in response.text
    assert "state.lodLevel === 'cluster'" in response.text
    assert "EDGE_TYPE_ORDER = ['CALLS', 'IMPORTS', 'DEFINES', 'CONTAINS', 'USES_VARIABLE', 'FLOWS_TO']" in response.text
    assert "DEFAULT_SELECTED_EDGE_TYPES = new Set(['CALLS', 'IMPORTS', 'DEFINES', 'CONTAINS'])" in response.text
    assert "function renderEdgeTypeControls" in response.text
    assert "label.append(input, text)" in response.text
    assert "--edge-color" not in response.text
    assert "edge-dot" not in response.text
    assert "color: style.color" in response.text
    assert "function setNodeKindVisibility" in response.text
    assert "function syncLoadedCounts" in response.text
    assert "function startFpsCounter" in response.text
    assert "elements.toggleSim.textContent = 'Play'" in response.text
    assert "elements.toggleSim.textContent = 'Stop'" in response.text
    assert "function fetchProjectStats" in response.text
    assert "async function activeProjectsWithNodes" in response.text
    assert "viewerTotalNodes > 0" in response.text
    assert "No active projects with graph nodes are available." in response.text
    assert "Loading up to ${formatNumber(requestedVisibleNodes)} visible nodes" in response.text
    assert "Loaded ${formatNumber(loadedVisibleNodes)} visible nodes" in response.text
    assert "remainingVisibleNodes = Math.max(1, targetVisibleNodes - state.loadedNodes)" in response.text
    assert "requestVisibleNodes = Math.min(remainingVisibleNodes, VISIBLE_NODE_BATCH_LIMIT)" in response.text
    assert "limit: String(requestVisibleNodes)" in response.text
    assert "function yieldToBrowserFrame" in response.text
    assert "loadNext" not in response.text
    assert "Load more" not in response.text
    assert "defaultDrawNodeHover: drawNodeHover" in response.text
    assert "HOVER_LABEL_FONT" in response.text
    assert "renderLabels: false" in response.text
    assert "forceLabel: false" in response.text
    assert "Sigma 3D projection" not in response.text
    assert "saveToken" not in response.text


def test_viewer_styles_keep_control_buttons_visible() -> None:
    response = TestClient(app).get("/viewer/styles.css")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert ".field-row {" in response.text
    assert "flex-wrap: wrap" in response.text
    assert ".field-row .btn {" in response.text
    assert "flex: 1 1 120px" in response.text
    assert ".focus-layer-control {" in response.text
    assert "left: 18px" in response.text
    assert "width: fit-content" in response.text
    assert "min-width: min(244px, calc(100% - 36px))" in response.text
    assert "max-width: min(720px, calc(100% - 36px))" in response.text
    assert ".focus-node-label {" in response.text
    assert "max-width: 100%" in response.text
    assert ".focus-layer-control input[type=\"range\"]" in response.text
    assert ".modal-backdrop {" in response.text
    assert ".layout-settings-grid {" in response.text
    assert ".modal-actions {" in response.text


def test_viewer_worker_asset_is_not_cached() -> None:
    response = TestClient(app).get("/viewer/worker.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"
    assert "preprocessBatch" in response.text
    assert "Float32Array" in response.text
    assert "initial3dPosition" in response.text
    assert "function normalizeLayoutSettings" in response.text
    assert "message.layoutSettings" in response.text
    assert "settings.repulsion" in response.text
    assert "LAYOUT_BARNES_HUT_THETA = 0.72" in response.text
    assert "function relaxLayoutPositions" in response.text
    assert "function applyCollisionForce" in response.text


def test_viewer_entrypoint_redirects_to_trailing_slash() -> None:
    response = TestClient(app).get("/viewer", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/viewer/"


def test_admin_embeds_versioned_graph_viewer() -> None:
    response = TestClient(app).get("/admin")

    assert response.status_code == 200
    assert 'data-src="/viewer/?v=1.30.44"' in response.text
    assert '<h1>CONTEXT GRAPH AGENT</h1>' in response.text
    assert '<span class="logo-text">CONTEXT GRAPH AGENT</span>' in response.text
    assert '<strong>Retrieve the right evidence before generation</strong>' in response.text
    assert 'Repository relationships, not only keywords' in response.text
    assert '<span class="cga-feature-step">Impact graph</span>' in response.text
    assert '<span class="cga-feature-step">Optimized context</span>' in response.text
    assert '<span class="cga-feature-step">Minimal code</span>' in response.text
    assert 'File/symbol targeting' in response.text
    assert 'Dependency awareness' in response.text
    assert 'id="wsr-save-btn" onclick="saveWorkReportEdit(\'this\')"' in response.text
    assert 'id="wsr-save-prev-btn" onclick="saveWorkReportEdit(\'prev\')"' in response.text
    assert 'id="wsr-preview" class="wsr-preview-editor"' in response.text
    assert 'id="wsr-preview-prev" class="wsr-preview-editor"' in response.text
    assert 'onclick="copyFalkorDbUrl()"' in response.text
    assert 'aria-label="Copy FalkorDB connection URL"' in response.text
    assert "const ADMIN_TAB_ROUTES" in response.text
    assert "viewer: '/admin/graph'" in response.text


def test_admin_exposes_settings_tab() -> None:
    response = TestClient(app).get("/admin")

    assert response.status_code == 200
    assert "settings: '/admin/settings'" in response.text
    assert 'id="tab-settings-btn"' in response.text
    assert 'id="pane-settings"' in response.text
    # Admin-only visibility is enforced via display:none + canOpenAdminTab.
    assert 'id="tab-settings-btn" style="display:none"' in response.text


def test_admin_deep_links_are_served() -> None:
    client = TestClient(app)

    for path in ["/admin/projects", "/admin/users", "/admin/audit", "/admin/graph", "/admin/settings"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "<title>CGA (ContextGraphAgent)</title>" in response.text
        assert '<span class="logo-text">CONTEXT GRAPH AGENT</span>' in response.text
        assert "const ADMIN_TAB_ROUTES" in response.text


def test_viewer_stats_advertises_500k_chunk_limit() -> None:
    graph = MagicMock()
    graph.query.return_value = _result([[0]])
    registry = _FakeRegistry(graph)

    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    app.dependency_overrides[get_registry] = lambda: registry
    try:
        response = TestClient(app).get("/api/viewer/graphs/contextgraph/stats")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["max_chunk_limit"] == 500000
