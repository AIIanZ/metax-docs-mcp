# 验收记录

验收日期：2026-09-18。主 Agent 负责方案、模块契约、代码审查与最终集成验收；子 Agent 分别实现索引、采集和 MCP/CLI。

## 阶段 1：来源与设计

- 当前目录初始为空 Git 仓库，无既存应用需迁移。
- 真实只读请求 GitHub `GET /repos/MetaX-MACA/maca-samples` 成功，默认分支 `main`。
- 真实 `GET /repos/MetaX-MACA/maca-samples/git/trees/main?recursive=1` 返回 commit `e6de14cc4d9248768d066a1475450ed7705fd766`，包含 18 个 Markdown 文档。
- 架构、模块接口与阶段门禁见 DESIGN.md 和 CONTRACT.md。

后续自动测试与双源实测结果在最终验收后补充。

## 阶段 2—3：双源集成

- 真实 `examples/smoke.json`：mx-smi preview 1484，同手册 HTML 链接抓取 8 页；MetaX-MACA/maca-samples 抓取 18 份文档。共 26 条，失败 0，选定范围无截断。
- 相同配置再次同步：indexed=0、unchanged=26、failed=0；正文和来源未变化时不创建重复记录。
- `scripts/verify_live.py` 验证 official/github 分源检索、文档回读、HTTPS 来源链接，以及 GitHub 40 位固定 commit 引用。
- “拓扑”中文查询命中 mx-smi 命令介绍；示例语料中 `CUDA_VISIBLE_DEVICES` 无命中，工具返回空结果，不能据此推断全站没有该变量。
- 官方目录入口另用 max_pages=3 实测：3 条成功，版本含 3.8.2.20x、3.8.3.x，退出码 2 并报告 partial/truncated。
- 评审后修复：fenced code 被误分章节、HTML 命令下划线被转义、FTS alias 导致静默降级、过滤条件晚于候选限制、跨手册/历史版本链接扩散、截断未报告、只读查询误建库。

## 阶段 4：Docker 镜像目录与三源验收

- 用户指定的 C500、MXMACA、docker、分层包筛选页通过公开
  `docker_package_info` API 同步；14 条唯一镜像全部入库，失败 0、截断 0。
- 源站 `_id` 可在不同系统镜像间复用；稳定 ID 改为镜像路径、包名、芯片与交付类型的组合，避免漏项。
- 最终三源样本共 40 条：官方手册 8、MetaX-MACA GitHub 18、Docker 镜像 14；首次同步失败 0。
- Docker 元数据检索保留镜像路径、tag、芯片、架构、系统、MXMACA 版本和更新时间。公开列表不提供 registry 主机时，拉取命令标记为 `inferred_from_path`，不得当作已验证命令执行。
- 自动测试最终为 29 项，包含真实 MCP stdio 握手、四个工具调用、三类来源过滤、分页重复检测及只读数据库行为。

这些结果只代表指定样本与目录入口验收，不代表全站、全历史版本或 MetaX-MACA 全组织覆盖。

## 0.2.0：框架源码和 MACA 编程扩展

验收日期：2026-09-30。已更新本机安装包和 Codex 现有配置指向的 `.data/index.sqlite3`。旧索引保留为 `.data/index-before-20260930.sqlite3`。默认配置和查询方式见 README，协议验收结果见 [扩展验收数据](acceptance-expanded-20260930.json)。

| 仓库 | 索引 ref | 文档及源码文件数 |
| --- | --- | ---: |
| MetaX-MACA/vLLM-metax | master | 1306 |
| MetaX-MACA/sglang | release/v0.5.14 | 7023 |
| MetaX-MACA/maca-samples | main | 70 |
| MetaX-MACA/gpuBenchmark | main | 56 |
| MetaX-MACA/mcoplib | main | 1473 |
| MetaX-MACA/TileOPs-Metax | dev | 641 |
| MetaX-MACA/TileKernels-Metax | dev | 124 |
| tile-ai/tilelang-metax | dev | 1673 |

GitHub 共 12,366 个文件。官方新增 11 类手册的 67 页，保留原 mx-smi 样本 8 页，共 75 页。Docker 目录新增 5 条，保留旧记录，共 19 条。正式索引总计 12,460 条。

