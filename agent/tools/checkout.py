import webbrowser
from urllib.parse import urlparse

from langchain_core.tools import tool

# url is untrusted: it comes from web_search or ucp_query results, i.e. scraped
# page content or a listing controlled by a third-party (possibly malicious or
# compromised) merchant. webbrowser.open() hands its argument straight to the
# OS's default handler for whatever scheme it's given, so without this check a
# crafted "file:///etc/passwd", "javascript:...", or other unusual URI scheme
# would be opened exactly like a normal product link. Restricting to http(s)
# keeps this tool to what it's meant to do: open a merchant web page.
#
# http is kept alongside https for compatibility with older/smaller merchant
# sites that still don't redirect to TLS — a real tradeoff, since it means this
# tool will happily open an unencrypted page. Drop "http" from this set to
# require https only, at the cost of refusing those sites entirely.
ALLOWED_URL_SCHEMES = {"https", "http"}


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
        If the URL's scheme is not in ALLOWED_URL_SCHEMES (checked regardless of
        open_browser, since url is untrusted and returning it as a "success" is
        itself a hazard even when nothing here opens it):
            {"success": False, "error": "..."}.
    """
    if not url:
        return {"success": False, "error": "No URL provided"}

    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        return {
            "success": False,
            "error": (
                f"Refusing checkout URL with scheme {scheme!r}; only "
                f"{sorted(ALLOWED_URL_SCHEMES)} URLs are allowed"
            ),
        }

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
