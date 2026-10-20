# dm_nl2sql_mcp
达梦的nl2sql mcp工具
=======
独立目录，只保留达梦访问、Excel 业务描述、FAISS 检索、受限 SQL、MCP 和 LangChain 问答。

若采用人工审阅工作流，日常只需要维护 [configs/catalog.xlsx](configs/catalog.xlsx) 一个文件。工作簿同时声明：

- 要查询哪些模式和表；
- 每张表开放哪些字段；
- 表和字段的业务名称、说明、单位、枚举；
- 业务实体、字段属性映射和同义词；
- 可选的语义关系、已核验物理关联路径和业务指标。

物理类型、可空性和真实主键从达梦实时读取，不需要写进 Excel。程序会把工作簿转换成内部 JSON 和本体目录，再用数据库元数据逐表逐字段核验，最后增量更新 FAISS。

也可以不先做 Excel：`discover` 会读取达梦可见表、表注释、字段注释和主键；`catalog-bootstrap` 按表名或通配符生成描述草稿，并直接完成元数据核验、FAISS 构建和配置发布。

## 快速启动

在本目录执行：

```powershell
cd D:\Agent_kf_project\dm_nl2sql_core
uv sync --locked --extra dameng --extra qa
uv run --locked --extra dameng --extra qa dm-nl2sql catalog-sync --excel configs/catalog.xlsx
uv run --locked --extra dameng --extra qa dm-qa --trace
```

也可以先在 `.env` 中设置：

```dotenv
DESCRIPTION_EXCEL=configs/catalog.xlsx
```

之后同步命令可以简化为：

```powershell
uv run --locked --extra dameng --extra qa dm-nl2sql catalog-sync
```

`catalog-sync` 的工作顺序是：

1. 完整读取并校验 Excel；
2. 读取达梦真实元数据；
3. 核验表和字段是否存在、类型是否可查询、本体引用和主键是否有效；
4. 只对新增或修改的表、字段、实体描述调用 Embedding；
5. 重建 FAISS，并原子切换 `artifacts/index/CURRENT`；
6. 向量发布成功后，才更新 `configs/tables.json`、`configs/descriptions` 和 `configs/ontology.json`。

### 自动发现与首次建库

先查看某个模式下实际可见的表和注释，不读取业务行：

```powershell
uv run --locked --extra dameng dm-nl2sql discover `
  --schema KHFW --schema ZKTOS `
  --include "BT_*" --exclude "*_LOG" `
  --details --output artifacts/schema-inventory.json
```

确认清单后，用同一套选表条件一步生成描述、核验字段和主键、构建 FAISS：

```powershell
uv run --locked --extra dameng dm-nl2sql catalog-bootstrap `
  --schema KHFW --schema ZKTOS `
  --include "BT_*" --exclude "*_LOG"
```

已经知道具体表时，可以精确指定，避免扫描整个模式：

```powershell
uv run --locked --extra dameng dm-nl2sql catalog-bootstrap `
  --tables "KHFW.BT_CHBSD,ZKTOS.BT_YDCHBSHB"
```

首次建库默认最多选择 200 张表，超过时需要缩小条件或显式提高 `--max-tables`。已有描述文件不会被覆盖；新增表只生成达梦注释和待补充占位。建议先加 `--no-index` 审阅，再运行：

```powershell
uv run --locked --extra dameng --extra qa dm-nl2sql catalog-sync --json
```

`--json` 会忽略 `DESCRIPTION_EXCEL`，直接使用 `configs/tables.json` 和 `configs/descriptions`。通过 `--keep-existing` 可以把新表追加到当前选表范围。自动发现不会根据未核验的字段同名关系生成 JOIN。

工作簿存在重复字段、未知列、未声明表、空说明或无效枚举，或者达梦元数据和 Embedding 不可用时，旧配置和旧索引保持原状。当前模板已带入 3 张表、62 个字段：

- `ZKTOS.BT_YDCHBSHB`：48 个开放字段；
- `KHFW.BT_CHBSD`：5 个开放字段；
- `KHFW.BT_CHBHW_DD`：9 个开放字段。

同时定义了 5 个业务实体和 5 条待确认语义关系。完整本体设计、状态规则和后续路线见[本体设计文档](docs/ontology-design.md)。

## Excel 格式

### Tables

一行声明一张真实表。必填列：

