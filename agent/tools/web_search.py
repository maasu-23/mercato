import os

from langchain_core.tools import tool
from tavily import TavilyClient

MISSING_KEY_ERROR = (
    "TAVILY_API_KEY is not set. Add it to your .env file "
    "(get a free key at https://tavily.com)."
)

_client = None


def _get_client() -> TavilyClient | None:
    """Return a lazily-initialised, module-level TavilyClient singleton.

    The API key is read from the TAVILY_API_KEY environment variable on first
    use. Returns None if the key is not set, so the caller can surface a missing
    key as an error dict rather than an exception.
    """
    global _client
    if _client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            return None
        _client = TavilyClient(api_key=api_key)
    return _client


@tool
def web_search(query: str) -> list[dict]:
    """Search the web for products matching a query.

    Use this tool for broad product discovery — finding candidate products,
    brands, models, or general shopping information across the open web. It is
    also the fallback when the Universal Commerce Protocol (UCP) query returns
    no results for a merchant.

    Args:
        query: A natural-language product search query, e.g.
            "wireless noise-cancelling headphones under $200" or
            "best 4k monitor for photo editing". Be specific for better results.

    Returns:
        A list of result dicts, each containing:
            source: Always "web".
            title: The result page title.
            url: The result URL.
            snippet: First 300 characters of the page content.
            price: Always None — Tavily does not extract structured prices.
        On failure — a missing TAVILY_API_KEY, or a Tavily API/network error —
        returns a single-element list containing an error dict with keys
        "source" ("web") and "error". Never raises for these expected failures.
    """
    client = _get_client()
    if client is None:
        return [{"source": "web", "error": MISSING_KEY_ERROR}]

    try:
        response = client.search(
            query=query,
            search_depth="advanced",
            max_results=8,
            include_answer=False,
        )

        results = []
        for item in response.get("results", []):
            results.append(
                {
                    "source": "web",
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": (item.get("content") or "")[:300],
                    "price": None,
                }
            )
        return results
    except Exception as e:
        return [{"source": "web", "error": f"Web search unavailable: {e}"}]
