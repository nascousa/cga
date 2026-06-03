import Graphology from 'graphology'
import Sigma from 'sigma'

const VIEWER_VERSION = '1.30.43'
const EDGE_STYLES = {
  CALLS: { label: 'Calls', color: '#4ae387', width: 1.7, priority: 6 },
  IMPORTS: { label: 'Imports', color: '#5badff', width: 1.45, priority: 5 },
  DEFINES: { label: 'Defines', color: '#ffd642', width: 1.4, priority: 4 },
  USES_VARIABLE: { label: 'Uses variable', color: '#42e694', width: 1.2, priority: 3 },
  FLOWS_TO: { label: 'Flows to', color: '#4ae387', width: 1.25, priority: 2 },
  CONTAINS: { label: 'Contains', color: '#adb8cc', width: 1.1, priority: 1 },
  UNKNOWN: { label: 'Unclassified', color: '#adb8cc', width: 1, priority: 0 },
}
const EDGE_TYPE_ORDER = ['CALLS', 'IMPORTS', 'DEFINES', 'CONTAINS', 'USES_VARIABLE', 'FLOWS_TO']
const DEFAULT_SELECTED_EDGE_TYPES = new Set(['CALLS', 'IMPORTS', 'DEFINES', 'CONTAINS'])

const KIND_ORDER = ['Repository', 'File', 'Symbol', 'Variable', 'Node']
const KIND_INDEX = new Map(KIND_ORDER.map((kind, index) => [kind, index]))
const NODE_KIND_COLORS = { Repository: '#e06c75', File: '#61afef', Symbol: '#c678dd', Variable: '#d19a66', Node: '#7f848e' }
function kindColor(kind) { return NODE_KIND_COLORS[kind] || NODE_KIND_COLORS.Node }
const MAX_CLIENT_NODES = 500000
const MAX_CHUNK_LIMIT = 500000
const MIN_CHUNK_LIMIT = 1
const DEFAULT_CHUNK_LIMIT = 250
const DEFAULT_EDGE_VISIBILITY = false
const DEFAULT_PERFORMANCE_MODE = true
const FALKOR_CONNECTION_URL = 'falkor://cga-falkordb-dev:6379'
const EDGE_VISIBILITY_STORAGE_KEY = 'cg_viewer_edges_visible_v4'
const NODE_KIND_VISIBILITY_STORAGE_KEY = 'cg_viewer_node_kinds_visible_v1'
const PERFORMANCE_MODE_STORAGE_KEY = 'cg_viewer_performance_mode_v1'
const LAYOUT_SETTINGS_STORAGE_KEY = 'cg_viewer_layout_settings_v1'
const FPS_SAMPLE_MS = 500
const MAX_AUTO_CHUNK_FETCHES = 100
const VISIBLE_NODE_BATCH_LIMIT = 5000
const LIVE_ROTATION_NODE_LIMIT = 120000
const LOD_NODE_THRESHOLD = 2500
const LOD_MID_RATIO = 0.72
const LOD_FAR_RATIO = 1.05
const EDGE_THROTTLE_NODE_THRESHOLD = 3500
const EDGE_MID_RATIO = 0.62
const EDGE_FAR_RATIO = 0.92
const CLUSTER_NODE_PREFIX = '__cg_cluster__'
const CLUSTER_NODE_VISIBILITY_RATIO = 0.84
const CLUSTER_NODE_THRESHOLD = 6000
const CLUSTER_ONLY_RATIO = 1.16
const CLUSTER_MOTION_RATIO = 0.72
const CAMERA_INTERACTION_IDLE_MS = 160
const MOTION_LOD_NODE_THRESHOLD = 1600
const MOTION_EDGE_NODE_THRESHOLD = 1600
const WORKER_TIMEOUT_MS = 8000

const ROTATION_FRAME_MS = 72
const GOLDEN_ANGLE = 2.399963229728653
const BASE_DEPTH = 900
const CAMERA_DISTANCE = 3200
const LAYOUT_RELAX_ITERATIONS = 3
const LAYOUT_RELAX_MAX_POINTS = 10000
const LAYOUT_BARNES_HUT_THETA = 0.72
const LAYOUT_REPULSION = 1500
const LAYOUT_LINK_DISTANCE = 210
const LAYOUT_LINK_STRENGTH = 0.035
const LAYOUT_COLLISION_PADDING = 22
const LAYOUT_COLLISION_CELL_SIZE = 260
const LAYOUT_MAX_STEP = 95
const LAYOUT_MAX_DEPTH = 16
const LAYOUT_SETTING_SCHEMA = Object.freeze({
  relaxIterations: { defaultValue: LAYOUT_RELAX_ITERATIONS, min: 0, max: 12, integer: true },
  relaxMaxPoints: { defaultValue: LAYOUT_RELAX_MAX_POINTS, min: 0, max: 50000, integer: true },
  barnesHutTheta: { defaultValue: LAYOUT_BARNES_HUT_THETA, min: 0.3, max: 1.5 },
  repulsion: { defaultValue: LAYOUT_REPULSION, min: 0, max: 6000 },
  linkDistance: { defaultValue: LAYOUT_LINK_DISTANCE, min: 20, max: 800 },
  linkStrength: { defaultValue: LAYOUT_LINK_STRENGTH, min: 0, max: 0.2 },
  collisionPadding: { defaultValue: LAYOUT_COLLISION_PADDING, min: 0, max: 120 },
  collisionCellSize: { defaultValue: LAYOUT_COLLISION_CELL_SIZE, min: 80, max: 800 },
  maxStep: { defaultValue: LAYOUT_MAX_STEP, min: 5, max: 300 },
  maxDepth: { defaultValue: LAYOUT_MAX_DEPTH, min: 8, max: 24, integer: true },
})
const BASE_NODE_SIZE = 3.6
const NODE_SIZE_STEP = 1.55
const MAX_NODE_SIZE = 18
const HOVER_LABEL_MAX_CHARS = 72
const HOVER_LABEL_FONT = '600 13px "Segoe UI", "Noto Sans", Arial, sans-serif'
const HOVER_LABEL_PADDING_X = 10
const HOVER_LABEL_HEIGHT = 28
const HOVER_LABEL_RADIUS = 6

function defaultLayoutSettings() {
  return Object.fromEntries(Object.entries(LAYOUT_SETTING_SCHEMA).map(([key, schema]) => [key, schema.defaultValue]))
}

function normalizeLayoutSetting(key, value) {
  const schema = LAYOUT_SETTING_SCHEMA[key]
  if (!schema) return undefined
  const numericValue = Number(value)
  const fallbackValue = schema.defaultValue
  const boundedValue = Number.isFinite(numericValue) ? clamp(numericValue, schema.min, schema.max) : fallbackValue
  return schema.integer ? Math.round(boundedValue) : Number(boundedValue.toFixed(4))
}

function normalizeLayoutSettings(settings = {}) {
  const normalized = {}
  for (const key of Object.keys(LAYOUT_SETTING_SCHEMA)) {
    normalized[key] = normalizeLayoutSetting(key, settings[key])
  }
  return normalized
}

const state = {
  graph: null,
  renderer: null,
  projectName: '',
  nextOffset: 0,
  loadedNodeIds: new Set(),
  loadedEdgeIds: new Set(),
  nodeTypes: new Map(),
  nodeDegrees: new Map(),
  nodesById: new Map(),
  loadedNodes: 0,
  loadedEdges: 0,
  skippedNodes: 0,
  skippedEdges: 0,
  hasNext: false,
  edgesVisible: DEFAULT_EDGE_VISIBILITY,
  performanceMode: DEFAULT_PERFORMANCE_MODE,
  layoutSettings: defaultLayoutSettings(),
  visibleNodeKinds: new Set(KIND_ORDER),
  controlPanelCollapsed: false,
  cameraRatio: 1,
  cameraMoving: false,
  cameraIdleTimer: null,
  lodLevel: 'full',
  edgeLevel: 'full',
  performanceSignature: '',
  performanceFrame: null,
  hoveredNode: null,
  focusRootNode: null,
  focusLayerDepth: 1,
  focusVisibleNodes: new Set(),
  focusVisibleEdges: new Set(),
  focusPreviousEdgesVisible: null,
  dataNodeCount: 0,
  clusterNodes: new Set(),
  clusterCounts: new Map(),
  edgesByNode: new Map(),
  outgoingEdgesByNode: new Map(),
  projectionNodeIds: [],
  nodeProjectionIndex: new Map(),
  projectionCapacity: 0,
  projectionX3d: new Float32Array(0),
  projectionY3d: new Float32Array(0),
  projectionZ3d: new Float32Array(0),
  projectionBaseSize: new Float32Array(0),
  projectionScratch: new Float32Array(0),
  worker: null,
  workerRequestId: 0,
  workerCallbacks: new Map(),
  rotationX: -0.58,
  rotationY: 0.64,
  rotationZ: 0.08,
  rotating: false,
  rotationFrame: null,
  lastRotationAt: 0,
  fpsFrame: null,
  fpsLastSampleAt: 0,
  fpsFrameCount: 0,
}

const elements = {
  workspace: document.getElementById('workspace'),
  copyFalkorUrl: document.getElementById('copy-falkor-url'),
  togglePanel: document.getElementById('toggle-panel'),
  projectSelect: document.getElementById('project-select'),
  refreshProjects: document.getElementById('refresh-projects'),
  loadFirst: document.getElementById('load-first'),
  clearGraph: document.getElementById('clear-graph'),
  fitView: document.getElementById('fit-view'),
  toggleSim: document.getElementById('toggle-sim'),
  toggleEdges: document.getElementById('toggle-edges'),
  togglePerformance: document.getElementById('toggle-performance'),
  openLayoutSettings: document.getElementById('open-layout-settings'),
  layoutSettingsModal: document.getElementById('layout-settings-modal'),
  closeLayoutSettings: document.getElementById('close-layout-settings'),
  applyLayoutSettings: document.getElementById('apply-layout-settings'),
  resetLayoutSettings: document.getElementById('reset-layout-settings'),
  layoutSettingInputs: [...document.querySelectorAll('[data-layout-setting]')],
  edgeGrid: document.getElementById('edge-grid'),
  nodeTypeInputs: [...document.querySelectorAll('input[name="node-type"]')],
  searchInput: document.getElementById('search-input'),
  chunkLimit: document.getElementById('chunk-limit'),
  graphRoot: document.getElementById('graph-root'),
  clusterOverlay: document.getElementById('cluster-overlay'),
  focusLayerControl: document.getElementById('focus-layer-control'),
  focusLayerSlider: document.getElementById('focus-layer-slider'),
  focusLayerValue: document.getElementById('focus-layer-value'),
  focusNodeLabel: document.getElementById('focus-node-label'),
  clearFocus: document.getElementById('clear-focus'),
  fpsCounter: document.getElementById('fps-counter'),
  statusLine: document.getElementById('status-line'),
  dbNodes: document.getElementById('db-nodes'),
  dbEdges: document.getElementById('db-edges'),
  loadedNodes: document.getElementById('loaded-nodes'),
  loadedEdges: document.getElementById('loaded-edges'),
  cursorValue: document.getElementById('cursor-value'),
}

