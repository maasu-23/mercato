import hashlib
import os
from datetime import datetime, timezone
from typing import Annotated

import boto3
from boto3.dynamodb.conditions import Key
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from agent.state import AgentState

DEFAULT_REGION = "ap-south-1"
DEFAULT_WISHLIST_TABLE = "mercato-wishlist"

_dynamodb_resource = None


def _get_dynamodb_resource():
    """Return a lazily-initialised, module-level boto3 DynamoDB resource singleton."""
    global _dynamodb_resource
    if _dynamodb_resource is None:
        region = os.getenv("AWS_REGION", DEFAULT_REGION)
        _dynamodb_resource = boto3.resource("dynamodb", region_name=region)
    return _dynamodb_resource


def _get_table():
    """Return the wishlist DynamoDB table handle."""
    table_name = os.getenv("DYNAMODB_WISHLIST_TABLE", DEFAULT_WISHLIST_TABLE)
    return _get_dynamodb_resource().Table(table_name)


def _product_id_for_url(url: str) -> str:
    """Derive a deterministic product_id from a URL (first 12 chars of its md5 hash)."""
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def _user_id_from_state(state: AgentState) -> str:
    """Pull the user_id out of the injected agent state, or fail loudly."""
    user_id = (state or {}).get("user_id", "")
    if not user_id:
        raise ValueError("No user_id in agent state — the session was not initialised correctly.")
    return user_id


def fetch_wishlist(user_id: str) -> list[dict]:
    """Query every wishlist item for a user, newest first.

    The plain-function core of the get_wishlist tool. Exposed separately so the
    CLI can read the wishlist directly, without constructing agent state or
    spending a model turn.
    """
    response = _get_table().query(KeyConditionExpression=Key("user_id").eq(user_id))
    items = response.get("Items", [])

    items.sort(key=lambda item: item.get("saved_at", ""), reverse=True)
    return items


@tool
def save_wishlist(
    title: str,
    url: str,
    state: Annotated[AgentState, InjectedState],
    price: float | None = None,
    merchant: str = "",
    currency: str = "INR",
) -> dict:
    """Save a product to the current user's wishlist.

    The product_id is derived deterministically from the URL (first 12 chars of
    its md5 hash), so saving the same URL twice updates the existing item rather
    than creating a duplicate.

    Args:
        title: The product title.
        url: The product URL — also the basis for the deterministic product_id.
        price: Optional product price.
        merchant: Optional selling merchant name.
        currency: Price currency (default "INR").

    Returns:
        A dict: {"saved": True, "product_id": ..., "title": ...}.
    """
    user_id = _user_id_from_state(state)

    product_id = _product_id_for_url(url)

    item = {
        "user_id": user_id,
        "product_id": product_id,
        "title": title,
        "url": url,
        "merchant": merchant,
        "currency": currency,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    # DynamoDB's Python SDK cannot serialize native floats, so store price as a str.
    if price is not None:
        item["price"] = str(price)

    _get_table().put_item(Item=item)

    return {"saved": True, "product_id": product_id, "title": title}


@tool
def get_wishlist(state: Annotated[AgentState, InjectedState]) -> list[dict]:
    """Retrieve everything on the current user's wishlist, newest first.

    Takes no arguments — the wishlist always belongs to the user in the current
    session.

    Returns:
        A list of wishlist item dicts, newest first.
    """
    return fetch_wishlist(_user_id_from_state(state))
