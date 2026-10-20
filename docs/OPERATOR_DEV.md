# 沐曦算子开发索引（metax-docs-mcp）

本文件由 `scripts/render_operator_guide.py` 从 `src/metax_docs_mcp/opdev.py` 生成，请勿手工编辑。

用途：把「算子开发」拆成可检索的阶段，每个阶段指向索引中真实存在的文档或代码文件。
查询入口：MCP 工具 `get_operator_guide`，或 CLI `metax-docs-mcp opdev [TOPIC]`。
引用按仓库路径或官方手册标题书写，不写死 commit；刷新索引后仍能解析到最新快照。

## 主题一览

| slug | 阶段 | 主题 | 引用数 |
| --- | --- | --- | --- |
| `overview` | 总览 | 算子开发总览：从源码到部署 | 4 |
| `kernel-cpp` | 基础 | 写一个 MACA kernel：C++、内建函数与官方样例 | 6 |
| `toolchain` | 基础 | 编译工具链与构建 | 4 |
| `op-library` | 工程 | mcoplib 算子库结构与算子清单 | 3 |
| `add-op` | 工程 | 新增或接入一个算子 | 4 |
| `benchmark` | 验证 | 算子性能与精度基准（mxbench / nvbench） | 3 |
| `profiling` | 验证 | 性能分析：mcProfiler / mcTracer | 3 |
| `optimize-c500` | 优化 | C500 算子优化 | 3 |
| `optimize-c600` | 优化 | C600-U 算子优化 | 3 |
| `triton` | 跨框架 | Triton 算子（mcTriton） | 5 |
| `tilelang` | 跨框架 | TileLang 算子库 | 4 |
| `optimization-program` | 优化 | 算子库优化方案与实操指南 | 5 |
| `attention-flash` | 算子族 | Attention / Flash 类算子 | 4 |
| `quantization` | 算子族 | 量化与量化 GEMM 算子 | 3 |
| `gemm-matlass` | 算子族 | GEMM 与模板库（MATLASS / CUTLASS 扩展） | 3 |
| `comm` | 算子族 | 通信算子（MCCL） | 3 |
| `inference-custom-op` | 交付 | 推理引擎自定义算子（MacaRT / ONNX） | 2 |

## 算子开发总览：从源码到部署（`overview`）

**阶段**：总览

MACA 算子分三层：官方 C++/内建函数/运行时手册定义语言与 API；mcoplib 等仓库给出 vLLM/SGLang/LMDeploy 的算子实现与优化资料；MacaRT 允许在推理引擎层注册自定义算子。

**可执行步骤**：

1. 先确认目标形态：独立 CUDA/MACA kernel、框架算子库（mcoplib）还是推理引擎自定义算子。
2. 用 search_metax_docs 查具体 API、符号与文件名，再读整节，不要用自然语言问题直接下结论。
3. 本索引的每个 topic 都返回可引用文档；先读官方手册确定语义，再读 mcoplib 确定工程做法。

**参考**：

- MXMACA C++ 编程指南（official: 沐曦通用GPU MXMACA C++编程指南）
- 运行时 API 编程指南（official: 沐曦通用GPU运行时API编程指南）
- mcoplib 项目代码结构（MetaX-MACA/mcoplib: docs/code_structure.md → mx 推理框架算子库的目录、模块与设计决策）
- mcOpLib 全量算子分类表（MetaX-MACA/mcoplib: docs/op_kernel_classification.md → 175 个 .cu 按 13 类算子的映射，用来判断算子归口）

## 写一个 MACA kernel：C++、内建函数与官方样例（`kernel-cpp`）

**阶段**：基础

MACA 编译 C/C++，CUDA 生态高度兼容但只支持 sm_80 及以下特性；官方样例覆盖 vectorAdd、sharedMemory、dynamicParallelism、graph、IPC 等最小可运行模式。

**可执行步骤**：

1. 从 maca-samples 的最小样例起步，理解 grid/block、共享内存与流式执行。
2. 用 C++ 编程指南确认语言扩展与 Cooperative Groups 的支持范围。
3. 遇到内建函数语义不确定时查《编译器内建函数编程指南》，不要按 NVIDIA 同名函数假定行为。

**参考**：

