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