function token() {
  return localStorage.getItem('cg_jwt') || ''
}

function setStatus(message, tone = 'neutral') {
  elements.statusLine.textContent = message
  elements.statusLine.dataset.tone = tone
}

function formatNumber(value) {
  return new Intl.NumberFormat().format(Number(value || 0))
}

async function copyTextToClipboard(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  textarea.style.top = '0'
  document.body.append(textarea)
  textarea.focus()
  textarea.select()
  const copied = document.execCommand('copy')
  textarea.remove()
  if (!copied) throw new Error('Clipboard copy is unavailable in this browser.')
}

function edgeStyle(edgeType) {
  return EDGE_STYLES[edgeType] || EDGE_STYLES.UNKNOWN
}

function renderEdgeTypeControls() {
  elements.edgeGrid.replaceChildren(
    ...EDGE_TYPE_ORDER.map((edgeType) => {
      const style = edgeStyle(edgeType)
      const label = document.createElement('label')

      const input = document.createElement('input')
      input.type = 'checkbox'
      input.name = 'edge-type'
      input.value = edgeType
      input.checked = DEFAULT_SELECTED_EDGE_TYPES.has(edgeType)

      const text = document.createElement('span')
      text.textContent = style.label

      label.append(input, text)
      return label
    }),
  )
}

function clamp(value, minValue, maxValue) {
  return Math.max(minValue, Math.min(maxValue, value))
}

function hashString(value) {
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

function createGraph() {
  return new Graphology({ type: 'directed', multi: true, allowSelfLoops: true })
}

function ensureGraph() {
  if (!state.graph) state.graph = createGraph()
  return state.graph
}

async function api(path) {
  const headers = { Accept: 'application/json' }
  const jwt = token()
  if (jwt) headers.Authorization = `Bearer ${jwt}`
  const response = await fetch(path, { headers })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = payload.detail || response.statusText
    throw new Error(response.status === 401 ? `${detail}. Open Admin and sign in first.` : detail)
  }
  return payload
}

function selectedEdgeTypes() {
  return [...document.querySelectorAll('input[name="edge-type"]:checked')]
    .map((input) => input.value)
    .join(',')
}

function normalizeNodeKind(kind) {
  return KIND_INDEX.has(kind) ? kind : 'Node'
}

function selectedNodeKinds() {
  return new Set(elements.nodeTypeInputs.filter((input) => input.checked).map((input) => input.value))
}

function isNodeKindVisible(kind) {
  return state.visibleNodeKinds.has(normalizeNodeKind(kind))
}

function restoreNodeKindVisibility() {
  const storedValue = localStorage.getItem(NODE_KIND_VISIBILITY_STORAGE_KEY)
  const storedKinds = storedValue === null
    ? new Set(KIND_ORDER)
    : new Set(storedValue.split(',').filter((kind) => KIND_INDEX.has(kind)))
  elements.nodeTypeInputs.forEach((input) => {
    input.checked = storedKinds.has(input.value)
  })
  state.visibleNodeKinds = selectedNodeKinds()
}

function persistNodeKindVisibility() {
  localStorage.setItem(NODE_KIND_VISIBILITY_STORAGE_KEY, [...state.visibleNodeKinds].join(','))
}

function chunkLimit() {
  const value = Number(elements.chunkLimit.value || DEFAULT_CHUNK_LIMIT)
  return Math.max(MIN_CHUNK_LIMIT, Math.min(MAX_CHUNK_LIMIT, value))
}

function updateCounters() {
  elements.loadedNodes.textContent = formatNumber(state.loadedNodes)
  elements.loadedEdges.textContent = formatNumber(state.loadedEdges)
  elements.cursorValue.textContent = formatNumber(state.nextOffset)
}

function syncLoadedCounts() {
  if (!state.graph) {
    state.loadedNodes = 0
    state.loadedEdges = 0
    return
  }
  let visibleNodes = 0
  state.graph.forEachNode((nodeId, attributes) => {
    if (!state.clusterNodes.has(nodeId) && attributes.hidden !== true) visibleNodes += 1
  })
  state.loadedNodes = visibleNodes
  state.loadedEdges = state.graph.size
}

function resizeGraphAfterPanelToggle() {
  window.setTimeout(() => {
    state.renderer?.resize?.()
    fitGraph()
  }, 220)
}

function setControlPanelCollapsed(collapsed) {
  state.controlPanelCollapsed = collapsed
  elements.workspace.classList.toggle('controls-collapsed', collapsed)
  elements.togglePanel.setAttribute('aria-label', collapsed ? 'Expand controls' : 'Collapse controls')
  elements.togglePanel.setAttribute('title', collapsed ? 'Expand controls' : 'Collapse controls')
  elements.togglePanel.setAttribute('aria-pressed', collapsed ? 'true' : 'false')
  localStorage.setItem('cg_viewer_controls_collapsed', collapsed ? '1' : '0')
  resizeGraphAfterPanelToggle()
}

function pointSizeForDegree(degree) {
  return Math.min(MAX_NODE_SIZE, BASE_NODE_SIZE + Math.log2(degree + 1) * NODE_SIZE_STEP)
}

function nodeKindIndex(kind) {
  return KIND_INDEX.get(normalizeNodeKind(kind)) ?? KIND_INDEX.get('Node')
}

function initial3dPosition(point, nodeIndex) {
  const kindIndex = nodeKindIndex(point.kind)
  const hash = hashString(point.id)
  const hashAngle = ((hash & 0xffff) / 0xffff) * Math.PI * 2
  const angle = nodeIndex * GOLDEN_ANGLE + hashAngle
  const localStep = nodeIndex > 250000 ? 2.9 : nodeIndex > 100000 ? 3.6 : 5.4
  const radius = Math.sqrt(nodeIndex + 1) * localStep + kindIndex * 240
  const layerOffset = (kindIndex - (KIND_ORDER.length - 1) / 2) * 260
  const depthNoise = (((hash >>> 16) & 0xffff) / 0xffff - 0.5) * BASE_DEPTH
  return {
    x3d: Math.cos(angle) * radius,
    y3d: Math.sin(angle) * radius,
    z3d: layerOffset + depthNoise,
  }
}

function layoutMass(degree) {
  return 1 + Math.log2(degree + 1) * 1.7
}

function layoutCollisionRadius(degree, settings) {
  return 18 + Math.log2(degree + 1) * 8 + settings.collisionPadding
}

function safeLayoutVector(deltaX, deltaY, fallbackIndex) {
  const distance = Math.hypot(deltaX, deltaY)
  if (distance > 0.0001) return { x: deltaX / distance, y: deltaY / distance, distance }
  const angle = fallbackIndex * GOLDEN_ANGLE
  return { x: Math.cos(angle), y: Math.sin(angle), distance: 0.0001 }
}

function buildLayoutModel(points, links, settings) {
  const indexById = new Map()
  points.forEach((point, index) => indexById.set(point.id, index))

  const degrees = new Float32Array(points.length)
  const edgeSources = []
  const edgeTargets = []

  for (const link of links || []) {
    const sourceIndex = indexById.get(link.source)
    const targetIndex = indexById.get(link.target)
    if (sourceIndex === undefined || targetIndex === undefined) continue
    degrees[sourceIndex] += 1
    degrees[targetIndex] += 1
    if (sourceIndex === targetIndex) continue
    edgeSources.push(sourceIndex)
    edgeTargets.push(targetIndex)
  }

  const masses = new Float32Array(points.length)
  const radii = new Float32Array(points.length)
  for (let index = 0; index < points.length; index += 1) {
    masses[index] = layoutMass(degrees[index])
    radii[index] = layoutCollisionRadius(degrees[index], settings)
  }

  return { degrees, masses, radii, edgeSources, edgeTargets }
}

function createQuadNode(minX, minY, maxX, maxY) {
  return { minX, minY, maxX, maxY, mass: 0, centerX: 0, centerY: 0, point: -1, children: null }
}

function subdivideQuad(node) {
  const midX = (node.minX + node.maxX) / 2
  const midY = (node.minY + node.maxY) / 2
  node.children = [
    createQuadNode(node.minX, node.minY, midX, midY),
    createQuadNode(midX, node.minY, node.maxX, midY),
    createQuadNode(node.minX, midY, midX, node.maxY),
    createQuadNode(midX, midY, node.maxX, node.maxY),
  ]
}

function quadChildFor(node, x, y) {
  const midX = (node.minX + node.maxX) / 2
  const midY = (node.minY + node.maxY) / 2
  return (y >= midY ? 2 : 0) + (x >= midX ? 1 : 0)
}

function insertQuadPoint(node, pointIndex, positionsX, positionsY, masses, settings, depth = 0) {
  const x = positionsX[pointIndex]
  const y = positionsY[pointIndex]
  const mass = masses[pointIndex]
  const nextMass = node.mass + mass
  node.centerX = nextMass ? ((node.centerX * node.mass) + (x * mass)) / nextMass : x
  node.centerY = nextMass ? ((node.centerY * node.mass) + (y * mass)) / nextMass : y
  node.mass = nextMass

  if (!node.children && node.point === -1) {
    node.point = pointIndex
    return
  }
  if (!node.children) {
    if (depth >= settings.maxDepth) return
    const existingPoint = node.point
    node.point = -1
    subdivideQuad(node)
    insertQuadPoint(node.children[quadChildFor(node, positionsX[existingPoint], positionsY[existingPoint])], existingPoint, positionsX, positionsY, masses, settings, depth + 1)
  }
  insertQuadPoint(node.children[quadChildFor(node, x, y)], pointIndex, positionsX, positionsY, masses, settings, depth + 1)
}

