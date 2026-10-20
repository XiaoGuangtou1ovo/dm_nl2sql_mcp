# 达梦 NL2SQL MCP 接入指南

本工具提供两个独立 MCP 服务：

| 服务 | 入口 | 用途 |
|---|---|---|
| 业务查询 MCP | `dm-mcp` | 给最终问答智能体使用，只读查询业务数据 |
| 管理 MCP | `dm-admin-mcp` | 管理员发现元数据、生成目录、检查和刷新索引 |

两者分离，避免业务问答模型获得修改正式索引或扩大查询范围的权限。

## 1. 安装与配置

在 `dm_nl2sql_core` 目录执行：

```powershell
uv sync --locked --extra dameng --extra qa
Copy-Item .env.example .env
```

至少配置：

```dotenv
DAMENG_HOST=127.0.0.1
DAMENG_PORT=5236
DAMENG_USER=readonly_user
DAMENG_PASSWORD=your_password

EMBEDDING_PROVIDER=ollama
EMBEDDING_BASE_URL=http://localhost:11434
EMBEDDING_MODEL=your-embedding-model

LLM_BASE_URL=http://localhost:8000/v1
LLM_MODEL=your-chat-model
LLM_API_KEY=
```

达梦账号应是只读业务账号，不要使用 `SYSDBA`。

## 2. 三种启动方式

### 2.1 手动初始化后查询

首次先确认选表范围：

```powershell
uv run --locked --extra dameng dm-nl2sql discover `
  --schema KHFW --schema ZKTOS `
  --include "BT_*" --exclude "*_LOG" `
  --max-tables 50
```

然后构建目录和索引：

```powershell
uv run --locked --extra dameng dm-nl2sql catalog-bootstrap `
  --schema KHFW --schema ZKTOS `
  --include "BT_*" --exclude "*_LOG" `
  --max-tables 50
```

启动业务 MCP：

```powershell
uv run --locked --extra dameng dm-mcp --env-file .env
```

### 2.2 业务 MCP 启动时自动建库

适合固定业务域。自动范围只能来自环境配置，问答模型不能临时扩大。

```dotenv
MCP_AUTO_BOOTSTRAP=true

# 精确表名方式，二选一
MCP_AUTO_TABLES=KHFW.BT_CHBSD,ZKTOS.BT_YDCHBSHB

# 或模式加通配符方式
MCP_AUTO_SCHEMAS=KHFW,ZKTOS
MCP_AUTO_INCLUDE=BT_*,VW_*
MCP_AUTO_EXCLUDE=*_LOG,*_HIS

MCP_AUTO_KEEP_EXISTING=false
MCP_AUTO_MAX_TABLES=50
MCP_AUTO_INVENTORY_PATH=artifacts/schema-inventory.json
```

启动 `dm-mcp` 时，服务会先检查 `CURRENT` 索引。只有索引缺失、描述变化或配置过期时才执行自举；索引有效时不会重复访问数据库或调用 Embedding。

自动建库失败时业务 MCP 不会带病启动，错误写入 stderr。

### 2.3 管理型 MCP

管理 MCP 默认拒绝启动，必须配置令牌：

```dotenv
MCP_ADMIN_TOKEN=replace-with-a-long-local-token
MCP_ADMIN_MAX_TABLES=200
```

启动：

```powershell
uv run --locked --extra dameng dm-admin-mcp --env-file .env
```

管理工具：

| 工具 | 是否修改状态 | 说明 |
|---|---|---|
| `get_catalog_status` | 否 | 检查索引是否就绪、目录版本和文档数量 |
| `discover_schema` | 否 | 发现表、注释，可选字段和主键 |
| `inspect_table` | 否 | 查看单表完整元数据 |
| `bootstrap_catalog` | 是 | 生成描述、核验元数据并构建索引 |
| `refresh_catalog` | 是 | 按现有选表范围重新同步，不扩大范围 |

修改类工具的 `confirm` 必须传 `true`。调用前应先向用户展示选表范围和影响。

管理 MCP 更新 `CURRENT` 后，已经运行中的业务 MCP 仍持有旧目录快照。需要重启业务 MCP 才能加载新版本。

## 3. 通用 MCP 客户端配置

Cursor、Claude Desktop 或其他使用 `mcpServers` 配置的客户端可以使用：

```json
{
  "mcpServers": {
    "dameng-nl2sql": {
      "command": "uv",
      "args": [
        "run",
        "--locked",
        "--extra",
        "dameng",
        "dm-mcp",
        "--env-file",
        "D:/Agent_kf_project/dm_nl2sql_core/.env"
      ],
      "cwd": "D:/Agent_kf_project/dm_nl2sql_core"
    }
  }
}
```

