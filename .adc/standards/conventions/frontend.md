# Frontend Application Policy

## Default Web App Experience
- **Dark Mode Default**: All web application projects MUST default to dark mode unless the product owner explicitly approves another theme. Light mode may exist as an option, but the first-run experience should be dark.
- **Admin UI Baseline**: Dashboard/admin surfaces SHOULD use the layout density, navigation rhythm, and component proportions of `https://admin-demo.vuestic.dev` as the default visual reference.
- **Login Background Default**: Login pages SHOULD use a Vanta.js net-style background with white dots/lines at 15% opacity. If Vanta.js cannot be loaded safely, provide a static CSS fallback that preserves the same white net-on-dark visual intent.
- **Accessible Contrast**: Dark mode colors MUST meet WCAG AA contrast for text and controls. Do not rely on opacity-only text for primary labels or actionable controls.

## Browser Debugging Policy
- **Built-In Browser First**: For all projects that design or modify web pages, agents MUST use the built-in browser shared page as the default debugging and self-validation surface.
- **Self-Debug Requirement**: Before concluding frontend work, agents SHOULD load the changed page in the built-in browser shared page, inspect visible layout/state, and capture console or network errors when available.
- **BrowserAgent Exception**: Use the BrowserAgent (BA) project plus browser extension only for special cases that require extension APIs, browser-permission flows, cross-browser behavior, or automation unavailable in the built-in browser shared page.
- **Evidence Discipline**: Frontend validation notes SHOULD state which page was opened, what viewport/state was checked, and whether console/runtime errors were observed.

## Default Frontend Integration
- **FastAPI Pairing**: New web apps SHOULD assume a FastAPI backend unless the target platform explicitly requires another API framework.
- **pgvector-Aware UX**: Search, recommendation, semantic retrieval, or AI-assist UI flows SHOULD be designed with PostgreSQL `pgvector` as the default vector persistence layer.
