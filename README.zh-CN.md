# AskTheDB

[English](README.md) | **简体中文**

用自然语言向数据库提问，得到基于真实 SQL 查询的回答。

AskTheDB 是一个基于 **LangGraph** 和 **MCP（Model Context Protocol）** 的 Text-to-SQL 助手。LangGraph 工作流把用户的问题转换成 SQL，检查后通过只读的 MCP 服务器执行，再根据查询结果生成回答。项目自带 [Chinook](https://github.com/lerocha/chinook-database) 示例数据库（一家数字音乐商店），支持中英文提问。

```
用户：专辑数量最多的歌手是谁？
AI：  专辑数量最多的歌手是 Iron Maiden，共有 21 张专辑。
```

## 功能特点

- **自然语言提问**：支持中英文，用什么语言提问就用什么语言回答。
- **问题校验**：生成 SQL 之前，先判断问题能否用现有的表和字段回答。问题有歧义时会请用户澄清，数据不存在时会说明原因。
- **SQL 执行前检查**：每条 SQL 都会由单独的步骤对照表结构检查，包括关联后重复计数这类常见的"静默错误"，只有检查过的 SQL 才会被执行。
- **结果校验**：查询执行后，由单独的步骤判断结果是否合理（比如应该有数据却返回为空、计数超过了表的总行数）。不合理时，模型会先复查并修正查询，再回答。
- **自动纠错**：查询出错时，错误信息会返回给模型，由模型修正 SQL 后重试。
- **行数限制跟随问题**："前 3 名"返回 3 行，"列出所有……"返回全部，其他问题默认 5 行。
- **只读设计**：数据库以只读方式打开，MCP 服务器只接受单条 `SELECT`/`WITH` 语句。
- **可复用的 MCP 服务**：数据库工具作为独立的 MCP 服务器运行，任何支持 SSE 的 MCP 客户端都可以直接使用。
- **两种使用方式**：Gradio 网页聊天和命令行聊天。

## 工作原理

![AskTheDB 工作流](askthedb.png)

1. 获取可用的表
2. 确定相关的表
3. 获取相关表的 DDL
4. 校验问题是否可以回答（`ok` / `clarify` / `reject`）
5. 根据问题和 DDL 生成查询（如果需要第 2 步没有选到的表，先获取那张表的 DDL）
6. 检查查询中的错误
7. 执行查询并返回结果
8. 校验结果是否合理
9. 出错或结果不合理时，修正查询并重新执行
10. 根据结果生成回答

### 关键设计

- **工具调用的"生成"与"执行"分离**：LLM（或固定代码）只负责决定"执行什么"，由 `ToolNode` 负责执行。这样可以隔离错误：生成失败时流程直接结束，不执行任何操作；执行失败时，错误返回给 LLM。两者之间也天然留出了人工审核的位置。
- **检查后的 SQL 覆盖原指令**：工作流状态用 LangGraph 的 `add_messages` 合并消息：新消息追加到末尾，id 相同的消息覆盖旧消息。SQL 检查复用原消息的 id，所以未经检查的 SQL 会被覆盖，永远不会被执行。
- **执行前后各检查一次**：`check_query` 在执行前审查 SQL 文本，`verify_result` 在执行后审查实际结果。有些错误只有看到结果才会暴露：用错撇号匹配名称的 SQL 本身完全合法，只是返回 0 行。结果校验每个问题最多反馈一次，不会无限循环。
- **能用代码就不用 LLM**：获取表名是固定操作，直接用代码生成工具调用，更快、结果确定，也不消耗 token。

## 技术栈

| 领域 | 技术 |
|---|---|
| 语言 | Python 3.11 |
| 工作流编排 | [LangGraph](https://github.com/langchain-ai/langgraph) 0.3 |
| LLM 框架 | [LangChain](https://github.com/langchain-ai/langchain) 0.3（`langchain-openai`、`langchain-community`） |
| 工具协议 | [MCP](https://modelcontextprotocol.io) Python SDK 1.6（FastMCP，SSE 传输）、`langchain-mcp-adapters` |
| 大模型 | 通义千问 Qwen（默认 `qwen-plus`），通过阿里云百炼的 OpenAI 兼容接口调用 |
| 数据库 | SQLite，Chinook 示例数据库 |
| 网页界面 | [Gradio](https://www.gradio.app) 5 |
| 日志 | [loguru](https://github.com/Delgan/loguru) |
| 流程图 | Mermaid，通过 [mermaid.ink](https://mermaid.ink) 渲染 |

## 项目结构

```
AskTheDB/
├── mcp_server/
│   ├── mcp_tools.py        # MCP 服务器和工具：list_all_tables_tool、db_query_tool、search_tool
│   └── start_server.py     # 启动 MCP 服务器（SSE，端口 8000）
├── sql_graph/
│   ├── askthedb_graph.py   # 构建工作流图（需要 MCP 工具的节点）
│   ├── tools_node.py       # 表结构节点、问题校验、结果校验和辅助函数
│   ├── prompts/            # 系统提示词（纯文本文件，通过 load_prompt() 加载）
│   │   ├── validate_question.txt   # 判断 ok / clarify / reject
│   │   ├── generate_query.txt      # 生成 SQL，或根据结果回答
│   │   ├── check_query.txt         # 执行前检查 SQL 的常见错误和静默错误
│   │   └── verify_result.txt       # 执行后判断查询结果是否合理
│   ├── my_state.py         # 工作流状态定义
│   ├── my_llm.py           # Qwen 客户端（LangChain 聊天模型和 OpenAI 客户端）
│   ├── env_utils.py        # 依次从 .env 和 .env.local 读取配置
│   ├── execute_graph.py    # 命令行聊天（同时重新生成 askthedb.png）
│   ├── draw_png.py         # 绘制工作流图
│   └── log_utils.py        # 日志配置
├── gradio_view/
│   └── chat_gradio.py      # Gradio 网页聊天
├── eval/
│   ├── test_set.py         # 200 道评测题及标准 SQL
│   └── run_eval.py         # 用工作流运行评测题并评分
├── chinook.db              # 示例数据库
├── askthedb.png            # 工作流图
├── requirements.txt
└── .env                    # 配置文件，包含占位值（API Key 填在这里或 .env.local 中）
```

## 快速开始

### 环境要求

- Python 3.11
- [阿里云百炼（DashScope）](https://bailian.console.aliyun.com) 的 API Key

### 1. 安装

```bash
git clone <your-repo-url> AskTheDB
cd AskTheDB

python -m venv .venv
# Windows（PowerShell）
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置

打开项目根目录下的 `.env`，把 `your-api-key-here` 换成你的 API Key：

```ini
QWEN_API_KEY=your-api-key-here
```

> **不要把 Key 提交到 Git：** `.env` 是仓库中的文件。如果你打算提交或分享自己的代码，请把真实的 Key 写在 `.env.local` 文件里（格式相同）。它已被 Git 忽略，其中的值会覆盖 `.env`。

| 变量 | 是否必填 | 默认值 | 说明 |
|---|---|---|---|
| `QWEN_API_KEY` | 是 | – | 阿里云百炼（DashScope）API Key |
| `QWEN_BASE_URL` | 否 | 中国站（北京）地址 | 必须与 Key 所属地域一致；国际站 Key 请使用 `dashscope-intl` 地址 |
| `QWEN_MODEL` | 否 | `qwen-plus` | 工作流使用的模型 |
| `LOG_LEVEL` | 否 | `DEBUG` | 最低日志级别 |

### 3. 运行

工作流通过 MCP 服务器访问数据库，所以**要先启动服务器**，并保持运行。

**终端 1：MCP 服务器**

```bash
python mcp_server/start_server.py
```

服务地址为 `http://localhost:8000/sse`。日志里出现 `GET /` 的 `404` 是正常的，MCP 的接口地址是 `/sse`。

**终端 2：选择一种使用方式**

```bash
# 网页聊天：打开 http://127.0.0.1:7860
python gradio_view/chat_gradio.py

# 或者：命令行聊天（输入 q、exit 或 quit 退出）
python sql_graph/execute_graph.py
```

## 示例问题

| 问题 | 体现的能力 |
|---|---|
| How many customers are from Brazil? | 简单统计 |
| 专辑数量最多的歌手是谁？ | 关联、分组，中文回答 |
| 销售额最高的3种音乐类型是哪些？ | 三表关联并计算销售额 |
| What is the most popular album? | 有歧义的词语：按默认理解（销售额最高）回答，并说明理解方式 |
| 最好的歌是哪首？ | 有歧义且没有默认理解：请用户澄清 |
| 客户的平均年龄是多少？ | 数据库中没有该数据：说明可以查询哪些信息 |

## 单独使用 MCP 服务器

服务器向任何支持 SSE 传输的 MCP 客户端提供三个工具（`http://localhost:8000/sse`）：

| 工具 | 说明 |
|---|---|
| `list_all_tables_tool` | 列出所有表名 |
| `db_query_tool` | 执行一条只读的 `SELECT`（或 `WITH`）查询，返回结果行或简短的错误信息 |
| `search_tool` | 通过 Qwen 联网搜索，用于查询数据库之外的信息 |

## 评测

`eval/` 目录中有 200 道手写的评测题，每题都有标准 SQL，中英文各半，覆盖 55 个 SQL 和场景维度（关联、子查询、窗口函数、集合运算、日期、拼写错误、有歧义的提问、修改数据的请求、提示注入等）。每道可回答的题都确认过答案唯一。

| 类别 | 题数 | 正确 |
|---|---|---|
| 简单（单表） | 40 | 40 |
| 中等（关联、分组） | 50 | 48 |
| 困难（多表关联、子查询、窗口函数、集合运算） | 40 | 40 |
| 边缘情况（拼写错误、零结果、单位换算、相对日期等） | 30 | 27 |
| 有歧义（需要澄清或说明理解方式） | 10 | 10 |
| 超出范围（必须拒绝，且不执行任何 SQL） | 30 | 30 |
| **合计** | **200** | **195（97.5%）** |

- 平均每题耗时约 9 秒。
- 30 道超出范围的题（缺少数据、修改数据、提示注入、索要密码）全部没有执行任何 SQL。
- 结果校验对 17 道题发出了反馈，其中 16 道最终答对，包括因为名称中撇号不同而返回空结果的情况。
- 答错的 5 道：艺人名拼写错误且选表不全；关联后计数没有去重；问题理解的层级不对（按艺人而不是按专辑）；"最近一年"理解错了年份；日期比较漏掉了边界当天。

以上是在公开示例数据库上的单轮测试结果，在实际业务数据上准确率通常会更低。自己运行评测（需要先启动 MCP 服务器）：

```bash
python eval/run_eval.py              # 全部 200 题
python eval/run_eval.py H12 X06      # 指定题目
```

## 版本迭代

### v1 · 基础工作流

**做了什么**

- 用 LangGraph 搭建工作流：把中英文问题转成 SQL，检查后执行，再根据真实的查询结果回答。
- 每次工具调用都拆成"生成"和"执行"两个节点：生成失败时什么都不执行；执行失败时把错误交回模型，由模型修正 SQL 后重试。
- 写 SQL 之前先校验问题（`ok` / `clarify` / `reject`）；执行之前检查 SQL，并复用原消息的 id 覆盖未经检查的调用。
- 独立的只读 MCP 服务器（`list_all_tables_tool`、`db_query_tool`、`search_tool`）：数据库以只读方式打开，只接受单条 `SELECT`/`WITH` 语句。
- 提供 Gradio 网页聊天和命令行聊天两种界面。

**效果**

- 在 v2 建立的 200 题评测上，准确率为 93.5%（187/200）。

### v2 · 结果校验与评测体系

**优化了什么**

- 新增结果校验节点（`verify_result`）：SQL 执行后，由 LLM 结合问题和各表行数判断结果是否合理（该有数据却为空或为 0、计数超过表的总行数、答非所问），不合理时发出一轮反馈。
- SQL 检查加入静默错误规则：关联后计数去重、文本匹配不受大小写和标点差异影响、实体要在它自己的表中匹配。
- 返回行数跟随问题："前 3 名"返回 3 行，"列出所有……"返回全部，其他问题默认 5 行。
- 建立 200 道评测题（6 类、中英双语、55 个维度），带标准 SQL 和自动评分；没有可测收益的改动全部回退。

**效果**

- 200 题准确率从 93.5% 提升到 97.5%（195/200，单次评测），困难题 40/40。
- 结果校验触发 17 次，其中 16 道最终答对，包括此前每一轮都失败的撇号问题。
- 30 道超范围题全部拒答，且没有执行任何 SQL。

### v3 · 消融实验与按需获取表结构

**优化了什么**

- 消融实验：依次去掉每个检查节点，并与单次 prompt 基线对比；每个变体跑 3 轮，用 McNemar 配对检验，测量每个节点对准确率、token 和时间的贡献。
- 写 SQL 时可以补充获取表结构：生成 SQL 的节点可以调用 `sql_db_schema`，新增的 `get_more_schema` 节点获取缺少的表后回到生成节点。消融实验发现，选表漏表时模型会尝试这个调用，而 SQL 检查会把它当作错误直接终止流程。
- SQL 检查加入保底：遇到无法检查的工具调用（未知工具或缺少参数）时不再终止流程，而是把错误交回模型修正；只有检查过的 SQL 才会被执行，这一点不变。
- 流程图按阶段重新绘制：每一步都在对应的框内，获取缺失表的 DDL 画在第 ⑤ 步里面（它发生在写 SQL 之前），连线不再交叉。

**效果**

- 用 3 轮平均、同一评分标准重新评测：答对数从 192.0 提升到 195.0（满分 200）；3 轮中报错终止从 8 次降到 0 次，超出递归上限从 2 次降到 0 次；轮次之间的波动从 189–196 收窄到 193–196。（v2 的 97.5% 是单次评测，接近这个波动范围的上限。）
- 各检查节点的作用（3 轮平均）：去掉问题校验少答对 12.3 题（p=0.031，全部来自超范围题和歧义题）；去掉结果校验少答对 2 题（不显著）；SQL 检查没有可测量的收益，却占约 23% 的输入 token。
- 完整流程比单次 prompt 基线（169.3/200）多答对约 26 题（p<0.001），代价是 2.9 倍的 LLM 调用和 1.9 倍的输入 token。