SGLang 的 `main` 在本次快照中没有 MACA 检测符号。默认 ref 改为实际含 `is_maca()` 的 `release/v0.5.14`，固定 commit 为 `a60ee6e5be6f19d94b0ac07bd1b281eb385f37bd`。临时数据库中的旧 `main` 文件已移除，正式索引只保留发布分支快照。

初次归档同步发现 TileLang 的两个上游 HIP/CUDA 头文件超过正文上限。配置显式排除这两个文件后，重新同步该仓库：1673 条 unchanged、失败 0、截断 0。SGLang 发布分支同步为 indexed=7023、失败 0、截断 0。其余仓库及选定官方手册在本次同步中没有采集失败。排除规则、扩展名和来源边界见 README。

自动测试为 `45 passed`。新增测试覆盖归档路径、链接拒绝、字节和成员上限、Git blob hash 校验，以及 token 不发送给归档主机。另验证仓库 ref、官方手册分类筛选、幂等写入和原有 MCP 协议行为。

`scripts/verify_expanded.py` 使用已安装的 0.2.0 服务入口和官方 SDK，通过 stdio 完成 initialize、tools/list 及四个工具调用。12 组真实查询均命中并成功回读。框架及源码查询包括 `MacaPlatformBase`、`is_maca`、`mcMalloc`、`WarpSize`、`awq_gemm`，以及三个 TileLang 仓库的 MACA 内容。官方查询包括 `mcMalloc`、“共享内存”和 `mxcc`，Docker 查询使用 `MXMACA`。官方命中另验证章节读取，GitHub 命中验证仓库、ref、40 位 commit 与固定来源 URL。

临时索引通过 SQLite `quick_check`。切换正式数据库后，12 组 MCP 查询再次通过。正式索引中没有 `text_truncated=true` 的 GitHub 文件；这些结果仅覆盖配置选定的文件和文档版本，历史版本、PDF、Notebook、子模块和二进制不在范围内。

## 0.3.0：算子开发索引与算子仓库扩展

验收日期：2026-10-14。一次真实同步并原子发布，正式索引 `.data/index.sqlite3`，旧索引备份 `.data/index.previous.sqlite3`，逐批报告与 MCP 探针记录在 `.data/refresh/20261013T163427.465065Z/`。

本次按用户要求把未入索引的算子相关仓库补进 `examples/sources.json`，并新增算子开发主题索引（`get_operator_guide` / `metax-docs-mcp opdev`）。

| 仓库 | 索引 ref | 文件数 | commit 前缀 |
| --- | --- | ---: | --- |
| MetaX-MACA/sglang | release/v0.5.14 | 7026 | 052dcfb5f7d9 |
| MetaX-MACA/mcoplib | main | 1771 | f325795aac1e |
| tile-ai/tilelang-metax | dev | 1673 | c6074aa11535 |
| MetaX-MACA/vLLM-metax | master | 1329 | cec91763dbe9 |
| MetaX-MACA/flashattn | 2.25.2 | 1068 | 8f279843ce18 |
| MetaX-MACA/TileOPs-Metax | dev | 641 | 4df32b284f46 |
| MetaX-MACA/mcTlass | 2.20.2 | 398 | 3d125cdd2ae2 |
| MetaX-MACA/mcTriton | 3.0 | 371 | 7dd407c26568 |
| MetaX-MACA/McFlashInfer | master | 198 | 4d23f89fab84 |
| MetaX-MACA/TileKernels-Metax | dev | 124 | 0266ab740980 |
| MetaX-MACA/maca-samples | main | 70 | e6de14cc4d92 |
| MetaX-MACA/gpuBenchmark | main | 56 | c036397faf50 |
| MetaX-MACA/op_optimization | master | 5 | 67875de0d2f1 |

GitHub 共 14,730 个文件，官方手册 67 页（3.8.0.x 16、3.9.1 51），Docker 目录 14 条，正式索引合计 14,811 条。同步报告 `failed=0`、`partial=false`、`truncated=false`，无 `text_truncated=true` 的 GitHub 文件。相对上一版：新增 2,044、更新 8,351、移除 2。移除的 2 条是 vLLM-metax 上游已删除的 `vllm_metax/patch/enhancement/deepseek_v41_support.py` 与 `.../quant_kernels/mxfp8_kernel.py`，由重建式发布自然清理并在报告中显式列出。

