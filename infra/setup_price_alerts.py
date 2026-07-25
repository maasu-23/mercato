"""Provision the SNS topic and IAM role the scheduled price checker needs.

Creates the mercato-price-alerts topic, subscribes ALERT_EMAIL to it, and creates
the price checker's execution role with permission to publish to that topic.

Safe to re-run: every step checks for what it is about to create first, and the
inline policy write overwrites rather than duplicating. The one thing a re-run
cannot undo is the confirmation email AWS sends on a new subscription — see the
note printed after the subscribe step.

Run this before deploying infra/price_checker_handler.py, then put the topic ARN
it prints into .env as SNS_TOPIC_ARN.
"""

import json
import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

console = Console()

DEFAULT_REGION = "ap-south-1"

TOPIC_NAME = "mercato-price-alerts"
ROLE_NAME = "mercato-price-checker-role"
PUBLISH_POLICY_NAME = "mercato-price-alerts-publish"

# Matches the tagging setup.py applies to every resource it creates, so price
# alert infrastructure shows up under the same cost attribution as the rest.
PROJECT_TAGS = {"Project": "mercato"}

# .env.example ships a placeholder address. Subscribing it would fire a real
# confirmation email at a domain reserved for documentation and leave a dead
# subscription on the topic, so treat these exactly as if ALERT_EMAIL were unset.
EMAIL_PLACEHOLDERS = {
    "",
    "your-email@example.com",
    "your@email.com",
    "changeme",
}

LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

# The same broad managed policies deploy_lambda.py attaches to mercato-lambda-role,
# and for the same reasons — the price checker runs the whole agent loop, so it
# needs everything the agent Lambda does:
#   DynamoDB — scan for alert-bearing items, then clear a triggered alert_threshold
#   Bedrock  — get_current_price asks the agent what a product costs
#   S3       — agent.chat writes session transcripts to S3_BUCKET_NAME
# Broad for a research project; worth narrowing to least-privilege before anything
# production-facing. The sns:Publish grant below is deliberately not part of this
# list, so it can stay scoped to the one topic.
MANAGED_POLICY_ARNS = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
    "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
    "arn:aws:iam::aws:policy/AmazonS3FullAccess",
]


def load_config() -> dict:
    load_dotenv()
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "alert_email": os.getenv("ALERT_EMAIL", "").strip(),
    }


def get_clients(region: str) -> dict:
    return {
        "sns": boto3.client("sns", region_name=region),
        # IAM is global — no region, matching how deploy_lambda.py builds it.
        "iam": boto3.client("iam"),
    }


def verify_alert_email(alert_email: str) -> None:
    """Exit unless ALERT_EMAIL holds something worth subscribing.

    There is nothing useful this script can do without a destination address: the
    topic alone notifies no one.
    """
    if alert_email.lower() in EMAIL_PLACEHOLDERS:
        console.print(
            "[red]✘[/red] ALERT_EMAIL is not set in your .env file (or is still the "
            "placeholder from .env.example)."
        )
        console.print(
            "  Set it to the address that should receive price drop alerts, then "
            "re-run this script:\n"
            "  [cyan]ALERT_EMAIL=you@example.com[/cyan]"
        )
        sys.exit(1)

    if "@" not in alert_email:
        console.print(
            f"[red]✘[/red] ALERT_EMAIL '{alert_email}' does not look like an email "
            "address — SNS will reject it."
        )
        sys.exit(1)

    console.print(f"[green]✔[/green] Alerts will be sent to [bold]{alert_email}[/bold]")


def find_topic_arn(sns, topic_name: str) -> str | None:
    """Return the ARN of an existing topic with this name, or None.

    Paginated: an account with more than 100 topics returns them in pages, and
    stopping at the first page would create a duplicate topic.
    """
    try:
        for page in sns.get_paginator("list_topics").paginate():
            for topic in page.get("Topics", []):
                arn = topic.get("TopicArn", "")
                if arn.rsplit(":", 1)[-1] == topic_name:
                    return arn
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Could not list SNS topics: {e}")
        sys.exit(1)

    return None