- C++ 语言扩展（official: 2. C++ 语言扩展 — 沐曦通用GPU MXMACA C++编程指南）
- 编译器内建函数编程指南（official: 沐曦通用GPU MXMACA 编译器内建函数编程指南）
- maca-samples 总览（MetaX-MACA/maca-samples: 0_Introduction/README.md → 基本 GPU 编程模式的参考实现清单）
- vectorAdd 样例（MetaX-MACA/maca-samples: 0_Introduction/vectorAdd/README.md → 最小 kernel + 主机侧启动/校验范例）
- sharedMemory 样例（MetaX-MACA/maca-samples: 0_Introduction/sharedMemory/README.md → 显式共享内存与同步写法）
- vectorAdd_mcrtc 样例（MetaX-MACA/maca-samples: 0_Introduction/vectorAdd_mcrtc/README.md → 运行时编译（RTC）路径）

## 编译工具链与构建（`toolchain`）

**阶段**：基础

算子用 mxcc 工具链编译；mcoplib 走 CMake + setuptools/pybind11，并支持按框架、按 CUTLASS、按 GPU 目标（xcore1000/xcore1089/xcore1500/xcore1501）分模块构建。

**可执行步骤**：

1. 离线编译用 mxcc；CMake/Makefile 模板见官方《mxcc编译器用户指南》第 5 章示例。
2. mcoplib 编译优先在已发布的 vLLM/SGLang 镜像内进行，构建依赖 Python 3.9-3.12、CMake>=3.26。
3. 子模块可用 BUILD_VLLM_SUBMODULE / BUILD_SGLANG_SUBMODULE / BUILD_LMDEPLOY_SUBMODULE 选择性关闭。
4. 多平台产物用 CUCC_TARGETS 指定目标列表，打包后用 mcoplib_version 校验。

**参考**：

- mxcc 编译器用户指南（official: 沐曦通用GPU mxcc编译器用户指南）
- mxcc 示例（official: 5. 示例 — 沐曦通用GPU mxcc编译器用户指南）
- mcoplib 构建方式总结（MetaX-MACA/mcoplib: docs/build_summary.md → CMake + setuptools 混合构建与流程图）
- mcoplib README 构建设置（MetaX-MACA/mcoplib: README.md → 编译依赖、环境变量与打包命令）

## mcoplib 算子库结构与算子清单（`op-library`）

**阶段**：工程

mcoplib 提供 175 个 .cu 内核：vLLM 47、SGLang 88、LMDeploy 3、默认算子 30，另有独立 deb 构建的 CV 算子；编译产物映射到 mcoplib.op、mcoplib._C、mcoplib.sgl_kernel 等模块。

**可执行步骤**：

1. 先读 code_structure.md 确定算子属于哪个子模块与哪个 .so。
2. 用 op_kernel_classification.md 定位算子类别（Attention/MoE/量化/归一化/KV Cache 等）。
3. 改算子前确认目标框架的绑定方式：vLLM 用 TORCH_LIBRARY，SGLang 用 TORCH_LIBRARY_FRAGMENT。

**参考**：

- 项目代码结构（MetaX-MACA/mcoplib: docs/code_structure.md → 目录、算子分模块映射与关键设计决策）
- 全量算子分类表（MetaX-MACA/mcoplib: docs/op_kernel_classification.md → 13 类、175 个内核的功能映射）
- 仓库开发说明（CLAUDE.md）（MetaX-MACA/mcoplib: CLAUDE.md → 模块划分、构建与扩展工作的操作说明）

## 新增或接入一个算子（`add-op`）

**阶段**：工程

新增算子的标准路径是：在 op/ 对应子目录加 .cu、在对应 CMakeLists.txt 注册源文件、在绑定 文件加 pybind11 或 TORCH_LIBRARY 入口，然后重编并跑单测/基准。

**可执行步骤**：

1. 选择子目录（vllm/sglang/lmdeploy/默认/CV），新建 .cu 并加入对应 CMakeLists.txt 源列表。
2. 在 pybind.cpp 或 torch_bindings.cpp 中注册算子入口，保持与既有算子的签名约定一致。
3. python setup.py build_ext --inplace 增量编译，再用 unit_test/ 与 benchmark/ 验证。
4. 需要调试输入时用 MCOP_DEBUG_TRACE / MCOP_DEBUG_PARAMS_DUMP 打开入参打印或落盘，不必重新编译。

**参考**：

- mcoplib 开发说明（MetaX-MACA/mcoplib: CLAUDE.md → Adding a New Operator 与修改既有 kernel 的流程）
- 如何添加新算子到 Benchmark 框架（MetaX-MACA/mcoplib: benchmark/How_To_Add_Op_Benchtest.md → 接入白名单、config 与 runner 的完整步骤）
- mxbench 算子测试技能（MetaX-MACA/mcoplib: Skills/mxbench_op_test/skill.md → 为新算子生成 nvbench 性能测试的操作规范）
- 算子入参 dump 说明（MetaX-MACA/mcoplib: README.md → MCOP_DEBUG_* 环境变量与 JSON 落盘示例）

