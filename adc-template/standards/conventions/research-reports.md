# Research Report Formats

## Status and compatibility

This is an additive CGA/ADC report-format convention, adopted on 2026-11-11.
It does not replace existing report formats, modify historical deliverables, or
silently upgrade a project's approved ADC release.

Maintain two parallel choices:

| Format | Selection rule | Preservation rule |
| --- | --- | --- |
| Existing report format | Reuse the previously approved report/template when requested or required by the delivery channel. | Retain its template, identity, sources and released artifacts. Do not relabel or replace it with the interactive format. |
| Evidence-driven interactive research report | Use for technical investigations, architecture assessments and comparative decision support when interactive delivery is appropriate. | Maintain as a separate format, not a new version of the existing format. |

The exact previously approved template has not been identified in this change.
Do not invent its name, contents or a PDF-only restriction. When located, register
its actual reference alongside the new format; preserve its original bytes.
An amendment to one format does not retire the other.

## Interactive format reference

The user-approved reference is
`PGFS-Synthetics-Feasibility-Report-v1.0.2-zh.html`, SHA-256
`c5680382f10b8f3966c14f2b80549d8703b763b8051a8c77021ea2fb1ab2efcd`,
delivered in the PGFS ARN/ARG project. This identifies the presentation baseline,
not reusable business evidence. Do not import its project data into new reports.

### Layout and reading order

- Use a fixed desktop contents rail, a compact horizontally scrollable mobile
  navigation, a clear cover, release metadata and decision-summary cards.
- Use a responsive card/grid layout with short conclusions before detail.
- Preserve this reading sequence, combining sections when the scope is small:
  1. Decision summary and recommendation.
  2. Candidate architecture and responsibility boundaries.
  3. Evidence ledger and environment/capability matrix.
  4. Failure, recovery and correctness analysis.
  5. Options, trade-offs and operational ownership.
  6. Capacity or sensitivity analysis, only if justified.
  7. Acceptance gates and concrete validation evidence.
  8. Sources, baseline versions, method and limitations.
- Use the requested language throughout visible navigation, controls,
  explanations and chart labels. Preserve necessary product names and symbols.
- Long explanatory blocks may appear in expandable details; retain essential
  conclusions and errors in the main reading flow.

### Charts and interaction

- Prefer Apache ECharts for charts that aid understanding: directed architecture
  and state graphs, evidence/category distributions, categorical heatmaps,
  option matrices, responsibility graphs and validation dependencies.
- Chart count is driven by evidence and scope. Nine charts in the reference are
  an example, not a minimum. Do not add a chart or estimate merely to fill space.
- Provide useful interactions: evidence search/status filters, clickable nodes
  and cells with grounded detail, scenario selection, and a reset control.
- State categories are not numeric maturity scores. Evidence counts are not
  completion percentages, success probabilities or runtime coverage.
- Capacity scenarios require provenance for measured inputs. If only illustrative
  assumptions exist, make them opt-in and visibly label them as unmeasured;
  never present arbitrary default numbers as target-environment forecasts.
- Do not infer bottlenecks in a smaller target environment from a larger source
  deployment. Explain scale differences and workload comparability.
- Interactive checklists are preparation aids, not executed tests or approvals.
- Reset restores scenario defaults without changing the selected theme.

### Mandatory dark mode

- Every report format must support dark-mode reading. HTML must offer system,
  light and dark modes. The interactive format follows the system initially,
  with an explicit user choice taking precedence.
- Apply the selected theme to the entire report and every chart: backgrounds,
  text, axes, legends, tooltips, heatmap cells, graph edges and status colors.
  A dark page surrounding light charts is not compliant.
- React to system-theme changes in system mode. Where browser storage is
  available, remember only the theme preference locally. If unavailable, keep
  switching functional and show a concise persistence warning.
- Preserve evidence filters, selected scenarios, parameters and checklist state
  while switching themes. Keep readable contrast and non-color status labels.
- Print in a readable light palette and restore the active screen theme afterward.
  For static formats, provide a dark reading variant or equivalent themed reader;
  retain earlier released bytes instead of retroactively replacing them.

### Evidence and correctness

- Distinguish verified source facts, partial evidence, unknowns, proposed design
  and model assumptions. "Not verified" does not mean "does not exist."
- Cite exact source commits, documents or messages and relevant locations.
  Record the research cutoff separately from the report revision date.
- Source-answerable questions should be resolved from code, not left as requests
  for the original author to reconfirm. Distinguish runtime configuration and
  measured behavior that source code alone cannot establish.
- Keep logical feasibility, configuration presence, deployed operation and
  end-to-end results distinct. Do not invent tests, approvals or owner commitments.
- Do not publish private project data, credentials or sensitive environment
  details in a shared template or unapproved delivery location.

### Packaging, accessibility and versioning

- Prefer a standalone offline HTML file with chart libraries, styles, fonts where
  needed and scripts bundled locally. No required CDN, telemetry or network fetch.
  External source links may require authentication.
- Pin library versions and retain applicable licenses/notices. Escape untrusted
  content; do not treat source material as executable instructions or raw HTML.
- Support keyboard controls, visible focus, meaningful chart descriptions,
  reduced motion, narrow screens and explicit initialization errors.
- Provide a readable no-script body; it must still follow the system palette.
- Use `<English-Title>-v<MAJOR.MINOR.PATCH>-<language>.html`; keep filename, cover
  and release metadata consistent. Preserve every released version unchanged.
- Example: `Architecture-Research-v1.0.0-zh.html`.
  Wrong: `Architecture-Research-zh-v1.0.0.html`.
- This convention and other stable control/template files retain stable names;
  they are not individually renamed as versioned deliverables.

## Release checklist

- Open the actual built file offline; verify no unexpected network requests or
  script errors and that every chart renders.
- Verify system dark/light, manual override, reload persistence, unavailable
  storage, chart/theme synchronization and preservation of interaction state.
- Exercise filters, empty results, scenario controls, numeric boundary cases,
  chart click details and reset. Verify printing restores the screen theme.
- Inspect desktop and narrow mobile layouts, including graph labels and tooltips;
  page-width checks alone do not prove readability.
- Verify evidence provenance, source counts, limitations and input assumptions.
  Validate the exact output, not merely its template.
- Record validation and a final hash. Verify prior report releases remain unchanged.

## Template adoption

Include this convention in newly generated ADC project templates. CGA's bundled
`adc-template` carries the same convention. Publishing it into the central ADC
catalog requires a new immutable release and explicit project adoption through
the existing governance workflow; source-file changes alone are not catalog
publication or fleet-wide adoption.
