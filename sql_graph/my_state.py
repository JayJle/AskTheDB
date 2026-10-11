from typing import TypedDict, Annotated, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class SQLState(TypedDict):
    """Shared state of the entire workflow: every node reads its context from here and writes its result back."""

    messages: Annotated[list[AnyMessage], add_messages]

    # New ids are appended, so later nodes can read the full history.
    # Check_query reuses the original id, so only the checked SQL is ever executed.
    # Error messages replace faulty instructions the same way.

    # Fields below are set by validate_question and are absent until it runs (read them with .get()).
    # They have no reducer, so each new value overwrites the previous one.
    # Result of the question check
    question_status: Literal['ok', 'clarify', 'reject']
    # Interpretation chosen for an ambiguous term ('' if none)
    assumption: str
    # Rows requested by the user: N for a specific number, -1 for all rows, 0 if not mentioned
    row_limit: int
    # Rounds of feedback already sent by verify_result (caps re-checking so it cannot loop)
    verify_rounds: int