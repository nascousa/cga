# Auto Ontology Performance Evaluation

- Document version: 1.0.0
- Language: en
- Run date: 2026-11-09, UTC-08:00
- Deployment: `cga-local:1.30.128-ontology-20261109`
- Image ID: `sha256:5350ea378a0d49dad6ee342cfe6fbc2e398d5f87493e61b79fa86ec2e1187ad7`
- Contract: `1.0.0`
- Scope: Deployed semantic contract and one-hop queries, not full indexing or LLM answer evaluation.

## Conclusions

1. **Functional consistency passed**: 97 fixed cases across three real projects
   matched raw graph evidence for all six relations in both directions. Actual
   Redis hits and zero graph-query counts verified 1,940 cache-hit measurements.
2. **Queries themselves are lightweight**: median uncached latency was 0.986 ms,
   compared with 0.575 ms for cache hits, a 41.7% median reduction. "Uncached"
   means Redis was disabled, not a disk-cold startup.
3. **The new tool is not faster or smaller than every legacy tool**: across 29
   comparable cases, it was 9.9% slower on average, about 0.091 ms more, with
   approximately 8.91 times the average JSON size. Output fields differ. This is
   the cost of the semantic contract, locations, and provenance, not a comparison
   of equivalent-format compression.
4. **Four concurrent requests gave a better balance in this sample**:
   authenticated HTTP achieved about 202 requests/second with p95 of 28.0 ms.
   At eight concurrent requests, throughput was about 201 requests/second and
   p95 rose to 81.1 ms, with no observed throughput gain.
5. **The real Relay path works**: a single stdio session using the installed
   hardened candidate achieved p50 of 13.5 ms, p95 of 16.8 ms, and approximately
   71.3 requests/second, excluding process startup.

This was a bounded evaluation in the deployed environment, not capacity
certification, a long-term stability test, or an SLO. No product code, concurrency
defaults, or deployment configuration was changed.

## Environment and Safeguards

- Used Python, FalkorDB, and Redis from the deployed containers. The host accessed
  the existing project-authenticated bridge over IPv4 loopback. No accounts were
  fabricated, shared credentials created, or authorization disabled.
- Docker exposed 32 logical CPUs and approximately 62.76 GiB of memory. The API
  had no separate CPU/memory hard limits. Other host and Docker services were
  running, so this was not a dedicated load-test environment.
- Business graphs were read-only: no reindexing, business-node creation, or shared
  cache flushes. Backend cache tests used a unique
  `cg:perf:ontology:<uuid>:` namespace and deleted only their own keys afterward.
- Actual HTTP calls still generated normal audit/runtime records and used the
  normal query cache. "Read-only" refers to business graphs, not zero writes
  anywhere in the system.
- Relay stdio measurements respected the machine-wide single-instance lock:
  temporarily exit the tray, run one MCP process, then restore the tray and
  verify HTTP 200 from its local status endpoint.
- Before/after checks covered all **24 projects and 21 graphs**. Project records
  and graph node/edge counts were unchanged, and the API was healthy afterward.
  This does not constitute a complete snapshot comparison of every node property.

## Fixed Workload

The primary project came from the existing Relay configuration. The other two
were active projects with existing graphs, selected from the database in ID
order. This was neither random nor business-frequency-weighted sampling. Project
names, identities, and exact paths remain only in local raw evidence; this report
uses aliases.

| Project | Nodes | Edges | Cases | Relation coverage |
| --- | ---: | ---: | ---: | --- |
| P1, current Relay project | 41,158 | 86,972 | 29 | Five; no File -> IMPORTS -> File edges were obtained in this sample |
| P2 | 5,718 | 11,412 | 34 | All six |
| P3 | 256,905 | 440,426 | 34 | All six |
| Total | 303,781 | 538,810 | 97 | All six, inbound/outbound |

For each project, relation, and direction, at most three actual anchors were
selected in exact-identity order, with `limit=20`. Every case stores its exact
input, expected edges, and provenance fields. Fixed-workload SHA-256:

`e577f51109dc711fd0fcf919d155f806fee9b734fc907f85271bbb8757372a21`

Backend measurements used three warmups and 20 measurements per case. Raw-query
and new/legacy handler execution order was interleaved with a fixed seed to reduce
ordering bias. Percentiles use nearest-rank, not averages of per-run percentiles.
HTTP and Relay measured only P1, which had existing authorization, using 29 inputs.
Their interface load tests therefore do not cover `IMPORTS`; backend tests cover
that relation in P2/P3.

