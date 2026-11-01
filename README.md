# AskTheDB

**English** | [简体中文](README.zh-CN.md)

Ask questions about a database in plain language and get answers backed by real SQL.

AskTheDB is a text-to-SQL assistant built with **LangGraph** and the **Model Context Protocol (MCP)**. A LangGraph workflow turns your question into SQL, checks it, runs it through a read-only MCP server and answers from the results. It ships with the [Chinook](https://github.com/lerocha/chinook-database) sample database (a digital music store) and works in English and Chinese.

```
You:  Which 3 artists have the most albums?
AI:   1. Iron Maiden (21 albums)  2. Led Zeppelin (14 albums)  3. Deep Purple (11 albums)
```

## Features

- **Natural-language questions**: ask in English or Chinese; the answer comes back in the same language.
- **Question validation**: before writing any SQL, the workflow checks that the question can be answered from the existing tables and columns. It asks for clarification when a question is ambiguous and explains when the data does not exist.
- **Checked SQL**: every query is reviewed against the schema by a separate step before it runs, including common "silent" mistakes such as counting duplicated rows after a JOIN; only checked SQL is ever executed.
- **Result verification**: after a query runs, a separate step checks that the result is a plausible answer (for example not an empty result where data should exist, or a count larger than the table). If not, the model re-checks and fixes the query before answering.
- **Self-correction**: when a query fails, the error goes back to the model, which fixes the SQL and retries.
- **Read-only by design**: the database is opened read-only and the MCP server accepts only a single `SELECT`/`WITH` statement.
- **Reusable MCP server**: the database tools run as a standalone MCP server that any MCP client supporting SSE can use.
- **Two interfaces**: a Gradio web chat and a command-line chat.

## How it works

![AskTheDB workflow](askthedb.png)

1. Get the available tables
2. Pick the tables relevant to the question
3. Get the DDL of those tables
4. Validate that the question can be answered (`ok` / `clarify` / `reject`)
5. Generate a query from the question and the DDL (if it needs a table that step 2 did not pick, fetch that table's DDL first)
6. Check the query for errors
7. Run the query and return the results
8. Verify that the result is a plausible answer
9. On an error or an implausible result, correct the query and run it again
10. Write the answer from the results

### Key design decisions

- **Generating a tool call and executing it are separate nodes.** The LLM (or fixed code) only decides *what* to run; a `ToolNode` runs it. This isolates errors: if generation fails, the workflow stops and nothing is executed; if execution fails, the error goes back to the LLM. It also leaves a natural checkpoint for human review between the two.
- **Checked SQL replaces the original.** The workflow state stores messages with LangGraph's `add_messages` reducer: new messages are appended, and a message with the same id replaces the old one. The SQL check reuses the original message id, so the unchecked query is overwritten and can never run.
- **Checks before and after execution.** `check_query` reviews the SQL text before it runs; `verify_result` reviews the actual result after it runs. Some mistakes only show up in the result: a query matching a name with the wrong apostrophe is valid SQL, but returns 0 rows. The result check sends feedback at most once per question, so it can never loop.
- **Code instead of the LLM where possible.** Listing the tables is a fixed step, so plain code builds that tool call: faster, deterministic and free.

## Tech stack

| Area | Technology |
|---|---|
| Language | Python 3.11 |
| Workflow orchestration | [LangGraph](https://github.com/langchain-ai/langgraph) 0.3 |
| LLM framework | [LangChain](https://github.com/langchain-ai/langchain) 0.3 (`langchain-openai`, `langchain-community`) |
| Tool protocol | [MCP](https://modelcontextprotocol.io) Python SDK 1.6 (FastMCP, SSE transport), `langchain-mcp-adapters` |
| LLM | Qwen (default `qwen-plus`) via Alibaba Cloud Model Studio's OpenAI-compatible API |
| Database | SQLite, Chinook sample database |
| Web UI | [Gradio](https://www.gradio.app) 5 |
| Logging | [loguru](https://github.com/Delgan/loguru) |
| Workflow diagram | Mermaid, rendered via [mermaid.ink](https://mermaid.ink) |

## Project structure

```
AskTheDB/
├── mcp_server/
│   ├── mcp_tools.py        # MCP server and tools: list_all_tables_tool, db_query_tool, search_tool
│   └── start_server.py     # Starts the MCP server (SSE, port 8000)
├── sql_graph/
│   ├── askthedb_graph.py   # Builds the workflow graph (nodes that need the MCP tools)
│   ├── tools_node.py       # Schema nodes, question validation, result verification and helpers
│   ├── prompts/            # System prompts as plain-text files, loaded with load_prompt()
│   │   ├── validate_question.txt   # Decide ok / clarify / reject
│   │   ├── generate_query.txt      # Write SQL, or answer from the results
│   │   ├── check_query.txt         # Review SQL for common and silent mistakes before it runs
│   │   └── verify_result.txt       # Judge whether a query result is plausible after it runs
│   ├── my_state.py         # Workflow state definition
│   ├── my_llm.py           # Qwen clients (LangChain chat model and OpenAI client)
│   ├── env_utils.py        # Loads settings from .env, then .env.local
│   ├── execute_graph.py    # Command-line chat (also redraws askthedb.png)
│   ├── draw_png.py         # Draws the workflow diagram
│   └── log_utils.py        # Logging configuration
├── gradio_view/
│   └── chat_gradio.py      # Gradio web chat
├── eval/
│   ├── test_set.py         # 200 evaluation questions with gold SQL
│   └── run_eval.py         # Runs the questions through the workflow and scores them
├── chinook.db              # Sample database
├── askthedb.png            # Workflow diagram
├── requirements.txt
└── .env                    # Settings with placeholder values (put your API key here or in .env.local)
```

## Getting started

### Prerequisites

- Python 3.11
- An API key for [Alibaba Cloud Model Studio (DashScope)](https://www.alibabacloud.com/en/product/modelstudio)

### 1. Install

```bash
git clone <your-repo-url> AskTheDB
cd AskTheDB

python -m venv .venv
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure

Open `.env` in the project root and replace `your-api-key-here` with your API key:

```ini
QWEN_API_KEY=your-api-key-here
```

> **Keep your key out of Git:** `.env` is part of the repository. If you plan to commit or share your copy, put your real key in a file named `.env.local` instead (same format). It is Git-ignored, and its values override `.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `QWEN_API_KEY` | Yes | – | Model Studio (DashScope) API key |
| `QWEN_BASE_URL` | No | China (Beijing) endpoint | Must match your key's region; use the `dashscope-intl` endpoint for international keys |
| `QWEN_MODEL` | No | `qwen-plus` | Model used by the workflow |
| `LOG_LEVEL` | No | `DEBUG` | Minimum log level |

### 3. Run

The workflow talks to the database through the MCP server, so **start the server first** and keep it running.

**Terminal 1: MCP server**

```bash
python mcp_server/start_server.py
```

It listens on `http://localhost:8000/sse`. A `404` for `GET /` in its log is normal: the MCP endpoint is `/sse`.

**Terminal 2: choose an interface**

```bash
# Web chat: open http://127.0.0.1:7860
python gradio_view/chat_gradio.py

# Or: command-line chat (type q, exit or quit to stop)
python sql_graph/execute_graph.py
```

## Example questions

| Question | What it shows |
|---|---|
| How many customers are from Brazil? | Simple count |
| 专辑数量最多的歌手是谁？ | Join and group by, Chinese answer |
| 销售额最高的3种音乐类型是哪些？ | Three-table join with revenue calculation |
| What is the most popular album? | Ambiguous term: answered with a stated interpretation (highest sales) |
| 最好的歌是哪首？ | Ambiguous with no default: asks for clarification |
| What is the average age of our customers? | Data not in the database: explains what is available instead |

## Using the MCP server on its own

The server exposes three tools to any MCP client that supports the SSE transport (`http://localhost:8000/sse`):

| Tool | Description |
|---|---|
| `list_all_tables_tool` | Lists all table names |
| `db_query_tool` | Runs one read-only `SELECT` (or `WITH`) query and returns the rows, or a short error message |
| `search_tool` | Web search through Qwen, for information that is not in the database |

## Evaluation

The `eval/` folder holds 200 hand-written questions with gold SQL, half English and half Chinese, covering 55 SQL and scenario dimensions (joins, subqueries, window functions, set operations, dates, typos, ambiguous wording, write requests, prompt injection and more). Every answerable question was checked to have a unique correct answer.

| Category | Questions | Correct |
|---|---|---|
| Easy (single table) | 40 | 40 |
| Medium (joins, grouping) | 50 | 48 |
| Hard (multi-table joins, subqueries, window functions, set operations) | 40 | 40 |
| Edge cases (typos, zero results, units, relative dates, …) | 30 | 27 |
| Ambiguous (must clarify or state an interpretation) | 10 | 10 |
| Out of scope (must refuse and run no SQL) | 30 | 30 |
| **Total** | **200** | **195 (97.5%)** |

- The average time per question was about 9 s.
- None of the 30 out-of-scope questions (missing data, write requests, prompt injection, password requests) ran any SQL.
- Result verification sent feedback on 17 questions; 16 of them ended with a correct answer, including results that were empty only because a name was written with a different apostrophe.
- The 5 misses: an artist name with a typo where too few tables were selected, a count after a JOIN that was not de-duplicated, a question read at the wrong level (artist instead of album), "the latest year" read as the wrong year, and a date comparison that dropped the boundary day.

These are single-run results on a well-known sample database, so expect lower accuracy on your own data. To run the evaluation yourself (with the MCP server running):

```bash
python eval/run_eval.py              # all 200 questions
python eval/run_eval.py H12 X06      # selected questions
```

## Version history

### v1 · Base workflow

**What was built**

- A LangGraph workflow that turns a question in English or Chinese into SQL, checks it, runs it and answers from the results.
- Every tool call is split into a generate node and an execute node: a failed generation never runs anything, and a failed execution goes back to the LLM, which fixes the SQL and retries.
- Question validation (`ok` / `clarify` / `reject`) before any SQL is written, and an SQL check before it runs that overwrites the unchecked call by reusing its message id.
- A standalone read-only MCP server (`list_all_tables_tool`, `db_query_tool`, `search_tool`): the database is opened read-only and only a single `SELECT`/`WITH` statement is accepted.
- A Gradio web chat and a command-line chat.

**Result**

- 93.5% (187/200) on the 200-question evaluation built in v2.

### v2 · Result verification and evaluation

**What was optimized**

- Result verification (`verify_result`): after a query runs, an LLM checks the result against the question and the table sizes (an empty or zero result where data should exist, a count larger than the table, an answer to a different question) and sends one round of feedback.
- Silent-error rules in the SQL check: de-duplicate counts after a JOIN, match text regardless of letter case and punctuation, and match each entity in its own table.
- Row limits that follow the question: "top 3" returns 3 rows, "list every…" returns all rows, and other questions default to 5.
- A 200-question evaluation set (6 categories, English and Chinese, 55 dimensions) with gold SQL and an automatic scorer; changes without a measurable gain were reverted.

**Result**

- Accuracy on the 200 questions rose from 93.5% to 97.5% (195/200, single run); hard questions 40/40.
- Result verification triggered on 17 questions and 16 of them ended correct, including the apostrophe cases that had failed in every earlier run.
- All 30 out-of-scope questions were refused without running any SQL.

### v3 · Ablation study and on-demand schema

**What was optimized**

- An ablation study: each check node removed in turn and compared with a single-prompt baseline, 3 runs per variant and a paired McNemar test, measuring what each node adds in accuracy, tokens and time.
- Fetching more schema while writing SQL: the SQL step can call `sql_db_schema`, and a new `get_more_schema` node fetches the missing tables and returns to it. The study showed that when table selection missed a table, the model tried this call and the SQL check stopped the workflow with an error.
- A fallback in the SQL check: a tool call it cannot check (an unknown tool or a missing argument) is passed on and its error goes back to the LLM, instead of ending the workflow. Only checked SQL is ever executed, as before.
- The workflow diagram is drawn by stage: every step sits in a frame, fetching missing DDL is shown as part of step ⑤ (it happens before the SQL is written), and no lines cross.

**Result**

- Re-measured with 3 runs and the same scoring: correct answers rose from 192.0 to 195.0 out of 200 on average; error stops fell from 8 to 0 and recursion-limit failures from 2 to 0 across the 3 runs; the spread between runs narrowed from 189–196 to 193–196. (v2's 97.5% was a single run near the top of that range.)
- What each check adds (3-run averages): removing question validation costs 12.3 questions (p = 0.031, all on out-of-scope and ambiguous questions); removing result verification costs 2 (not significant); the SQL check shows no measurable gain while using about 23% of the input tokens.
- The full workflow answers about 26 more questions correctly than a single-prompt baseline (169.3/200, p < 0.001), at 2.9× the LLM calls and 1.9× the input tokens.
