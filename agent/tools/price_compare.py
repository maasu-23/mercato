import json
import os
from datetime import datetime, timezone

import boto3
from langchain_core.tools import tool

DEFAULT_REGION = "ap-south-1"

_s3_client = None


def _get_s3_client():
    """Return a lazily-initialised, module-level boto3 S3 client singleton."""
    global _s3_client
    if _s3_client is None:
        region = os.getenv("AWS_REGION", DEFAULT_REGION)
        _s3_client = boto3.client("s3", region_name=region)
    return _s3_client


def _to_float(value) -> float | None:
    """Coerce a price value to float, returning None if it cannot be parsed."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@tool
def price_compare(products: list[dict], query: str = "", user_id: str = "") -> dict:
    """Rank products by price and save a comparison snapshot to S3.

    Ranks the given products cheapest first, flags the single best deal, and
    (when possible) persists the ranked comparison as a JSON artifact in S3 for
    later reference.

    The input products can come from either web_search or ucp_query results —
    the two can be combined into one list. Any dict carrying an "error" key
    (produced when a ucp_query call fails and falls back) is filtered out before
    ranking and is never treated as a real product.

    Args:
        products: A list of product dicts from web_search and/or ucp_query.
            Dicts with an "error" key are ignored.
        query: Optional original search query, stored in the S3 artifact for
            context.
        user_id: Optional hashed IAM ARN of the current user. Required (together
            with a configured S3_BUCKET_NAME) for the comparison to be saved to
            S3; if either is missing, saving is skipped silently.

    Returns:
        A dict with keys:
            ranked: The full sorted product list — priced items cheapest first
                (each flagged with "best_deal"), followed by unpriced items.
            total: The number of real products ranked.
            cheapest: The cheapest product dict, or None if the list is empty.
            s3_artifact: The "s3://..." path of the saved artifact, or None if
                nothing was saved.
    """
    real_products = [p for p in products if "error" not in p]

    priced = []
    unpriced = []
    for product in real_products:
        if _to_float(product.get("price")) is not None:
            priced.append(product)
        else:
            unpriced.append(product)

    priced.sort(key=lambda p: _to_float(p.get("price")))

    ranked = []
    for index, product in enumerate(priced):
        ranked.append({**product, "best_deal": index == 0})
    for product in unpriced:
        ranked.append({**product, "best_deal": False})

    cheapest = ranked[0] if ranked else None

    s3_artifact = None
    bucket_name = os.getenv("S3_BUCKET_NAME", "")
    if user_id and bucket_name:
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            key = f"users/{user_id}/comparisons/{timestamp}.json"
            body = json.dumps(
                {"query": query, "timestamp": timestamp, "ranked": ranked},
                default=str,
            )
            _get_s3_client().put_object(
                Bucket=bucket_name,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
            )
            s3_artifact = f"s3://{bucket_name}/{key}"
        except Exception:
            s3_artifact = None

    return {
        "ranked": ranked,
        "total": len(ranked),
        "cheapest": cheapest,
        "s3_artifact": s3_artifact,
    }
