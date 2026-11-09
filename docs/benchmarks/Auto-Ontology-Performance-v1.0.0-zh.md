# Auto Ontology Performance Evaluation

- Document version: 1.0.0
- Language: zh
- Run date: 2026-11-09, UTC-08:00
- Deployment: `cga-local:1.30.128-ontology-20261109`
- Image ID: `sha256:5350ea378a0d49dad6ee342cfe6fbc2e398d5f87493e61b79fa86ec2e1187ad7`
- Contract: `1.0.0`
- Scope: 已部署的语义契约与一跳查询，不是全量索引或 LLM 答案评测。

## 结论

1. **功能一致性通过**：3 个真实项目、97 个固定用例，六种关系双向查询与
   原始图证据一致；1,940 次缓存命中经实际 Redis 命中和零图查询计数确认。
2. **查询本身较轻**：无缓存中位数 0.986 ms，缓存命中 0.575 ms，
   中位延迟降低 41.7%。这里的“无缓存”仅指禁用 Redis，不是磁盘冷启动。
3. **并非比所有旧工具更快或更省输出**：29 个可比用例中，新工具平均耗时
   比旧工具高 9.9%（增加约 0.091 ms），平均 JSON 体积约 8.91 倍。
   新旧输出字段不同；这是语义契约、位置与来源信息的成本，不是等价格式压缩。
4. **本次样本以 4 并发更均衡**：认证 HTTP 实测约 202 请求/秒，p95 为
   28.0 ms。8 并发约 201 请求/秒，p95 升到 81.1 ms，没有观察到吞吐收益。
5. **真实 Relay 可用**：已安装加固候选的单一 stdio 会话，p50 为 13.5 ms、
   p95 为 16.8 ms，约 71.3 请求/秒；不含进程启动。

这是一轮有边界的现场性能评估，不是容量认证、长期稳定性测试或 SLO。
未改动产品代码、并发默认值或部署配置。

## 环境与保护措施

- 使用已部署容器的 Python、FalkorDB 与 Redis，宿主机从 IPv4 loopback
  访问现有项目认证桥接接口；没有伪造账号、创建共享凭据或关闭授权。
- Docker 可见 32 个逻辑 CPU、约 62.76 GiB 内存；API 未设置单独 CPU/内存
  硬限额。宿主机和 Docker 同时运行其他服务，不是专用压测环境。
- 业务图只读：不重建索引、不创建业务节点、不清空共享缓存。
  后端缓存测试使用唯一 `cg:perf:ontology:<uuid>:` 命名空间，结束后只删自己的键。
- 实际 HTTP 调用仍产生系统正常的审计/运行记录，并使用正常查询缓存。
  因此“只读”指业务图，而不是系统完全零写入。
- Relay stdio 测量遵守机器级单实例锁：暂时退出原托盘，只运行一个 MCP
  进程，结束后恢复托盘并确认本地状态接口 HTTP 200。
- 前后核对全部 **24 个项目、21 个图**，项目记录与图节点/边数量保持不变；
  API 最终健康。该核对不等于对每个节点属性做完整快照比对。

## 固定工作负载

主项目取现有 Relay 配置；另外两个项目取数据库中按 ID 排序、实际已有图的
活动项目。不是随机或按业务频率抽样。项目名、标识和精确路径只保留在本地
原始证据中，本文使用别名。

| 项目 | 节点 | 边 | 用例 | 关系覆盖 |
| --- | ---: | ---: | ---: | --- |
| P1，当前 Relay 项目 | 41,158 | 86,972 | 29 | 五种；本次未取到 File → IMPORTS → File 边 |
| P2 | 5,718 | 11,412 | 34 | 六种 |
| P3 | 256,905 | 440,426 | 34 | 六种 |
| 合计 | 303,781 | 538,810 | 97 | 六种、入向/出向 |

每个项目、关系、方向最多取三个按精确标识排序的实际锚点，`limit=20`。
所有用例保存精确输入、期望边及来源字段。固定工作负载 SHA-256：

`e577f51109dc711fd0fcf919d155f806fee9b734fc907f85271bbb8757372a21`

后端每用例预热 3 次、测量 20 次；原始查询和新/旧 handler 的执行顺序按
固定种子交错，减轻顺序偏差。百分位使用 nearest-rank，不平均各轮百分位。
HTTP/Relay 只测有现成授权的 P1，共 29 个输入，因此不能宣称其接口负载
测试覆盖了 `IMPORTS`；该关系在 P2/P3 的后端测试中覆盖。

