"""Scheduled Lambda that checks wishlist price alerts.

Scans the wishlist table for items carrying an alert_threshold (written by
save_wishlist), asks the agent what each one currently costs, and reports the
ones that have dropped to or below their threshold.

Notifications are live when SNS_TOPIC_ARN is configured: each triggered alert is
published to that topic, which fans out to whatever is subscribed (ALERT_EMAIL,
typically). With SNS_TOPIC_ARN unset the run still happens end to end but sends
nothing, so an unconfigured environment is safe to invoke — triggered alerts are
printed to CloudWatch and returned in the response either way.

A triggered alert is consumed: its alert_threshold is removed from the item so
the same price drop is not reported on every subsequent run.

Unlike infra/lambda_handler.py this is not fronted by API Gateway, so the return
value is a plain dict for the invoker/logs rather than an HTTP response shape.
"""

import json
import os
import traceback
from decimal import Decimal, InvalidOperation

import boto3

DEFAULT_REGION = "ap-south-1"
DEFAULT_WISHLIST_TABLE = "mercato-wishlist"

SUBJECT_PREFIX = "Mercato Price Alert: "
# SNS requires a Subject of ASCII text under 100 characters with no line breaks.
MAX_SUBJECT_LENGTH = 99

PRICE_PROMPT = (
    "What is the current price of this exact product?\n\n"
    "{product}\n\n"
    "Price the specific listing identified above. Do not price a similarly-named "
    "product, a different variant (size, colour, capacity, model year), or the "
    "same product from a different seller. "
    "Reply with ONLY the numeric price, no currency symbol, no text, no "
    "explanation. If you cannot find a price for this exact product, reply with "
    "exactly: UNKNOWN"
)


def _config() -> dict:
    """Read runtime configuration from the environment, matching the other infra files."""
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "wishlist_table": os.getenv("DYNAMODB_WISHLIST_TABLE", DEFAULT_WISHLIST_TABLE),
        # No default: an empty topic ARN is the signal to skip publishing, and a
        # made-up default would only turn that into a runtime failure.
        "sns_topic_arn": os.getenv("SNS_TOPIC_ARN", ""),
    }


def _get_table():
    """Return the wishlist DynamoDB table handle."""
    config = _config()
    resource = boto3.resource("dynamodb", region_name=config["region"])
    return resource.Table(config["wishlist_table"])


def _as_float(value) -> float | None:
    """Coerce a DynamoDB attribute to float, or None if it will not convert.

    Thresholds are written by save_wishlist as strings (the Python SDK cannot
    serialize native floats), but DynamoDB hands numeric attributes back as
    Decimal, and an item written by some other path may be either. Accept both
    rather than assuming one.
    """
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            return float(value)
        return float(str(value).strip())
    except (TypeError, ValueError, InvalidOperation):
        return None


def _product_description(title: str, url: str = "", merchant: str = "") -> str:
    """Format a saved product's identifying details for the price prompt.

    url and merchant are optional on wishlist items (save_wishlist defaults
    merchant to ""), so only the fields actually stored are included — an empty
    "URL:" line would just invite the agent to fill the gap by guessing.
    """
    lines = [f"Title: {title}"]
    if url:
        lines.append(f"URL: {url}")
    if merchant:
        lines.append(f"Merchant: {merchant}")
    return "\n".join(lines)


def _alert_subject(title: str) -> str:
    """Build the SNS Subject line for an alert, trimmed to what SNS will accept.

    A product title is arbitrary text pulled off a merchant page, but SNS rejects
    a Subject that runs past 100 characters, breaks a line, or carries non-ASCII —
    so an em dash or a long title would fail the publish outright rather than just
    read badly. Whitespace is collapsed, non-ASCII dropped, and the result cut to
    length.
    """
    flattened = " ".join(str(title).split())
    ascii_only = flattened.encode("ascii", "ignore").decode("ascii").strip()
    subject = f"{SUBJECT_PREFIX}{ascii_only}" if ascii_only else "Mercato Price Alert"

    if len(subject) > MAX_SUBJECT_LENGTH:
        subject = subject[: MAX_SUBJECT_LENGTH - 3] + "..."
    return subject


