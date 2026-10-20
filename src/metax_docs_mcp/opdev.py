"""Curated operator-development guide for the local MetaX index.

The guide is data, not prose: every entry points at documents that the
synchronized index actually contains (official MACA manual pages or files under
a MetaX-MACA repository).  :func:`resolve_topic` looks the references up
through ``Store.find_documents`` so a renamed or removed upstream file is
reported as ``missing`` instead of silently disappearing from the answer.

The curated text is a map, not a substitute for the sources: callers should
read the referenced documents before acting, and re-check them after a refresh
because MetaX re-issues the manuals per MACA version.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SOURCES = ("official", "github", "docker")

_USAGE = (
    "Call get_operator_guide() without arguments for the topic list, then "
    "get_operator_guide(topic=<slug>) for the resolved documents. Every entry "
    "is a pointer into this local index: read it with get_metax_document, or "
    "get_metax_section when a section is named."
)


@dataclass(frozen=True, slots=True)
class OpReference:
    """One pointer into the local index."""

    label: str
    note: str
    source: str
    repository: str = ""
    path: str = ""
    path_prefix: str = ""
    title: str = ""
    section: str = ""


@dataclass(frozen=True, slots=True)
class OperatorTopic:
    """A stage of the operator-development workflow."""

    slug: str
    title: str
    stage: str
    summary: str
    steps: tuple[str, ...]
    refs: tuple[OpReference, ...]


def _ref(label: str, note: str, source: str, **kwargs: str) -> OpReference:
    return OpReference(label=label, note=note, source=source, **kwargs)


TOPICS: tuple[OperatorTopic, ...] = (
    OperatorTopic(
        slug="overview",
        title="算子开发总览：从源码到部署",
        stage="总览",
        summary=(
            "MACA 算子分三层：官方 C++/内建函数/运行时手册定义语言与 API；mcoplib 等仓库给出 "
            "vLLM/SGLang/LMDeploy 的算子实现与优化资料；MacaRT 允许在推理引擎层注册自定义算子。"
        ),
        steps=(
            "先确认目标形态：独立 CUDA/MACA kernel、框架算子库（mcoplib）还是推理引擎自定义算子。",
            "用 search_metax_docs 查具体 API、符号与文件名，再读整节，不要用自然语言问题直接下结论。",
            "本索引的每个 topic 都返回可引用文档；先读官方手册确定语义，再读 mcoplib 确定工程做法。",
        ),
        refs=(
            _ref("MXMACA C++ 编程指南", "语言扩展与 Cooperative Groups，kernel 语义的权威来源", "official",
                 title="沐曦通用GPU MXMACA C++编程指南"),
            _ref("运行时 API 编程指南", "编程模型、核函数、动态并行、图编程", "official",
                 title="沐曦通用GPU运行时API编程指南"),
            _ref("mcoplib 项目代码结构", "mx 推理框架算子库的目录、模块与设计决策", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/code_structure.md"),
            _ref("mcOpLib 全量算子分类表", "175 个 .cu 按 13 类算子的映射，用来判断算子归口", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/op_kernel_classification.md"),
        ),
    ),
    OperatorTopic(
        slug="kernel-cpp",
        title="写一个 MACA kernel：C++、内建函数与官方样例",
        stage="基础",
        summary=(
            "MACA 编译 C/C++，CUDA 生态高度兼容但只支持 sm_80 及以下特性；官方样例覆盖 vectorAdd、"
            "sharedMemory、dynamicParallelism、graph、IPC 等最小可运行模式。"
        ),
        steps=(
            "从 maca-samples 的最小样例起步，理解 grid/block、共享内存与流式执行。",
            "用 C++ 编程指南确认语言扩展与 Cooperative Groups 的支持范围。",
            "遇到内建函数语义不确定时查《编译器内建函数编程指南》，不要按 NVIDIA 同名函数假定行为。",
        ),
        refs=(
            _ref("C++ 语言扩展", "MACA C++ 扩展：限定符、地址空间、协作组", "official",
                 title="2. C++ 语言扩展 — 沐曦通用GPU MXMACA C++编程指南"),
            _ref("编译器内建函数编程指南", "内建函数类型定义与接口", "official",
                 title="沐曦通用GPU MXMACA 编译器内建函数编程指南"),
            _ref("maca-samples 总览", "基本 GPU 编程模式的参考实现清单", "github",
                 repository="MetaX-MACA/maca-samples", path="0_Introduction/README.md"),
            _ref("vectorAdd 样例", "最小 kernel + 主机侧启动/校验范例", "github",
                 repository="MetaX-MACA/maca-samples", path="0_Introduction/vectorAdd/README.md"),
            _ref("sharedMemory 样例", "显式共享内存与同步写法", "github",
                 repository="MetaX-MACA/maca-samples", path="0_Introduction/sharedMemory/README.md"),
            _ref("vectorAdd_mcrtc 样例", "运行时编译（RTC）路径", "github",
                 repository="MetaX-MACA/maca-samples", path="0_Introduction/vectorAdd_mcrtc/README.md"),
        ),
    ),
    OperatorTopic(
        slug="toolchain",
        title="编译工具链与构建",
        stage="基础",
        summary=(
            "算子用 mxcc 工具链编译；mcoplib 走 CMake + setuptools/pybind11，并支持按框架、按 CUTLASS、"
            "按 GPU 目标（xcore1000/xcore1089/xcore1500/xcore1501）分模块构建。"
        ),
        steps=(
            "离线编译用 mxcc；CMake/Makefile 模板见官方《mxcc编译器用户指南》第 5 章示例。",
            "mcoplib 编译优先在已发布的 vLLM/SGLang 镜像内进行，构建依赖 Python 3.9-3.12、CMake>=3.26。",
            "子模块可用 BUILD_VLLM_SUBMODULE / BUILD_SGLANG_SUBMODULE / BUILD_LMDEPLOY_SUBMODULE 选择性关闭。",
            "多平台产物用 CUCC_TARGETS 指定目标列表，打包后用 mcoplib_version 校验。",
        ),
        refs=(
            _ref("mxcc 编译器用户指南", "使用前准备、支持的选项、编译与调试", "official",
                 title="沐曦通用GPU mxcc编译器用户指南"),
            _ref("mxcc 示例", "Makefile 与 CMake 编译示例", "official",
                 title="5. 示例 — 沐曦通用GPU mxcc编译器用户指南"),
            _ref("mcoplib 构建方式总结", "CMake + setuptools 混合构建与流程图", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/build_summary.md"),
            _ref("mcoplib README 构建设置", "编译依赖、环境变量与打包命令", "github",
                 repository="MetaX-MACA/mcoplib", path="README.md"),
        ),
    ),
    OperatorTopic(
        slug="op-library",
        title="mcoplib 算子库结构与算子清单",
        stage="工程",
        summary=(
            "mcoplib 提供 175 个 .cu 内核：vLLM 47、SGLang 88、LMDeploy 3、默认算子 30，另有独立 "
            "deb 构建的 CV 算子；编译产物映射到 mcoplib.op、mcoplib._C、mcoplib.sgl_kernel 等模块。"
        ),
        steps=(
            "先读 code_structure.md 确定算子属于哪个子模块与哪个 .so。",
            "用 op_kernel_classification.md 定位算子类别（Attention/MoE/量化/归一化/KV Cache 等）。",
            "改算子前确认目标框架的绑定方式：vLLM 用 TORCH_LIBRARY，SGLang 用 TORCH_LIBRARY_FRAGMENT。",
        ),
        refs=(
            _ref("项目代码结构", "目录、算子分模块映射与关键设计决策", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/code_structure.md"),
            _ref("全量算子分类表", "13 类、175 个内核的功能映射", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/op_kernel_classification.md"),
            _ref("仓库开发说明（CLAUDE.md）", "模块划分、构建与扩展工作的操作说明", "github",
                 repository="MetaX-MACA/mcoplib", path="CLAUDE.md"),
        ),
    ),
    OperatorTopic(
        slug="add-op",
        title="新增或接入一个算子",
        stage="工程",
        summary=(
            "新增算子的标准路径是：在 op/ 对应子目录加 .cu、在对应 CMakeLists.txt 注册源文件、在绑定 "
            "文件加 pybind11 或 TORCH_LIBRARY 入口，然后重编并跑单测/基准。"
        ),
        steps=(
            "选择子目录（vllm/sglang/lmdeploy/默认/CV），新建 .cu 并加入对应 CMakeLists.txt 源列表。",
            "在 pybind.cpp 或 torch_bindings.cpp 中注册算子入口，保持与既有算子的签名约定一致。",
            "python setup.py build_ext --inplace 增量编译，再用 unit_test/ 与 benchmark/ 验证。",
            "需要调试输入时用 MCOP_DEBUG_TRACE / MCOP_DEBUG_PARAMS_DUMP 打开入参打印或落盘，不必重新编译。",
        ),
        refs=(
            _ref("mcoplib 开发说明", "Adding a New Operator 与修改既有 kernel 的流程", "github",
                 repository="MetaX-MACA/mcoplib", path="CLAUDE.md"),
            _ref("如何添加新算子到 Benchmark 框架", "接入白名单、config 与 runner 的完整步骤", "github",
                 repository="MetaX-MACA/mcoplib", path="benchmark/How_To_Add_Op_Benchtest.md"),
            _ref("mxbench 算子测试技能", "为新算子生成 nvbench 性能测试的操作规范", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/mxbench_op_test/skill.md"),
            _ref("算子入参 dump 说明", "MCOP_DEBUG_* 环境变量与 JSON 落盘示例", "github",
                 repository="MetaX-MACA/mcoplib", path="README.md"),
        ),
    ),
    OperatorTopic(
        slug="benchmark",
        title="算子性能与精度基准（mxbench / nvbench）",
        stage="验证",
        summary=(
            "mcoplib 的 benchmark 框架基于 nvbench：先做余弦相似度精度验证，再采样 GPU Time、Elem/s、"
            "GlobalMem BW、BWUtil，并支持 --generate/--update/--compare 维护 CSV 基准库。"
        ),
        steps=(
            "先保证精度验证通过（run_verification / 1-CosSim），再谈性能数字。",
            "每个算子一个 config JSON + 一个 runner，注册进 SUPPORTED_OPERATORS 白名单。",
            "跨版本或跨卡比较时固定输入 shape、dtype 与 warmup/采样次数，并记录真实文件与命令。",
        ),
        refs=(
            _ref("mxbench 性能测试工具", "基准框架总览与运行方式", "github",
                 repository="MetaX-MACA/mcoplib", path="benchmark/README.md"),
            _ref("如何添加新算子到 Benchmark 框架", "从配置到校验的逐步说明", "github",
                 repository="MetaX-MACA/mcoplib", path="benchmark/How_To_Add_Op_Benchtest.md"),
            _ref("gpuBenchmark", "官方 GPU 基准测试仓库（BLAS/带宽等）", "github",
                 repository="MetaX-MACA/gpuBenchmark", path="README.md"),
        ),
    ),
    OperatorTopic(
        slug="profiling",
        title="性能分析：mcProfiler / mcTracer",
        stage="验证",
        summary=(
            "mcProfiler 采集 kernel 级指标，mcTracer 做时间线追踪并用 mcTracer-Viewer 查看；"
            "mcoplib 侧还有 profiler.py 与 profilerScope 样例。"
        ),
        steps=(
            "先用 mcProfiler 拿到带宽/利用率量级，判断是 memory-bound 还是 compute-bound。",
            "kernel 间依赖或 launch 间隔异常时用 mcTracer 看时间线。",
            "profiler 采样会改运行时序，结论要以 back-to-back 或 CUDA Graph 复测为准。",
        ),
        refs=(
            _ref("mcProfiler 用户手册", "指标含义与采集方式", "official",
                 title="沐曦通用GPU mcProfiler用户手册"),
            _ref("mcTracer 用户手册", "时间线追踪与 Viewer", "official",
                 title="沐曦通用GPU mcTracer用户手册"),
            _ref("profilerScope 样例", "代码内插入 profiler 范围的最小示例", "github",
                 repository="MetaX-MACA/maca-samples", path="0_Introduction/profilerScope/README.md"),
        ),
    ),
    OperatorTopic(
        slug="optimize-c500",
        title="C500 算子优化",
        stage="优化",
        summary=(
            "C500 为 sm_80 兼容架构（104 SM、60 个 SM 每卡拓扑见文档），算力约为 A100 的 80%；"
            "指南给出硬件参数、优化手段与 GEMM 反量化融合技巧。"
        ),
        steps=(
            "先按硬件参数判断瓶颈类型，再套对应手段，不要先改代码。",
            "C500 原子操作指令较少，尽量避免依赖 atomic 的实现。",
            "优化前后用同一 benchmark 口径对比，并用余弦相似度守住精度。",
        ),
        refs=(
            _ref("C500 CUDA Kernel 优化指南", "硬件参数、优化手段与检查清单", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/references/c500-optimization-guide.md"),
            _ref("C500 GEMM 反量化融合技巧", "dequant-fused GEMM 的优化手法", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/references/c500-gemm-dequant-fused-techniques.md"),
            _ref("优化技能入口", "按 ReAct 流程做读码→优化→benchmark 的技能定义", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/SKILL.md"),
        ),
    ),
    OperatorTopic(
        slug="optimize-c600",
        title="C600-U 算子优化",
        stage="优化",
        summary=(
            "C600-U 仍是 sm_80 兼容：执行层级为 AP/PEU/warp（warp 为 64 线程），设备上报的 "
            "multiProcessorCount 与物理 AP 数不一致，必须按实测值算网格。"
        ),
        steps=(
            "先建立 AP/PEU/warp 心智模型，再回答“瓶颈在哪”三个问题，最后才改代码。",
            "按案例（fused_add_rmsnorm、INT8 quant、bitonic sort、MoE swiglu 等）找同类手法。",
            "注意文档中的反直觉坑：网格与并行度按实测 mpc，而非标称 SM 数。",
        ),
        refs=(
            _ref("C600-U CUDA Kernel 优化指南", "硬件模型、核心招式、反直觉坑与 10 个案例", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/references/c600-optimization-guide.md"),
            _ref("C600-U Triton 算子优化指南", "按算子分类选招式、constexpr 化与计时口径", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/references/c600u_triton_kernel_optimize.md"),
            _ref("sgl_fused_add_rmsnorm 深度分析", "优化案例的源码级拆解", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/fused_add_rmsnorm_analysis.md"),
        ),
    ),
    OperatorTopic(
        slug="triton",
        title="Triton 算子（mcTriton）",
        stage="跨框架",
        summary=(
            "mcTriton 是在 Triton 上增加 MXMACA 后端的实现，兼容原生 Triton 用法；官方手册给出 "
            "tl.dot 等算子的支持矩阵，并公开源码仓库与 tutorial。"
        ),
        steps=(
            "先按《快速安装》配好 MACA_PATH / LD_LIBRARY_PATH / MACA_CLANG_PATH 再 import triton。",
            "tl.dot 等算子在不同产品线的数据类型支持不同，按《功能支持》的表逐项核对。",
            "优化时按算子分类（纯流式 / gather+dot / 流式+查表）选 num_warps 与 constexpr 策略。",
        ),
        refs=(
            _ref("mcTriton 用户指南", "概述与版本对应关系", "official",
                 title="沐曦通用GPU mcTriton用户指南"),
            _ref("mcTriton 功能支持", "tl.dot 数据类型矩阵、环境变量与日志级别", "official",
                 title="3. 功能支持 — 沐曦通用GPU mcTriton用户指南"),
            _ref("mcTriton 开源资源", "官方给出的源码仓库与 tutorial 地址", "official",
                 title="4. 开源资源 — 沐曦通用GPU mcTriton用户指南", section="4. 开源资源"),
            _ref("C600-U Triton 算子优化实战", "分类、autotune key、计时口径与案例", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/references/c600u_triton_kernel_optimize.md"),
            _ref("mcTriton 源码仓库", "org 内 Triton+MACA 后端源码快照", "github",
                 repository="MetaX-MACA/mcTriton", path="README.md"),
        ),
    ),
    OperatorTopic(
        slug="tilelang",
        title="TileLang 算子库",
        stage="跨框架",
        summary=(
            "TileLang 路线是声明式/规格驱动：TileOPs-Metax 用 spec + manifest 描述算子、按 roofline 评估；"
            "TileKernels-Metax 是 tilelang 写的 kernel 库；tilelang-metax 是 MACA 后端。"
        ),
        steps=(
            "按 spec 驱动流程添加算子：写 manifest、生成实现、用 roofline 上限评估而不是比基线。",
            "先读 TileOPs 的分层（Op/Kernel）与算子接口设计，再写实现。",
            "MACA 后端相关问题落到 tilelang-metax 的 backend/build 技能文档。",
        ),
        refs=(
            _ref("TileOPs 说明", "spec-driven 算子库的定位与快速开始", "github",
                 repository="MetaX-MACA/TileOPs-Metax", path="README.md"),
            _ref("TileOPs 算子接口设计", "Op 接口与 Kernel 分层约定", "github",
                 repository="MetaX-MACA/TileOPs-Metax", path="docs/design/ops-design.md"),
            _ref("TileKernels 说明", "tilelang 编写的 kernel 库", "github",
                 repository="MetaX-MACA/TileKernels-Metax", path="README.md"),
            _ref("tilelang MACA 后端技能", "后端集成与构建的操作说明", "github",
                 repository="tile-ai/tilelang-metax", path=".agents/skills/tilelang-backend/SKILL.md"),
        ),
    ),
    OperatorTopic(
        slug="optimization-program",
        title="算子库优化方案与实操指南",
        stage="优化",
        summary=(
            "op_optimization 收集了 MetaX 官方的算子库优化方案与上手材料：面向 AI Agent 的开发范式、"
            "TileLang 在 MACA 上的构建指南、sample 跑通记录，以及推理前沿算子优化比赛方案。"
        ),
        steps=(
            "先用《优化方案》确定工作流：读规格→生成/改写 kernel→按 roofline 评估→回归。",
            "按《TileLang MACA 构建指南》把环境搭起来，再用 race_tests 指南跑通 sample。",
            "把比赛方案的评测口径当作自测基线，避免只跑通不算达标。",
        ),
        refs=(
            _ref("op_optimization 仓库", "方案入口与文档清单", "github",
                 repository="MetaX-MACA/op_optimization", path="README.md"),
            _ref("AI Agent 开发范式优化方案", "面向国产 GPU 算子库的 Agent 工作流设计", "github",
                 repository="MetaX-MACA/op_optimization",
                 path="基于AI Agent开发范式的国产GPU大模型算子推理库优化方案.md"),
            _ref("TileLang MACA 构建指南", "在 MACA 上构建与运行 TileLang", "github",
                 repository="MetaX-MACA/op_optimization",
                 path="tilelang_maca_build_guide_模力方舟.md"),
            _ref("race_tests 跑通指南", "基于 TileLang 算子 sample 的测试跑通步骤", "github",
                 repository="MetaX-MACA/op_optimization",
                 path="race_tests_run_guide基于tilelang算子sample跑通测试.md"),
            _ref("推理前沿算子优化比赛方案", "优化比赛的评测口径与任务定义", "github",
                 repository="MetaX-MACA/op_optimization",
                 path="基于国产软件栈的推理前沿算子优化比赛方案.md"),
        ),
    ),
    OperatorTopic(
        slug="attention-flash",
        title="Attention / Flash 类算子",
        stage="算子族",
        summary=(
            "Attention 是改动最频繁的一族：mcoplib 内有 14 个注意力内核（Paged Attention、MLA、"
            "Flash Attention、状态合并），另有 flashattn、McFlashInfer 等独立仓库。"
        ),
        steps=(
            "先确定形态：paged / MLA / flash / 状态合并，再找对应实现文件。",
            "KV cache 布局、block size 与 head 切分决定访存形态，改这些参数要重跑精度与带宽。",
            "MQA/GQA 用 fused_repeat_kv，MLA 路径看 fused_mla 相关内核。",
        ),
        refs=(
            _ref("注意力算子分类与文件清单", "14 个注意力内核的输入输出与关键技术", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/op_kernel_classification.md"),
            _ref("flashattn 仓库", "FlashAttention 在 MACA 上的实现", "github",
                 repository="MetaX-MACA/flashattn", path="README.md"),
            _ref("flashattn MACA 说明", "该分支的 MetaX 适配与构建说明", "github",
                 repository="MetaX-MACA/flashattn", path="README_MX.md"),
            _ref("McFlashInfer 仓库", "FlashInfer 的 MACA 适配", "github",
                 repository="MetaX-MACA/McFlashInfer", path="README.md"),
        ),
    ),
    OperatorTopic(
        slug="quantization",
        title="量化与量化 GEMM 算子",
        stage="算子族",
        summary=(
            "mcoplib 有 23 个量化内核与 19 个量化 GEMM（FP8/INT8/W8A8/W4A16/GPTQ/AWQ/GGUF/Marlin），"
            "分目录组织在 op/vllm/quantization/ 下。"
        ),
        steps=(
            "先确定量化方案与权重布局（对称/非对称、per-token/per-channel、打包位宽）。",
            "量化 GEMM 的瓶颈常在反量化与 epilogue，参考 C500 GEMM 反量化融合技巧。",
            "精度回归要覆盖量化误差，不能只看性能。",
        ),
        refs=(
            _ref("量化算子分类", "量化与量化 GEMM 的文件清单", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/op_kernel_classification.md"),
            _ref("C500 GEMM 反量化融合技巧", "dequant 与 GEMM 融合的优化手法", "github",
                 repository="MetaX-MACA/mcoplib", path="Skills/optimized-cuda-kernels/references/c500-gemm-dequant-fused-techniques.md"),
            _ref("GPTQ GEMM kernel 分析", "量化 GEMM 的源码级拆解", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/hgemm_gptq_analysis.md"),
        ),
    ),
    OperatorTopic(
        slug="gemm-matlass",
        title="GEMM 与模板库（MATLASS / CUTLASS 扩展）",
        stage="算子族",
        summary=(
            "mcoplib 的 GEMM 走 CUTLASS/MATLASS 模板；mcTlass 是 MACA 侧模板库，默认开启 CUTLASS OP API "
            "编译，可用 ENABLE_BUILD_CUTLASS_OP 控制。"
        ),
        steps=(
            "先判断是 dense、分组还是量化 GEMM，再选模板与 tile 配置。",
            "需要新模板能力时看 mcTlass，而不是改 mcoplib 内部副本。",
            "用 roofline/带宽上限判断是否还有优化空间，避免无效调参。",
        ),
        refs=(
            _ref("mcTlass 仓库", "MACA 侧 MATLASS/CUTLASS 模板库", "github",
                 repository="MetaX-MACA/mcTlass", path="README_MX.md"),
            _ref("router GEMM 分析", "fp32_router_gemm 的源码级拆解", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/fp32_router_gemm_analysis.md"),
            _ref("mcoplib 构建选项", "CUTLASS OP API 与 GPTQ Marlin 编译开关", "github",
                 repository="MetaX-MACA/mcoplib", path="README.md"),
        ),
    ),
    OperatorTopic(
        slug="comm",
        title="通信算子（MCCL）",
        stage="算子族",
        summary=(
            "MCCL 是多卡通信库，提供集合通信原语；mcoplib 内有 all_reduce / custom_all_reduce 等通信算子，"
            "MoE 与 PD 分离场景依赖这类算子。"
        ),
        steps=(
            "集合通信语义与环境变量以《MCCL编程指南》为准。",
            "框架内的通信算子（如 custom_all_reduce）要与框架的 EP/TP 拓扑一起验证。",
            "多卡问题先确认卡间链路与 RDMA 状态，再怀疑算子实现。",
        ),
        refs=(
            _ref("MCCL 编程指南", "编程模型与环境变量", "official",
                 title="沐曦通用GPU MCCL编程指南"),
            _ref("通信算子分类", "AllReduce / Custom AllReduce / MSCCLPP 清单", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/op_kernel_classification.md"),
            _ref("allreduce 内核解读", "minimax_allreduce_rms_qk 的源码级分析", "github",
                 repository="MetaX-MACA/mcoplib", path="docs/minimax_allreduce_rms_qk_analysis.md"),
        ),
    ),
    OperatorTopic(
        slug="inference-custom-op",
        title="推理引擎自定义算子（MacaRT / ONNX）",
        stage="交付",
        summary=(
            "MacaRT 支持用户定义非官方 ONNX 算子：用 C++ API 构建算子库，注册到 SessionOptions 的 "
            "MacaEP，再加载含自定义算子的模型推理。"
        ),
        steps=(
            "先判断能否用现有算子组合解决；确实需要新算子再走自定义 ONNX 算子路径。",
            "按文档定义 Domain 与 Kernel 输入输出数据排布。",
            "用 Python API 或 C++ API 注册到 MacaEP 后，用真实模型回归精度。",
        ),
        refs=(
            _ref("MacaRT 自定义算子", "Domain 定义与 Kernel 输入输出排布", "official",
                 title="3. MacaRT C++ API — 沐曦通用GPU AI推理用户手册", section="3.5. 自定义算子"),
            _ref("AI 推理用户手册", "MacaRT 工具链与推理框架总览", "official",
                 title="沐曦通用GPU AI推理用户手册"),
        ),
    ),
)


def validate_topics() -> None:
    """Raise ``ValueError`` when the curated map is inconsistent."""

    seen: set[str] = set()
    for topic in TOPICS:
        if not topic.slug or topic.slug in seen:
            raise ValueError(f"duplicate or empty topic slug: {topic.slug!r}")
        seen.add(topic.slug)
        if not topic.refs:
            raise ValueError(f"topic {topic.slug} has no references")
        for ref in topic.refs:
            if ref.source not in SOURCES:
                raise ValueError(f"unknown source {ref.source!r} in topic {topic.slug}")
            if ref.source == "github" and not ref.repository:
                raise ValueError(f"github reference without repository in topic {topic.slug}")
            if ref.source == "github" and not (ref.path or ref.path_prefix):
                raise ValueError(f"github reference without path in topic {topic.slug}")
            if ref.source == "official" and not ref.title:
                raise ValueError(f"official reference without title in topic {topic.slug}")
            if not ref.label.strip():
                raise ValueError(f"empty reference label in topic {topic.slug}")


def topic_slugs() -> tuple[str, ...]:
    return tuple(topic.slug for topic in TOPICS)


def get_topic(identifier: str) -> OperatorTopic | None:
    """Resolve a topic by slug, or by a case-insensitive title/stage keyword."""

    wanted = identifier.strip()
    if not wanted:
        return None
    for topic in TOPICS:
        if topic.slug == wanted:
            return topic
    folded = wanted.casefold()
    matches = [
        topic
        for topic in TOPICS
        if folded in topic.title.casefold()
        or folded in topic.summary.casefold()
        or folded in topic.stage.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def catalogue() -> list[dict[str, Any]]:
    """Return the bounded topic list used by ``get_operator_guide()``."""

    return [
        {
            "slug": topic.slug,
            "title": topic.title,
            "stage": topic.stage,
            "summary": topic.summary,
            "steps": list(topic.steps),
            "reference_count": len(topic.refs),
        }
        for topic in TOPICS
    ]


def resolve_topic(store: Any, identifier: str, limit: int = 60) -> dict[str, Any] | None:
    """Resolve a topic's references against the local index.

    ``store`` only needs ``find_documents``.  Returns ``None`` when the
    identifier does not select exactly one topic.
    """

    topic = get_topic(identifier)
    if topic is None:
        return None
    references: list[dict[str, Any]] = []
    read_order: list[str] = []
    missing: list[str] = []
    for ref in topic.refs:
        matches = store.find_documents(
            source=ref.source,
            repository=ref.repository or None,
            path=ref.path or None,
            path_prefix=ref.path_prefix or None,
            title=ref.title or None,
            limit=limit,
        )
        if not matches:
            missing.append(ref.label)
        for match in matches:
            if match["id"] not in read_order:
                read_order.append(match["id"])
        references.append(
            {
                "label": ref.label,
                "note": ref.note,
                "source": ref.source,
                "repository": ref.repository,
                "path": ref.path,
                "path_prefix": ref.path_prefix,
                "title": ref.title,
                "section": ref.section,
                "missing": not matches,
                "matches": matches,
            }
        )
    return {
        "topic": {
            "slug": topic.slug,
            "title": topic.title,
            "stage": topic.stage,
            "summary": topic.summary,
            "steps": list(topic.steps),
        },
        "references": references,
        "read_order": read_order,
        "missing_references": missing,
        "usage": _USAGE,
    }


def render_markdown() -> str:
    """Render the curated map as Markdown (``docs/OPERATOR_DEV.md``)."""

    lines = [
        "# 沐曦算子开发索引（metax-docs-mcp）",
        "",
        "本文件由 `scripts/render_operator_guide.py` 从 `src/metax_docs_mcp/opdev.py` 生成，"
        "请勿手工编辑。",
        "",
        "用途：把「算子开发」拆成可检索的阶段，每个阶段指向索引中真实存在的文档或代码文件。",
        "查询入口：MCP 工具 `get_operator_guide`，或 CLI `metax-docs-mcp opdev [TOPIC]`。",
        "引用按仓库路径或官方手册标题书写，不写死 commit；刷新索引后仍能解析到最新快照。",
        "",
        "## 主题一览",
        "",
        "| slug | 阶段 | 主题 | 引用数 |",
        "| --- | --- | --- | --- |",
    ]
    for topic in TOPICS:
        lines.append(
            f"| `{topic.slug}` | {topic.stage} | {topic.title} | {len(topic.refs)} |"
        )
    for topic in TOPICS:
        lines += ["", f"## {topic.title}（`{topic.slug}`）", "", f"**阶段**：{topic.stage}", "",
                  topic.summary, "", "**可执行步骤**：", ""]
        lines += [f"{index}. {step}" for index, step in enumerate(topic.steps, start=1)]
        lines += ["", "**参考**：", ""]
        for ref in topic.refs:
            if ref.source == "official":
                where = f"official: {ref.title}"
                if ref.section:
                    where += f" / {ref.section}"
            elif ref.source == "docker":
                where = "docker"
            else:
                where = f"{ref.repository}: {ref.path or ref.path_prefix + '*'} → {ref.note}"
            lines.append(f"- {ref.label}（{where}）")
    lines.append("")
    return "\n".join(lines)


def usage() -> str:
    """Return the short usage hint shared by the MCP tool and the CLI."""

    return _USAGE


validate_topics()


__all__ = [
    "OpReference",
    "OperatorTopic",
    "SOURCES",
    "TOPICS",
    "catalogue",
    "get_topic",
    "render_markdown",
    "resolve_topic",
    "topic_slugs",
    "usage",
    "validate_topics",
]
