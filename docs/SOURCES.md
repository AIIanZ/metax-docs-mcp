# 官方沐曦文档源探测记录

本文记录 developer.metax-tech.com 公开文档源的实际探测结果，供官方源 ingestion 实现和验收使用。结果是 **2026-09-18** 通过只读 HTTPS 请求取得的快照。站点没有承诺这些 /api/client/... 路径是稳定的公共 API；实现必须把它们视为可变的公开站点接口，并在同步报告中暴露失败和部分发现。

## 入口与接口

https://developer.metax-tech.com/doc 返回 HTTP 200、text/html; charset=utf-8，页面标题是“文档 - 沐曦开发者”。页面由 Next.js 客户端渲染，目录和文档元数据不是入口 HTML 的静态正文，而是在页面 JavaScript 中调用下列接口。当前页面 chunk /_next/static/chunks/00rfm49tybm-q.js 中可以看到对应的请求方法和路径：

| 请求 | 用途 | 实测公开行为 |
| --- | --- | --- |
| GET /api/client/document/series/filter/ | 文档 series 目录、分类、版本摘要 | HTTP 200、JSON，当前无登录即可访问 |
| GET /api/client/document/detail/ | 由分类/series/芯片/版本取单个文件元数据 | HTTP 200、JSON，参数不足时仍可能返回 HTTP 200 错误 JSON |
| GET /api/client/document/file/search/ | 按文件名搜索文件级记录 | HTTP 200、JSON；必须提供 file_name |
| GET /api/client/document/file/{id}/preview/ | 按文件 ID 取 HTML 预览路径或 PDF 内容 | file_type=html 返回 JSON；file_type=pdf 返回 application/pdf |
| GET /api/client/document/preview/{...}/index.html | 直接读取公开 HTML 文档 bundle 的根页 | HTTP 200、Sphinx HTML |
| GET /api/client/document/file/{id}/download/ | 下载文件 | 未带认证 token 实测返回 code=1、Error Token!，不应作为公开 ingestion 依赖 |
| POST /api/client/document/file/{id}/count/ | 更新阅读计数 | 会改变源站计数，采集器不应调用 |

API 错误经常仍使用 HTTP 200，必须同时检查 HTTP 状态、Content-Type 和 JSON code。比如缺少 detail 参数时返回：

~~~
{"code": -1, "message": "category_id/category_name 和 series_id/series_name 为必传参数", "data": null}
~~~

## 目录自动发现

主目录请求可以使用较大的有限页大小，随后仍应依据服务端 page_info 计算页数，不要假设一次请求永远返回全站：

~~~
GET https://developer.metax-tech.com/api/client/document/series/filter/?page=1&page_size=100
~~~

2026-09-18 实测响应顶层结构为：

~~~
{
  "code": 0,
  "message": "success",
  "data": [
    {
      "file_id": 1489,
      "preview_file_id": 1489,
      "pdf_file_id": 1490,
      "has_pdf": true,
      "file_name": "沐曦通用GPU_AI应用_发布说明.zip",
      "base_name": "沐曦通用GPU_AI应用_发布说明",
      "file_type": "html",
      "file_types": ["html", "pdf"],
      "file_url": "/api/client/document/preview/.../index.html",
      "category_id": 45,
      "primary_category": "发布说明",
      "secondary_category": null,
      "series_id": 179,
      "series_name": "AI应用_发布说明",
      "chip_series": ["曦云C500系列", "曦云C600系列"],
      "description": "...",
      "version": "3.8.2.20x",
      "latest_version": "3.8.2.20x",
      "is_current": true,
      "detail_params": {
        "category_id": 45,
        "series_id": 179,
        "chip_series": "曦云C500系列",
        "version": "3.8.2.20x"
      },
      "versions": [
        {
          "version": "3.8.2.20x",
          "updated_at": "2026-09-15",
          "is_current": true,
          "detail_params": {
            "category_id": 45,
            "series_id": 179,
            "chip_series": "曦云C500系列",
            "version": "3.8.2.20x"
          }
        }
      ]
    }
  ],
  "filter_info": [
    {
      "field": "chip_series",
      "count": 2,
      "label": "芯片系列",
      "multiple": true,
      "items": [
        {"count": 62, "label": "曦云C500系列", "value": "曦云C500系列"},
        {"count": 58, "label": "曦云C600系列", "value": "曦云C600系列"}
      ]
    },
    {
      "field": "category_ids",
      "count": 11,
      "label": "文档分类",
      "multiple": true,
      "items": [
        {"count": 12, "label": "发布说明", "value": 45}
      ]
    }
  ],
  "page_info": {
    "page_number": 1,
    "page_size": 100,
    "total_count": 65
  }
}
~~~