## 算子性能与精度基准（mxbench / nvbench）（`benchmark`）

**阶段**：验证

mcoplib 的 benchmark 框架基于 nvbench：先做余弦相似度精度验证，再采样 GPU Time、Elem/s、GlobalMem BW、BWUtil，并支持 --generate/--update/--compare 维护 CSV 基准库。

**可执行步骤**：

1. 先保证精度验证通过（run_verification / 1-CosSim），再谈性能数字。
2. 每个算子一个 config JSON + 一个 runner，注册进 SUPPORTED_OPERATORS 白名单。
3. 跨版本或跨卡比较时固定输入 shape、dtype 与 warmup/采样次数，并记录真实文件与命令。

**参考**：

- mxbench 性能测试工具（MetaX-MACA/mcoplib: benchmark/README.md → 基准框架总览与运行方式）
- 如何添加新算子到 Benchmark 框架（MetaX-MACA/mcoplib: benchmark/How_To_Add_Op_Benchtest.md → 从配置到校验的逐步说明）
- gpuBenchmark（MetaX-MACA/gpuBenchmark: README.md → 官方 GPU 基准测试仓库（BLAS/带宽等））

## 性能分析：mcProfiler / mcTracer（`profiling`）

**阶段**：验证

mcProfiler 采集 kernel 级指标，mcTracer 做时间线追踪并用 mcTracer-Viewer 查看；mcoplib 侧还有 profiler.py 与 profilerScope 样例。

**可执行步骤**：

1. 先用 mcProfiler 拿到带宽/利用率量级，判断是 memory-bound 还是 compute-bound。
2. kernel 间依赖或 launch 间隔异常时用 mcTracer 看时间线。
3. profiler 采样会改运行时序，结论要以 back-to-back 或 CUDA Graph 复测为准。

**参考**：

- mcProfiler 用户手册（official: 沐曦通用GPU mcProfiler用户手册）
- mcTracer 用户手册（official: 沐曦通用GPU mcTracer用户手册）
- profilerScope 样例（MetaX-MACA/maca-samples: 0_Introduction/profilerScope/README.md → 代码内插入 profiler 范围的最小示例）

## C500 算子优化（`optimize-c500`）

**阶段**：优化

C500 为 sm_80 兼容架构（104 SM、60 个 SM 每卡拓扑见文档），算力约为 A100 的 80%；指南给出硬件参数、优化手段与 GEMM 反量化融合技巧。

**可执行步骤**：

1. 先按硬件参数判断瓶颈类型，再套对应手段，不要先改代码。
2. C500 原子操作指令较少，尽量避免依赖 atomic 的实现。
3. 优化前后用同一 benchmark 口径对比，并用余弦相似度守住精度。

**参考**：

- C500 CUDA Kernel 优化指南（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/references/c500-optimization-guide.md → 硬件参数、优化手段与检查清单）
- C500 GEMM 反量化融合技巧（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/references/c500-gemm-dequant-fused-techniques.md → dequant-fused GEMM 的优化手法）
- 优化技能入口（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/SKILL.md → 按 ReAct 流程做读码→优化→benchmark 的技能定义）

## C600-U 算子优化（`optimize-c600`）

**阶段**：优化

C600-U 仍是 sm_80 兼容：执行层级为 AP/PEU/warp（warp 为 64 线程），设备上报的 multiProcessorCount 与物理 AP 数不一致，必须按实测值算网格。

**可执行步骤**：

1. 先建立 AP/PEU/warp 心智模型，再回答“瓶颈在哪”三个问题，最后才改代码。
2. 按案例（fused_add_rmsnorm、INT8 quant、bitonic sort、MoE swiglu 等）找同类手法。
3. 注意文档中的反直觉坑：网格与并行度按实测 mpc，而非标称 SM 数。

**参考**：

- C600-U CUDA Kernel 优化指南（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/references/c600-optimization-guide.md → 硬件模型、核心招式、反直觉坑与 10 个案例）
- C600-U Triton 算子优化指南（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/references/c600u_triton_kernel_optimize.md → 按算子分类选招式、constexpr 化与计时口径）
- sgl_fused_add_rmsnorm 深度分析（MetaX-MACA/mcoplib: docs/fused_add_rmsnorm_analysis.md → 优化案例的源码级拆解）

## Triton 算子（mcTriton）（`triton`）

**阶段**：跨框架

mcTriton 是在 Triton 上增加 MXMACA 后端的实现，兼容原生 Triton 用法；官方手册给出 tl.dot 等算子的支持矩阵，并公开源码仓库与 tutorial。