| 列 | 含义 |
|---|---|
| `schema` | 达梦模式，例如 `ZKTOS` |
| `table` | 真实表名，例如 `BT_YDCHBSHB` |
| `name` | 给模型和用户看的业务名称 |
| `description` | 这张表保存什么、一行代表什么、有哪些业务边界 |

可选列：

| 列 | 含义 |
|---|---|
| `aliases` | 用户可能使用的别名，逗号、分号或换行分隔 |
| `grain` | 明确的一行业务粒度；不确定时留空 |
| `default_time` | 用户说“最近几天”时默认使用的时间字段；必须是 `Fields` 中已开放的日期时间列 |
| `entity_id` | 该表的主要业务实体，引用 `Entities` 中的实体 ID |

不要为了填 `default_time` 猜测时间口径。例如 `YDSHJ` 是预抵时间，只有业务方确认“最近几天”等于“最近几天预抵”后才能填写。

### Fields

一行开放一个字段。必填列：

| 列 | 含义 |
|---|---|
| `schema` | 字段所属模式 |
| `table` | 字段所属表 |
| `field` | 真实物理字段名，大小写必须与达梦一致 |
| `description` | 业务含义、使用场景、容易混淆的边界 |

可选列：

| 列 | 含义 |
|---|---|
| `name` | 简短中文名；留空时程序从说明首个短句提取 |
| `unit` | 已确认的单位 |
| `enum` | 枚举值；每项写成 `真实值=中文含义` |
| `null_meaning` | 空值的业务含义，例如“空表示未申报” |

`enum` 可使用换行、分号或竖线分隔，例如：

```text
Y=是
N=否
```

只有出现在 `Fields` 中的数据列会被模型看到。删除某行就是撤销该字段的查询权限；删除某张表的全部字段会阻止同步，需同时从 `Tables` 删除该表。

物理字段类型、长度、可空性和主键一律不填。主键冲突、字段不存在和字段类型变化由程序处理。

`Fields` 还包含可选的语义映射列：

| 列 | 含义 |
|---|---|
| `entity_id` | 字段描述哪个业务实体 |
| `property_id` | 字段对应实体的哪个业务属性 |
| `semantic_type` | `identifier`、`code`、`datetime`、`quantity`、`boolean` 等语义类型 |
| `semantic_role` | `planned_time`、`actual_time`、`dimension`、`measure` 等语义角色 |
| `synonyms` | 用户可能使用的其他叫法 |

`entity_id`和`property_id`必须成对填写。它们用于让模型理解“预抵时间”和“船名”分别属于哪个业务对象，不会替代真实字段引用。

### Entities

一行定义一个业务实体：

| 列 | 含义 |
|---|---|
| `entity_id` | 稳定的英文实体 ID，例如 `ship` |
| `name` | 中文名称 |
| `description` | 实体含义、覆盖范围和边界 |
| `aliases` | 用户可能使用的名称 |
| `grain` | 一个实体代表什么 |
| `status` | `verified`、`pending` 或 `conflict` |

### Relations

可选工作表，只表达业务实体之间的语义关系，不直接生成 SQL。

| 列 | 含义 |
|---|---|
| `relation_id` | 稳定的关系 ID |
| `source_entity` | 源实体 |
| `target_entity` | 目标实体 |
| `predicate` | 业务谓词，例如 `reported_for_ship` |
| `cardinality` | `many_to_one`、`one_to_one`、`one_to_many` 或 `many_to_many` |
| `description` | 业务关系和适用条件 |
| `status` | `verified`、`pending` 或 `conflict` |

`pending`和`conflict`关系只用于理解和澄清，绝不会生成 JOIN。

### JoinPaths

可选工作表，保存已经确认的物理关联路径。

| 列 | 含义 |
|---|---|
| `relation_id` | 对应的语义关系 |
| `path_id` | 稳定路径 ID |
| `schema` | 源表模式 |
| `table` | 源表名 |
| `local_field` | 源表已开放的关联字段 |
| `target` | 完整目标表名 |
| `target_field` | 目标表已开放的关联字段 |
| `description` | 路径说明 |

复合键可写多行，共享相同的`relation_id`和`path_id`。只有`verified`关系可以配置路径，而且当前版本必须且只能配置一个路径。目标字段必须完整匹配目标表真实主键。

### Metrics

可选工作表。没有明确统计口径时保持空表。