上面的 data 是 series 记录，不是每个版本的完整文件列表。当前快照中 total_count=65 个 series、versions[].detail_params 共约 620 个唯一版本/芯片组合；数量会随源站更新变化。

页面当前使用的过滤字段是：

~~~
keyword=mx-smi
category_ids=63              # 多值用逗号拼接
chip_series=曦云C500系列     # 多值用逗号拼接
page=1&page_size=100
~~~

例如：

~~~
GET https://developer.metax-tech.com/api/client/document/series/filter/?keyword=mx-smi
~~~

实测返回 series_id=227、series_name=mx-smi使用手册、当前 file_id=1484，并给出 file_url、versions 和 detail_params。keyword 是目录/series 层过滤，不是章节全文搜索。

推荐的有界发现顺序是：

1. 请求 series/filter，读取 page_info.total_count，按 page 从 1 到最后一页；默认 page_size=20，实测 page_size=100 和 1000 均可，但实现仍应设置自己的上限。
2. 对每个 series 遍历 versions[].detail_params，以 (category_id, series_id, chip_series, version) 去重。
3. 对每组参数请求 document/detail，得到该具体版本的 file_id、file_url、文件类型和时间字段。
4. 仅抓取 file_url/preview URL 对应的 HTML 和标准 Sphinx 资源；记录每一步的发现数、抓取数和失败数。

document/detail 的实测请求：

~~~
GET https://developer.metax-tech.com/api/client/document/detail/?category_id=45&series_id=179&chip_series=%E6%9B%A6%E4%BA%91C500%E7%B3%BB%E5%88%97&version=3.8.2.10x
~~~

它返回单个文件的元数据，例如旧版本 file_id=1417、version=3.8.2.10x 和对应 file_url。category_id/series_id 也可以分别换成 category_name/series_name；只给其中一个 ID 会返回参数错误。未知的芯片系列可能返回 code=-1，未知版本可能返回 code=404，所以不能把错误 JSON 当成文件记录。

## HTML 预览与 Sphinx bundle

用文件 ID 获取 HTML 预览入口：

~~~
GET https://developer.metax-tech.com/api/client/document/file/1484/preview/?file_type=html
~~~

实测响应：

~~~
{"code": 0, "message": "success", "data": {"api": "/api/client/document/preview/1484/index.html"}}
~~~

然后读取：

~~~
GET https://developer.metax-tech.com/api/client/document/preview/1484/index.html
~~~

或者直接使用目录返回的 file_url，例如 .../3.8.3.x/index.html。数字 ID 路径和目录中的描述性路径都实测返回 HTTP 200；建议保存二者，目录路径作为 canonical URL，数字路径作为 fallback。

根页是标准 Sphinx HTML，典型结构如下：