def tag_topic(sns, topic_arn: str) -> None:
    """Apply project tags. Idempotent — tag_resource overwrites existing keys."""
    try:
        sns.tag_resource(
            ResourceArn=topic_arn,
            Tags=[{"Key": k, "Value": v} for k, v in PROJECT_TAGS.items()],
        )
        console.print(f"  [green]✔[/green] Tagged the topic with {PROJECT_TAGS}")
    except (ClientError, BotoCoreError) as e:
        console.print(f"  [red]✘[/red] Failed to tag the topic: {e}")


def ensure_topic(sns, topic_name: str) -> str:
    """Create the alerts topic if it does not already exist. Returns its ARN."""
    existing = find_topic_arn(sns, topic_name)
    if existing:
        console.print(f"[yellow]![/yellow] SNS topic '{topic_name}' already exists — reusing it")
        tag_topic(sns, existing)
        return existing

    try:
        topic_arn = sns.create_topic(
            Name=topic_name,
            Tags=[{"Key": k, "Value": v} for k, v in PROJECT_TAGS.items()],
        )["TopicArn"]
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to create SNS topic '{topic_name}': {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Created SNS topic '{topic_name}'")
    return topic_arn


def find_subscription(sns, topic_arn: str, alert_email: str) -> dict | None:
    """Return the existing subscription for this address on this topic, or None.

    Matched on Endpoint rather than ARN, since a pending subscription has no real
    ARN yet — SubscriptionArn reads literally "PendingConfirmation" until the
    recipient clicks through.
    """
    target = alert_email.lower()

    try:
        paginator = sns.get_paginator("list_subscriptions_by_topic")
        for page in paginator.paginate(TopicArn=topic_arn):
            for subscription in page.get("Subscriptions", []):
                if subscription.get("Endpoint", "").lower() == target:
                    return subscription
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Could not list subscriptions for the topic: {e}")
        sys.exit(1)

    return None


def print_confirmation_notice(alert_email: str, already_pending: bool) -> None:
    """Make the pending-confirmation step impossible to miss.

    An unconfirmed email subscription silently receives nothing. Every alert would
    publish successfully and land nowhere, which looks identical to the price
    checker not working.
    """
    if already_pending:
        headline = "This subscription is still waiting to be confirmed."
        action = (
            f"AWS already emailed [bold]{alert_email}[/bold]. Find that message and "
            "click\nthe confirmation link. No alerts are delivered until you do."
        )
    else:
        headline = "AWS has just sent a confirmation email."
        action = (
            f"Check [bold]{alert_email}[/bold] and click the confirmation link in the\n"
            "message from AWS Notifications. Until then the subscription stays in\n"
            "PendingConfirmation and every alert published to this topic is dropped."
        )

    console.print(
        Panel(
            f"[bold yellow]{headline}[/bold yellow]\n\n{action}\n\n"
            "[dim]The link expires after 3 days. If it lapses, re-run this script to "
            "send a new one.[/dim]",
            title="[bold yellow]⚠  Action required — check your inbox[/bold yellow]",
            border_style="yellow",
        )
    )


def ensure_subscription(sns, topic_arn: str, alert_email: str) -> None:
    """Subscribe the alert address to the topic, unless it is already subscribed."""
    existing = find_subscription(sns, topic_arn, alert_email)

    if existing:
        subscription_arn = existing.get("SubscriptionArn", "")
        if subscription_arn == "PendingConfirmation":
            console.print(
                f"[yellow]![/yellow] '{alert_email}' is already subscribed but "
                "[bold]not yet confirmed[/bold]"
            )
            print_confirmation_notice(alert_email, already_pending=True)
        else:
            console.print(
                f"[green]✔[/green] '{alert_email}' is already subscribed and confirmed "
                "— nothing to do"
            )
        return

    try:
        sns.subscribe(
            TopicArn=topic_arn,
            Protocol="email",
            Endpoint=alert_email,
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to subscribe '{alert_email}': {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Subscribed '{alert_email}' to the topic")
    print_confirmation_notice(alert_email, already_pending=False)


def ensure_price_checker_role(iam) -> str:
    """Create the price checker's execution role and attach its managed policies.

    Follows the same shape as deploy_lambda.ensure_execution_role — the Lambda
    itself has not been deployed yet, so this is where its role first appears.

    Returns the role ARN.
    """
    try:
        role_arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
            Description="Execution role for the Mercato scheduled price checker Lambda",
            Tags=[{"Key": k, "Value": v} for k, v in PROJECT_TAGS.items()],
        )["Role"]["Arn"]
        console.print(f"[green]✔[/green] Created IAM role '{ROLE_NAME}'")
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        console.print(f"[yellow]![/yellow] IAM role '{ROLE_NAME}' already exists — reusing it")
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to create IAM role '{ROLE_NAME}': {e}")
        sys.exit(1)

    for policy_arn in MANAGED_POLICY_ARNS:
        try:
            # attach_role_policy is idempotent, so re-attaching is safe on a re-run.
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
            console.print(f"  [green]✔[/green] Attached {policy_arn.split('/')[-1]}")
        except (ClientError, BotoCoreError) as e:
            console.print(f"[red]✘[/red] Failed to attach {policy_arn}: {e}")
            sys.exit(1)

    return role_arn


def grant_sns_publish(iam, topic_arn: str) -> None:
    """Allow the price checker role to publish to this one topic.

    Written as an inline policy scoped to the exact topic ARN rather than
    AmazonSNSFullAccess or a "sns:Publish on *" wildcard: a role that can publish
    to every topic in the account can also reach anything else subscribed to them,
    and this Lambda only ever needs the one.
    """
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublishPriceAlerts",
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": topic_arn,
            }
        ],
    }

    try:
        # put_role_policy replaces a policy of the same name outright, so a re-run
        # updates in place instead of stacking duplicates.
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=PUBLISH_POLICY_NAME,
            PolicyDocument=json.dumps(policy_document),
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to grant sns:Publish to '{ROLE_NAME}': {e}")
        sys.exit(1)

    console.print(
        f"[green]✔[/green] Granted sns:Publish on the alerts topic to '{ROLE_NAME}' "
        f"(inline policy '{PUBLISH_POLICY_NAME}')"
    )


