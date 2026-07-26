"""Provision the SNS topic and IAM role the scheduled price checker needs.

Creates the mercato-price-alerts topic, subscribes ALERT_EMAIL to it, and creates
the price checker's execution role carrying a single least-privilege inline
policy — no AWS-managed *FullAccess policies at all. See
ensure_least_privilege_policy for why this role is scoped harder than the rest of
the project's roles.

Safe to re-run: every step checks for what it is about to create first, and the
inline policy write overwrites rather than duplicating. A re-run also migrates a
role provisioned by an earlier version of this script, detaching the managed
policies it used to attach and deleting the inline policy it used to write. The
one thing a re-run cannot undo is the confirmation email AWS sends on a new
subscription — see the note printed after the subscribe step.

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
DEFAULT_WISHLIST_TABLE = "mercato-wishlist"
# Mirrors agent/agent.py and infra/price_checker_handler.py, so an unset
# BEDROCK_MODEL_ID scopes the policy to the same model the checker will call.
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"

TOPIC_NAME = "mercato-price-alerts"
ROLE_NAME = "mercato-price-checker-role"
POLICY_NAME = "mercato-price-checker-least-privilege"

# Must match FUNCTION_NAME in infra/deploy_price_checker.py — Lambda derives the
# log group name from it, and the policy below grants writes to that exact group.
# Change one without the other and the function loses its logging.
FUNCTION_NAME = "mercato-price-checker"

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

# Nothing is attached to this role any more — the inline policy built by
# ensure_least_privilege_policy is the whole of its permissions.
#
# These four are what earlier versions of this script attached, kept here only so
# a re-run can take them back off a role that already has them. AWSLambdaBasicExecutionRole
# is in the list too: its logs:* grants are reissued explicitly and scoped to this
# function's own log group, so the managed version is redundant.
LEGACY_MANAGED_POLICY_ARNS = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
    "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
    "arn:aws:iam::aws:policy/AmazonS3FullAccess",
]

# Written by earlier versions of this script; its sns:Publish grant is now one
# statement inside POLICY_NAME, so the standalone policy is deleted on re-run.
LEGACY_INLINE_POLICY_NAMES = ["mercato-price-alerts-publish"]

# Cross-region inference profile prefixes. A model id carrying one of these
# addresses a routing profile rather than a model directly — see
# _split_model_id and the policy comments in build_least_privilege_policy.
INFERENCE_PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.", "us-gov.")


def load_config() -> dict:
    # override=True so .env wins over anything already exported in the shell.
    # Without it an inherited AWS_REGION silently beats the .env value and the
    # topic gets provisioned in a region the rest of the stack is not in.
    load_dotenv(override=True)
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "alert_email": os.getenv("ALERT_EMAIL", "").strip(),
    }


def get_clients(region: str) -> dict:
    return {
        "sns": boto3.client("sns", region_name=region),
        # IAM is global — no region, matching how deploy_lambda.py builds it.
        "iam": boto3.client("iam"),
        "sts": boto3.client("sts", region_name=region),
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
    """Create the price checker's execution role. Returns the role ARN.

    Creates the role and nothing else — no managed policies are attached. Its
    permissions come entirely from the inline policy that
    ensure_least_privilege_policy writes next.
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

    return role_arn


def detach_legacy_managed_policies(iam) -> None:
    """Take the old *FullAccess grants back off a previously-provisioned role.

    Dropping them from this script is not enough on its own: IAM keeps whatever
    was attached before, so a role created by an earlier version would silently
    keep AmazonS3FullAccess and friends for the rest of its life. A re-run has to
    actively detach them for the least-privilege migration to mean anything.

    Only the four policies this script used to attach are removed. Anything else
    someone attached deliberately is reported and left alone.
    """
    try:
        attached = iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Could not list policies attached to '{ROLE_NAME}': {e}")
        sys.exit(1)

    if not attached:
        console.print("[green]✔[/green] No managed policies attached — nothing to detach")
        return

    for policy in attached:
        policy_arn = policy["PolicyArn"]

        if policy_arn not in LEGACY_MANAGED_POLICY_ARNS:
            console.print(
                f"  [yellow]![/yellow] Leaving unrecognised managed policy attached: "
                f"{policy_arn}"
            )
            continue

        try:
            iam.detach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
            console.print(f"  [green]✔[/green] Detached {policy_arn.split('/')[-1]}")
        except (ClientError, BotoCoreError) as e:
            console.print(f"[red]✘[/red] Failed to detach {policy_arn}: {e}")
            sys.exit(1)