function buildBarnesHutTree(positionsX, positionsY, masses, count, settings) {
  let minX = Number.POSITIVE_INFINITY
  let minY = Number.POSITIVE_INFINITY
  let maxX = Number.NEGATIVE_INFINITY
  let maxY = Number.NEGATIVE_INFINITY
  for (let index = 0; index < count; index += 1) {
    minX = Math.min(minX, positionsX[index])
    minY = Math.min(minY, positionsY[index])
    maxX = Math.max(maxX, positionsX[index])
    maxY = Math.max(maxY, positionsY[index])
  }
  const span = Math.max(maxX - minX, maxY - minY, 1)
  const padding = span * 0.05 + 1
  const root = createQuadNode(minX - padding, minY - padding, maxX + padding, maxY + padding)
  for (let index = 0; index < count; index += 1) insertQuadPoint(root, index, positionsX, positionsY, masses, settings)
  return root
}

function applyBarnesHutRepulsion(node, pointIndex, positionsX, positionsY, masses, deltasX, deltasY, settings) {
  if (!node.mass || (node.point === pointIndex && !node.children)) return
  const deltaX = positionsX[pointIndex] - node.centerX
  const deltaY = positionsY[pointIndex] - node.centerY
  const vector = safeLayoutVector(deltaX, deltaY, pointIndex)
  const width = node.maxX - node.minX

  if (!node.children || width / Math.max(vector.distance, 1) < settings.barnesHutTheta) {
    const distanceSq = Math.max(vector.distance * vector.distance, 64)
    const force = (settings.repulsion * masses[pointIndex] * node.mass) / distanceSq
    deltasX[pointIndex] += vector.x * force
    deltasY[pointIndex] += vector.y * force
    return
  }

  for (const child of node.children) applyBarnesHutRepulsion(child, pointIndex, positionsX, positionsY, masses, deltasX, deltasY, settings)
}

function applyLinkAttraction(model, positionsX, positionsY, deltasX, deltasY, settings) {
  for (let edgeIndex = 0; edgeIndex < model.edgeSources.length; edgeIndex += 1) {
    const sourceIndex = model.edgeSources[edgeIndex]
    const targetIndex = model.edgeTargets[edgeIndex]
    const deltaX = positionsX[targetIndex] - positionsX[sourceIndex]
    const deltaY = positionsY[targetIndex] - positionsY[sourceIndex]
    const vector = safeLayoutVector(deltaX, deltaY, sourceIndex + targetIndex)
    const minDegree = Math.max(1, Math.min(model.degrees[sourceIndex], model.degrees[targetIndex]))
    const targetDistance = settings.linkDistance + Math.log2(minDegree + 1) * 18
    const linkStrength = settings.linkStrength / minDegree
    const pull = (vector.distance - targetDistance) * linkStrength
    const sourceMass = model.masses[sourceIndex]
    const targetMass = model.masses[targetIndex]
    const totalMass = sourceMass + targetMass
    const sourceShare = targetMass / totalMass
    const targetShare = sourceMass / totalMass
    deltasX[sourceIndex] += vector.x * pull * sourceShare
    deltasY[sourceIndex] += vector.y * pull * sourceShare
    deltasX[targetIndex] -= vector.x * pull * targetShare
    deltasY[targetIndex] -= vector.y * pull * targetShare
  }
}

function collisionCellKey(cellX, cellY) {
  return `${cellX}:${cellY}`
}

function applyCollisionForce(model, positionsX, positionsY, deltasX, deltasY, settings) {
  const cells = new Map()
  for (let index = 0; index < positionsX.length; index += 1) {
    const cellX = Math.floor(positionsX[index] / settings.collisionCellSize)
    const cellY = Math.floor(positionsY[index] / settings.collisionCellSize)
    const key = collisionCellKey(cellX, cellY)
    let bucket = cells.get(key)
    if (!bucket) {
      bucket = []
      cells.set(key, bucket)
    }
    bucket.push(index)
  }

  for (let index = 0; index < positionsX.length; index += 1) {
    const cellX = Math.floor(positionsX[index] / settings.collisionCellSize)
    const cellY = Math.floor(positionsY[index] / settings.collisionCellSize)
    for (let offsetX = -1; offsetX <= 1; offsetX += 1) {
      for (let offsetY = -1; offsetY <= 1; offsetY += 1) {
        const bucket = cells.get(collisionCellKey(cellX + offsetX, cellY + offsetY))
        if (!bucket) continue
        for (const otherIndex of bucket) {
          if (otherIndex <= index) continue
          const requiredDistance = model.radii[index] + model.radii[otherIndex]
          const deltaX = positionsX[index] - positionsX[otherIndex]
          const deltaY = positionsY[index] - positionsY[otherIndex]
          const distance = Math.hypot(deltaX, deltaY)
          if (distance >= requiredDistance) continue
          const vector = safeLayoutVector(deltaX, deltaY, index + otherIndex)
          const push = (requiredDistance - distance) * 0.58
          deltasX[index] += vector.x * push
          deltasY[index] += vector.y * push
          deltasX[otherIndex] -= vector.x * push
          deltasY[otherIndex] -= vector.y * push
        }
      }
    }
  }
}

function relaxLayoutPositions(points, links, baseNodeIndex, layoutSettings = state.layoutSettings) {
  const settings = normalizeLayoutSettings(layoutSettings)
  const positions = new Float32Array(points.length * 3)
  for (let index = 0; index < points.length; index += 1) {
    const position = initial3dPosition(points[index], baseNodeIndex + index)
    const offset = index * 3
    positions[offset] = position.x3d
    positions[offset + 1] = position.y3d
    positions[offset + 2] = position.z3d
  }

  if (points.length < 2 || points.length > settings.relaxMaxPoints) return positions

  const model = buildLayoutModel(points, links, settings)
  const positionsX = new Float32Array(points.length)
  const positionsY = new Float32Array(points.length)
  const deltasX = new Float32Array(points.length)
  const deltasY = new Float32Array(points.length)
  for (let index = 0; index < points.length; index += 1) {
    const offset = index * 3
    positionsX[index] = positions[offset]
    positionsY[index] = positions[offset + 1]
  }

  for (let iteration = 0; iteration < settings.relaxIterations; iteration += 1) {
    deltasX.fill(0)
    deltasY.fill(0)
    const root = buildBarnesHutTree(positionsX, positionsY, model.masses, points.length, settings)
    for (let index = 0; index < points.length; index += 1) {
      applyBarnesHutRepulsion(root, index, positionsX, positionsY, model.masses, deltasX, deltasY, settings)
    }
    applyLinkAttraction(model, positionsX, positionsY, deltasX, deltasY, settings)
    applyCollisionForce(model, positionsX, positionsY, deltasX, deltasY, settings)

    for (let index = 0; index < points.length; index += 1) {
      const step = Math.hypot(deltasX[index], deltasY[index])
      const scale = step > settings.maxStep ? settings.maxStep / step : 1
      positionsX[index] += deltasX[index] * scale
      positionsY[index] += deltasY[index] * scale
    }
  }

  for (let index = 0; index < points.length; index += 1) {
    const offset = index * 3
    positions[offset] = positionsX[index]
    positions[offset + 1] = positionsY[index]
  }
  return positions
}

function clusterNodeId(kind) {
  return `${CLUSTER_NODE_PREFIX}${kind}`
}

function cluster3dPosition(kind) {
  const kindIndex = nodeKindIndex(kind)
  const angle = (kindIndex / KIND_ORDER.length) * Math.PI * 2 - Math.PI / 2
  const radius = 520
  return {
    x3d: Math.cos(angle) * radius,
    y3d: Math.sin(angle) * radius,
    z3d: (kindIndex - (KIND_ORDER.length - 1) / 2) * 320,
  }
}

function ensureProjectionCapacity(nextCapacity) {
  if (state.projectionCapacity >= nextCapacity) return
  let next = Math.max(1024, state.projectionCapacity || 0)
  while (next < nextCapacity) next *= 2

  const nextX3d = new Float32Array(next)
  const nextY3d = new Float32Array(next)
  const nextZ3d = new Float32Array(next)
  const nextBaseSize = new Float32Array(next)
  const nextScratch = new Float32Array(next * 4)
  nextX3d.set(state.projectionX3d)
  nextY3d.set(state.projectionY3d)
  nextZ3d.set(state.projectionZ3d)
  nextBaseSize.set(state.projectionBaseSize)
  nextScratch.set(state.projectionScratch)
  state.projectionCapacity = next
  state.projectionX3d = nextX3d
  state.projectionY3d = nextY3d
  state.projectionZ3d = nextZ3d
  state.projectionBaseSize = nextBaseSize
  state.projectionScratch = nextScratch
}

function resetProjectionStorage() {
  state.projectionNodeIds = []
  state.nodeProjectionIndex.clear()
  state.projectionCapacity = 0
  state.projectionX3d = new Float32Array(0)
  state.projectionY3d = new Float32Array(0)
  state.projectionZ3d = new Float32Array(0)
  state.projectionBaseSize = new Float32Array(0)
  state.projectionScratch = new Float32Array(0)
}

function recordProjectionNode(nodeId, position, baseSize) {
  let index = state.nodeProjectionIndex.get(nodeId)
  if (index === undefined) {
    index = state.projectionNodeIds.length
    state.projectionNodeIds.push(nodeId)
    state.nodeProjectionIndex.set(nodeId, index)
    ensureProjectionCapacity(index + 1)
  }
  state.projectionX3d[index] = position.x3d
  state.projectionY3d[index] = position.y3d
  state.projectionZ3d[index] = position.z3d
  state.projectionBaseSize[index] = baseSize
  return index
}

function projectionAttributesForIndex(index, color) {
  const projection = project3d(
    state.projectionX3d[index] || 0,
    state.projectionY3d[index] || 0,
    state.projectionZ3d[index] || 0,
  )
  const scratchOffset = index * 4
  const baseSize = state.projectionBaseSize[index] || BASE_NODE_SIZE
  const size = clamp(baseSize * (0.72 + projection.scale * 0.28), 1.2, MAX_NODE_SIZE + 16)
  state.projectionScratch[scratchOffset] = projection.x
  state.projectionScratch[scratchOffset + 1] = projection.y
  state.projectionScratch[scratchOffset + 2] = size
  state.projectionScratch[scratchOffset + 3] = projection.depth
  return {
    x: projection.x,
    y: projection.y,
    size,
    color,
    zIndex: projection.depth,
  }
}

function pointPositionFromBatch(batch, pointIndex) {
  if (batch.positions instanceof Float32Array) {
    const offset = pointIndex * 3
    return {
      x3d: batch.positions[offset],
      y3d: batch.positions[offset + 1],
      z3d: batch.positions[offset + 2],
    }
  }
  return initial3dPosition(batch.points[pointIndex], state.dataNodeCount)
}

