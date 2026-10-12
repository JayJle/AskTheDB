"""Workflow pieces that do not depend on the MCP connection.

Contains the schema nodes (call_get_schema, get_schema_node, get_more_schema_node), the question validation node,
and shared helpers for error handling and routing. The system prompts are loaded from
sql_graph/prompts/.
"""
import sqlite3
from pathlib import Path
from typing import Literal

from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

from sql_graph.log_utils import log
from sql_graph.my_llm import llm
from sql_graph.my_state import SQLState
from sql_graph.prompts import load_prompt

# region Setup: database connection and schema tool
DB_PATH = Path(__file__).resolve().parent.parent / 'chinook.db'
db = SQLDatabase.from_uri(f'sqlite:///{DB_PATH.as_posix()}')
toolkit = SQLDatabaseToolkit(db=db, llm=llm)

tools = toolkit.get_tools()

# LangChain tool that returns the schema (DDL and sample rows) of the given tables
get_schema_tool = next(tool for tool in tools if tool.name == 'sql_db_schema')

# Row limit injected into the SQL-generation prompt for each question
DEFAULT_ROW_LIMIT = 5     # the question does not say how many rows it wants
MAX_ROW_LIMIT = 1000      # the question asks for all rows: a safety cap instead of no limit

# Result verification: at most this many rounds of feedback per question, so it can never loop
MAX_VERIFY_ROUNDS = 1
# Name given to feedback messages from verify_result, so they are never taken for the user's question
RESULT_FEEDBACK = 'result_check'


def _table_row_counts() -> str:
    """Row count of every table, read once at startup (gives the result check a sense of scale)."""
    with sqlite3.connect(f'file:{DB_PATH.as_posix()}?mode=ro', uri=True) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return '\n'.join(f'- {t}: {conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]}' for t in tables)


TABLE_ROW_COUNTS = _table_row_counts()
# endregion


# region Helpers: error handling and routing
def generation_error(step: str, error: Exception | str, message_id: str | None = None) -> AIMessage:
    """Build the message returned when generating a tool call fails.

    The message has no tool calls, so the routing ends the workflow instead of raising an exception.
    Pass message_id to overwrite the faulty tool-call message, so it is never executed.
    """
    if isinstance(error, Exception):
        log.exception(error)
    message = AIMessage(content=f'Sorry, an error occurred while {step}, so this query was stopped: {error}')
    if message_id:
        message.id = message_id  # Same id: add_messages replaces the original tool-call message
    return message


def has_tool_calls(state: SQLState) -> bool:
    """Route: execute only if the last message has tool calls; otherwise (generation failed) end."""
    return bool(getattr(state["messages"][-1], "tool_calls", None))


def latest_tool_output(state: SQLState, tool_name: str) -> str:
    """Return the most recent result of the given tool, or '' if it has not run yet."""
    for message in reversed(state["messages"]):
        if isinstance(message, ToolMessage) and message.name == tool_name:
            return message.content
    return ''


def latest_schema(state: SQLState) -> str:
    """Return the most recent table schema (DDL) returned by get_schema_tool, or '' if there is none."""
    return latest_tool_output(state, get_schema_tool.name)


def all_schemas(state: SQLState) -> str:
    """Return every table schema (DDL) fetched so far: the first pick plus any fetched later, or ''."""
    return '\n\n'.join(str(m.content) for m in state["messages"]
                       if isinstance(m, ToolMessage) and m.name == get_schema_tool.name)


def latest_question(state: SQLState) -> str:
    """Return the user's most recent question (skipping feedback messages from verify_result)."""
    for message in reversed(state["messages"]):
        if isinstance(message, HumanMessage) and message.name != RESULT_FEEDBACK:
            return message.content
    return ''


def question_is_valid(state: SQLState) -> bool:
    """Route after validate_question: continue only if the question can be answered."""
    return state.get("question_status") == 'ok'


def resolve_row_limit(state: SQLState) -> int:
    """Turn the row_limit found by validate_question into the limit for the SQL-generation prompt."""
    requested = state.get("row_limit", 0)
    if requested > 0:
        return min(requested, MAX_ROW_LIMIT)
    if requested < 0:
        return MAX_ROW_LIMIT
    return DEFAULT_ROW_LIMIT
# endregion


# region Node 3: pick the relevant tables (LLM generates the sql_db_schema tool call)
def call_get_schema(state: SQLState):
    """Node 3: have the LLM pick the relevant tables and generate the sql_db_schema tool call."""
    # Force this tool by name
    llm_with_tools = llm.bind_tools([get_schema_tool], tool_choice=get_schema_tool.name)
    try:
        response = llm_with_tools.invoke(state["messages"])
    except Exception as e:
        return {"messages": [generation_error('picking the relevant tables', e)]}

    return {"messages": [response]}
# endregion


# region Node 4: execute sql_db_schema locally with LangGraph's prebuilt ToolNode
get_schema_node = ToolNode([get_schema_tool], name="get_schema_tool")
# endregion


# region Node 6b: execute sql_db_schema again when generate_query asks for more tables
# Same tool as node 4, as its own node so the result goes back to generate_query
get_more_schema_node = ToolNode([get_schema_tool], name="get_more_schema")
# endregion


