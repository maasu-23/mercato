import webbrowser

from langchain_core.tools import tool


@tool
def checkout_url(url: str, title: str = "", open_browser: bool = True) -> dict:
    """Return a product's checkout link and optionally open it in the browser.

    For UCP-sourced products the URL is a direct checkout permalink that leads
    straight to purchase. For web_search-sourced products it is just the product
    page, from which the user completes checkout manually.

    Args:
        url: The product checkout/permalink or product page URL.
        title: Optional product title, used to make the returned message
            friendlier.
        open_browser: Whether to attempt opening the URL in the default web
            browser (default True). If False, the link is returned without
            opening anything.

    Returns:
        A dict. On success:
            {"success": True, "url": ..., "title": ..., "browser_opened": bool,
             "message": ...}
        If no URL is provided:
            {"success": False, "error": "No URL provided"}.
    """
    if not url:
        return {"success": False, "error": "No URL provided"}

    opened = False
    if open_browser:
        try:
            webbrowser.open(url)
            opened = True
        except Exception:
            opened = False

    message = f"Opening checkout for: {title}" if opened else f"Checkout URL: {url}"

    return {
        "success": True,
        "url": url,
        "title": title,
        "browser_opened": opened,
        "message": message,
    }
