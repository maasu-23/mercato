import json
import os
from datetime import datetime, timezone

import boto3
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from agent.state import AgentState
from agent.tools import ALL_TOOLS

DEFAULT_REGION = "ap-south-1"
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
MAX_HISTORY_MESSAGES = 20

AWS_REGION = os.getenv("AWS_REGION", DEFAULT_REGION)
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)

# UCP (Universal Commerce Protocol) is an emerging standard with no stable
# public endpoint as of this writing — api.ucp.dev does not currently resolve.
# ucp_query is kept in the tool registry as an architectural showcase of
# protocol-first commerce discovery, ready to become useful the moment a real
# endpoint exists, but it is deliberately NOT mandated below: forcing a call
# to an endpoint that can't succeed burns a full model round-trip on every
# single product search for no benefit. web_search is the primary discovery
# path; the model is free to try ucp_query when it judges it worthwhile.
SYSTEM_PROMPT = """You are Mercato, an agentic shopping assistant.

Your job is to help users find products, compare prices, save items to their \
wishlist, and get checkout links.

Follow these rules on every turn:
- Use web_search as your primary tool for product discovery. You may also try \
ucp_query if you judge it likely to help — it queries UCP-compliant merchants \
for structured price data and verified checkout permalinks — but UCP coverage \
is currently limited, so don't rely on it or retry it repeatedly.
- After gathering results, always call price_compare before presenting \
anything to the user. Never show products without ranking them first.
- Be concise. Present only the top 3-5 results, not everything you found.
- Never fabricate product data. Only report what the tools actually return — \
titles, prices, merchants, and URLs must come from tool output.
- The default currency is INR unless the user explicitly specifies another.

When the user wants to save an item, use save_wishlist. To review saved items, \
use get_wishlist. To buy or open a product, use checkout_url.

When a user asks to save an item AND names a price they want to be alerted at \
(e.g. "save this and let me know if it drops below 10000"), pass that number to \
save_wishlist as alert_threshold. Only set it when the user actually asks for an \
alert — never infer one from the current price."""

_graph = None


def _build_llm() -> ChatBedrockConverse:
    """Construct the ChatBedrockConverse LLM from configuration."""
    return ChatBedrockConverse(
        model=BEDROCK_MODEL_ID,
        region_name=AWS_REGION,
        max_tokens=2048,
        temperature=0.2,
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

    # Cap what gets resent to Bedrock each turn — history accumulates full tool
    # outputs (search/UCP results, ranked comparisons) and grows unbounded over
    # a long session, inflating both token cost and context window usage.
    history = history[-MAX_HISTORY_MESSAGES:]

    result = get_graph().invoke({"messages": history, "user_id": user_id})

    updated_messages = result["messages"]
    reply = str(updated_messages[-1].content)

    _save_session_to_s3(user_id, updated_messages)

    return reply, updated_messages