P1 的导入边缺口是本次发现的**覆盖限制**，不是已证明的解析器缺陷，
也不是本体层能自动补齐的数据。没有为改善数字而重新索引业务图。

## 后端结果

时间单位为 ms。此层在部署容器的独立 Python 进程中调用 handler，
**不含认证、HTTP、结果 JSON 序列化、应用 trace 和审计开销**。
原始 Cypher 基线使用与新工具完全相同的查询及参数。

| 路径 | 测量数 | p50 | p95 | p99 | 均值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 相同 Cypher，直接读图 | 1,940 | 0.914 | 1.463 | 5.498 | 1.044 |
| 新工具，禁用 Redis | 1,940 | 0.986 | 1.572 | 5.695 | 1.114 |
| 新工具，确认 Redis 命中 | 1,940 | 0.575 | 0.855 | 1.496 | 0.628 |

新工具相对同一 Cypher 的平均附加耗时约 **0.070 ms**；
缓存命中相对新工具无缓存平均延迟降低 **43.6%**。
缓存验证不仅观察“更快”，还断言命中计数增长且没有执行图查询。

### 六种关系的 p95

| 关系 | 用例数 | 无缓存 p95 | 缓存 p95 |
| --- | ---: | ---: | ---: |
| CONTAINS | 13 | 5.820 | 0.846 |
| DEFINES | 18 | 1.506 | 0.905 |
| IMPORTS | 12 | 1.403 | 0.825 |
| CALLS | 18 | 1.352 | 0.911 |
| USES_VARIABLE | 18 | 1.495 | 0.864 |
| FLOWS_TO | 18 | 1.264 | 0.774 |

`CONTAINS` 的无缓存尾延迟相对更高，值得在更多仓库大小和扇出下复测。
当前数据不足以归因到具体索引或算法，不在本轮推测性修改查询。

### 与原有工具的严格配对比较

仅纳入结果没有截断、邻接标识完全一致的 `CALLS`/`IMPORTS` 用例：
旧工具为 `find_callers`、`find_callees`、`get_file_imports`、
`get_file_dependents`，共 29 个用例、每侧 580 次测量。
其余关系没有同语义旧工具，不强行比较。

| 项目 | 旧工具 | 新工具 |
| --- | ---: | ---: |
| p50 | 0.870 ms | 0.957 ms |
| p95 | 1.199 ms | 1.365 ms |
| 均值 | 0.916 ms | 1.007 ms |
| 平均紧凑 JSON 体积 | 300.9 bytes | 2,680 bytes |

一致性指**关系身份**；新旧返回结构不是同构，旧导入工具的 `language`
字段也不在新工具中。不能把不同字段集的字节数当成检索质量或 token 优劣。

## 已部署认证 HTTP 结果

先完成短轮试测（720 次、零失败），再扩大为每并发档 3 轮、每轮 600 次，
共 **5,400 次测量请求，零失败**。每档测量前明确预热该并发数的连接池；
29 个查询输入也已预热。所有返回都核对完整边证据与 `has_more`。

下表为各档 1,800 次原始延迟的合并百分位，吞吐为成功数除以该档各轮
测量墙钟时间之和，**不是最好一轮的峰值**。采用有界 closed-loop 客户端，
不计创建任务后的排队时间，没有重试掩盖错误。

| 并发 | 请求数 | p50 | p95 | p99 | 合并请求/秒 | 各轮请求/秒范围 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 1,800 | 10.091 ms | 13.157 ms | 40.150 ms | 87.8 | 73.2–99.6 |
| 4 | 1,800 | 18.341 ms | 28.011 ms | 41.364 ms | 202.3 | 193.1–219.3 |
| 8 | 1,800 | 30.226 ms | 81.052 ms | 132.977 ms | 200.6 | 194.1–206.9 |

同一输入混合下，HTTP 解码后响应体平均约 3,134 bytes。
这里只计响应体，不含 HTTP header、TLS 或 TCP/IP 开销，也不是压缩传输量。
三轮总查询测量时间约 38.38 秒，不足以代表长期容量。
第 3 轮单并发吞吐下降也说明现场存在波动，不能只引用最好数字。

## 实际 Relay stdio 路径

测量已安装的 Windows 加固内部候选，而不是 debug 测试替身。
在一个持续存活的 MCP 会话中完成初始化、工具发现和输入预热，再顺序测量：

| 项目 | 数值 |
| --- | ---: |
| 图查询测量请求 | 180 |
| 失败数 | 0 |
| p50 / p95 / p99 | 13.499 / 16.758 / 18.339 ms |
| 实际顺序吞吐 | 71.3 请求/秒 |
| 平均 stdio 响应大小 | 6,674 bytes |

