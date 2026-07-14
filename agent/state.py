from typing import Annotated

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    """State object that flows through the LangGraph agent loop.

    Fields:
        messages: The full conversation history. Annotated with ``add_messages``
            so that LangGraph appends new messages to the existing list rather
            than overwriting it on each node update.
        user_id: The hashed IAM ARN identifying the current user. Set once at
            startup and passed into every tool that writes to DynamoDB so reads
            and writes are scoped to this user.
    """

    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