function rejectWorkerCallbacks(error) {
  for (const callback of state.workerCallbacks.values()) {
    window.clearTimeout(callback.timeout)
    callback.resolve(preprocessBatchOnMainThread(callback.batch, callback.baseNodeIndex, callback.layoutSettings))
  }
  state.workerCallbacks.clear()
  console.warn(error)
}

function handleWorkerMessage(event) {
  const message = event.data || {}
  if (message.type !== 'preprocessBatchDone') return
  const callback = state.workerCallbacks.get(message.id)
  if (!callback) return
  state.workerCallbacks.delete(message.id)
  window.clearTimeout(callback.timeout)
  callback.resolve({
    ...callback.batch,
    positions: new Float32Array(message.positions),
  })
}

function ensurePreprocessWorker() {
  if (state.worker || !('Worker' in window)) return state.worker
  try {
    const workerUrl = new URL('./worker.js', import.meta.url)
    workerUrl.search = `v=${VIEWER_VERSION}`
    state.worker = new Worker(workerUrl, { type: 'module' })
    state.worker.addEventListener('message', handleWorkerMessage)
    state.worker.addEventListener('error', (error) => {
      state.worker?.terminate()
      state.worker = null
      rejectWorkerCallbacks(error)
    })
  } catch (error) {
    state.worker = null
    console.warn(error)
  }
  return state.worker
}

function preprocessBatchOnMainThread(batch, baseNodeIndex, layoutSettings = state.layoutSettings) {
  const positions = relaxLayoutPositions(batch.points, batch.links, baseNodeIndex, layoutSettings)
  return { ...batch, positions }
}

async function preprocessBatch(batch, baseNodeIndex) {
  const layoutSettings = normalizeLayoutSettings(state.layoutSettings)
  if (!state.performanceMode) return preprocessBatchOnMainThread(batch, baseNodeIndex, layoutSettings)
  const worker = ensurePreprocessWorker()
  if (!worker) return preprocessBatchOnMainThread(batch, baseNodeIndex, layoutSettings)

  const id = state.workerRequestId + 1
  state.workerRequestId = id
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      state.workerCallbacks.delete(id)
      resolve(preprocessBatchOnMainThread(batch, baseNodeIndex, layoutSettings))
    }, WORKER_TIMEOUT_MS)
    state.workerCallbacks.set(id, { batch, baseNodeIndex, layoutSettings, resolve, timeout })
    try {
      worker.postMessage({
        id,
        type: 'preprocessBatch',
        baseNodeIndex,
        layoutSettings: state.layoutSettings,
        points: batch.points.map((point) => ({ id: point.id, kind: point.kind })),
        links: (batch.links || []).map((link) => ({ source: link.source, target: link.target })),
      })
    } catch (error) {
      window.clearTimeout(timeout)
      state.workerCallbacks.delete(id)
      console.warn(error)
      resolve(preprocessBatchOnMainThread(batch, baseNodeIndex, layoutSettings))
    }
  })
}

let _projCosX = Math.cos(-0.58)
let _projSinX = Math.sin(-0.58)
let _projCosY = Math.cos(0.64)
let _projSinY = Math.sin(0.64)
let _projCosZ = Math.cos(0.08)
let _projSinZ = Math.sin(0.08)
let _projRotX = -0.58
let _projRotY = 0.64
let _projRotZ = 0.08

function syncProjectionTrig() {
  if (state.rotationX !== _projRotX || state.rotationY !== _projRotY || state.rotationZ !== _projRotZ) {
    _projCosX = Math.cos(state.rotationX)
    _projSinX = Math.sin(state.rotationX)
    _projCosY = Math.cos(state.rotationY)
    _projSinY = Math.sin(state.rotationY)
    _projCosZ = Math.cos(state.rotationZ)
    _projSinZ = Math.sin(state.rotationZ)
    _projRotX = state.rotationX
    _projRotY = state.rotationY
    _projRotZ = state.rotationZ
  }
}

function project3d(x3d, y3d, z3d) {
  const yAfterX = y3d * _projCosX - z3d * _projSinX
  const zAfterX = y3d * _projSinX + z3d * _projCosX

  const xAfterY = x3d * _projCosY + zAfterX * _projSinY
  const zAfterY = -x3d * _projSinY + zAfterX * _projCosY

  const xAfterZ = xAfterY * _projCosZ - yAfterX * _projSinZ
  const yAfterZ = xAfterY * _projSinZ + yAfterX * _projCosZ

  const perspective = clamp(CAMERA_DISTANCE / Math.max(900, CAMERA_DISTANCE + zAfterY), 0.32, 2.15)
  const depth = clamp(zAfterY / BASE_DEPTH, -1, 1)
  return {
    x: xAfterZ * perspective,
    y: yAfterZ * perspective,
    depth,
    scale: perspective,
  }
}

function projectedNodeAttributes(attributes) {
  const projectionIndex = state.nodeProjectionIndex.get(attributes.nodeId)
  if (projectionIndex !== undefined) return projectionAttributesForIndex(projectionIndex, attributes.baseColor || NODE_KIND_COLORS.Node)
  const projection = project3d(attributes.x3d || 0, attributes.y3d || 0, attributes.z3d || 0)
  const baseSize = attributes.baseSize || BASE_NODE_SIZE
  return {
    x: projection.x,
    y: projection.y,
    size: clamp(baseSize * (0.72 + projection.scale * 0.28), 1.2, MAX_NODE_SIZE + 16),
    color: attributes.baseColor || NODE_KIND_COLORS.Node,
    zIndex: projection.depth,
  }
}

function truncateHoverLabel(label) {
  const value = String(label || '')
  if (value.length <= HOVER_LABEL_MAX_CHARS) return value
  return `${value.slice(0, HOVER_LABEL_MAX_CHARS - 3)}...`
}

function drawRoundedRect(context, x, y, width, height, radius) {
  const nextRadius = Math.min(radius, width / 2, height / 2)
  context.beginPath()
  context.moveTo(x + nextRadius, y)
  context.lineTo(x + width - nextRadius, y)
  context.quadraticCurveTo(x + width, y, x + width, y + nextRadius)
  context.lineTo(x + width, y + height - nextRadius)
  context.quadraticCurveTo(x + width, y + height, x + width - nextRadius, y + height)
  context.lineTo(x + nextRadius, y + height)
  context.quadraticCurveTo(x, y + height, x, y + height - nextRadius)
  context.lineTo(x, y + nextRadius)
  context.quadraticCurveTo(x, y, x + nextRadius, y)
  context.closePath()
}

function drawNodeHover(context, nodeData) {
  const label = truncateHoverLabel(nodeData.label || nodeData.rawLabel || nodeData.key)
  const nodeRadius = Math.max(nodeData.size || BASE_NODE_SIZE, BASE_NODE_SIZE)

  context.save()
  context.lineWidth = 2.25
  context.strokeStyle = '#f6fbff'
  context.fillStyle = 'rgba(255, 255, 255, 0.12)'
  context.beginPath()
  context.arc(nodeData.x, nodeData.y, nodeRadius + 4, 0, Math.PI * 2)
  context.fill()
  context.stroke()

  if (label) {
    context.font = HOVER_LABEL_FONT
    context.textBaseline = 'middle'
    context.textAlign = 'left'

    const textWidth = Math.ceil(context.measureText(label).width)
    const boxWidth = textWidth + HOVER_LABEL_PADDING_X * 2
    const boxHeight = HOVER_LABEL_HEIGHT
    const gap = nodeRadius + 10
    const canvasWidth = context.canvas.width
    const canvasHeight = context.canvas.height
    const boxX = clamp(nodeData.x + gap, 6, canvasWidth - boxWidth - 6)
    const boxY = clamp(nodeData.y - boxHeight / 2, 6, canvasHeight - boxHeight - 6)

    context.shadowColor = 'rgba(0, 0, 0, 0.45)'
    context.shadowBlur = 12
    context.shadowOffsetY = 3
    drawRoundedRect(context, boxX, boxY, boxWidth, boxHeight, HOVER_LABEL_RADIUS)
    context.fillStyle = 'rgba(11, 18, 28, 0.96)'
    context.fill()

    context.shadowColor = 'transparent'
    context.lineWidth = 1.25
    context.strokeStyle = '#75d6ff'
    context.stroke()

    context.fillStyle = '#f8fbff'
    context.fillText(label, boxX + HOVER_LABEL_PADDING_X, boxY + boxHeight / 2)
  }

  context.restore()
}

function shouldUseClusterLod() {
  if (!state.performanceMode || state.dataNodeCount < CLUSTER_NODE_THRESHOLD) return false
  if (state.cameraRatio >= CLUSTER_ONLY_RATIO) return true
  return state.cameraMoving && state.cameraRatio >= CLUSTER_MOTION_RATIO
}

function computeLodLevel() {
  if (!state.performanceMode) return 'full'
  const motionLodActive = state.cameraMoving && state.dataNodeCount >= MOTION_LOD_NODE_THRESHOLD
  if (!motionLodActive && state.dataNodeCount < LOD_NODE_THRESHOLD) return 'full'
  if (shouldUseClusterLod()) return 'cluster'
  if (motionLodActive) {
    return state.cameraRatio >= LOD_MID_RATIO ? 'far' : 'mid'
  }
  if (state.cameraRatio >= LOD_FAR_RATIO) return 'far'
  if (state.cameraRatio >= LOD_MID_RATIO) return 'mid'
  return 'full'
}

function computeEdgeLevel() {
  if (!state.performanceMode) return 'full'
  const motionEdgeActive = state.cameraMoving && state.dataNodeCount >= MOTION_EDGE_NODE_THRESHOLD
  if (shouldUseClusterLod() || motionEdgeActive) return 'hidden'
  if (state.dataNodeCount < EDGE_THROTTLE_NODE_THRESHOLD) return 'full'
  if (state.rotating || state.cameraRatio >= EDGE_FAR_RATIO) return 'focus'
  if (state.cameraRatio >= EDGE_MID_RATIO) return 'priority'
  return 'full'
}

function shouldShowClusters() {
  if (state.focusRootNode) return false
  return state.performanceMode
    && state.dataNodeCount >= LOD_NODE_THRESHOLD
    && (state.lodLevel === 'cluster' || state.cameraRatio >= CLUSTER_NODE_VISIBILITY_RATIO)
}