管理 MCP 单独配置，不要和业务 MCP 混为同一个服务：

```json
{
  "mcpServers": {
    "dameng-admin": {
      "command": "uv",
      "args": [
        "run",
        "--locked",
        "--extra",
        "dameng",
        "dm-admin-mcp",
        "--env-file",
        "D:/Agent_kf_project/dm_nl2sql_core/.env"
      ],
      "cwd": "D:/Agent_kf_project/dm_nl2sql_core"
    }
  }
}
```

不同客户端的字段名可能略有差异，但核心都是“启动命令、参数、工作目录、环境文件”。

## 4. 业务 MCP 工具

### `list_datasets`

列出当前索引允许查询的数据集。适合调试或让智能体了解可用范围，不适合每次业务问题都先全量读取。

### `search_catalog`

输入自然语言问题，查找相关表、字段证据和 `dataset_id`，不访问业务数据。

请求：

```json
{
  "question": "最近预到船舶申报信息",
  "limit": 5
}
```

响应核心字段：

```json
{
  "status": "success",
  "candidates": [
    {
      "dataset_id": "table_d8376ec7d4c1c237",
      "name": "预到船舶申报",
      "physical_table": "ZKTOS.BT_YDCHBSHB",
      "matched_kind": "field",
      "matched_name": "YDSHJ",
      "evidence": "预抵时间 ETA"
    }
  ]
}
```

### `query_data`

将完整自然语言问题转换为受限查询计划、核验字段和主键、访问达梦并返回结构化结果。

请求：

```json
{
  "question": "查看最近 3 天预到时间在 2026-10-20 之后的船舶申报，最多 20 行",
  "dataset_ids": ["table_d8376ec7d4c1c237"],
  "max_rows": 20
}
```

`dataset_ids` 只能缩小服务端允许范围，不能扩大。

结果状态包括：

- `success`：查询成功，仍需检查 `returned_rows` 和 `truncated`。
- `needs_clarification`：问题或字段口径不明确。
- `denied`：数据集超出范围或权限拒绝。
- `error`：索引、模型、数据库或输入错误。

## 5. 智能体调用顺序

推荐流程：

```text
用户问题
  ↓
search_catalog(question)
  ↓
有明确候选时，将 dataset_id 传给 query_data
  ↓
候选冲突或证据不足时向用户澄清
  ↓
依据 query_data 的 status/data/time_range/truncated 回答
```

智能体系统提示词可以使用：

```text
你是只读达梦数据库问答助手。
事实查询必须调用 query_data，禁止生成或执行任意 SQL。
业务表不明确时，先调用 search_catalog 获取候选表和 dataset_id。
只有候选表与问题明确匹配时才传入 dataset_ids；候选冲突时先澄清。
error 或 denied 不能回答为“没有记录”。
最终回答必须说明来源表、时间范围和结果是否被截断。
```

不要要求模型“先列出整库所有字段”。目录检索已经负责只返回相关表和字段证据，全量上下文会降低准确性。

## 6. 自建智能体示例

现成 LangChain 问答客户端：

```powershell
uv run --locked --extra dameng --extra qa dm-qa --trace
```

单次调用：

```powershell
uv run --locked --extra dameng --extra qa dm-qa `
  --question "最近预到船舶申报有多少条" --trace
```

不接大模型、直接验证 MCP 工具链：

```powershell
uv run --locked --extra dameng --extra qa `
  python examples/mcp_client_demo.py `
  --env-file .env `
  --question "最近预到船舶申报信息"
```

示例脚本位于 `examples/mcp_client_demo.py`，它先调用 `search_catalog`，再把首选 `dataset_id` 传给 `query_data`。
仅验证目录检索、不访问大模型和业务行时可加 `--search-only`。

自建智能体时，应把 MCP 工具动态注册给模型，不要把工具调用逻辑写死在业务提示词里。工具的参数 Schema 通过 MCP `list_tools` 获取。

## 7. 安全边界

- 达梦连接必须是只读业务账号。
- 业务 MCP 不提供任意 SQL、写操作或建索引工具。
- 自动自举范围来自 `.env`，不接受问答模型临时指定新 Schema。
- 默认最多自动选择 50 张表，超过上限拒绝启动。
- 未核验的实体关系和物理外键不会自动生成 JOIN。
- 管理 MCP 单独启动，并设置本地管理员令牌。
- 业务行不会写入 FAISS，只有表、字段、实体和指标的描述会向量化。