P1's missing import-edge sample is a **coverage limitation**, not a proven parser
defect or data that the ontology layer can automatically supply. Business graphs
were not reindexed to improve the numbers.

## Backend Results

All times are in milliseconds. This layer calls handlers in a separate Python
process inside the deployed container and **excludes authentication, HTTP, result
JSON serialization, application tracing, and auditing**. The raw Cypher baseline
uses exactly the same query and parameters as the new tool.

| Path | Measurements | p50 | p95 | p99 | Mean |
| --- | ---: | ---: | ---: | ---: | ---: |
| Same Cypher, direct graph read | 1,940 | 0.914 | 1.463 | 5.498 | 1.044 |
| New tool, Redis disabled | 1,940 | 0.986 | 1.572 | 5.695 | 1.114 |
| New tool, verified Redis hit | 1,940 | 0.575 | 0.855 | 1.496 | 0.628 |

The new tool adds approximately **0.070 ms** mean latency over the same Cypher.
Cache hits reduce mean latency by **43.6%** relative to the uncached new tool.
Cache validation checks both increased hit counts and the absence of graph
queries, rather than merely observing faster execution.

### p95 by Relation

| Relation | Cases | Uncached p95 | Cached p95 |
| --- | ---: | ---: | ---: |
| CONTAINS | 13 | 5.820 | 0.846 |
| DEFINES | 18 | 1.506 | 0.905 |
| IMPORTS | 12 | 1.403 | 0.825 |
| CALLS | 18 | 1.352 | 0.911 |
| USES_VARIABLE | 18 | 1.495 | 0.864 |
| FLOWS_TO | 18 | 1.264 | 0.774 |

Uncached `CONTAINS` tail latency is relatively high and warrants retesting across
more repository sizes and fan-outs. These data cannot identify a specific index
or algorithm as the cause; no speculative query changes were made.

### Strictly Paired Legacy-Tool Comparison

Only untruncated `CALLS`/`IMPORTS` cases with exactly matching adjacent identities
were included. Legacy tools were `find_callers`, `find_callees`,
`get_file_imports`, and `get_file_dependents`: 29 cases and 580 measurements per
side. Other relations have no semantically equivalent legacy tool and were not
forced into this comparison.

| Metric | Legacy tools | New tool |
| --- | ---: | ---: |
| p50 | 0.870 ms | 0.957 ms |
| p95 | 1.199 ms | 1.365 ms |
| Mean | 0.916 ms | 1.007 ms |
| Mean compact JSON size | 300.9 bytes | 2,680 bytes |

Consistency concerns **relation identity**, not identical response structures.
For example, the legacy import tools' `language` field is absent from the new
tool. Byte counts for different field sets do not establish retrieval quality
or token efficiency.

## Deployed Authenticated HTTP Results

A short pilot completed 720 requests with zero failures. The expanded run then
used three repetitions of 600 requests per concurrency level: **5,400 measured
requests with zero failures**. Each level explicitly warmed its connection pool
at that concurrency before measurement; all 29 query inputs were also warm.
Every response was checked against complete edge evidence and `has_more`.

The table pools all 1,800 raw latencies per level. Throughput is successful
requests divided by the sum of measurement wall times across repetitions,
**not the peak of the best repetition**. The client used bounded closed-loop
concurrency, excluded queueing after task creation, and used no retries to hide
errors.

| Concurrency | Requests | p50 | p95 | p99 | Pooled requests/second | Per-run requests/second range |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 1,800 | 10.091 ms | 13.157 ms | 40.150 ms | 87.8 | 73.2-99.6 |
| 4 | 1,800 | 18.341 ms | 28.011 ms | 41.364 ms | 202.3 | 193.1-219.3 |
| 8 | 1,800 | 30.226 ms | 81.052 ms | 132.977 ms | 200.6 | 194.1-206.9 |

For the same input mix, mean decoded HTTP response-body size was approximately
3,134 bytes. This counts only the body, excluding HTTP headers, TLS, and TCP/IP
overhead; it is not compressed wire size. Total query measurement time across
the repetitions was approximately 38.38 seconds, too short to establish sustained
capacity. The single-concurrency slowdown in the third repetition also shows
environmental variation; quoting only the best number would be misleading.

## Actual Relay Stdio Path

Measurements used the installed hardened internal Windows candidate, not a debug
test substitute. One persistent MCP session completed initialization, tool
discovery, and input warmup before sequential measurement:

