# Autonomous PR Checklist
*AI Agents MUST read and verify every item below before generating a Git commit or PR.*

- [ ] Are all unit tests and E2E tests passing?
- [ ] Did I verify the CVSS score of all new dependencies introduced?
- [ ] Did I verify that all added or changed communication paths use PQC/CNSA 2.0-compliant ML-KEM/ML-DSA or approved CRYSTALS/PQC successor algorithms?
- [ ] Did I verify that ContextGraph indexing and project change information aggregation completed through `cga-relay`, and that no direct API or `cga-mcp-server` fallback was counted as official success?
- [ ] Did I auto-update the Mermaid diagrams in `.adc/knowledge/diagrams/` to match my architectural modifications?
- [ ] Are Docker CPU/Memory resource limits properly set as environment variables?