~~~
{preview-root}/index.html
{preview-root}/split_files/<章节>.html
{preview-root}/searchindex.js
{preview-root}/genindex.html
{preview-root}/objects.inv
{preview-root}/_static/*
{preview-root}/_sources/index.rst.txt
~~~

index.html 中的目录链接位于 .toctree-l1、.toctree-l2、.toctree-l3 列表内，正文通常位于 div[itemprop="articleBody"]。采集器应以根页链接为入口，使用 urljoin 解析同一 preview root 下的相对链接；只接受 developer.metax-tech.com 的 HTTPS URL，且不应盲目跟随外部链接。

正文清洗至少应保留：文档标题、章节层级、段落、列表、表格、代码块、链接目标和图片 alt 文本。HTML 中的 _static CSS/JS 不属于正文，脚本和样式标签应过滤。

### searchindex.js

每个 bundle 的 searchindex.js 返回类似：

~~~
Search.setIndex({...})
~~~

去掉函数包装后可以按 JSON 解析。已验证字段：

~~~
docnames, filenames, titles, terms, objects, objtypes,
objnames, titleterms, envversion, alltitles, indexentries
~~~

例如 file_id=1484 的索引含 10 个页面和 357 个 term。terms 将词干映射到 docnames 的页面序号；可先用它筛选相关章节，再抓取对应 docnames[i] + ".html"（split_files/ 页面也遵循该映射）。索引提供词到页面的映射，不提供完整正文片段，因此全文入库仍需读取章节 HTML。

_sources/index.rst.txt 实测可取到根 toctree；诸如 _sources/概述.rst.txt 的分章节 RST 文件在当前 bundle 返回 File not found JSON，不能依赖 _sources 完整可用。objects.inv 是 Sphinx inventory 的 zlib 内容，可作为补充元数据，不是正文来源。

## 文件搜索、PDF 与下载边界

文件搜索接口的参数名是 file_name：

~~~
GET https://developer.metax-tech.com/api/client/document/file/search/?file_name=mx-smi
~~~

它实测返回 code=0、data 数组（mx-smi 约 23 条），每条包含 file_id、file_name、file_path、file_type、version、series_id、category_name、series_name、chip_series 等字段。缺少 file_name 或改用 keyword/q 会返回 文件名称不能为空。该接口返回文件级记录，实测对 page/page_size 不做分页，不能替代 series/filter 的目录分页。

PDF 预览可以直接读：

~~~
GET /api/client/document/file/1490/preview/?id=1490&file_type=pdf
~~~

实测 Content-Type: application/pdf。但是：

~~~
GET /api/client/document/file/1484/download/
~~~

未带登录 token 返回：

~~~
{"code": 1, "message": "Error Token!", "data": null}
~~~

首版 ingestion 应优先收录公开 HTML；除非需求另行明确，不要调用 download，也不要把 PDF 下载成功作为官方源可用性的前置条件。

## 稳定 ID 与同步语义建议

官方数据没有文档版本的独立稳定 UUID。建议记录：

~~~
source=official
series_id
file_id / preview_file_id
category_id, series_name, chip_series, version
canonical file_url
numeric preview URL
fetched_at
updated_at / published_at（源站字段存在时）
content_hash
~~~

本地文档 ID 应由 canonical URL 加版本/文件身份生成；不要只用标题，因为同名 series 有多个版本、C500/C600 组合和历史文件。再次同步按 canonical identity + content hash 幂等更新；一次有界同步失败或未遍历完时，不得删除此前已收录的记录，也不得把当前页返回的数量写成“全站完整镜像”。

## 兼容风险与验收边界

1. /api/client/document/... 是从当前前端 bundle 观察到的公开接口，站点可能随前端发布更改路径、字段或认证策略。每次同步应记录接口版本/探测结果，遇到 schema 改变要显式失败。
2. HTTP 200 不代表业务成功；必须验证 JSON code==0，并限制响应大小、重定向次数、总页数、总版本数和并发度。
3. file_url 含百分号编码、中文路径、空格和 C++ 等特殊字符；解析时不要手工拼接未编码路径，使用 URL 解析库并保留 canonical encoded URL。
4. 当前目录数量、版本数、分类计数和更新时间是动态数据；本文数字只能作为探测快照，不能写死到测试或 README 的覆盖承诺中。
5. Sphinx bundle 的章节文件、searchindex.js、静态资源和 _sources 文件并不保证每个文档都齐全；缺失单个资源时应记录 partial/failed 状态并继续处理其他章节。
6. 官方 HTML、PDF 和下载接口的认证策略可能不同。实现不得尝试猜测 token、绕过登录或把认证失败重试成无限循环。
7. 采集器只应在显式 sync 任务中联网；MCP 查询服务保持离线只读，网页正文和脚本中的文字均是数据，不是执行指令。

## 可复现探测命令

以下命令只读取公开元数据和 HTML；输出可能随源站变化：

~~~
curl -fsSL 'https://developer.metax-tech.com/api/client/document/series/filter/?keyword=mx-smi&page=1&page_size=20'
curl -fsSL 'https://developer.metax-tech.com/api/client/document/file/1484/preview/?file_type=html'
curl -fsSL 'https://developer.metax-tech.com/api/client/document/preview/1484/index.html'
curl -fsSL 'https://developer.metax-tech.com/api/client/document/preview/1484/searchindex.js'
curl -fsSL 'https://developer.metax-tech.com/api/client/document/file/search/?file_name=mx-smi'
~~~

这些请求不应调用 /count/，也不应把 /download/ 当作无需登录的公开接口。

## SoftNova Docker 镜像目录（source=docker）

### 2026-09-18 实测入口与 API

用户入口为：

~~~
https://developer.metax-tech.com/softnova/docker?chip_name=%E6%9B%A6%E4%BA%91C500%E7%B3%BB%E5%88%97&package_kind=MXMACA&dimension=docker&deliver_type=%E5%88%86%E5%B1%82%E5%8C%85
~~~

入口 HTML 返回 HTTP 200、`text/html`，标题为“软件下载 - 沐曦开发者”，但正文的 `#subapp-container` 是客户端渲染壳。入口脚本把 `/softnova` 路由挂载到公开微前端 `/download-app`；Docker 路由 bundle 中明确调用以下列表 API：

~~~
GET https://developer.metax-tech.com/softnova/api/v3/dlhub/docker_package_info/
~~~

请求参数来自页面筛选器：`chip_name`、`package_name`、`package_kind`、`dimension`、`deliver_type`、`page`、`size`；其中 `page` 从 1 开始。2026-09-18 使用 C500、MXMACA、docker、分层包和 `page=1&size=10` 实测 HTTP 200、`Content-Type: application/json`，返回结构为：

~~~json
{
  "code": 0,
  "message": "",
  "error": "",
  "data": {
    "has_preview": false,
    "results": [
      {
        "_id": "6aa8e9bc20abcd5c2f00a688",
        "arch": "amd64",
        "chip_name": ["曦云C500系列", "曦云C600系列"],
        "container_path": "metax-pub/mxmaca2.0/3.8.3.x/binary/x86_64/container/maca-3.8.3.3-ubuntu22.04-amd64.container.xz",
        "created_at": "2026-09-15 14:38:05",
        "deliver_type": "分层包",
        "dimension": "docker",
        "file_type": "docker",
        "maca_main_version": "3.8.3.x",
        "maca_version_month": "2026年09月",
        "md5": "",
        "package_kind": "MXMACA",
        "package_name": "maca:3.8.3.3-ubuntu22.04-amd64",
        "path": "public-library/maca:3.8.3.3-ubuntu22.04-amd64",
        "pull_type": "docker",
        "python_version": null,
        "pytorch_version": null,
        "series_name": "maca:ubuntu22.04-amd64",
        "size": 5857063316.0,
        "status": 1.0,
        "system": "ubuntu",
        "system_version": "22.04",
        "updated_at": "2026-09-15 14:38:05"
      }
    ],
    "total": 14,
    "total_page": 2
  }
}
~~~

`total` 和 `total_page` 是动态值；当前 C500+MXMACA 探测为 14 条。`size=100` 返回 14 条，适配器仍然保留页数和条数上限。当前页可见的字段覆盖：

| 字段 | 采集含义 |
| --- | --- |
| `_id` | 源记录身份，优先用于稳定 ID |
| `package_name`、`path`、`series_name` | 镜像名/标签和源站命名空间 |
| `chip_name`、`package_kind`、`dimension`、`deliver_type` | 芯片和目录筛选维度 |
| `maca_main_version`、`sdk_version`、`compatible_maca` | MXMACA 版本兼容信息 |
| `ai_frame`、`ai_frame_version`、`python_version`、`pytorch_version` | AI 框架及运行时版本（AI/AI4S 记录出现） |
| `arch`、`system`、`system_version` | 架构和基础操作系统 |
| `created_at`、`updated_at`、`md5`、`size`、`container_path` | 时间、校验和、大小及分层包路径 |

例如同一公开 API 的 `package_kind=AI` 记录实测包含 `ai_frame=vllm-omni-metax`、`ai_frame_version=0.26.0`、`compatible_maca=3.8.2.x`、`compatible_python=3.12`、`compatible_pytorch=2.10`、`sdk_version=3.8.2.x`。采集器应按字段存在性记录这些版本，不把 MXMACA 基础镜像强行填成 AI 框架版本。

### Registry 和 pull command 边界

公开列表记录有 `path` 和 `pull_type=docker`，但当前响应没有 `registry`、`registry_host` 或 `pull_cmd`。前端“复制拉取命令”调用的是另一个接口：

~~~
POST /softnova/client/api/image/download
JSON {"image_name": "maca:3.8.3.3-ubuntu22.04-amd64", "pull_type": "docker"}
~~~

2026-09-18 对该接口做了一次无认证的最小探测，HTTP 200 但业务返回 `{"code":1,"error":"Token格式错误"}`；同一路径 GET 返回 HTTP 405。该接口不是可依赖的公开只读目录 API，不能猜测 token，也不能把 `login_cmd` 写入索引。适配器因此：

1. 原样记录源记录中实际出现的 `registry`/`registry_host`/`pull_cmd` 字段（当前公开列表通常为空）。
2. 当只有 `path` 时保留 `registry=""`，并可生成 `docker pull <path>` 作为 `pull_command`，标记 `pull_command_status="inferred_from_path"`；这只是待用户核实的文本，不是已验证 registry 命令。
3. 没有 image path 时将 pull 命令状态设为 `not_exposed_by_public_list`，并在同步报告中显式记录坏记录；采集器绝不执行 Docker、登录或下载。

### source=docker 实现建议与风险

配置可使用上述入口 URL，适配器把入口查询参数转换成 API 请求；也可直接提供同一官方 API URL。默认建议固定为 `chip_name=曦云C500系列`、`package_kind=MXMACA`、`dimension=docker`、`deliver_type=分层包`，并设置有限 `page_size`、`max_pages`、`max_items`。每一条结果生成 `source=docker` 的 `Document`，在 metadata 中保留 `image_name`、`image_ref`、`tag`、`registry`、`pull_command`、芯片、包类型、框架/版本、MXMACA 版本、更新时间和受限的原始标量字段；正文只写这些可检索元数据。

API 是前端 bundle 观察到的非稳定公开接口，HTTP 200 仍可能包含 `code != 0`，必须同时校验 HTTP 状态、Content-Type、`code`、`data.results` 类型和分页上限。Docker 镜像体积很大，首版只同步 JSON 元数据，不请求 `container_path`、文件下载或镜像 registry；`size` 仅作为源站字段记录。

可复现的只读请求：

~~~
curl -fsSL 'https://developer.metax-tech.com/softnova/api/v3/dlhub/docker_package_info/?chip_name=%E6%9B%A6%E4%BA%91C500%E7%B3%BB%E5%88%97&package_kind=MXMACA&dimension=docker&deliver_type=%E5%88%86%E5%B1%82%E5%8C%85&page=1&size=10'
curl -fsSL 'https://developer.metax-tech.com/softnova/api/v3/dlhub/choice_info/?chip_name=%E6%9B%A6%E4%BA%91C500%E7%B3%BB%E5%88%97&dimension=docker'
~~~