**可执行步骤**：

1. 先按《快速安装》配好 MACA_PATH / LD_LIBRARY_PATH / MACA_CLANG_PATH 再 import triton。
2. tl.dot 等算子在不同产品线的数据类型支持不同，按《功能支持》的表逐项核对。
3. 优化时按算子分类（纯流式 / gather+dot / 流式+查表）选 num_warps 与 constexpr 策略。

**参考**：

- mcTriton 用户指南（official: 沐曦通用GPU mcTriton用户指南）
- mcTriton 功能支持（official: 3. 功能支持 — 沐曦通用GPU mcTriton用户指南）
- mcTriton 开源资源（official: 4. 开源资源 — 沐曦通用GPU mcTriton用户指南 / 4. 开源资源）
- C600-U Triton 算子优化实战（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/references/c600u_triton_kernel_optimize.md → 分类、autotune key、计时口径与案例）
- mcTriton 源码仓库（MetaX-MACA/mcTriton: README.md → org 内 Triton+MACA 后端源码快照）

## TileLang 算子库（`tilelang`）

**阶段**：跨框架

TileLang 路线是声明式/规格驱动：TileOPs-Metax 用 spec + manifest 描述算子、按 roofline 评估；TileKernels-Metax 是 tilelang 写的 kernel 库；tilelang-metax 是 MACA 后端。

**可执行步骤**：

1. 按 spec 驱动流程添加算子：写 manifest、生成实现、用 roofline 上限评估而不是比基线。
2. 先读 TileOPs 的分层（Op/Kernel）与算子接口设计，再写实现。
3. MACA 后端相关问题落到 tilelang-metax 的 backend/build 技能文档。

**参考**：

- TileOPs 说明（MetaX-MACA/TileOPs-Metax: README.md → spec-driven 算子库的定位与快速开始）
- TileOPs 算子接口设计（MetaX-MACA/TileOPs-Metax: docs/design/ops-design.md → Op 接口与 Kernel 分层约定）
- TileKernels 说明（MetaX-MACA/TileKernels-Metax: README.md → tilelang 编写的 kernel 库）
- tilelang MACA 后端技能（tile-ai/tilelang-metax: .agents/skills/tilelang-backend/SKILL.md → 后端集成与构建的操作说明）

## 算子库优化方案与实操指南（`optimization-program`）

**阶段**：优化

op_optimization 收集了 MetaX 官方的算子库优化方案与上手材料：面向 AI Agent 的开发范式、TileLang 在 MACA 上的构建指南、sample 跑通记录，以及推理前沿算子优化比赛方案。

**可执行步骤**：

1. 先用《优化方案》确定工作流：读规格→生成/改写 kernel→按 roofline 评估→回归。
2. 按《TileLang MACA 构建指南》把环境搭起来，再用 race_tests 指南跑通 sample。
3. 把比赛方案的评测口径当作自测基线，避免只跑通不算达标。

**参考**：

- op_optimization 仓库（MetaX-MACA/op_optimization: README.md → 方案入口与文档清单）
- AI Agent 开发范式优化方案（MetaX-MACA/op_optimization: 基于AI Agent开发范式的国产GPU大模型算子推理库优化方案.md → 面向国产 GPU 算子库的 Agent 工作流设计）
- TileLang MACA 构建指南（MetaX-MACA/op_optimization: tilelang_maca_build_guide_模力方舟.md → 在 MACA 上构建与运行 TileLang）
- race_tests 跑通指南（MetaX-MACA/op_optimization: race_tests_run_guide基于tilelang算子sample跑通测试.md → 基于 TileLang 算子 sample 的测试跑通步骤）
- 推理前沿算子优化比赛方案（MetaX-MACA/op_optimization: 基于国产软件栈的推理前沿算子优化比赛方案.md → 优化比赛的评测口径与任务定义）

## Attention / Flash 类算子（`attention-flash`）

**阶段**：算子族

Attention 是改动最频繁的一族：mcoplib 内有 14 个注意力内核（Paged Attention、MLA、Flash Attention、状态合并），另有 flashattn、McFlashInfer 等独立仓库。

**可执行步骤**：

1. 先确定形态：paged / MLA / flash / 状态合并，再找对应实现文件。
2. KV cache 布局、block size 与 head 切分决定访存形态，改这些参数要重跑精度与带宽。
3. MQA/GQA 用 fused_repeat_kv，MLA 路径看 fused_mla 相关内核。

**参考**：