function buildFocusScope(rootNodeId, layerDepth) {
  const visibleNodes = new Set()
  const visibleEdges = new Set()
  if (!state.graph?.hasNode(rootNodeId)) return { visibleNodes, visibleEdges }

  visibleNodes.add(rootNodeId)
  let frontier = new Set([rootNodeId])
  for (let depth = 0; depth < layerDepth; depth += 1) {
    const nextFrontier = new Set()
    for (const nodeId of frontier) {
      const edgeIds = state.outgoingEdgesByNode.get(nodeId)
      if (!edgeIds) continue
      for (const edgeId of edgeIds) {
        if (!state.graph.hasEdge(edgeId)) continue
        const attributes = state.graph.getEdgeAttributes(edgeId)
        if (attributes.sourceNodeId !== nodeId) continue
        const targetNodeId = attributes.targetNodeId
        if (!targetNodeId || !state.graph.hasNode(targetNodeId)) continue
        visibleEdges.add(edgeId)
        if (!visibleNodes.has(targetNodeId)) nextFrontier.add(targetNodeId)
        visibleNodes.add(targetNodeId)
      }
    }
    if (!nextFrontier.size) break
    frontier = nextFrontier
  }

  return { visibleNodes, visibleEdges }
}

function rebuildFocusScope() {
  if (!state.focusRootNode) return
  const scope = buildFocusScope(state.focusRootNode, state.focusLayerDepth)
  state.focusVisibleNodes = scope.visibleNodes
  state.focusVisibleEdges = scope.visibleEdges
}

function nodeHiddenByFocus(nodeId, attributes) {
  if (!state.focusRootNode) return false
  if (attributes.clusterNode === true) return true
  return !state.focusVisibleNodes.has(nodeId)
}

function edgeHiddenByFocus(edgeId) {
  if (!state.focusRootNode) return false
  return !state.focusVisibleEdges.has(edgeId)
}

function kindLodBase(kind) {
  switch (normalizeNodeKind(kind)) {
    case 'Repository': return 4
    case 'Symbol': return 3
    case 'File': return 2
    case 'Variable': return 1
    case 'Node': return 1
    default: return 1
  }
}

function lodTierFor(kind, degree) {
  const base = kindLodBase(kind)
  if (degree >= 24) return 4
  if (degree >= 10) return Math.max(base, 3)
  if (degree >= 4) return Math.max(base, 2)
  return base
}

function nodeHiddenByPerformance(_nodeId, attributes) {
  if (!state.performanceMode) return false
  if (state.focusRootNode && state.focusVisibleNodes.has(_nodeId)) return false
  if (attributes.clusterNode === true) return !shouldShowClusters()
  if (state.lodLevel === 'full') return false
  if (state.lodLevel === 'cluster') return true
  const tier = attributes.lodTier || lodTierFor(attributes.kind, attributes.degree || 0)
  if (state.lodLevel === 'far') return tier < 4
  if (state.lodLevel === 'mid') return tier < 2
  return false
}

function edgeHiddenByPerformance(_edgeId, attributes) {
  if (!state.performanceMode || attributes.hidden === true) return false
  if (state.focusRootNode && state.focusVisibleEdges.has(_edgeId)) return false
  if (state.edgeLevel === 'full') return false
  if (state.edgeLevel === 'hidden') return true
  const hoveredNode = state.hoveredNode
  if (hoveredNode && (attributes.sourceNodeId === hoveredNode || attributes.targetNodeId === hoveredNode)) return false
  if (state.edgeLevel === 'focus') return true
  const style = edgeStyle(attributes.edgeType)
  if (style.priority >= edgeStyle('DEFINES').priority) return false
  const sourceDegree = state.nodeDegrees.get(attributes.sourceNodeId) || 0
  const targetDegree = state.nodeDegrees.get(attributes.targetNodeId) || 0
  return sourceDegree < 8 && targetDegree < 8
}

function reduceNode(nodeId, attributes) {
  const reduced = { ...attributes }
  if (nodeHiddenByFocus(nodeId, attributes) || nodeHiddenByPerformance(nodeId, attributes)) reduced.hidden = true
  if (!reduced.hidden && state.focusRootNode === nodeId) {
    reduced.color = '#66d9ef'
    reduced.size = Math.max(reduced.size || attributes.size || BASE_NODE_SIZE, (attributes.baseSize || BASE_NODE_SIZE) + 3)
    reduced.zIndex = 3
  }
  if (attributes.clusterNode === true) {
    reduced.size = clamp(attributes.baseSize || BASE_NODE_SIZE, 10, MAX_NODE_SIZE + 16)
    reduced.zIndex = 1
  }
  return reduced
}

function reduceEdge(edgeId, attributes) {
  const reduced = { ...attributes }
  if (edgeHiddenByFocus(edgeId, attributes) || edgeHiddenByPerformance(edgeId, attributes)) reduced.hidden = true
  return reduced
}

function updateClusterOverlay() {
  if (!elements.clusterOverlay) return
  const show = shouldShowClusters() && state.clusterCounts.size > 0
  elements.clusterOverlay.hidden = !show
  if (!show) {
    elements.clusterOverlay.replaceChildren()
    return
  }
  const rows = KIND_ORDER
    .map((kind) => [kind, state.clusterCounts.get(kind) || 0])
    .filter(([, count]) => count > 0)
    .map(([kind, count]) => {
      const row = document.createElement('div')
      row.style.setProperty('--node-color', kindColor(kind))
      const dot = document.createElement('i')
      dot.className = 'node-dot'
      const label = document.createElement('span')
      label.textContent = `${kind === 'Node' ? 'Other' : kind} ${formatNumber(count)}`
      row.append(dot, label)
      return row
    })
  elements.clusterOverlay.replaceChildren(...rows)
}

function updatePerformanceStateFromCamera() {
  state.cameraRatio = state.renderer?.getCamera().getState().ratio || 1
  state.lodLevel = computeLodLevel()
  state.edgeLevel = computeEdgeLevel()
}

function performanceSignature() {
  return [
    state.performanceMode ? '1' : '0',
    state.lodLevel,
    state.edgeLevel,
    state.hoveredNode || '',
    state.focusRootNode || '',
    String(state.focusLayerDepth),
    state.rotating ? '1' : '0',
    state.cameraMoving ? '1' : '0',
  ].join(':')
}

function refreshPerformanceView(force = false) {
  if (!state.renderer) return
  updatePerformanceStateFromCamera()
  const signature = performanceSignature()
  updateClusterOverlay()
  if (!force && signature === state.performanceSignature) return
  state.performanceSignature = signature
  state.renderer.refresh({ skipIndexation: true, schedule: true })
}

function schedulePerformanceRefresh(force = false) {
  if (!state.renderer) return
  if (state.performanceFrame) return
  state.performanceFrame = requestAnimationFrame(() => {
    state.performanceFrame = null
    refreshPerformanceView(force)
  })
}

function clearCameraActivityTimer() {
  if (!state.cameraIdleTimer) return
  window.clearTimeout(state.cameraIdleTimer)
  state.cameraIdleTimer = null
}

function resetCameraActivity() {
  clearCameraActivityTimer()
  state.cameraMoving = false
}

function markCameraActivity() {
  const wasMoving = state.cameraMoving
  state.cameraMoving = true
  clearCameraActivityTimer()
  state.cameraIdleTimer = window.setTimeout(() => {
    state.cameraIdleTimer = null
    if (!state.cameraMoving) return
    state.cameraMoving = false
    schedulePerformanceRefresh(true)
  }, CAMERA_INTERACTION_IDLE_MS)
  schedulePerformanceRefresh(!wasMoving)
}

function rendererSettings() {
  return {
    allowInvalidContainer: true,
    autoCenter: true,
    autoRescale: true,
    defaultEdgeType: 'line',
    defaultNodeColor: NODE_KIND_COLORS.Node,
    defaultDrawNodeHover: drawNodeHover,
    defaultEdgeColor: '#4a5368',
    enableEdgeEvents: false,
    hideEdgesOnMove: true,
    hideLabelsOnMove: true,
    itemSizesReference: 'screen',
    labelColor: { color: '#edf1f7' },
    labelDensity: 0,
    labelFont: 'Segoe UI, Noto Sans, Arial, sans-serif',
    labelRenderedSizeThreshold: Number.POSITIVE_INFINITY,
    labelSize: 11,
    minEdgeThickness: 0.85,
    nodeReducer: reduceNode,
    edgeReducer: reduceEdge,
    renderEdgeLabels: false,
    renderLabels: false,
    stagePadding: 28,
    zIndex: true,
  }
}

function tuneRendererForScale() {
  if (!state.renderer || !state.graph) return
  const nodeCount = state.graph.order
  state.renderer.setSettings({
    hideEdgesOnMove: nodeCount > 70000,
    hideLabelsOnMove: nodeCount > 12000,
    labelDensity: 0,
    labelRenderedSizeThreshold: Number.POSITIVE_INFINITY,
    renderLabels: false,
  })
}

function bindRendererEvents() {
  state.renderer.getCamera().on('updated', markCameraActivity)
  state.renderer.on('enterNode', ({ node }) => {
    state.hoveredNode = node
    schedulePerformanceRefresh(true)
  })
  state.renderer.on('leaveNode', ({ node }) => {
    if (state.hoveredNode === node) state.hoveredNode = null
    schedulePerformanceRefresh(true)
  })
  state.renderer.on('clickNode', ({ node }) => {
    const point = state.nodesById.get(node)
    if (!point && state.graph?.getNodeAttribute(node, 'clusterNode') === true) {
      const kind = state.graph.getNodeAttribute(node, 'kind')
      const count = state.graph.getNodeAttribute(node, 'clusterCount') || 0
      setStatus(`${kind === 'Node' ? 'Other' : kind}: ${formatNumber(count)} loaded nodes in the aggregate view.`, 'ok')
      return
    }
    if (!point) return
    const location = point.file_path ? ` - ${point.file_path}${point.line_start ? `:${point.line_start}` : ''}` : ''
    const edgeType = state.nodeTypes.get(node) || 'UNKNOWN'
    const typeLabel = edgeStyle(edgeType).label
    const degree = state.nodeDegrees.get(node) || 0
    setStatus(`${point.kind}: ${point.label}${location} (${typeLabel}, ${formatNumber(degree)} connection${degree === 1 ? '' : 's'})`, 'ok')
  })
  state.renderer.on('rightClickNode', ({ node, event }) => {
    event?.preventSigmaDefault?.()
    event?.original?.preventDefault?.()
    event?.original?.stopPropagation?.()
    focusNodeNeighborhood(node, true)
  })
}