该层包括 stdio、Relay JSON、真实认证 HTTP、后端和返回编码；
不含进程启动。单实例 Relay 当前按输入顺序处理消息，因此本轮没有通过
启动多个 Relay 绕过锁来制造并发数字。

`get_graph_ontology` 静态契约的对照：

| 层 | 请求数 | p50 | p95 | 响应大小 |
| --- | ---: | ---: | ---: | ---: |
| 后端 handler | 200 | 0.010 ms | 0.011 ms | 2,154 bytes，紧凑 JSON |
| 认证 HTTP | 50 | 9.530 ms | 12.009 ms | 2,265 bytes，解码响应体 |
| 已安装 Relay stdio | 30 | 11.392 ms | 13.410 ms | 4,823 bytes，含换行 |

相同静态契约在 Relay 中的体积约为 HTTP 的 2.13 倍；现有响应同时包含
文本内容与 `data`，造成重复编码。不同层的单次耗时不能简单相减后当成
因果分解结果，但通信/认证路径和重复输出是值得优先剖析的方向。

## 建议与非目标

1. **使用建议**：同一会话发现一次契约，不必每次关系查询前重新获取；
   保持现有按项目/ref/generation 隔离的缓存语义。
2. **并发起点**：后续调优从 4 个 HTTP 并发开始。这里不是正式容量上限，
   更没有自动把生产并发设为 4。
3. **优先减少输出冗余**：评估可选精简模式、稳定契约引用和兼容的结构化返回，
   保留现有默认来源证据，不直接删除字段或破坏已有客户端。
4. **再做定点剖析**：认证、审计、序列化和通信分别计时，再决定是否改同步
   调用或线程模型；本次数据不能证明其中任何一个就是唯一瓶颈。
5. **补充质量与规模评估**：后续使用真实任务频率、更多大图/高扇出、
   缓存失效和新进程启动、持续负载测试；P1 导入覆盖单独调查。

本次不调用外部模型、不发送源码、不新增依赖、不测全量/增量索引；
没有测量 LLM 最终答案正确率，也不宣称新的 token 节省或回答成功率提升。

## 原始证据与复现

完整原始样本、精确锚点及本次执行脚本保留在 git 忽略的
`tmp\benchmarks\ontology-performance-20261109`，不提交项目标识或源码路径：

- [汇总指标](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-Summary-v1.0.0-en.json)
- [后端及固定用例](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-Backend-v1.0.0-en.json)
- [扩大 HTTP 采样](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-HTTP-Steady-v1.0.0-en.json)
- [Relay 样本](../../tmp/benchmarks/ontology-performance-20261109/Auto-Ontology-Performance-Relay-v1.0.0-en.json)

三个主要输入文件的 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| Backend | `99200efefb3568ee274783f3de4933b2a8dbfa9aebcfed6c609ce9c488028815` |
| HTTP Steady | `8999631a5aa9af47c1c86481e12b67efedb21e6823d05a19cca2135303699fbc` |
| Relay | `4b219b5b64c40b4810ac40b6da54da0282f0a4b04853dc75f2bb36aa44740569` |

命令形状如下；复跑必须使用新输出目录，不覆盖此次固定证据。
实际项目 ID 取本机已有 Relay 配置，不把凭据写进参数或报告。
后端工作负载重新抽取后必须比较 SHA，不能默认它与本轮一致。

```powershell
docker cp <local-script-directory>\backend_benchmark.py cga-desktop-api:/tmp/cga-ontology-benchmark.py
docker exec cga-desktop-api python /tmp/cga-ontology-benchmark.py --primary-id <existing-project-id> --projects 3 --anchors 3 --iterations 20 --warmup 3 --output /tmp/backend-new-run.json
docker cp cga-desktop-api:/tmp/backend-new-run.json <new-output-directory>\backend.json
.\.venv\Scripts\python.exe <local-script-directory>\http_benchmark.py --backend <new-output-directory>\backend.json --output <new-output-directory>\http.json --requests 600 --repetitions 3 --pool-warmup 2
.\.venv\Scripts\python.exe <local-script-directory>\relay_benchmark.py --backend <new-output-directory>\backend.json --output <new-output-directory>\relay.json --requests 180
```

最后一条要求先退出已有托盘，并在完成或失败后恢复原托盘；不绕过锁。
新发布的报告/JSON 应按文档命名规则使用一致的新版本。