| 列 | 含义 |
|---|---|
| `schema` | 指标所属模式 |
| `table` | 指标所属表 |
| `id` | 稳定的英文指标 ID |
| `name` | 指标中文名 |
| `description` | 指标口径和边界 |
| `aggregation` | `count`、`count_distinct`、`sum`、`avg`、`min` 或 `max` |
| `field` | 已开放字段 |
| `unit` | 可选单位 |

`sum` 和 `avg` 只能指向数值字段。程序不会自动把“船舶数”解释成按船名去重，也不会自动推断装船、卸船条件下的统计口径。

## 描述质量

字段描述是模型识别字段的主要语义来源。建议使用下面的模板：

```text
中文名称。字段实际业务含义；常见用户叫法；与相邻字段的区别；已知边界。
```

例如：

```text
预计抵港时间。预到或预计抵港时间；不能用于回答实际到港记录。
```

冲突信息要明确标注“待确认”，不要编造枚举、单位或默认时间。数据库注释可以作为初始线索，但最终口径应由业务方确认。

## 增量索引

目录中的每条表、字段、实体和指标描述都会进入 FAISS；数据库业务行不会被写入向量索引。

同步时程序按 Embedding 模型指纹和描述文本哈希复用旧向量：

| Excel 变化 | 结果 |
|---|---|
| 只改一个字段说明 | 只重新 embedding 一条文本 |
| 增加字段 | 只为新增描述调用 Embedding |
| 删除字段 | 不调用 Embedding，重建 FAISS 并移除旧记录 |
| 增加实体、同义词或语义关系 | 只计算新增或变化的描述 |
| 表结构或主键变化 | 拒绝使用旧目录，要求重新同步 |
| 只增加业务数据行 | 不需要重建索引，查询时实时访问达梦 |

Embedding 失败时不会切换 `CURRENT`，旧索引仍可保持原状态。修改 Embedding 模型、地址、部署版本、维度或归一化方式后必须重新同步。

## 模型配置

连接和模型配置都在 `.env` 中，参考 `.env.example`：

- `DAMENG_*`：达梦只读账号；
- `LLM_*`：生成受限查询计划的模型；
- `QA_*`：支持工具调用的问答模型，留空时复用 `LLM_*`；
- `EMBEDDING_*`：API、Ollama 或 vLLM Embedding 服务。

生成目录默认写入`configs/tables.json`、`configs/descriptions`和`configs/ontology.json`。可通过`TABLES_PATH`、`DESCRIPTIONS_DIR`和`ONTOLOGY_PATH`调整位置。

OpenAI 兼容 API 和 vLLM 使用带 `/v1` 的根地址，`EMBEDDING_PROVIDER` 分别配置为 `openai_compatible` 或 `vllm`。Ollama 原生接口使用 `http://localhost:11434`，配置为 `ollama`。

同名模型静默更换权重时，应主动修改 `EMBEDDING_MODEL_REVISION`，否则旧向量缓存会继续复用。

## MCP 和问答

独立启动 stdio MCP：

```powershell
uv run --locked --extra dameng dm-mcp
```

对外提供三个只读工具：

- `list_datasets`：列出当前允许查询的数据集稳定 ID；
- `search_catalog`：按自然语言先找相关表、字段证据和 `dataset_id`，不读取业务数据；
- `query_data`：接收自然语言问题、可选数据集范围和安全行数上限。

首次建库也可以交给业务 MCP 在启动时完成，但范围必须固定在环境中：

```dotenv
MCP_AUTO_BOOTSTRAP=true
MCP_AUTO_SCHEMAS=KHFW,ZKTOS
MCP_AUTO_INCLUDE=BT_*
MCP_AUTO_EXCLUDE=*_LOG
MCP_AUTO_MAX_TABLES=50
```

索引有效时不会重复建库；索引缺失或描述变化时才执行自动同步。自动建库失败时服务拒绝启动。

需要让智能体参与首次发现和维护时，单独启动管理型 MCP：

```powershell
$env:MCP_ADMIN_TOKEN="replace-with-a-long-local-token"
uv run --locked --extra dameng dm-admin-mcp
```

管理工具包括 `get_catalog_status`、`discover_schema`、`inspect_table`、`bootstrap_catalog` 和 `refresh_catalog`。修改类工具必须显式传 `confirm=true`。

