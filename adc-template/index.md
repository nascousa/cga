---
project-name: "Agentic Boilerplate Project"
version: "1.0.0"
description: "A reference implementation of the Agent Development Constitution (ADC)."
tech-stack:
  - React 18
  - Node.js 20
  - PostgreSQL
architecture-style: "Microservices"
entry-points:
  - src/main.ts
---

# Project Overview
This project serves as a reference implementation of the ADC standard. 
It defines the exact rules, constraints, and architecture that AI Agents must follow.

## Core Modules
- `src/api`: Core backend services
- `src/web`: Frontend React application
- `src/cga-relay`: CGA-Relay (local MCP gateway and project sync)

## Required Integrations
This project **MUST** integrate with the ContextGraph ecosystem:
- **ContextGraph Project**: Central repository and artifact management system (registration via CGA Admin UI at `http://localhost:18001/admin`)
- **CGA-Relay** (`src/cga-relay`): Local MCP gateway and project sync agent
- **CGA MCP Server**: Model Context Protocol endpoint for programmatic ContextGraph access (default local dev SSE endpoint: `http://localhost:18001/mcp/sse`)
- **CGA MCP Server Profile Standard**: `.adc/cga-relay/mcp/mcp-servers.json` MUST include a `cga-mcp-server` entry so ADC-based projects can auto-load CGA MCP wiring.
- **Initial ContextGraph Indexing**: Immediately after CGA-Relay + CGA MCP Server integration, run full-project indexing once so retrieval/review tools can access the entire repository context.

ContextGraph MCP access is endpoint-first by default. A local MCP implementation is optional and repository-specific.

## Research Report Formats

Keep the existing report format and the evidence-driven ECharts interactive
format as parallel choices. All reports require dark-mode support, and charts
must follow the active theme. See
[Research Report Formats](standards/conventions/research-reports.md)
for layout, evidence, offline packaging, preservation and validation requirements.

## Environment Requirements
Refer to `bootstrap.md` for exact start-up commands.


