import hashlib
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from langchain_core.tools import tool

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


@tool
def save_wishlist(
    user_id: str,
    title: str,
    url: str,
    price: float | None = None,
    merchant: str = "",
    currency: str = "INR",
) -> dict:
    """Save a product to the user's wishlist in DynamoDB.

    The product_id is derived deterministically from the URL (first 12 chars of
    its md5 hash), so saving the same URL twice updates the existing item rather
    than creating a duplicate.

    Args:
        user_id: The hashed IAM ARN identifying the current user (required).
        title: The product title.
        url: The product URL — also the basis for the deterministic product_id.
        price: Optional product price. Stored as a string because DynamoDB's
            Python SDK cannot serialize native floats.
        merchant: Optional selling merchant name.
        currency: Price currency (default "INR").

    Returns:
        A dict: {"saved": True, "product_id": ..., "title": ...}.
    """
    if not user_id:
        raise ValueError("user_id must be a non-empty string to save a wishlist item.")

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
def get_wishlist(user_id: str) -> list[dict]:
    """Retrieve all wishlist items for the given user, newest first.

    Queries DynamoDB for every item belonging to the user and returns them
    sorted by saved_at descending (most recently saved first).

    Args:
        user_id: The hashed IAM ARN identifying the current user (required).

    Returns:
        A list of wishlist item dicts, newest first.
    """
    if not user_id:
        raise ValueError("user_id must be a non-empty string to retrieve a wishlist.")

    response = _get_table().query(KeyConditionExpression=Key("user_id").eq(user_id))
    items = response.get("Items", [])

    items.sort(key=lambda item: item.get("saved_at", ""), reverse=True)
    return items