完整的客户端配置、工具协议、智能体提示词和自建调用示例见 [MCP 接入指南](docs/mcp-integration.md)。

LangChain 问答：

```powershell
uv run --locked --extra dameng --extra qa dm-qa --trace
```

`--trace` 会显示检索、规划、SQL 和查询状态，便于联调；不会打印问题参数或业务行。

## 兼容入口

旧的 JSON 描述仍然可用：

```powershell
uv run --locked --extra dameng dm-nl2sql catalog-init
uv run --locked --extra dameng --extra qa dm-nl2sql catalog-sync
```

采用 Excel 工作流后，`configs/tables.json` 和 `configs/descriptions` 是生成结果，日常不要同时手工修改两套来源。

## 安全边界

模型只能生成受限 `QueryPlan`，不能提交任意 SQL。程序将计划编译成参数化 SELECT，并检查：

- 表和字段必须已开放；
- 聚合和字段类型必须匹配；
- 相对时间由服务端计算；
- 只有`verified`本体关系能生成 JOIN；
- 物理路径目标键必须完整匹配真实主键；
- `pending/conflict`关系只能触发澄清；
- 禁止写操作、CTE、子查询、OR、HAVING 和自由表达式；
- 返回行数、字节数和执行时间受限。

当前版本不提供公网认证、用户级行列权限、连接池或自动热加载。一个服务进程对应一个达梦实例，可查询多个已选模式。

管理型 MCP 更新索引后，需要重启业务 MCP 才能加载新的目录快照。

## 开发验证

2026-10-20 已用当前 `configs/catalog.xlsx` 完整执行：

```text
Excel解析：3张表、62个字段、5个实体、5条待确认语义关系
达梦元数据核验：通过
首次本体同步：action=built，embedded=74，reused=3，documents=77，dimension=1024
正式模板复跑：action=reused，embedded=0，documents=77
```

复跑`embedded=0`说明工作簿内容与当前目录一致，程序没有重复调用 Embedding。自动测试覆盖工作簿导入、本体映射、待确认关系禁 JOIN、目标主键核验、非法工作簿不覆盖旧配置、内存核验后发布，以及只修改一条字段说明时只重算一条向量。

本体改造后的真实单表查询也已通过：问题要求查看一条船员申报记录的数量、换员状态和上船人数，最终返回`success`和1条记录。查询ID为`5576d500a3fb4a5f862d8e2068d91607`。当前本体中的5条跨表关系均为`pending`，所以目录中的物理JOIN数量保持为0。

完整 MCP 封装验证：

```text
自动测试：23 passed
业务 MCP：3个只读工具，真实stdio调用通过
管理 MCP：5个管理工具，真实stdio调用通过
真实达梦自动建库：1张表、7个字段、8条向量、dimension=1024
首次 action=built，复跑 action=ready，未重复调用Embedding
```

```powershell
uv run --locked --extra qa pytest -q
uv run --locked --extra qa ruff check src tests
uv run --locked --extra qa ruff format --check src tests
```

核心代码位于 `src/dm_core`：

| 文件 | 职责 |
|---|---|
| `discovery.py` | 模式/通配符选表、表清单和范围保护 |
| `bootstrap.py` | 首次发现、注释草稿、核验、建索引和发布编排 |
| `spreadsheet.py` | Excel 解析、标准化和发布前校验 |
| `catalog.py` | 业务描述、真实元数据和内部目录生成 |
| `models.py` | 实体、属性映射、语义关系和物理路径契约 |
| `index.py` | 增量向量缓存、FAISS 和原子版本切换 |
| `database.py` | 达梦元数据与只读查询 |
| `sql.py` | 受限计划编译、参数绑定和 SELECT 校验 |
| `planner.py` | 模型生成查询计划 |
| `service.py` | 查询编排、状态与审计 |
| `mcp.py` | 三个只读 MCP 工具 |
| `admin_mcp.py` | 独立管理型 MCP：发现、检查和维护索引 |
| `qa.py` | LangChain 问答客户端 |
| `cli.py` | Excel 导入、同步和查询命令 |

保留原项目 MIT 许可证。达梦连接生命周期源于 [CleanCodeStar/dm_mcp_server](https://github.com/CleanCodeStar/dm_mcp_server)，业务目录、Excel 导入、索引和查询编排由本项目实现。

214b709 (首次提交)