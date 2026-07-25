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


NO_USER_ID_ERROR = "Could not determine user identity — session may not be initialized correctly."


def _user_id_from_state(state: AgentState) -> str:
    """Pull the user_id out of the injected agent state.

    Returns an empty string if it is missing, so the calling tool can surface an
    error dict rather than raising.
    """
    return (state or {}).get("user_id", "")


def fetch_wishlist(user_id: str) -> list[dict]:
    """Query every wishlist item for a user, newest first.

    The plain-function core of the get_wishlist tool. Exposed separately so the
    CLI can read the wishlist directly, without constructing agent state or
    spending a model turn.

    Returns whole items as stored, so an item saved with a price alert carries
    its "alert_threshold" back; items saved without one simply lack the key.

    Unlike the tools, this returns a bare list and lets DynamoDB errors propagate:
    it is not LLM-facing, so the error-dict convention buys nothing here, and its
    only caller (cli/main.py) already handles the exception and renders the list.
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
    alert_threshold: float | None = None,
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
        alert_threshold: Optional price alert. When set, the user wants to be
            notified once the product's price drops to or below this value.
            Nothing in this module acts on it — the comparison is the job of a
            separate scheduled process that is not built yet; save_wishlist only
            records the user's intent. Omitted entirely from the stored item when
            not provided, so items without an alert carry no empty field.

    Returns:
        A dict. On success:
            {"saved": True, "product_id": ..., "title": ...}
        On failure — the session carries no user identity, or the DynamoDB write
        fails — a dict with an "error" key describing what went wrong. Never
        raises for these expected failures.
    """
    user_id = _user_id_from_state(state)
    if not user_id:
        return {"error": NO_USER_ID_ERROR}

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
    # Same float restriction applies to the alert threshold.
    if alert_threshold is not None:
        item["alert_threshold"] = str(alert_threshold)

    try:
        _get_table().put_item(Item=item)
    except Exception as e:
        return {"error": f"Could not save to your wishlist: {e}"}

    return {"saved": True, "product_id": product_id, "title": title}


@tool
def get_wishlist(state: Annotated[AgentState, InjectedState]) -> dict:
    """Retrieve everything on the current user's wishlist, newest first.

    Takes no arguments — the wishlist always belongs to the user in the current
    session.

    Returns:
        A dict. On success:
            {"items": [...]} — the wishlist item dicts, newest first. An empty
            list means the wishlist is empty, which is not an error. An item may
            optionally include "alert_threshold": the price the user asked to be
            alerted at when they saved it. Items saved without an alert omit the
            key entirely.
        On failure — the session carries no user identity, or the DynamoDB read
        fails — a dict with an "error" key describing what went wrong. Never
        raises for these expected failures.
    """
    user_id = _user_id_from_state(state)
    if not user_id:
        return {"error": NO_USER_ID_ERROR}

    try:
        return {"items": fetch_wishlist(user_id)}
    except Exception as e:
        return {"error": f"Could not load your wishlist: {e}"}