function ensureRenderer() {
  const graph = ensureGraph()
  if (state.renderer) return state.renderer
  state.renderer = new Sigma(graph, elements.graphRoot, rendererSettings())
  bindRendererEvents()
  updatePerformanceStateFromCamera()
  tuneRendererForScale()
  return state.renderer
}

function stopRotation() {
  if (state.rotationFrame) cancelAnimationFrame(state.rotationFrame)
  state.rotationFrame = null
  state.rotating = false
  elements.toggleSim.textContent = 'Play'
  schedulePerformanceRefresh(true)
}

async function destroyGraph() {
  stopRotation()
  resetCameraActivity()
  if (state.performanceFrame) cancelAnimationFrame(state.performanceFrame)
  state.performanceFrame = null
  state.renderer?.kill?.()
  state.renderer = null
  state.graph = null
  elements.graphRoot.replaceChildren()
  elements.clusterOverlay?.replaceChildren()
}

function resetState() {
  state.nextOffset = 0
  state.loadedNodeIds.clear()
  state.loadedEdgeIds.clear()
  state.nodeTypes.clear()
  state.nodeDegrees.clear()
  state.nodesById.clear()
  state.loadedNodes = 0
  state.loadedEdges = 0
  state.skippedNodes = 0
  state.skippedEdges = 0
  state.hasNext = false
  state.hoveredNode = null
  state.focusRootNode = null
  state.focusLayerDepth = 1
  state.focusVisibleNodes.clear()
  state.focusVisibleEdges.clear()
  state.focusPreviousEdgesVisible = null
  state.dataNodeCount = 0
  state.clusterNodes.clear()
  state.clusterCounts.clear()
  state.edgesByNode.clear()
  state.outgoingEdgesByNode.clear()
  state.cameraRatio = 1
  resetCameraActivity()
  state.lodLevel = 'full'
  state.edgeLevel = 'full'
  state.performanceSignature = ''
  resetProjectionStorage()
  state.rotationX = -0.58
  state.rotationY = 0.64
  state.rotationZ = 0.08
  stopRotation()
  updateFocusLayerControl()
  updateCounters()
}

function applyProjectionToAllNodes() {
  if (!state.graph || !state.graph.order) return
  syncProjectionTrig()
  state.graph.updateEachNodeAttributes(
    (nodeId, attributes) => {
      const projectionIndex = state.nodeProjectionIndex.get(nodeId)
      if (projectionIndex === undefined) return attributes
      const projection = projectionAttributesForIndex(projectionIndex, attributes.baseColor || NODE_KIND_COLORS.Node)
      attributes.x = projection.x
      attributes.y = projection.y
      attributes.size = projection.size
      attributes.color = attributes.baseColor || NODE_KIND_COLORS.Node
      attributes.zIndex = projection.zIndex
      return attributes
    },
    { attributes: ['x', 'y', 'size', 'color', 'zIndex'] },
  )
  refreshPerformanceView(true)
}

function fitGraph() {
  if (!state.renderer) return
  state.renderer.getCamera().animatedReset({ duration: 350 })
}

function updateFpsCounter(now) {
  if (!elements.fpsCounter) return
  if (!state.fpsLastSampleAt) {
    state.fpsLastSampleAt = now
    state.fpsFrameCount = 0
  } else {
    state.fpsFrameCount += 1
    const elapsed = now - state.fpsLastSampleAt
    if (elapsed >= FPS_SAMPLE_MS) {
      elements.fpsCounter.textContent = `FPS ${Math.round((state.fpsFrameCount * 1000) / elapsed)}`
      state.fpsLastSampleAt = now
      state.fpsFrameCount = 0
    }
  }
  state.fpsFrame = requestAnimationFrame(updateFpsCounter)
}

function startFpsCounter() {
  if (!elements.fpsCounter || state.fpsFrame) return
  elements.fpsCounter.textContent = 'FPS --'
  state.fpsLastSampleAt = 0
  state.fpsFrameCount = 0
  state.fpsFrame = requestAnimationFrame(updateFpsCounter)
}

function skippedSummary() {
  if (!state.skippedNodes && !state.skippedEdges) return ''
  return `${formatNumber(state.skippedNodes)} nodes and ${formatNumber(state.skippedEdges)} edges skipped after the 500,000-node cap.`
}

function statusWithSkippedSummary(message) {
  const skipped = skippedSummary()
  return skipped ? `${message} ${skipped}` : message
}

function yieldToBrowserFrame() {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()))
}

function setEdgesVisible(visible, showStatus = false) {
  state.edgesVisible = visible
  elements.toggleEdges.textContent = visible ? 'Hide Edges' : 'Show Edges'
  elements.toggleEdges.setAttribute('aria-pressed', visible ? 'true' : 'false')
  localStorage.setItem(EDGE_VISIBILITY_STORAGE_KEY, visible ? '1' : '0')
  refreshEdgeVisibility(true)
  syncLoadedCounts()
  updateCounters()
  if (showStatus) setStatus(statusWithSkippedSummary(`Edges ${visible ? 'shown' : 'hidden'}.`), 'ok')
}

function setPerformanceMode(enabled, showStatus = false) {
  state.performanceMode = enabled
  if (elements.togglePerformance) {
    elements.togglePerformance.textContent = enabled ? 'Performance On' : 'Performance Off'
    elements.togglePerformance.setAttribute('aria-pressed', enabled ? 'true' : 'false')
  }
  localStorage.setItem(PERFORMANCE_MODE_STORAGE_KEY, enabled ? '1' : '0')
  refreshPerformanceView(true)
  if (showStatus) setStatus(`Performance mode ${enabled ? 'enabled' : 'disabled'}.`, 'ok')
}

function formatLayoutSettingValue(key, value) {
  if (LAYOUT_SETTING_SCHEMA[key]?.integer) return String(Math.round(value))
  if (key === 'linkStrength') return String(Number(value).toFixed(3))
  return String(Number(value))
}

function updateLayoutSettingsForm() {
  for (const input of elements.layoutSettingInputs) {
    const key = input.dataset.layoutSetting
    if (!key || state.layoutSettings[key] === undefined) continue
    input.value = formatLayoutSettingValue(key, state.layoutSettings[key])
  }
}

function restoreLayoutSettings() {
  const storedValue = localStorage.getItem(LAYOUT_SETTINGS_STORAGE_KEY)
  if (!storedValue) {
    state.layoutSettings = defaultLayoutSettings()
    updateLayoutSettingsForm()
    return
  }
  try {
    state.layoutSettings = normalizeLayoutSettings(JSON.parse(storedValue))
  } catch (error) {
    console.warn(error)
    state.layoutSettings = defaultLayoutSettings()
  }
  updateLayoutSettingsForm()
}

function persistLayoutSettings() {
  localStorage.setItem(LAYOUT_SETTINGS_STORAGE_KEY, JSON.stringify(state.layoutSettings))
}

function readLayoutSettingsForm() {
  const nextSettings = {}
  for (const input of elements.layoutSettingInputs) {
    const key = input.dataset.layoutSetting
    if (!key) continue
    nextSettings[key] = input.value
  }
  return normalizeLayoutSettings(nextSettings)
}

function setLayoutSettings(settings, persist = true) {
  state.layoutSettings = normalizeLayoutSettings(settings)
  if (persist) persistLayoutSettings()
  updateLayoutSettingsForm()
}

function openLayoutSettingsModal() {
  if (!elements.layoutSettingsModal) return
  updateLayoutSettingsForm()
  elements.layoutSettingsModal.hidden = false
  elements.layoutSettingInputs[0]?.focus()
}

function closeLayoutSettingsModal() {
  if (elements.layoutSettingsModal) elements.layoutSettingsModal.hidden = true
}

async function applyLayoutSettingsToLoadedGraph(showStatus = false) {
  if (!state.graph?.order || !state.nodesById.size) {
    if (showStatus) setStatus('Layout settings saved.', 'ok')
    return
  }
  const pointIds = state.projectionNodeIds.filter((nodeId) => state.nodesById.has(nodeId))
  const points = pointIds.map((nodeId) => state.nodesById.get(nodeId))
  const pointIdSet = new Set(pointIds)
  const links = []
  state.graph.forEachEdge((_edgeId, attributes) => {
    if (pointIdSet.has(attributes.sourceNodeId) && pointIdSet.has(attributes.targetNodeId)) {
      links.push({ source: attributes.sourceNodeId, target: attributes.targetNodeId })
    }
  })
  setStatus(`Applying layout settings to ${formatNumber(points.length)} loaded nodes...`)
  const preprocessed = await preprocessBatch({ points, links }, 0)
  for (let index = 0; index < points.length; index += 1) {
    const point = points[index]
    const baseSize = state.graph.getNodeAttribute(point.id, 'baseSize') || BASE_NODE_SIZE
    recordProjectionNode(point.id, pointPositionFromBatch(preprocessed, index), baseSize)
  }
  applyProjectionToAllNodes()
  if (showStatus) setStatus(`Layout settings applied to ${formatNumber(points.length)} loaded nodes.`, 'ok')
}

async function applyLayoutSettingsFromModal() {
  setLayoutSettings(readLayoutSettingsForm())
  closeLayoutSettingsModal()
  await applyLayoutSettingsToLoadedGraph(true)
}

function resetLayoutSettings() {
  localStorage.removeItem(LAYOUT_SETTINGS_STORAGE_KEY)
  setLayoutSettings(defaultLayoutSettings(), false)
  setStatus('Layout settings reset.', 'ok')
}

function updateFocusLayerControl() {
  if (!elements.focusLayerControl) return
  const hasFocus = Boolean(state.focusRootNode)
  elements.focusLayerControl.hidden = !hasFocus
  if (elements.focusLayerSlider) elements.focusLayerSlider.value = String(state.focusLayerDepth)
  if (elements.focusLayerValue) elements.focusLayerValue.textContent = String(state.focusLayerDepth)
  if (!elements.focusNodeLabel) return
  if (!hasFocus) {
    elements.focusNodeLabel.textContent = ''
    return
  }
  const point = state.nodesById.get(state.focusRootNode)
  elements.focusNodeLabel.textContent = truncateHoverLabel(point?.label || state.focusRootNode)
}

function refreshFocusView() {
  updateFocusLayerControl()
  updateClusterOverlay()
  state.performanceSignature = ''
  state.renderer?.refresh({ skipIndexation: false, schedule: true })
  refreshPerformanceView(true)
}