| Metric | Value |
| --- | ---: |
| Measured graph queries | 180 |
| Failures | 0 |
| p50 / p95 / p99 | 13.499 / 16.758 / 18.339 ms |
| Actual sequential throughput | 71.3 requests/second |
| Mean stdio response size | 6,674 bytes |

This layer includes stdio, Relay JSON, actual authenticated HTTP, backend
execution, and response encoding, but excludes process startup. A single Relay
currently processes messages in input order. Multiple Relay processes were not
started to bypass the lock and manufacture concurrency numbers.

Static-contract comparison for `get_graph_ontology`:

| Layer | Requests | p50 | p95 | Response size |
| --- | ---: | ---: | ---: | --- |
| Backend handler | 200 | 0.010 ms | 0.011 ms | 2,154 bytes, compact JSON |
| Authenticated HTTP | 50 | 9.530 ms | 12.009 ms | 2,265 bytes, decoded body |
| Installed Relay stdio | 30 | 11.392 ms | 13.410 ms | 4,823 bytes, including newline |

The same static contract is approximately 2.13 times larger through Relay than
HTTP. The existing response includes both text content and `data`, duplicating
encoding. Subtracting individual timings across layers does not establish a
causal breakdown, but communication/authentication and duplicate output are
worth profiling first.

## Recommendations and Non-Goals

1. **Usage**: discover the contract once per session rather than before every
   relation query. Preserve cache isolation by project/ref/generation.
2. **Concurrency starting point**: start future tuning at four concurrent HTTP
   requests. This is neither a formal capacity limit nor an automatic change
   of production concurrency to four.
3. **Reduce output redundancy first**: evaluate an optional compact mode, stable
   contract references, and compatible structured responses. Preserve existing
   default provenance; do not simply remove fields or break existing clients.
4. **Profile specific stages next**: measure authentication, auditing,
   serialization, and communication separately before changing synchronous calls
   or the threading model. These data do not establish any one as the sole
   bottleneck.
5. **Extend quality and scale evaluation**: use real task frequencies, more
   large/high-fan-out graphs, cache invalidation, new-process startup, and sustained
   loads. Investigate P1 import coverage separately.

This evaluation called no external model, sent no source code, added no
dependencies, and measured neither full nor incremental indexing. It did not
measure final LLM answer correctness and makes no new claim of token savings or
improved answer success.

## Raw Evidence and Reproduction

Full raw samples, exact anchors, and execution scripts remain in git-ignored
`tmp\benchmarks\ontology-performance-20261109`. Project identities and source
paths are not committed:

- [Summary metrics](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-Summary-v1.0.0-en.json)
- [Backend and fixed cases](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-Backend-v1.0.0-en.json)
- [Expanded HTTP samples](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-HTTP-Steady-v1.0.0-en.json)
- [Relay samples](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-Relay-v1.0.0-en.json)

SHA-256 hashes of the three main input files:

| File | SHA-256 |
| --- | --- |
| Backend | `99200efefb3568ee274783f3de4933b2a8dbfa9aebcfed6c609ce9c488028815` |
| HTTP Steady | `8999631a5aa9af47c1c86481e12b67efedb21e6823d05a19cca2135303699fbc` |
| Relay | `4b219b5b64c40b4810ac40b6da54da0282f0a4b04853dc75f2bb36aa44740569` |

Command shapes follow. Reruns must use a new output directory and must not
overwrite this fixed evidence. Read the actual project ID from the existing local
Relay configuration; do not put credentials in command arguments or reports.
If the backend workload is sampled again, compare its SHA rather than assuming
it is identical to this run.

```powershell
docker cp <local-script-directory>\backend_benchmark.py cga-desktop-api:/tmp/cga-ontology-benchmark.py
docker exec cga-desktop-api python /tmp/cga-ontology-benchmark.py --primary-id <existing-project-id> --projects 3 --anchors 3 --iterations 20 --warmup 3 --output /tmp/backend-new-run.json
docker cp cga-desktop-api:/tmp/backend-new-run.json <new-output-directory>\backend.json
.\.venv\Scripts\python.exe <local-script-directory>\http_benchmark.py --backend <new-output-directory>\backend.json --output <new-output-directory>\http.json --requests 600 --repetitions 3 --pool-warmup 2
.\.venv\Scripts\python.exe <local-script-directory>\relay_benchmark.py --backend <new-output-directory>\backend.json --output <new-output-directory>\relay.json --requests 180
```

The last command requires exiting the existing tray first and restoring it after
success or failure; do not bypass the lock. Newly published reports and JSON
files must use a consistent new version under the document-naming convention.
