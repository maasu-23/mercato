import json
import os
from datetime import datetime, timezone

import boto3
from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent.state import AgentState
from agent.tools import ALL_TOOLS

DEFAULT_REGION = "ap-south-1"
DEFAULT_MODEL_ID = "anthropic.claude-sonnet-4-5"

AWS_REGION = os.getenv("AWS_REGION", DEFAULT_REGION)
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)

SYSTEM_PROMPT = """You are Mercato, an agentic shopping assistant.

Your job is to help users find products, compare prices, save items to their \
wishlist, and get checkout links.

Follow these rules on every turn:
- Always try the ucp_query tool FIRST for product discovery. It returns \
structured price data and verified checkout permalinks.
- Only fall back to web_search if ucp_query returns an error or no results.
- After gathering results from ucp_query and/or web_search, always call \
price_compare before presenting anything to the user. Never show products \
without ranking them first.
- Be concise. Present only the top 3-5 results, not everything you found.
- Never fabricate product data. Only report what the tools actually return — \
titles, prices, merchants, and URLs must come from tool output.
- The default currency is INR unless the user explicitly specifies another.

When the user wants to save an item, use save_wishlist. To review saved items, \
use get_wishlist. To buy or open a product, use checkout_url."""

_graph = None


def _build_llm() -> ChatBedrock:
    """Construct the ChatBedrock LLM from configuration."""
    return ChatBedrock(
        model_id=BEDROCK_MODEL_ID,
        region_name=AWS_REGION,
        model_kwargs={"max_tokens": 2048, "temperature": 0.2},
    )


def _save_session_to_s3(user_id: str, messages: list) -> None:
    """Persist a session transcript to S3. Never raises — logging must not crash the agent."""
    if not S3_BUCKET_NAME:
        return

    try:
        serialized = [
            {"role": m.type, "content": str(m.content)} for m in messages
        ]
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"users/{user_id}/sessions/{timestamp}.json"
        body = json.dumps(serialized, default=str).encode("utf-8")
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    except Exception:
        pass


def build_graph():
    """Build and compile the LangGraph agent loop."""
    llm_with_tools = _build_llm().bind_tools(ALL_TOOLS)

    def agent_node(state: AgentState) -> AgentState:
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm_with_tools.invoke(messages)
        return {
            "messages": [response],
            "user_id": state["user_id"],
            "last_results": state["last_results"],
        }

    tool_node = ToolNode(ALL_TOOLS)

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tool_node)

    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")

    return graph.compile()


def get_graph():
    """Return a lazily-compiled, module-level graph singleton (reused across Lambda warm starts)."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def chat(user_message: str, history: list, user_id: str) -> tuple[str, list]:
    """Run one conversational turn through the agent.

    Args:
        user_message: The user's latest message text.
        history: The prior conversation as a list of BaseMessage objects.
        user_id: The hashed IAM ARN identifying the current user.

    Returns:
        A tuple of (reply text, updated message list).
    """
    history = history + [HumanMessage(content=user_message)]

    result = get_graph().invoke(
        {"messages": history, "user_id": user_id, "last_results": []}
    )

    updated_messages = result["messages"]
    reply = str(updated_messages[-1].content)

    _save_session_to_s3(user_id, updated_messages)

    return reply, updated_messages