function focusStatusMessage() {
  const point = state.nodesById.get(state.focusRootNode)
  const label = point?.label || state.focusRootNode
  const nodeCount = state.focusVisibleNodes.size
  const edgeCount = state.focusVisibleEdges.size
  return `Focused ${label} at layer ${state.focusLayerDepth}. Showing ${formatNumber(nodeCount)} downstream node${nodeCount === 1 ? '' : 's'} and ${formatNumber(edgeCount)} edge${edgeCount === 1 ? '' : 's'}.`
}

function focusNodeNeighborhood(nodeId, showStatus = false) {
  if (!state.graph?.hasNode(nodeId)) return
  if (state.graph.getNodeAttribute(nodeId, 'clusterNode') === true) return
  const newFocus = !state.focusRootNode
  if (newFocus) state.focusPreviousEdgesVisible = state.edgesVisible
  state.focusRootNode = nodeId
  rebuildFocusScope()
  if (newFocus && !state.edgesVisible) setEdgesVisible(true)
  refreshFocusView()
  if (showStatus) setStatus(focusStatusMessage(), 'ok')
}

function setFocusLayerDepth(layerDepth, showStatus = false) {
  state.focusLayerDepth = clamp(Math.round(Number(layerDepth) || 1), 1, 2)
  if (!state.focusRootNode) {
    updateFocusLayerControl()
    return
  }
  focusNodeNeighborhood(state.focusRootNode, showStatus)
}

function clearFocusedGraph(showStatus = false) {
  if (!state.focusRootNode) return
  const previousEdgesVisible = state.focusPreviousEdgesVisible
  state.focusRootNode = null
  state.focusVisibleNodes.clear()
  state.focusVisibleEdges.clear()
  state.focusPreviousEdgesVisible = null
  refreshFocusView()
  if (previousEdgesVisible !== null && previousEdgesVisible !== state.edgesVisible) setEdgesVisible(previousEdgesVisible)
  if (showStatus) setStatus(statusWithSkippedSummary('Graph focus cleared.'), 'ok')
}

function nodeIsHidden(nodeId) {
  return state.graph?.getNodeAttribute(nodeId, 'hidden') === true
}

function shouldHideEdge(attributes) {
  return !state.edgesVisible || nodeIsHidden(attributes.sourceNodeId) || nodeIsHidden(attributes.targetNodeId)
}

function refreshEdgeVisibility(skipIndexation = true) {
  if (!state.graph?.size) {
    state.renderer?.refresh({ skipIndexation })
    return
  }
  state.graph.updateEachEdgeAttributes(
    (_edgeId, attributes) => ({ ...attributes, hidden: shouldHideEdge(attributes) }),
    { attributes: ['hidden'] },
  )
  state.renderer?.refresh({ skipIndexation, schedule: true })
  refreshPerformanceView(true)
}

function setNodeKindVisibility(showStatus = false) {
  state.visibleNodeKinds = selectedNodeKinds()
  persistNodeKindVisibility()
  if (state.graph?.order) {
    state.graph.updateEachNodeAttributes(
      (_nodeId, attributes) => ({ ...attributes, hidden: !isNodeKindVisible(attributes.kind) }),
      { attributes: ['hidden'] },
    )
    refreshEdgeVisibility(false)
  }
  syncLoadedCounts()
  updateCounters()
  if (showStatus) {
    const count = state.visibleNodeKinds.size
    setStatus(statusWithSkippedSummary(`${formatNumber(count)} node type${count === 1 ? '' : 's'} visible.`), 'ok')
  }
}

function incrementNodeDegree(nodeId) {
  const degree = (state.nodeDegrees.get(nodeId) || 0) + 1
  state.nodeDegrees.set(nodeId, degree)
  return degree
}

function indexEdgeForNode(nodeId, edgeId) {
  let edgeIds = state.edgesByNode.get(nodeId)
  if (!edgeIds) {
    edgeIds = new Set()
    state.edgesByNode.set(nodeId, edgeIds)
  }
  edgeIds.add(edgeId)
}

function indexOutgoingEdgeForNode(nodeId, edgeId) {
  let edgeIds = state.outgoingEdgesByNode.get(nodeId)
  if (!edgeIds) {
    edgeIds = new Set()
    state.outgoingEdgesByNode.set(nodeId, edgeIds)
  }
  edgeIds.add(edgeId)
}

function assignNodeType(nodeId, edgeType) {
  const currentType = state.nodeTypes.get(nodeId)
  if (currentType && edgeStyle(currentType).priority >= edgeStyle(edgeType).priority) return
  state.nodeTypes.set(nodeId, edgeType)
}

function updateNodeStyle(nodeId) {
  if (!state.graph?.hasNode(nodeId)) return
  const degree = state.nodeDegrees.get(nodeId) || 0
  const attributes = state.graph.getNodeAttributes(nodeId)
  const nextAttributes = {
    ...attributes,
    baseColor: kindColor(attributes.kind),
    baseSize: pointSizeForDegree(degree),
    degree,
    lodTier: lodTierFor(attributes.kind, degree),
  }
  const projectionIndex = state.nodeProjectionIndex.get(nodeId)
  if (projectionIndex !== undefined) state.projectionBaseSize[projectionIndex] = nextAttributes.baseSize
  state.graph.replaceNodeAttributes(nodeId, {
    ...nextAttributes,
    ...projectedNodeAttributes(nextAttributes),
  })
}

function addPoint(point, position) {
  const graph = ensureGraph()
  if (graph.hasNode(point.id)) return false
  if (state.dataNodeCount >= MAX_CLIENT_NODES) {
    state.skippedNodes += 1
    return false
  }

  const baseSize = pointSizeForDegree(0)
  recordProjectionNode(point.id, position, baseSize)
  const baseAttributes = {
    ...position,
    nodeId: point.id,
    baseColor: kindColor(point.kind),
    baseSize,
    clusterNode: false,
    degree: 0,
    forceLabel: false,
    hidden: !isNodeKindVisible(point.kind),
    kind: point.kind,
    label: point.label,
    lodTier: lodTierFor(point.kind, 0),
    rawLabel: point.label,
    subtitle: point.subtitle,
  }
  graph.addNode(point.id, {
    ...baseAttributes,
    ...projectedNodeAttributes(baseAttributes),
  })
  state.dataNodeCount += 1
  state.clusterCounts.set(normalizeNodeKind(point.kind), (state.clusterCounts.get(normalizeNodeKind(point.kind)) || 0) + 1)
  state.loadedNodeIds.add(point.id)
  state.nodesById.set(point.id, point)
  return true
}

function syncClusterNodes() {
  const graph = ensureGraph()
  for (const kind of KIND_ORDER) {
    const count = state.clusterCounts.get(kind) || 0
    if (!count) continue
    const nodeId = clusterNodeId(kind)
    const position = cluster3dPosition(kind)
    const baseSize = clamp(10 + Math.log2(count + 1) * 3.4, 14, MAX_NODE_SIZE + 16)
    const label = `${kind === 'Node' ? 'Other' : kind}: ${formatNumber(count)}`
    recordProjectionNode(nodeId, position, baseSize)
    const attributes = {
      ...position,
      nodeId,
      baseColor: kindColor(kind),
      baseSize,
      clusterCount: count,
      clusterNode: true,
      degree: count,
      forceLabel: false,
      hidden: !isNodeKindVisible(kind),
      kind,
      label,
      lodTier: 5,
      rawLabel: label,
      subtitle: 'aggregate',
    }
    if (graph.hasNode(nodeId)) {
      graph.replaceNodeAttributes(nodeId, {
        ...attributes,
        ...projectedNodeAttributes(attributes),
      })
    } else {
      graph.addNode(nodeId, {
        ...attributes,
        ...projectedNodeAttributes(attributes),
      })
      state.clusterNodes.add(nodeId)
    }
  }
  updateClusterOverlay()
}

function addLink(link, dirtyNodes) {
  const graph = ensureGraph()
  if (state.loadedEdgeIds.has(link.id)) return false
  if (!graph.hasNode(link.source) || !graph.hasNode(link.target)) {
    state.skippedEdges += 1
    return false
  }

  const style = edgeStyle(link.type)
  const edgeAttributes = {
    color: style.color,
    edgeType: link.type,
    label: style.label,
    size: style.width,
    sourceNodeId: link.source,
    targetNodeId: link.target,
    type: 'line',
  }
  graph.addDirectedEdgeWithKey(link.id, link.source, link.target, {
    ...edgeAttributes,
    hidden: shouldHideEdge(edgeAttributes),
  })
  state.loadedEdgeIds.add(link.id)
  indexEdgeForNode(link.source, link.id)
  indexEdgeForNode(link.target, link.id)
  indexOutgoingEdgeForNode(link.source, link.id)
  assignNodeType(link.source, link.type)
  assignNodeType(link.target, link.type)
  incrementNodeDegree(link.source)
  incrementNodeDegree(link.target)
  dirtyNodes.add(link.source)
  dirtyNodes.add(link.target)
  return true
}

function appendBatchToGraph(batch) {
  const visibleNodesBefore = state.loadedNodes
  let addedNodes = 0
  let addedEdges = 0
  const dirtyNodes = new Set()

  syncProjectionTrig()

  for (let index = 0; index < batch.points.length; index += 1) {
    const point = batch.points[index]
    if (addPoint(point, pointPositionFromBatch(batch, index))) addedNodes += 1
  }
  for (const link of batch.links) {
    if (addLink(link, dirtyNodes)) addedEdges += 1
  }

  for (const nodeId of dirtyNodes) {
    updateNodeStyle(nodeId)
  }
  syncClusterNodes()
  rebuildFocusScope()

  ensureRenderer()
  tuneRendererForScale()
  refreshPerformanceView(true)
  state.renderer.refresh({ skipIndexation: false, schedule: true })
  syncLoadedCounts()
  return { addedNodes, addedEdges, addedVisibleNodes: Math.max(0, state.loadedNodes - visibleNodesBefore) }
}

function stepRotation(deltaX, deltaY, deltaZ) {
  state.rotationX += deltaX
  state.rotationY += deltaY
  state.rotationZ += deltaZ
  applyProjectionToAllNodes()
}

function rotateFrame(now) {
  if (!state.rotating) return
  if (now - state.lastRotationAt >= ROTATION_FRAME_MS) {
    state.lastRotationAt = now
    stepRotation(0.004, 0.012, 0.002)
  }
  state.rotationFrame = requestAnimationFrame(rotateFrame)
}

