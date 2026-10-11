import re
from pathlib import Path

from langchain_community.utilities import SQLDatabase
from mcp.server import FastMCP

from sql_graph.env_utils import QWEN_MODEL
from sql_graph.my_llm import qwen_client

DB_PATH = Path(__file__).resolve().parent.parent / 'chinook.db'

mcp_server = FastMCP(
    name='AskTheDB',
    instructions=(
        'AskTheDB: tools for answering questions from a SQLite database, plus web search. '
        'Typical workflow: call list_all_tables_tool to discover tables, then db_query_tool to run '
        'read-only SELECT queries. Use search_tool only for information not in the database.'
    ),
    port=8000,
)
db = SQLDatabase.from_uri(f'sqlite:///file:{DB_PATH.as_posix()}?mode=ro&uri=true')

_SQL_COMMENT = re.compile(r'--[^\n]*|/\*.*?\*/', re.DOTALL)





def check_select_only(query: str) -> str | None:
    """Return an error message if the query is not a single SELECT/WITH statement, else None."""
    sql = _SQL_COMMENT.sub(' ', query).strip().rstrip(';').strip()
    if not sql:
        return 'Error: The query is empty. Provide a single SELECT statement.'
    if ';' in sql:
        return ('Error: Multiple query statements detected. ')
    if not re.match(r'(SELECT|WITH)\b', sql, re.IGNORECASE):
        keyword = sql.split(None, 1)[0].upper()
        return (f'Error: The query starts with "{keyword}", but only SELECT queries (optionally '
                'starting with WITH) are allowed. ')
    return None





def format_db_error(raw: str) -> str:
    """Turn a SQLAlchemy error string into a short, actionable message for the model."""
    # 'Error: (sqlite3.OperationalError) no such column: x\n[SQL: ...]\n(Background on this error at: ...)'
    message = re.sub(r'^Error:\s*(\([\w.]+\)\s*)?', '', raw.splitlines()[0]).strip()
    if 'readonly database' in message:
        return ('Error: The query tried to modify data, but this database is read-only. '
                'Only SELECT queries can be run.')
    return f'Error: {message}. Fix the query and try again.'





@mcp_server.tool()
def search_tool(query: str) -> str:
    """Search the web for up-to-date information that is not stored in the database.

    Use this for general knowledge or current events. Do not use it to answer
    questions about data in the Chinook database; query the database instead.

    Args:
        query: A concise natural-language search query, e.g. "AC/DC band history".

    Returns:
        A summary of what the web search found, or a message explaining that
        no results were found.
    """
    try:
        response = qwen_client.chat.completions.create(
            model=QWEN_MODEL,
            messages=[
                {'role': 'system', 'content': 'Search the web and summarize the relevant facts concisely.'},
                {'role': 'user', 'content': query},
            ],
            extra_body={'enable_search': True, 'enable_thinking': False},
        )
        content = response.choices[0].message.content
        if content:
            return content
    except Exception as e:
        print(e)
    return 'No search results found. Try rephrasing the query.'





@mcp_server.tool()
def list_all_tables_tool() -> str:
    """List all tables in the database.

    Call this function to learn which tables exist.
    Takes no arguments.

    Returns:
        A comma-separated list of table names, e.g. "albums, artists, customers".
    """
    return ", ".join(db.get_usable_table_names())





@mcp_server.tool()
def db_query_tool(query: str) -> str:
    """Execute a read-only SQL query and return the rows.

    The database is read-only and this is enforced: only a single SELECT statement
    (optionally starting with WITH) is accepted. INSERT, UPDATE, DELETE, DROP, PRAGMA,
    ATTACH and multiple statements separated by ";" are rejected.

    Guidelines:
    - Use SQLite syntax.
    - Reference only tables and columns that exist; check with list_all_tables_tool and
      the table schema first.
    - Select only the columns you need, and add a LIMIT unless the user asks for all rows.

    Args:
        query: One syntactically valid SQLite SELECT statement, e.g.
            "SELECT Name FROM artists ORDER BY Name LIMIT 5".

    Returns:
        The result rows as a list of tuples, "Query returned no rows." if nothing matched,
        or a message starting with "Error:" that explains the problem. On error, fix the
        query as described and call this tool again.
    """
    error = check_select_only(query) # Enforce read-only SQL queries
    if error:
        return error
    
    result = db.run_no_throw(query)  # Returns an "Error: ..." string instead of raising
    if not result:
        return "Query returned no rows."
    if isinstance(result, str) and result.startswith('Error:'):
        return format_db_error(result)
    return result
