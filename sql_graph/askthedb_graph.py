"""AskTheDB workflow: answers natural-language questions from the database.

Every tool call is split into two nodes: one node generates the tool-call instruction
(with the LLM or fixed code) and a ToolNode executes it. If generation fails, the workflow
ends without executing anything; if execution fails, the error goes back to the LLM to retry.
"""
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.constants import START, END
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode

from sql_graph.my_llm import llm
from sql_graph.my_state import SQLState
from sql_graph.tools_node import generate_query_system_prompt, query_check_system, call_get_schema, get_schema_node, \
    generation_error, has_tool_calls, all_schemas, validate_question, question_is_valid, resolve_row_limit, \
    verify_result, get_schema_tool, get_more_schema_node

mcp_server_config = {
    "url": "http://localhost:8000/sse",
    "transport": "sse"
}


def should_continue(state: SQLState) -> Literal[END, "get_more_schema", "check_query"]:
    """Route after generate_query: fetch the schema of more tables if the LLM asked for it,
    check the SQL for any other tool call, otherwise end with its answer."""
    messages = state["messages"]
    last_message = messages[-1]
    if not last_message.tool_calls:
        return END
    if any(call["name"] == get_schema_tool.name for call in last_message.tool_calls):
        return "get_more_schema"
    return "check_query"


@asynccontextmanager
async def make_graph():
    """Connect to the MCP server, then build and compile the workflow graph.

    Nodes are defined inside this function so they can use the MCP tools loaded from the connection.
    """
    async with MultiServerMCPClient({'askthedb': mcp_server_config}) as client:
        # region Load MCP tools
        tools = client.get_tools()
        # MCP tool that lists all table names
        list_all_tables_mcp_tool = next(tool for tool in tools if tool.name == "list_all_tables_tool")
        # MCP tool that runs a read-only SQL query
        db_query_tool = next(tool for tool in tools if tool.name == "db_query_tool")
        # endregion

        # region Node 1: build the list_all_tables_tool call (fixed code)
        def call_list_all_tables(state: SQLState):
            """Node 1: build the list_all_tables_tool call with fixed code (no LLM needed)."""
            tool_call = {
                "name": "list_all_tables_tool",
                "args": {},
                "id": f"call_{uuid4().hex}",  # Unique id that links this call to its ToolMessage result
                "type": "tool_call",
            }
            tool_call_message = AIMessage(content="", tool_calls=[tool_call])
            return {"messages": [tool_call_message]}
        # endregion

        # region Node 2: execute list_all_tables_tool through MCP
        list_all_tables_node = ToolNode([list_all_tables_mcp_tool], name="list_all_tables_tool")
        # endregion

        # Nodes 3, 4, 5 (call_get_schema, get_schema_node, validate_question) and 9 (verify_result)
        # are defined in tools_node.py

        # region Node 6: generate a SQL tool call or the final answer (LLM)
        def generate_query(state: SQLState):
            """Node 6: have the LLM write a SQL tool call, or the final answer once it has the results."""
            # Row limit for this question: the number the user asked for, all rows, or the default
            system_prompt = generate_query_system_prompt.format(top_k=resolve_row_limit(state))
            if state.get("assumption"):
                # Interpretation chosen by validate_question for an ambiguous term
                system_prompt += (f"\nInterpret the question as follows, and state this interpretation "
                                  f"in your final answer: {state['assumption']}\n")
            system_message = {
                "role": "system",
                "content": system_prompt,
            }
            # The tool call is not forced, so the model can reply with the answer when it is done.
            # sql_db_schema lets it fetch tables that call_get_schema did not pick. One tool call per
            # message, so each message either fetches schema or runs SQL (see should_continue)
            llm_with_tools = llm.bind_tools([db_query_tool, get_schema_tool], parallel_tool_calls=False)
            try:
                resp = llm_with_tools.invoke([system_message] + state['messages'])
            except Exception as e:
                # A message without tool calls makes should_continue end the workflow
                return {'messages': [generation_error('generating the SQL', e)]}
            return {'messages': [resp]}
        # endregion

        # region Node 7: check the SQL and generate a checked tool call (LLM)
        def check_query(state: SQLState):
            """Node 7: check each SQL query against the schema and generate a new, checked tool call."""
            last_message = state["messages"][-1]
            system_message = {
                "role": "system",
                "content": query_check_system.format(schema=all_schemas(state) or '(none)'),
            }
            llm_with_tools = llm.bind_tools([db_query_tool], tool_choice=db_query_tool.name)

            checked_calls = []
            for tool_call in last_message.tool_calls:  # Check every tool call separately
                query = tool_call.get("args", {}).get("query")
                if tool_call["name"] != db_query_tool.name or not query:
                    # No SQL to check (an unknown tool or a missing argument): pass the call on unchanged.
                    # It cannot run any SQL; run_query answers it with an error that goes back to the LLM
                    checked_calls.append(tool_call)
                    continue
                try:
                    response = llm_with_tools.invoke([system_message, {"role": "user", "content": query}])
                except Exception as e:
                    return {"messages": [generation_error('checking the SQL', e, last_message.id)]}
                if not response.tool_calls:
                    return {"messages": [generation_error('checking the SQL', 'the check did not produce a tool call', last_message.id)]}
                checked_calls.append(response.tool_calls[0])

            # Reuse the original message id to overwrite generate_query's instruction,
            # so only checked SQL is ever executed
            return {"messages": [AIMessage(content='', tool_calls=checked_calls, id=last_message.id)]}
        # endregion

        # region Node 8: execute db_query_tool through MCP
        run_query_node = ToolNode([db_query_tool], name="run_query")
        # endregion

        # region Build the graph: register nodes, connect edges, compile
        workflow = StateGraph(SQLState)
        workflow.add_node(call_list_all_tables)
        workflow.add_node(list_all_tables_node)
        workflow.add_node(call_get_schema)
        workflow.add_node(get_schema_node)
        workflow.add_node(validate_question)
        workflow.add_node(generate_query)
        workflow.add_node(get_more_schema_node)
        workflow.add_node(check_query)
        workflow.add_node(run_query_node)
        workflow.add_node(verify_result)

        workflow.add_edge(START, "call_list_all_tables")
        workflow.add_edge("call_list_all_tables", "list_all_tables_tool")
        workflow.add_edge("list_all_tables_tool", "call_get_schema")
        # If generating a tool call failed, the message has no tool calls: end without executing anything
        workflow.add_conditional_edges("call_get_schema", has_tool_calls, {True: "get_schema_tool", False: END})
        workflow.add_edge("get_schema_tool", "validate_question")
        # Unanswerable or ambiguous questions end here with a clarifying message, before any SQL is written
        workflow.add_conditional_edges("validate_question", question_is_valid, {True: "generate_query", False: END})
        workflow.add_conditional_edges('generate_query', should_continue)
        # The schema of the extra tables goes straight back to generate_query
        workflow.add_edge("get_more_schema", "generate_query")
        workflow.add_conditional_edges("check_query", has_tool_calls, {True: "run_query", False: END})
        # Every result is checked for anomalies; feedback (if any) goes back to generate_query with it
        workflow.add_edge("run_query", "verify_result")
        workflow.add_edge("verify_result", "generate_query")

        graph = workflow.compile()
        # endregion
        yield graph