- 注意力算子分类与文件清单（MetaX-MACA/mcoplib: docs/op_kernel_classification.md → 14 个注意力内核的输入输出与关键技术）
- flashattn 仓库（MetaX-MACA/flashattn: README.md → FlashAttention 在 MACA 上的实现）
- flashattn MACA 说明（MetaX-MACA/flashattn: README_MX.md → 该分支的 MetaX 适配与构建说明）
- McFlashInfer 仓库（MetaX-MACA/McFlashInfer: README.md → FlashInfer 的 MACA 适配）

## 量化与量化 GEMM 算子（`quantization`）

**阶段**：算子族

mcoplib 有 23 个量化内核与 19 个量化 GEMM（FP8/INT8/W8A8/W4A16/GPTQ/AWQ/GGUF/Marlin），分目录组织在 op/vllm/quantization/ 下。

**可执行步骤**：

1. 先确定量化方案与权重布局（对称/非对称、per-token/per-channel、打包位宽）。
2. 量化 GEMM 的瓶颈常在反量化与 epilogue，参考 C500 GEMM 反量化融合技巧。
3. 精度回归要覆盖量化误差，不能只看性能。

**参考**：

- 量化算子分类（MetaX-MACA/mcoplib: docs/op_kernel_classification.md → 量化与量化 GEMM 的文件清单）
- C500 GEMM 反量化融合技巧（MetaX-MACA/mcoplib: Skills/optimized-cuda-kernels/references/c500-gemm-dequant-fused-techniques.md → dequant 与 GEMM 融合的优化手法）
- GPTQ GEMM kernel 分析（MetaX-MACA/mcoplib: docs/hgemm_gptq_analysis.md → 量化 GEMM 的源码级拆解）

## GEMM 与模板库（MATLASS / CUTLASS 扩展）（`gemm-matlass`）

**阶段**：算子族

mcoplib 的 GEMM 走 CUTLASS/MATLASS 模板；mcTlass 是 MACA 侧模板库，默认开启 CUTLASS OP API 编译，可用 ENABLE_BUILD_CUTLASS_OP 控制。

**可执行步骤**：

1. 先判断是 dense、分组还是量化 GEMM，再选模板与 tile 配置。
2. 需要新模板能力时看 mcTlass，而不是改 mcoplib 内部副本。
3. 用 roofline/带宽上限判断是否还有优化空间，避免无效调参。

**参考**：

- mcTlass 仓库（MetaX-MACA/mcTlass: README_MX.md → MACA 侧 MATLASS/CUTLASS 模板库）
- router GEMM 分析（MetaX-MACA/mcoplib: docs/fp32_router_gemm_analysis.md → fp32_router_gemm 的源码级拆解）
- mcoplib 构建选项（MetaX-MACA/mcoplib: README.md → CUTLASS OP API 与 GPTQ Marlin 编译开关）

## 通信算子（MCCL）（`comm`）

**阶段**：算子族

MCCL 是多卡通信库，提供集合通信原语；mcoplib 内有 all_reduce / custom_all_reduce 等通信算子，MoE 与 PD 分离场景依赖这类算子。

**可执行步骤**：

1. 集合通信语义与环境变量以《MCCL编程指南》为准。
2. 框架内的通信算子（如 custom_all_reduce）要与框架的 EP/TP 拓扑一起验证。
3. 多卡问题先确认卡间链路与 RDMA 状态，再怀疑算子实现。

**参考**：

- MCCL 编程指南（official: 沐曦通用GPU MCCL编程指南）
- 通信算子分类（MetaX-MACA/mcoplib: docs/op_kernel_classification.md → AllReduce / Custom AllReduce / MSCCLPP 清单）
- allreduce 内核解读（MetaX-MACA/mcoplib: docs/minimax_allreduce_rms_qk_analysis.md → minimax_allreduce_rms_qk 的源码级分析）

## 推理引擎自定义算子（MacaRT / ONNX）（`inference-custom-op`）

**阶段**：交付

MacaRT 支持用户定义非官方 ONNX 算子：用 C++ API 构建算子库，注册到 SessionOptions 的 MacaEP，再加载含自定义算子的模型推理。

**可执行步骤**：

1. 先判断能否用现有算子组合解决；确实需要新算子再走自定义 ONNX 算子路径。
2. 按文档定义 Domain 与 Kernel 输入输出数据排布。
3. 用 Python API 或 C++ API 注册到 MacaEP 后，用真实模型回归精度。

**参考**：

- MacaRT 自定义算子（official: 3. MacaRT C++ API — 沐曦通用GPU AI推理用户手册 / 3.5. 自定义算子）
- AI 推理用户手册（official: 沐曦通用GPU AI推理用户手册）
