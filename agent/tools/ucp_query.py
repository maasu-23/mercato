import os

import httpx
from langchain_core.tools import tool

DEFAULT_UCP_ENDPOINT = "https://api.ucp.dev/v1"
REQUEST_TIMEOUT = 10.0
DEFAULT_CURRENCY = "INR"


def _get_endpoint() -> str:
    """Return the UCP base endpoint from the environment, or the default."""
    return os.getenv("UCP_ENDPOINT", DEFAULT_UCP_ENDPOINT)


@tool
def ucp_query(query: str, category: str = "", max_results: int = 6) -> list[dict]:
    """Query UCP-compliant merchants for live, structured product data.

    UCP (Universal Commerce Protocol) is a standardised API for querying
    compliant merchants — such as Walmart, Nike, and Shopify-hosted stores —
    through a single consistent interface.

    Call this tool FIRST, before web_search. Unlike a general web search, UCP
    returns structured price data, stock status, and verified checkout
    permalinks that can be used directly for purchase. Only fall back to
    web_search if UCP returns no results or is unavailable.

    Args:
        query: A natural-language or keyword product search, e.g.
            "running shoes men size 10" or "4k monitor 27 inch".
        category: Optional category filter to narrow results, e.g.
            "electronics" or "footwear". Pass an empty string to omit it.
        max_results: Maximum number of products to return (default 6).

    Returns:
        A list of product dicts, each containing:
            source: Always "ucp".
            title: The product name.
            merchant: The selling merchant's name.
            price: The product price.
            currency: The price currency (default "INR").
            in_stock: Whether the product is currently in stock.
            url: The verified checkout permalink.
            product_id: The merchant's product identifier.
            image_url: A URL to the product image.
        On failure, returns a single-element list containing an error dict
        with keys "source" ("ucp") and "error", signalling the caller to fall
        back to web_search.
    """
    endpoint = _get_endpoint()
    params = {"q": query, "limit": max_results}
    if category:
        params["category"] = category

    try:
        response = httpx.get(
            f"{endpoint}/catalog/search",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        results = []
        for item in payload.get("results", []):
            results.append(
                {
                    "source": "ucp",
                    "title": item.get("title"),
                    "merchant": item.get("merchant"),
                    "price": item.get("price"),
                    "currency": item.get("currency", DEFAULT_CURRENCY),
                    "in_stock": item.get("in_stock"),
                    "url": item.get("permalink_url"),
                    "product_id": item.get("product_id"),
                    "image_url": item.get("image_url"),
                }
            )
        return results
    except httpx.TimeoutException:
        return [{"source": "ucp", "error": "UCP request timed out — falling back to web_search"}]
    except httpx.HTTPStatusError as e:
        return [
            {
                "source": "ucp",
                "error": f"UCP API error {e.response.status_code} — falling back to web_search",
            }
        ]
    except Exception as e:
        return [{"source": "ucp", "error": f"UCP unavailable: {e} — falling back to web_search"}]
