"""Workflow pieces that do not depend on the MCP connection.

Contains the schema nodes (call_get_schema, get_schema_node), the question validation node,
and shared helpers for error handling and routing. The system prompts are loaded from
sql_graph/prompts/.
"""
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


def latest_question(state: SQLState) -> str:
    """Return the user's most recent question."""
    for message in reversed(state["messages"]):
        if isinstance(message, HumanMessage):
            return message.content
    return ''


def question_is_valid(state: SQLState) -> bool:
    """Route after validate_question: continue only if the question can be answered."""
    return state.get("question_status") == 'ok'
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


# region Node 5: validate the question (can it be answered from the tables and columns?)
class QuestionCheck(BaseModel):
    """Decide whether the user's question can be answered from the database."""
    status: Literal['ok', 'clarify', 'reject'] = Field(
        description="ok: answerable; clarify: ambiguous, ask the user; reject: cannot be answered from this database")
    message: str = Field(
        default='', description="For clarify/reject: the message to the user, in the language of the question")
    assumption: str = Field(
        default='', description="For ok: the interpretation used when a term was ambiguous, otherwise empty")


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
        return {"question_status": 'ok', "assumption": ''}

    if check.status != 'ok':
        return {"question_status": check.status, "messages": [AIMessage(content=check.message)]}
    # generate_query adds the assumption to its system prompt and mentions it in the answer
    return {"question_status": 'ok', "assumption": check.assumption}
# endregion


# region System prompts (text in sql_graph/prompts/*.txt)
# validate_question: decide ok / clarify / reject. {tables} and {schema} are filled in at run time
validate_question_system = load_prompt('validate_question')

# generate_query: write a SQL query for the question, or answer from the results.
# The dialect and row limit are fixed, so they are filled in once here
generate_query_system_prompt = load_prompt('generate_query').format(dialect=db.dialect, top_k=5)

# check_query: review the SQL for common mistakes and against the schema.
# {schema} is filled in at run time with latest_schema()
query_check_system = load_prompt('check_query')
# endregion