def print_summary(topic_arn: str, role_arn: str) -> None:
    console.print(f"[bold cyan]SNS topic ARN:[/bold cyan] {topic_arn}")
    console.print(f"[bold cyan]Execution role ARN:[/bold cyan] {role_arn}")
    console.print()
    console.print(
        Panel(
            "Add this line to your [bold].env[/bold] file:\n\n"
            f"  [bold green]SNS_TOPIC_ARN={topic_arn}[/bold green]\n\n"
            "[dim]The price checker only publishes when SNS_TOPIC_ARN is set. Without "
            "it the run\ncompletes and logs what it found, but sends nothing — and the "
            "alerts it consumed\nare gone.[/dim]",
            title="[bold]Next step[/bold]",
            border_style="cyan",
        )
    )


def main() -> None:
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
    # Without this, setup crashes mid-run and leaves infrastructure half-created.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()

    console.rule("[bold]Mercato price alert setup[/bold]")
    console.print(f"Region: {config['region']}\n")

    verify_alert_email(config["alert_email"])

    try:
        clients = get_clients(config["region"])
    except NoCredentialsError:
        console.print("[red]✘[/red] No AWS credentials found. Run 'aws configure' first.")
        sys.exit(1)

    console.rule("SNS topic")
    topic_arn = ensure_topic(clients["sns"], TOPIC_NAME)

    console.rule("Email subscription")
    ensure_subscription(clients["sns"], topic_arn, config["alert_email"])

    console.rule("IAM")
    role_arn = ensure_price_checker_role(clients["iam"])
    grant_sns_publish(clients["iam"], topic_arn)

    console.rule("Summary")
    print_summary(topic_arn, role_arn)


if __name__ == "__main__":
    main()