新仓库采用按仓库排除模式（`github.repository_exclude_globs`）去掉重复的 vendored 副本，同时保留 MetaX 自己的目录：McFlashInfer 排除 `3rdparty/**`（内含一份 mcTlass 拷贝），flashattn 排除 `csrc/mctlass/**`，mcTriton 排除上游 `third_party/amd|nvidia|json|pybind11|proton/**` 并保留 `third_party/metax/**`（MACA 后端 55 个文件）。

第一次尝试在第 9 个仓库中断：GitHub 未认证 API 达到每小时限额，`failed=4`，`refresh_index.py` 拒绝发布并保留原服务索引。随后给刷新流程增加了“按仓库分批 + 到达限流重置时间后重试同一批”的处理（`METAX_REFRESH_GITHUB_BATCH`，默认每批 4 个仓库），重跑后 4 个 GitHub 批次、1 个官方/Docker 批次全部成功；所有批次写入同一个 staging 数据库，upsert 幂等，结果与单次全量同步一致。SGLang 发布分支探测在限流时降级为沿用配置中的 `release/v0.5.14`，实际索引版本在 `list_metax_sources` 中可见。

`scripts/verify_expanded.py` 通过已安装的 stdio MCP 完成 initialize、tools/list 与工具的实调用：工具列表为 5 个（新增 `get_operator_guide`），17 组查询全部命中并成功回读，其中新增 mcTlass、mcTriton、flashattn、McFlashInfer、op_optimization 各一组探针；同时逐个解析 17 个算子开发主题，引用全部命中（`missing_references` 为空），否则本次验收不会通过。

`scripts/verify_operator_guide.py .data/index.sqlite3` 独立复核：17 个主题、62 条引用、0 条缺失；CLI `metax-docs-mcp --db .data/index.sqlite3 opdev optimize-c600` 返回 3 条引用对应的真实文档并给出阅读顺序。自动测试 62 项通过，新增覆盖 `Store.find_documents` 的路径/前缀/标题匹配、主题一致性、主题解析与缺失上报、按仓库排除模式、限流重试与“非限流失败不重试”等路径。

已运行的 MCP 进程需要重启（或在客户端重连）后才能看到新增的 `get_operator_guide`；本次 stdio 验收使用新启动的进程，与客户端重启后行为一致。

## 2026-10-19：每周资料更新

本周更新已发布到本机 MCP。收录范围为配置选定的 13 个 GitHub 仓库、官方手册和 Docker 镜像目录。索引共 15,143 条：GitHub 15,062 条、官方手册 67 条、Docker 目录 14 条。来源版本和完整 commit 见 [本周记录](refresh/2026-10-19.json)。

相对上次发布，新增 371 条、更新 3,618 条、移除 39 条。“更新”包含正文、来源版本、commit 和元数据变化；其中已有 ID 的正文变化为 434 条。vLLM-metax、TileOPs-Metax 和 tilelang-metax 的 commit 有变化，SGLang 仍为 `release/v0.5.14`。官方手册包含 `3.9.1` 和 AI 推理手册的 `3.8.0.x`。

首次抓取因 GitHub `flashattn` 接口返回 `503 Service Unavailable` 而停止，原索引保持可用。接口恢复后，本轮复用已完整完成的批次，清除暂存库中失败批次的数据，再重建剩余批次。原始失败与恢复记录均保留在本机。恢复后的汇总报告为 `failed=0`、`partial=false`、`truncated=false`，通过 SQLite 和 FTS 完整性检查后才发布。

17 组 stdio MCP 检索回读全部通过，17 个算子主题的引用均可解析。发布后调用当前连接的 `list_metax_sources`，返回 15,143 条；`MacaPlatformBase` 检索返回 vLLM-metax 新 commit `9f03f6c8d19d0264d965fcb3d89dff81e13e512d` 的正文。本周代码提交前重新运行离线测试，结果为 `62 passed`。