function startRotationWindow(showStatus = false) {
  if (!state.graph?.order) return
  if (state.graph.order > LIVE_ROTATION_NODE_LIMIT) {
    stepRotation(0.08, 0.18, 0.03)
    setStatus(statusWithSkippedSummary('Applied one 3D projection step for the large graph.'), 'ok')
    return
  }
  stopRotation()
  state.rotating = true
  state.lastRotationAt = 0
  elements.toggleSim.textContent = 'Stop'
  schedulePerformanceRefresh(true)
  if (showStatus) setStatus('Rotating the 3D projection...', 'ok')
  state.rotationFrame = requestAnimationFrame(rotateFrame)
}

async function appendBatch(batch, reset) {
  if (reset) await destroyGraph()
  const preprocessed = await preprocessBatch(batch, state.dataNodeCount)
  const normalized = appendBatchToGraph(preprocessed)
  state.hasNext = batch.next_offset !== null && state.dataNodeCount < MAX_CLIENT_NODES
  state.nextOffset = batch.next_offset ?? (batch.offset + batch.points.length)
  updateCounters()
  if (reset || normalized.addedNodes) fitGraph()
  return normalized
}

async function fetchProjectStats(projectName) {
  return api(`/api/viewer/graphs/${encodeURIComponent(projectName)}/stats`)
}

function applyProjectStats(stats) {
  elements.dbNodes.textContent = formatNumber(stats.total_nodes)
  elements.dbEdges.textContent = formatNumber(stats.total_edges)
  if (stats.max_chunk_limit) {
    elements.chunkLimit.max = String(Math.min(MAX_CHUNK_LIMIT, stats.max_chunk_limit))
  }
}

async function loadStats(projectName) {
  const stats = await fetchProjectStats(projectName)
  applyProjectStats(stats)
  return stats
}

async function projectWithGraphStats(project) {
  try {
    const stats = await fetchProjectStats(project.project_name)
    return { ...project, viewerStats: stats, viewerTotalNodes: Number(stats.total_nodes || 0) }
  } catch (error) {
    console.warn(`Skipping project without readable graph stats: ${project.project_name}`, error)
    return { ...project, viewerStats: null, viewerTotalNodes: 0 }
  }
}

async function activeProjectsWithNodes(projects) {
  const activeProjects = projects.filter((project) => project.is_active)
  const projectsWithStats = await Promise.all(activeProjects.map(projectWithGraphStats))
  return projectsWithStats
    .filter((project) => project.viewerTotalNodes > 0)
    .sort((left, right) => {
      const leftName = String(left.project_name || '')
      const rightName = String(right.project_name || '')
      return leftName.localeCompare(rightName, undefined, { sensitivity: 'base' })
    })
}

async function loadChunk(reset = false) {
  const projectName = elements.projectSelect.value
  if (!projectName) {
    setStatus('Select a project first.', 'warn')
    return
  }
  state.projectName = projectName
  const offset = reset ? 0 : state.nextOffset
  const edgeTypes = selectedEdgeTypes()
  if (!edgeTypes) {
    setStatus('Select at least one edge type.', 'warn')
    return
  }
  if (!state.visibleNodeKinds.size) {
    setStatus('Select at least one node type.', 'warn')
    return
  }
  const requestedVisibleNodes = chunkLimit()
  const targetVisibleNodes = reset ? requestedVisibleNodes : state.loadedNodes + requestedVisibleNodes
  const startingVisibleNodes = reset ? 0 : state.loadedNodes
  const search = elements.searchInput.value.trim()

  elements.loadFirst.disabled = true
  setStatus(`Loading up to ${formatNumber(requestedVisibleNodes)} visible nodes from ${projectName}...`)
  try {
    if (reset) {
      resetState()
      await loadStats(projectName)
    }
    let totalAddedEdges = 0
    let fetchCount = 0
    while (state.loadedNodes < targetVisibleNodes && fetchCount < MAX_AUTO_CHUNK_FETCHES) {
      const remainingVisibleNodes = Math.max(1, targetVisibleNodes - state.loadedNodes)
      const requestVisibleNodes = Math.min(remainingVisibleNodes, VISIBLE_NODE_BATCH_LIMIT)
      const params = new URLSearchParams({
        offset: String(fetchCount === 0 ? offset : state.nextOffset),
        limit: String(requestVisibleNodes),
        edge_types: edgeTypes,
      })
      if (search) params.set('search', search)
      const batch = await api(`/api/viewer/graphs/${encodeURIComponent(projectName)}/chunk?${params}`)
      const normalized = await appendBatch(batch, reset && fetchCount === 0)
      totalAddedEdges += normalized.addedEdges
      fetchCount += 1
      if (state.loadedNodes >= targetVisibleNodes || !state.hasNext) break
      const loadedVisibleNodes = state.loadedNodes - startingVisibleNodes
      setStatus(`Loaded ${formatNumber(loadedVisibleNodes)} of ${formatNumber(requestedVisibleNodes)} visible nodes from ${projectName} in ${formatNumber(fetchCount)} batch${fetchCount === 1 ? '' : 'es'}...`)
      await yieldToBrowserFrame()
    }
    const loadedVisibleNodes = Math.max(0, state.loadedNodes - startingVisibleNodes)
    const hitScanLimit = state.loadedNodes < targetVisibleNodes && state.hasNext
    const more = state.dataNodeCount >= MAX_CLIENT_NODES
      ? 'The 500,000-node client cap has been reached.'
      : hitScanLimit ? 'More matching nodes may exist; narrow filters and load again.'
        : state.hasNext ? 'Additional matching nodes remain; increase Display Nodes and load again.' : 'No more chunks for this filter.'
    setStatus(statusWithSkippedSummary(`Loaded ${formatNumber(loadedVisibleNodes)} visible nodes and ${formatNumber(totalAddedEdges)} edges. ${more}`), 'ok')
  } catch (error) {
    setStatus(error.message, 'error')
  } finally {
    elements.loadFirst.disabled = false
  }
}

async function loadProjects() {
  setStatus('Loading projects...')
  try {
    const selectedProject = elements.projectSelect.value
    const projects = await api('/api/auth/projects')
    const activeProjects = await activeProjectsWithNodes(projects)
    elements.projectSelect.replaceChildren(
      ...activeProjects.map((project) => {
        const option = document.createElement('option')
        option.value = project.project_name
        option.textContent = project.project_name
        return option
      }),
    )
    if (activeProjects.length) {
      const selectedStillAvailable = activeProjects.some((project) => project.project_name === selectedProject)
      elements.projectSelect.value = selectedStillAvailable ? selectedProject : activeProjects[0].project_name
      const selected = activeProjects.find((project) => project.project_name === elements.projectSelect.value)
      if (selected?.viewerStats) applyProjectStats(selected.viewerStats)
      setStatus(`Showing ${formatNumber(activeProjects.length)} project${activeProjects.length === 1 ? '' : 's'} with graph nodes.`, 'ok')
    } else {
      elements.dbNodes.textContent = '-'
      elements.dbEdges.textContent = '-'
      setStatus('No active projects with graph nodes are available.', 'warn')
    }
  } catch (error) {
    setStatus(error.message, 'error')
  }
}

function wireEvents() {
  elements.copyFalkorUrl?.addEventListener('click', async () => {
    try {
      await copyTextToClipboard(FALKOR_CONNECTION_URL)
      setStatus(`Copied ${FALKOR_CONNECTION_URL}.`, 'ok')
    } catch (error) {
      setStatus(error.message, 'error')
    }
  })
  elements.togglePanel.addEventListener('click', () => setControlPanelCollapsed(!state.controlPanelCollapsed))
  elements.refreshProjects.addEventListener('click', loadProjects)
  elements.loadFirst.addEventListener('click', () => loadChunk(true))
  elements.clearGraph.addEventListener('click', async () => {
    await destroyGraph()
    resetState()
    setStatus('Graph cleared.', 'ok')
  })
  elements.fitView.addEventListener('click', fitGraph)
  elements.toggleEdges.addEventListener('click', () => {
    if (state.focusRootNode) state.focusPreviousEdgesVisible = null
    setEdgesVisible(!state.edgesVisible, true)
  })
  elements.togglePerformance?.addEventListener('click', () => setPerformanceMode(!state.performanceMode, true))
  elements.openLayoutSettings?.addEventListener('click', openLayoutSettingsModal)
  elements.closeLayoutSettings?.addEventListener('click', closeLayoutSettingsModal)
  elements.applyLayoutSettings?.addEventListener('click', () => applyLayoutSettingsFromModal())
  elements.resetLayoutSettings?.addEventListener('click', resetLayoutSettings)
  elements.layoutSettingsModal?.addEventListener('click', (event) => {
    if (event.target === elements.layoutSettingsModal) closeLayoutSettingsModal()
  })
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !elements.layoutSettingsModal?.hidden) closeLayoutSettingsModal()
  })
  elements.focusLayerSlider?.addEventListener('input', () => setFocusLayerDepth(elements.focusLayerSlider.value, true))
  elements.clearFocus?.addEventListener('click', () => clearFocusedGraph(true))
  elements.graphRoot?.addEventListener('contextmenu', (event) => event.preventDefault())
  elements.nodeTypeInputs.forEach((input) => input.addEventListener('change', () => setNodeKindVisibility(true)))
  elements.toggleSim.addEventListener('click', () => {
    if (!state.graph) return
    if (state.rotating) stopRotation()
    else startRotationWindow(true)
  })
  elements.projectSelect.addEventListener('change', async () => {
    if (elements.projectSelect.value) await loadStats(elements.projectSelect.value)
  })
}

async function boot() {
  renderEdgeTypeControls()
  elements.chunkLimit.max = String(MAX_CHUNK_LIMIT)
  if (Number(elements.chunkLimit.value) > MAX_CHUNK_LIMIT) elements.chunkLimit.value = String(DEFAULT_CHUNK_LIMIT)
  setControlPanelCollapsed(localStorage.getItem('cg_viewer_controls_collapsed') === '1')
  restoreNodeKindVisibility()
  restoreLayoutSettings()
  const storedEdgeVisibility = localStorage.getItem(EDGE_VISIBILITY_STORAGE_KEY)
  setEdgesVisible(storedEdgeVisibility === null ? DEFAULT_EDGE_VISIBILITY : storedEdgeVisibility === '1')
  const storedPerformanceMode = localStorage.getItem(PERFORMANCE_MODE_STORAGE_KEY)
  setPerformanceMode(storedPerformanceMode === null ? DEFAULT_PERFORMANCE_MODE : storedPerformanceMode === '1')
  startFpsCounter()
  wireEvents()
  await loadProjects()
}

boot().catch((error) => setStatus(error.message, 'error'))