# region Node 5: validate the question (can it be answered from the tables and columns?)
class QuestionCheck(BaseModel):
    """Decide whether the user's question can be answered from the database."""
    status: Literal['ok', 'clarify', 'reject'] = Field(
        description="ok: answerable; clarify: ambiguous, ask the user; reject: cannot be answered from this database")
    message: str = Field(
        default='', description="For clarify/reject: the message to the user, in the language of the question")
    assumption: str = Field(
        default='', description="For ok: the interpretation used when a term was ambiguous, otherwise empty")
    row_limit: int = Field(
        default=0, description="How many result rows the question asks for: N if it asks for a specific number "
                               "(e.g. 'top 3' -> 3); -1 if it asks for all of them (e.g. 'all', 'every', 'each', "
                               "'list every ...', or a breakdown per group, per year or per month); "
                               "0 if it does not say")


def validate_question(state: SQLState):
    """Node 5: check the question against the tables and columns before any SQL is generated.

    clarify/reject end the workflow with a message to the user. If the check itself fails,
    the question is treated as valid, so a validation problem never blocks a normal answer.
    """
    prompt = validate_question_system.format(
        tables=latest_tool_output(state, 'list_all_tables_tool') or '(unknown)',
        schema=latest_schema(state) or '(none)',
    )
    # Force the structured result by tool name (Qwen/DashScope does not support tool_choice="required")
    llm_with_check = llm.bind_tools([QuestionCheck], tool_choice=QuestionCheck.__name__)
    try:
        response = llm_with_check.invoke([{"role": "system", "content": prompt},
                                          {"role": "user", "content": latest_question(state)}])
        check = QuestionCheck(**response.tool_calls[0]["args"])
    except Exception as e:
        log.warning(f'Question validation failed, continuing without it: {e}')
        return {"question_status": 'ok', "assumption": '', "row_limit": 0}

    if check.status != 'ok':
        return {"question_status": check.status, "messages": [AIMessage(content=check.message)]}
    # generate_query adds the assumption to its system prompt and mentions it in the answer
    return {"question_status": 'ok', "assumption": check.assumption, "row_limit": check.row_limit}
# endregion


# region Node 9: verify the result (is it a plausible answer to the question?)
class ResultCheck(BaseModel):
    """Decide whether a query result is a plausible answer to the user's question."""
    plausible: bool = Field(description="True if the result is a plausible answer to the question")
    issue: str = Field(default='', description="If not plausible: the problem and what to verify, in one or two sentences")


def verify_result(state: SQLState):
    """Node 9: check the latest query result for anomalies before the LLM answers from it.

    Anomalies (an empty or zero result where data should exist, a count larger than the table,
    a query that answers a different question) produce one feedback message, so generate_query
    re-checks and fixes the query. Errors are skipped: they already go back to the LLM. If the
    check itself fails, the result is accepted, so verification never blocks an answer.
    """
    rounds = state.get("verify_rounds", 0)
    if rounds >= MAX_VERIFY_ROUNDS:
        return {}

    # The queries just executed, and their results
    messages = state["messages"]
    call_message = next((m for m in reversed(messages) if isinstance(m, AIMessage) and m.tool_calls), None)
    if call_message is None:
        return {}
    results = {m.tool_call_id: str(m.content) for m in messages if isinstance(m, ToolMessage)}
    executions = [(c["args"].get("query", ''), results.get(c["id"], '')) for c in call_message.tool_calls]
    executions = [(sql, result) for sql, result in executions if result and not result.startswith('Error')]
    if not executions:
        return {}

    prompt = verify_result_system.format(
        table_counts=TABLE_ROW_COUNTS,
        question=latest_question(state),
        executions='\n\n'.join(f'SQL: {sql}\nResult: {result[:2000]}' for sql, result in executions),
    )
    llm_with_check = llm.bind_tools([ResultCheck], tool_choice=ResultCheck.__name__)
    try:
        response = llm_with_check.invoke([{"role": "user", "content": prompt}])
        check = ResultCheck(**response.tool_calls[0]["args"])
    except Exception as e:
        log.warning(f'Result verification failed, accepting the result: {e}')
        return {}

    if check.plausible:
        return {}
    feedback = (f'Check of the query result: {check.issue}\n'
                'Verify this with another query and fix the query if needed. '
                'If the result turns out to be correct, answer with it.')
    return {"messages": [HumanMessage(content=feedback, name=RESULT_FEEDBACK)], "verify_rounds": rounds + 1}
# endregion


# region System prompts (text in sql_graph/prompts/*.txt)
# validate_question: decide ok / clarify / reject. {tables} and {schema} are filled in at run time
validate_question_system = load_prompt('validate_question')

# generate_query: write a SQL query for the question, or answer from the results.
# The dialect is filled in once here; {top_k} is filled in per question with resolve_row_limit()
generate_query_system_prompt = load_prompt('generate_query').format(dialect=db.dialect, top_k='{top_k}')

# check_query: review the SQL for common mistakes and against the schema.
# {schema} is filled in at run time with latest_schema()
query_check_system = load_prompt('check_query')

# verify_result: judge whether a query result is plausible. Filled in at run time
verify_result_system = load_prompt('verify_result')
# endregion