def delete_legacy_inline_policies(iam) -> None:
    """Remove inline policies superseded by POLICY_NAME.

    The old standalone sns:Publish policy is now one statement inside the
    least-privilege document, so leaving it behind would duplicate a grant across
    two policies and make the role's real permissions harder to read off.
    """
    for name in LEGACY_INLINE_POLICY_NAMES:
        try:
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName=name)
            console.print(f"  [green]✔[/green] Deleted superseded inline policy '{name}'")
        except iam.exceptions.NoSuchEntityException:
            # Never existed, or already cleaned up by a previous run.
            continue
        except (ClientError, BotoCoreError) as e:
            console.print(f"[red]✘[/red] Failed to delete inline policy '{name}': {e}")
            sys.exit(1)


def _split_model_id(model_id: str) -> tuple[str | None, str]:
    """Split a Bedrock model id into (inference profile id, foundation model id).

    A model id carrying a region prefix — "global.", "us.", "eu.", "apac." —
    names a cross-region inference profile, which routes the call to a foundation
    model whose own id is the same string with the prefix removed. Both identities
    need to appear in the policy; see build_least_privilege_policy.

    A model id with no such prefix addresses a foundation model directly, so there
    is no profile to authorise and the first element comes back None.
    """
    for prefix in INFERENCE_PROFILE_PREFIXES:
        if model_id.startswith(prefix):
            return model_id, model_id[len(prefix):]

    return None, model_id


def build_least_privilege_policy(
    region: str, account_id: str, topic_arn: str, wishlist_table: str, model_id: str
) -> dict:
    """Build the one policy document that is the whole of this role's permissions.

    DELIBERATELY least-privilege, unlike mercato-lambda-role and mercato-agent,
    which carry broad AWS-managed *FullAccess policies. That difference is
    intentional and should not be "made consistent" by widening this one.

    The price checker is the only component that feeds untrusted third-party text
    — product titles, URLs and merchant names scraped off merchant pages — into a
    model, on an unattended daily schedule with nobody reading the output. An
    audit of this feature found the broad grants materially increased the blast
    radius of that exposure: AmazonS3FullAccess reaches every bucket in the
    account, and AmazonBedrockFullAccess allows provisioned-throughput purchases.
    Neither is anywhere near what the code calls. See
    infra/price_checker_handler.get_current_price for the matching narrowing on
    the model side.

    Every statement below maps to a call the handler actually makes. Nothing is
    included speculatively — notably no S3 at all, since dropping the agent loop
    also dropped the session-transcript write that used to need it.
    """
    inference_profile_id, foundation_model_id = _split_model_id(model_id)

    # Invoking through an inference profile is authorised against BOTH the profile
    # and the foundation model behind it, and a policy naming only one of them
    # fails with AccessDeniedException:
    #
    #   - the profile ARN is regional and account-owned, and carries the full
    #     prefixed id ("global.anthropic.claude-...")
    #   - the foundation-model ARN is AWS-owned, so its account field is empty
    #     ("arn:aws:bedrock:*::foundation-model/..."), carries the unprefixed id,
    #     and needs the region wildcard because a global profile routes the call
    #     to whichever region has capacity
    #
    # WARNING: this is scoped to one model. Changing BEDROCK_MODEL_ID in .env
    # without re-running this script leaves the role unable to invoke the new
    # model, and the price checker fails on every item with AccessDeniedException.
    bedrock_resources = [f"arn:aws:bedrock:*::foundation-model/{foundation_model_id}"]
    if inference_profile_id:
        bedrock_resources.insert(
            0,
            f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{inference_profile_id}",
        )

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                # scan_wishlist_for_alerts scans for alert-bearing items;
                # _claim_alert and _restore_alert_threshold conditionally update
                # the threshold on one item. Conditional writes need no extra
                # permission beyond UpdateItem.
                # No Query/PutItem/GetItem — this handler makes none of those
                # calls — and no DescribeTable, since boto3's Table() resource is
                # lazy and never fetches metadata here. The sessions table is not
                # touched at all.
                "Sid": "WishlistTableAccess",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:Scan",
                    "dynamodb:UpdateItem",
                ],
                "Resource": f"arn:aws:dynamodb:{region}:{account_id}:table/{wishlist_table}",
            },
            {
                # get_current_price makes one Converse call per item. The Converse
                # API authorises against bedrock:InvokeModel; the streaming
                # variant would additionally need
                # bedrock:InvokeModelWithResponseStream, which is deliberately
                # absent because the handler calls .invoke(), not .stream().
                "Sid": "InvokeBedrockModel",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": bedrock_resources,
            },
            {
                # publish_alert sends one message per triggered alert. Scoped to
                # the one topic: a role that can publish to every topic in the
                # account can reach anything else subscribed to them.
                "Sid": "PublishPriceAlerts",
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": topic_arn,
            },
            {
                # Replaces AWSLambdaBasicExecutionRole. CreateLogGroup cannot be
                # scoped to a single group — the group does not exist yet when
                # Lambda creates it — so it takes the account/region wildcard that
                # the managed policy uses too.
                "Sid": "CreateFunctionLogGroup",
                "Effect": "Allow",
                "Action": "logs:CreateLogGroup",
                "Resource": f"arn:aws:logs:{region}:{account_id}:*",
            },
            {
                # Writing, unlike creating, is scoped to this function's own log
                # group — which is the part AWSLambdaBasicExecutionRole leaves
                # open across every function in the account.
                "Sid": "WriteFunctionLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": (
                    f"arn:aws:logs:{region}:{account_id}:log-group:"
                    f"/aws/lambda/{FUNCTION_NAME}:*"
                ),
            },
        ],
    }


