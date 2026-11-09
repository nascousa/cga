# Auto Ontology Adoption for CGA

- Document version: 1.0.0
- Language: zh
- Research date: 2026-11-09
- Upstream: https://github.com/NVIDIA/auto-ontology
- Reviewed revision: `c447003f72a822301e927196a4ecb5b597c308d5`

## 结论

采用其**语义层优先、受控工具面、确定性查询与可追溯证据**设计，
而不是将完整 Auto Ontology 安装进 CGA 后端。
本次交付是基于调研的独立实现，不是调用 NVIDIA 服务、集成上游 SDK，
也不是自动生成业务本体。CGA 保留本地优先、AST 图和已有权限边界。

## 调研证据

以下链接均固定到本次核实的版本，避免把后续主分支变化当成本次依据。

| 来源 | 核实内容 | 对 CGA 的意义 |
| --- | --- | --- |
| [README](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/README.md) | 企业数据目录、业务语义层、自然语言转 SQL；FastAPI、Next.js、Postgres/pgvector；默认 NIM 模型端点 | 属于数据分析上下文层，不是代码 AST 索引替代品 |
| [MCP 文档](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/docs/mcp.md) | 先发现业务术语，检查可回答性，再提问；复用用户授权；只读工具；握手指令 | 工具应解释含义和调用顺序，而非暴露底层任意查询 |
| [工具实现](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/mcp/auto_ontology_mcp/tools.py) | 显式允许列表、面向使用者的名称和说明、契约漂移检查 | CGA 增加两个有边界的工具，不扩张为任意 Cypher 执行器 |
| [就绪检查](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/mcp/auto_ontology_mcp/readiness.py) | 编译状态、连接、目录不能互相替代；错误与权限不可混同 | CGA 的契约可用不等于已索引、最新或可回答 |
| [依赖清单](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/pyproject.toml) | Python 3.12 至 3.13、LangChain/LangGraph、多种数据库连接器和模型 SDK | 直接嵌入会显著增加依赖和运维面，本阶段无必要 |
| [许可证](https://github.com/NVIDIA/auto-ontology/blob/c447003f72a822301e927196a4ecb5b597c308d5/LICENSE) | Apache-2.0 | 允许按许可证条件采用；后续分发上游代码需保留相应声明 |

上游关于业务数据查询或 text-to-SQL 的效果不能直接作为 CGA 代码检索的效果。
本次不宣称新的 token 节省、正确率或延迟提升。

## 已落地

### 1. 可发现的代码图语义契约

新增 [ontology.py](../src/backend/graph/ontology.py)，契约版本为 `1.0.0`。

| 实体 | 精确标识 |
| --- | --- |
| Repository | path |
| File | path |
| Symbol | qualified_name |
| Variable | qualified_name |

| 关系 | 方向 | 语义边界 |
| --- | --- | --- |
| CONTAINS | Repository → File | 不是任意目录或类包含关系 |
| DEFINES | File → Symbol | 不代表额外的类到方法边 |
| IMPORTS | File → File | 已解析的本地导入，不等同外部依赖目录 |
| CALLS | Symbol → Symbol | 静态调用解析，不保证运行时执行或重载精确性 |
| USES_VARIABLE | Symbol → Variable | 作用域变量，依赖解析器支持 |
| FLOWS_TO | Variable → Variable | 轻量数据流，可包含已解析的跨函数参数/返回流；不是完整污点分析 |

契约只公开当前写入路径支持的六种关系。修正原 schema 顶部注释中
列出未写入的 `REFERENCES`、遗漏变量关系的问题；没有图迁移。

### 2. 确定性、受限的一跳查询

在 [MCP server](../src/backend/tools/server.py) 增加：

- `get_graph_ontology()`：返回实体、标识、关系、限制和工作顺序。
- `query_graph_relation(relation, identity, direction, limit)`：
  只接受六种关系，精确匹配标识，支持入向和出向，限制 1 至 100 条。

只有契约拥有的标签、属性和关系名称进入查询结构；标识和数量参数化。
额外取一条判断 `has_more`，不把恰好达到 limit 误报为一定被截断。
返回规范方向的 source/target、索引文件/行号、可用的数据流边属性，
以及图名、契约版本、实际 Cypher 和参数。

空结果使用 `no_matches`，明确不代表关系不存在；图连接或执行失败继续抛错，
不降级成空的成功结果。没有生成或执行 SQL，也没有任意 Cypher 输入。

### 3. 复用项目权限、分支和缓存

- 使用已有项目/ref 认证上下文及图路由。
- 使用既有按项目、图 generation 隔离的读取缓存和 trace 记录。
- 不增加共享服务账号，不绕过已有通信配置或授权。
- [Relay 桥接](../src/backend/cga_relay/router.py) 与
  [Rust Relay](../src/cga-relay/src/main.rs) 同时支持两个工具；
  新 schema 包含允许关系、方向和数量范围。
- 后端 MCP 握手增加语义工作顺序；Relay 工具说明也解释使用顺序和限制。
- 原有索引、检索、管理界面和图数据库结构保持不变。

### 4. 使用方法

1. 调用 `get_graph_ontology` 理解当前语义。
2. 用 `find_symbol` 或已有检索结果取得精确标识。
3. 例如用 `CALLS`、该符号标识和 `inbound` 查询调用者。
4. 检查来源、范围与 `has_more`；必要时读源码并运行测试。

详细参数见 [MCP quickstart](mcp-agent-query-quickstart.md#typed-semantic-graph-queries)。
默认最小结果不能替代完整变更影响分析或实时源码校验。

## 验证

本地使用现有虚拟环境和已运行的 FalkorDB；测试仅创建并清理随机命名的
测试图，不修改已有项目图。

- Python 定向回归：覆盖契约、所有关系双向查询、非法参数、注入字符串、
  结果边界、MCP 发现与调用、未授权拒绝、分支路由、原有 MCP 和 Relay。
- 真实图：通过原有 `IndexPipeline` 索引两个 Python 文件，验证六种关系
  的入向/出向结果及来源；同时运行原有 MCP 端到端用例。
- Rust：验证工具发现 schema 与认证转发，并运行相关 MCP 回归。
- 静态检查：新模块 mypy；修改 Python 文件 Ruff。

最终结果：

| 验证项 | 结果 |
| --- | --- |
| Python 定向回归（下列 pytest 命令） | 107 passed，含真实图测试，无跳过 |
| Rust `mcp_` 定向回归 | 15 passed，含两个新工具的实际认证转发 |
| Rust `ontology_tools_are_discoverable_with_typed_arguments` | 1 passed |
| mypy 契约编译器 | passed |
| Ruff（保留已确认的既有 E402） | passed |
| `git diff --check` | passed |

初次真实图连接默认 `16379` 端口被关闭，改用现有本地 `26381` 后通过。
Cargo 不在 PATH，使用已安装的 `%USERPROFILE%\.cargo\bin\cargo.exe`，
没有安装额外依赖。原有 `server.py` 存在 `E402`（ADC 延后导入），
已与 HEAD 核实；不顺手修改无关的初始化顺序。

可重复命令（PowerShell，仓库根目录）：

```powershell
$env:FALKORDB_PORT = "26381"
.\.venv\Scripts\python.exe -m pytest src\tests\test_graph_ontology.py src\tests\test_mcp_tools.py src\tests\test_mcp_e2e.py src\tests\test_cga_relay_router.py --no-cov -ra
.\.venv\Scripts\python.exe -m mypy --follow-imports=skip src\backend\graph\ontology.py
.\.venv\Scripts\python.exe -m ruff check --ignore E402 src\backend\graph\ontology.py src\backend\tools\server.py src\backend\cga_relay\router.py src\tests\test_graph_ontology.py src\tests\test_cga_relay_router.py
& "$env:USERPROFILE\.cargo\bin\cargo.exe" test --locked --manifest-path src\cga-relay\Cargo.toml mcp_
& "$env:USERPROFILE\.cargo\bin\cargo.exe" test --locked --manifest-path src\cga-relay\Cargo.toml ontology_tools_are_discoverable_with_typed_arguments
```

## 未采纳与后续条件

- **未安装或部署 Auto Ontology 服务**：无 NIM 调用、API key 或源码外传。
- **未导入上游代码或模型权重**：本次为原创实现加来源引用，无新增第三方运行依赖。
  若后续复制/分发上游组件，应更新第三方 notices 并核对其依赖许可证。
- **未引入自动业务术语生成、术语认证、纠错写回或自然语言编译器**：
  这些会改变持久化和治理模型，需要独立设计、审批和质量评测。
- **未实现新的综合 readiness 判定**：明确区分契约、索引存在性与新鲜度；
  仍需使用现有索引状态、实际查询和来源核验。
- **未发布部署、未提交或创建 PR**：当前工作树还有其他任务的修改，
  本次仅完成本地代码和验证，不混合发布这些更改。
- 若未来 CGA 需要同时回答业务数据问题，优先把单独部署的 Auto Ontology
  作为同一 agent harness 的并列 MCP 服务，而非把数据库连接器嵌入 CGA。
  上线前需核实授权、数据出站策略、通信安全要求、部署就绪和版本固定。
- 后续效果评估应使用 CGA 的真实代码任务，对比调用误选率、证据覆盖、
  token、延迟和最终任务正确率；本次测试只证明行为契约和兼容性。