def publish_alert(sns_client, topic_arn: str, alert: dict) -> bool:
    """Publish one triggered alert to the SNS topic.

    Args:
        sns_client: A boto3 SNS client, created once by the caller and reused for
            every alert in the run.
        topic_arn: The topic to publish to. Assumed non-empty — the caller decides
            whether notifications are configured at all.
        alert: One result dict from check_all_alerts.

    Returns:
        True if SNS accepted the publish, False if it did not. Never raises: one
        undeliverable alert must not abort the alerts behind it or fail the run,
        and the alert is reported in the response regardless.
    """
    title = alert.get("title", "this item")

    lines = [
        f"{title} has dropped to the price you were watching for.",
        "",
        f"Current price: {alert.get('current_price')}",
        f"Your alert price: {alert.get('alert_threshold')}",
    ]

    url = alert.get("url", "")
    if url:
        lines.extend(["", url])

    if alert.get("alert_cleared"):
        lines.extend(
            [
                "",
                "This alert is now used up. Save the item again with a new alert "
                "price to keep watching it.",
            ]
        )
    else:
        # The threshold could not be removed from the item, so the next scheduled
        # run will find it again. Say so, rather than let a repeat notification
        # look like a second, separate price drop.
        lines.extend(
            [
                "",
                "Note: this alert could not be cleared, so you may receive it "
                "again on the next check.",
            ]
        )

    try:
        sns_client.publish(
            TopicArn=topic_arn,
            Subject=_alert_subject(title),
            Message="\n".join(lines),
        )
        return True
    except Exception as e:
        print(f"[price-check] WARNING failed to publish alert for {title!r}: {e}")
        return False


def _clear_alert_threshold(user_id: str, product_id: str) -> bool:
    """Remove alert_threshold from a single wishlist item, consuming its alert.

    Only the one attribute is removed; the item stays on the wishlist with its
    title, url, price and saved_at untouched. Once cleared the item no longer
    matches the scan filter, so it is not checked again until the user sets a new
    threshold.

    Returns:
        True if the attribute was removed, False if the key was incomplete or the
        update failed. Never raises — the caller has already decided to notify,
        and a bookkeeping failure must not suppress that.
    """
    if not user_id or not product_id:
        print(
            f"[price-check] WARNING cannot clear alert, incomplete key "
            f"user={user_id!r} product_id={product_id!r} — alert will re-fire"
        )
        return False

    try:
        _get_table().update_item(
            Key={"user_id": user_id, "product_id": product_id},
            UpdateExpression="REMOVE alert_threshold",
        )
        return True
    except Exception as e:
        print(
            f"[price-check] WARNING failed to clear alert for product_id="
            f"{product_id} user={user_id}: {e} — alert will re-fire next run"
        )
        return False


def scan_wishlist_for_alerts() -> list[dict]:
    """Return every wishlist item that has an alert_threshold set, across all users.

    Uses scan rather than query on purpose: the table is partitioned by user_id
    and this needs items belonging to everyone, so there is no single partition
    key to query on. The filter is applied after the read, so this still pays for
    scanning the whole table — fine at current size, worth revisiting (a sparse
    GSI on alert_threshold) if the wishlist grows large.
    """
    table = _get_table()
    items: list[dict] = []
    scan_kwargs = {"FilterExpression": "attribute_exists(alert_threshold)"}

    # A scan returns at most 1 MB per call, so follow LastEvaluatedKey to the end
    # rather than silently checking only the first page of alerts.
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        scan_kwargs["ExclusiveStartKey"] = last_key


def get_current_price(
    title: str, user_id: str, url: str = "", merchant: str = ""
) -> float | None:
    """Ask the agent for the current price of a specific saved product.

    Reuses the full agent loop (search, UCP query, price comparison) instead of
    reimplementing price scraping here, so this stays in sync with whatever the
    agent's tools can do.

    The url and merchant are fed to the agent alongside the title so it prices
    the listing the user actually saved. Title alone is ambiguous — it matches
    other variants and other sellers, which is how an alert ends up firing on a
    product the user was never watching.

    Cost note: this spends a real Bedrock call — usually several, since the agent
    runs its own tool loop — for every wishlist item that has an alert. Total
    spend scales with (number of users) x (number of alert items per user) x
    (check frequency), so an hourly schedule over a few hundred alert items is a
    meaningfully larger bill than it looks. Pick the schedule accordingly.

    Args:
        title: The product title to price.
        user_id: The owning user, passed through so the agent's S3 session
            logging still attributes the transcript to the right person.
        url: The saved product URL, when the item has one.
        merchant: The saved merchant name, when the item has one.

    Returns:
        The parsed price as a float, or None if the agent answered UNKNOWN, gave
        something unparseable, or the call itself failed. Never raises.
    """
    # Imported here rather than at module scope: the import pulls in LangChain and
    # builds the tool registry, and keeping it out of the module body means a
    # config or dependency problem surfaces as a handled None instead of an
    # import-time crash that Lambda reports with no context.
    try:
        from agent.agent import chat

        product = _product_description(title, url, merchant)
        reply, _ = chat(PRICE_PROMPT.format(product=product), [], user_id)
    except Exception as e:
        print(f"[price-check] agent call failed for {title!r}: {e}")
        return None

    cleaned = str(reply).strip()
    if cleaned.upper() == "UNKNOWN":
        return None

    try:
        return float(cleaned)
    except (TypeError, ValueError):
        print(f"[price-check] unparseable price reply for {title!r}: {cleaned!r}")
        return None