def ensure_least_privilege_policy(iam, sts, region: str, topic_arn: str) -> None:
    """Write the price checker's sole inline policy, replacing whatever came before.

    Resolves the account id at runtime rather than hardcoding it, the same way
    setup_api_gateway.py resolves it for the API Gateway invoke permission.

    Idempotent in both directions: put_role_policy replaces a policy of the same
    name outright, and the legacy managed and inline policies are removed first,
    so re-running against a role provisioned by an earlier version of this script
    migrates it rather than layering on top of it.
    """
    try:
        account_id = sts.get_caller_identity()["Account"]
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        console.print(f"[red]✘[/red] Could not resolve your AWS account id: {e}")
        sys.exit(1)

    wishlist_table = os.getenv("DYNAMODB_WISHLIST_TABLE", DEFAULT_WISHLIST_TABLE)
    model_id = os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)

    console.print("Removing the broad grants this role used to carry ...")
    detach_legacy_managed_policies(iam)
    delete_legacy_inline_policies(iam)

    policy_document = build_least_privilege_policy(
        region=region,
        account_id=account_id,
        topic_arn=topic_arn,
        wishlist_table=wishlist_table,
        model_id=model_id,
    )

    try:
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(policy_document),
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to write inline policy to '{ROLE_NAME}': {e}")
        sys.exit(1)

    console.print(
        f"[green]✔[/green] Wrote least-privilege inline policy '{POLICY_NAME}' "
        f"to '{ROLE_NAME}'"
    )
    for statement in policy_document["Statement"]:
        actions = statement["Action"]
        actions = [actions] if isinstance(actions, str) else actions
        console.print(f"  [dim]{statement['Sid']}: {', '.join(actions)}[/dim]")

    console.print(f"  [dim]Wishlist table: {wishlist_table}[/dim]")
    console.print(f"  [dim]Bedrock model:  {model_id}[/dim]")


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
    console.print(
        Panel(
            f"[bold]{ROLE_NAME}[/bold] carries one inline policy and no managed "
            "policies.\n\n"
            "[dim]It is scoped to the wishlist table, one Bedrock model, one SNS "
            "topic, and its\nown log group. Changing BEDROCK_MODEL_ID or "
            "DYNAMODB_WISHLIST_TABLE in .env means\nre-running this script — the "
            "policy names those resources explicitly, and the\nchecker will fail "
            "with AccessDeniedException until it is rewritten.[/dim]",
            title="[bold]Permissions[/bold]",
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
    ensure_least_privilege_policy(
        clients["iam"], clients["sts"], config["region"], topic_arn
    )

    console.rule("Summary")
    print_summary(topic_arn, role_arn)


if __name__ == "__main__":
    main()