def check_all_alerts(items: list[dict] | None = None) -> list[dict]:
    """Check every alert-bearing wishlist item and return the ones that should notify.

    Prints one summary line per item so a scheduled run is readable in CloudWatch.

    Triggering an alert consumes it — see the inline comment on the clear step.

    Args:
        items: Optional pre-fetched scan result. Defaults to scanning the table
            itself; the handler passes its own scan in so a single run does not
            pay for the table scan twice.

    Returns:
        A list of dicts, one per item whose current price is at or below its
        threshold, each with user_id, title, url, alert_threshold, current_price,
        should_notify, and alert_cleared (False means clearing the threshold
        failed and this alert will fire again on the next run).
    """
    if items is None:
        items = scan_wishlist_for_alerts()
    print(f"[price-check] {len(items)} wishlist item(s) with an alert_threshold")

    results: list[dict] = []

    for item in items:
        title = item.get("title", "")
        user_id = item.get("user_id", "")
        product_id = item.get("product_id", "")
        url = item.get("url", "")
        merchant = item.get("merchant", "")
        threshold = _as_float(item.get("alert_threshold"))

        if not title:
            print(f"[price-check] SKIP (no title) user={user_id} url={url}")
            continue

        if threshold is None:
            print(
                f"[price-check] SKIP (bad threshold "
                f"{item.get('alert_threshold')!r}) {title!r} user={user_id}"
            )
            continue

        current_price = get_current_price(title, user_id, url, merchant)

        if current_price is None:
            print(f"[price-check] NO PRICE FOUND {title!r} (threshold {threshold})")
            continue

        if current_price <= threshold:
            print(
                f"[price-check] ALERT {title!r} at {current_price} "
                f"<= threshold {threshold} user={user_id}"
            )

            # Consume the alert by removing alert_threshold from the item. Without
            # this, a price that stays below the threshold re-triggers on every
            # scheduled run — one notification per run, indefinitely, for a single
            # price drop. The item itself is left on the wishlist; only the alert
            # is spent. A user who wants to keep watching re-saves it with a new
            # threshold.
            #
            # Clearing is best-effort: a failed update means a duplicate alert
            # next run, which is far better than dropping a notification the user
            # asked for, so the item is still reported either way.
            alert_cleared = _clear_alert_threshold(user_id, product_id)

            results.append(
                {
                    "user_id": user_id,
                    "title": title,
                    "url": url,
                    "alert_threshold": threshold,
                    "current_price": current_price,
                    "should_notify": True,
                    "alert_cleared": alert_cleared,
                }
            )
        else:
            print(
                f"[price-check] no alert {title!r} at {current_price} "
                f"> threshold {threshold}"
            )

    return results


def handler(event, context):
    """Entry point for the scheduled price check.

    Scans, prices, notifies. Returns a 200 with the triggered alerts and how many
    of them reached SNS, or a 500 carrying the error message. This runs unattended
    on a schedule, so failures are logged with a full traceback and reported in the
    return value rather than passing silently.
    """
    try:
        config = _config()
        items = scan_wishlist_for_alerts()
        results = check_all_alerts(items)

        # Log the triggered alerts as one JSON blob regardless of whether they were
        # published, so a run can be inspected after the fact without replaying it.
        print(f"[price-check] results: {json.dumps(results, default=str)}")

        published = 0
        topic_arn = config["sns_topic_arn"]

        if not topic_arn:
            # No topic configured: complete the run but send nothing, so an
            # unprovisioned environment is safe to invoke. Note the alerts have
            # already been consumed by this point — the log above is the only
            # record of what would have gone out.
            print(
                "[price-check] SNS_TOPIC_ARN not set — notifications skipped, "
                "alerts logged only"
            )
        elif results:
            sns_client = boto3.client("sns", region_name=config["region"])
            for alert in results:
                if publish_alert(sns_client, topic_arn, alert):
                    published += 1
            print(f"[price-check] published {published}/{len(results)} alert(s)")

        return {
            "statusCode": 200,
            "checked": len(items),
            "alerts_triggered": len(results),
            "published": published,
            "results": results,
        }
    except Exception as e:
        print(traceback.format_exc())
        return {"statusCode": 500, "error": str(e)}
