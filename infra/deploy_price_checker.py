"""Deploy the scheduled price checker Lambda and its daily trigger.

Uploads the price-checker package, creates (or updates) the mercato-price-checker
function against the role setup_price_alerts.py already provisioned, then wires an
EventBridge rule that invokes it once a day.

Safe to re-run: the function is created only if absent and otherwise has its code
and configuration reconciled, put_rule and put_targets both overwrite in place, and
the invoke permission is skipped when it already exists.

Prerequisites, in order:
    python infra/setup_price_alerts.py                   # topic + execution role
    python infra/build_lambda_package.py price-checker   # deployment package
    python infra/deploy_price_checker.py                 # this script
"""

import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

console = Console()

ROOT_DIR = Path(__file__).resolve().parent.parent
ZIP_PATH = ROOT_DIR / "build" / "mercato-price-checker.zip"

DEFAULT_REGION = "ap-south-1"
FUNCTION_NAME = "mercato-price-checker"
S3_KEY = "lambda/mercato-price-checker.zip"

# Created by setup_price_alerts.py, which also writes the inline policy granting
# sns:Publish on the alerts topic. Deliberately not created here — a role built by
# this script would be missing that grant, and every alert would fail to publish.
ROLE_NAME = "mercato-price-checker-role"

LAMBDA_RUNTIME = "python3.11"
LAMBDA_HANDLER = "price_checker_handler.handler"
# Items are checked one at a time and each one runs a full agent loop (search, UCP
# query, Bedrock turns), so the ceiling scales with how many alerts are pending.
# 5 minutes is Lambda's practical middle ground here; the agent Lambda's 60s is far
# too short for a batch.
LAMBDA_TIMEOUT = 300
LAMBDA_MEMORY_MB = 512

RULE_NAME = "mercato-price-checker-daily"
# 03:30 UTC is 09:00 IST — EventBridge schedules are UTC-only, with no local time
# zone and no daylight saving adjustment (India has none either, so this holds all
# year). Fields are: minute hour day-of-month month day-of-week year.
SCHEDULE_EXPRESSION = "cron(30 3 * * ? *)"
SCHEDULE_DESCRIPTION_IST = "09:00 IST daily"
TARGET_ID = "mercato-price-checker-target"
PERMISSION_STATEMENT_ID = "mercato-schedule-invoke"

PROJECT_TAGS = {"Project": "mercato"}

# AWS_REGION is reserved: Lambda populates it from the function's own region and
# rejects CreateFunction/UpdateFunctionConfiguration outright if it appears here.
# The handler's _config() reads it from the runtime-provided value instead.
RESERVED_ENV_VARS = {"AWS_REGION"}

# IAM is eventually consistent: a role created moments ago by setup_price_alerts.py
# is not immediately assumable by Lambda, and CreateFunction fails with
# InvalidParameterValueException until it propagates. There is no waiter, so retry.
ROLE_PROPAGATION_ATTEMPTS = 12
ROLE_PROPAGATION_DELAY = 5


def load_config() -> dict:
    # override=True so .env wins over anything already exported in the shell.
    # Without it an inherited AWS_REGION silently beats the .env value and the
    # function gets deployed to a region the rest of the stack is not in.
    load_dotenv(override=True)
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        # Deploy-time only: where the zip is uploaded for Lambda to pull from.
        # Not passed to the function — the handler makes no S3 calls.
        "bucket_name": os.getenv("S3_BUCKET_NAME", ""),
        # Exactly what price_checker_handler._config() reads, and nothing else.
        #
        # S3_BUCKET_NAME, DYNAMODB_SESSIONS_TABLE and TAVILY_API_KEY used to be
        # here because the handler ran the full agent loop, which wrote session
        # transcripts to S3 and could reach the Tavily-backed web_search tool.
        # That loop is gone (see price_checker_handler.get_current_price), so
        # they are dead config — and TAVILY_API_KEY is a live billable secret,
        # readable by anyone with lambda:GetFunctionConfiguration, on a function
        # that can no longer use it.
        #
        # AWS_REGION is absent because Lambda reserves it and populates it from
        # the function's own region; see RESERVED_ENV_VARS.
        "env_vars": {
            "DYNAMODB_WISHLIST_TABLE": os.getenv("DYNAMODB_WISHLIST_TABLE", "mercato-wishlist"),
            "BEDROCK_MODEL_ID": os.getenv("BEDROCK_MODEL_ID", ""),
            "SNS_TOPIC_ARN": os.getenv("SNS_TOPIC_ARN", ""),
        },
    }


def verify_env_vars(env_vars: dict, region: str) -> None:
    """Check the function's environment for the mistakes that fail silently.

    None of these abort the deploy — the handler tolerates a missing topic and the
    rest surface as ordinary runtime errors — but an alert that publishes nowhere
    looks identical to a price checker that simply found no price drops, so it is
    worth saying out loud at deploy time.
    """
    for name in RESERVED_ENV_VARS & env_vars.keys():
        console.print(
            f"[red]✘[/red] '{name}' is reserved by Lambda and cannot be set on a "
            "function — remove it from the environment block"
        )
        sys.exit(1)

    topic_arn = env_vars["SNS_TOPIC_ARN"]

    if not topic_arn:
        console.print(
            "[yellow]![/yellow] SNS_TOPIC_ARN is not set in .env — the function will "
            "deploy and run, but it will log alerts instead of emailing them"
        )
        console.print(
            "  [dim]Run infra/setup_price_alerts.py and copy the topic ARN it prints "
            "into .env.[/dim]"
        )
        return

    # An SNS client can only address topics in its own region, so a topic ARN from
    # another region fails every publish with "Invalid parameter: TopicArn" — after
    # the alert has already been consumed off the wishlist.
    topic_region = topic_arn.split(":")[3] if topic_arn.count(":") >= 5 else ""
    if topic_region != region:
        console.print(
            f"[red]✘[/red] SNS_TOPIC_ARN is in region '{topic_region}' but this "
            f"function deploys to '{region}' — every publish would be rejected"
        )
        console.print(f"  [dim]{topic_arn}[/dim]")
        sys.exit(1)

    console.print(f"[green]✔[/green] Alerts will publish to {topic_arn}")


def verify_zip_exists() -> None:
    if not ZIP_PATH.exists():
        console.print(f"[red]✘[/red] No deployment package found at {ZIP_PATH}")
        console.print(
            "Run [bold]python infra/build_lambda_package.py price-checker[/bold] first, "
            "then deploy."
        )
        sys.exit(1)

    size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    console.print(f"[green]✔[/green] Found deployment package ({size_mb:.2f} MB)")


def get_execution_role_arn(iam) -> str:
    """Look up the price checker's execution role. Exits if it is not there yet."""
    try:
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        console.print(f"[red]✘[/red] IAM role '{ROLE_NAME}' does not exist")
        console.print(
            "Run [bold]python infra/setup_price_alerts.py[/bold] first — it creates "
            "this role along with the SNS topic and the scoped sns:Publish grant "
            "that the role needs."
        )
        sys.exit(1)
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Could not look up IAM role '{ROLE_NAME}': {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Using execution role '{ROLE_NAME}'")
    return role_arn


def upload_to_s3(s3, bucket_name: str) -> None:
    console.print(f"Uploading to s3://{bucket_name}/{S3_KEY} ...")
    try:
        s3.upload_file(str(ZIP_PATH), bucket_name, S3_KEY)
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Upload failed: {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Uploaded to s3://{bucket_name}/{S3_KEY}")


def function_exists(lambda_client) -> bool:
    try:
        lambda_client.get_function(FunctionName=FUNCTION_NAME)
        return True
    except lambda_client.exceptions.ResourceNotFoundException:
        return False
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Could not check whether '{FUNCTION_NAME}' exists: {e}")
        sys.exit(1)


def create_function(lambda_client, role_arn: str, bucket_name: str, env_vars: dict) -> None:
    last_error = None

    for attempt in range(1, ROLE_PROPAGATION_ATTEMPTS + 1):
        try:
            lambda_client.create_function(
                FunctionName=FUNCTION_NAME,
                Runtime=LAMBDA_RUNTIME,
                Role=role_arn,
                Handler=LAMBDA_HANDLER,
                Code={"S3Bucket": bucket_name, "S3Key": S3_KEY},
                Timeout=LAMBDA_TIMEOUT,
                MemorySize=LAMBDA_MEMORY_MB,
                Environment={"Variables": env_vars},
                Description="Mercato scheduled wishlist price checker",
                Tags=PROJECT_TAGS,
            )
            console.print(f"[green]✔[/green] Created Lambda function '{FUNCTION_NAME}'")
            return
        except lambda_client.exceptions.InvalidParameterValueException as e:
            # Almost always "The role defined for the function cannot be assumed
            # by Lambda" — the role exists but hasn't propagated yet. Retry.
            last_error = e
            if attempt < ROLE_PROPAGATION_ATTEMPTS:
                console.print(
                    f"  [dim]Role not assumable yet (attempt {attempt}/"
                    f"{ROLE_PROPAGATION_ATTEMPTS}) — retrying in {ROLE_PROPAGATION_DELAY}s[/dim]"
                )
                time.sleep(ROLE_PROPAGATION_DELAY)
        except (ClientError, BotoCoreError) as e:
            console.print(f"[red]✘[/red] Failed to create '{FUNCTION_NAME}': {e}")
            sys.exit(1)

    console.print(f"[red]✘[/red] Role never became assumable: {last_error}")
    sys.exit(1)


def create_function_if_missing(
    lambda_client, iam, bucket_name: str, env_vars: dict
) -> bool:
    """Create the function if absent. Returns True if it was created."""
    if function_exists(lambda_client):
        console.print(
            f"[yellow]![/yellow] Function '{FUNCTION_NAME}' already exists — "
            "skipping creation, will update its code and configuration instead"
        )
        return False

    console.print(f"Function '{FUNCTION_NAME}' not found — creating it from scratch")

    role_arn = get_execution_role_arn(iam)
    create_function(lambda_client, role_arn, bucket_name, env_vars)
    return True


def update_function_code(lambda_client, bucket_name: str) -> None:
    # The package exceeds Lambda's 50 MB direct-upload limit, so the code must be
    # handed over as an S3 reference rather than an inline zip.
    try:
        lambda_client.update_function_code(
            FunctionName=FUNCTION_NAME,
            S3Bucket=bucket_name,
            S3Key=S3_KEY,
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to update code for '{FUNCTION_NAME}': {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Code update submitted for '{FUNCTION_NAME}'")


def update_function_configuration(lambda_client, role_arn: str, env_vars: dict) -> None:
    """Reconcile an existing function's settings with .env and the constants here.

    deploy_lambda.py updates code only, which is fine for a function whose
    configuration never changes. This one's does: SNS_TOPIC_ARN is the switch
    between emailing alerts and silently logging them, and it is filled in after
    the first deploy. Without this step a re-run would report success and leave the
    function still pointing at the old value.

    Must follow the code update, not precede it — Lambda rejects a configuration
    change while a code update is still in progress.
    """
    try:
        lambda_client.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Role=role_arn,
            Handler=LAMBDA_HANDLER,
            Runtime=LAMBDA_RUNTIME,
            Timeout=LAMBDA_TIMEOUT,
            MemorySize=LAMBDA_MEMORY_MB,
            Environment={"Variables": env_vars},
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to update configuration for '{FUNCTION_NAME}': {e}")
        sys.exit(1)

    console.print(
        f"[green]✔[/green] Configuration update submitted "
        f"(timeout {LAMBDA_TIMEOUT}s, memory {LAMBDA_MEMORY_MB} MB, {len(env_vars)} env vars)"
    )


def wait_for_update(lambda_client) -> dict:
    console.print("Waiting for the update to finish ...")
    try:
        waiter = lambda_client.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=FUNCTION_NAME, WaiterConfig={"Delay": 3, "MaxAttempts": 60})
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Update did not complete: {e}")
        sys.exit(1)

    config = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)
    console.print(f"[green]✔[/green] Update complete — state is {config['State']}")
    return config


def wait_for_active(lambda_client) -> dict:
    console.print("Waiting for the new function to become active ...")
    try:
        waiter = lambda_client.get_waiter("function_active_v2")
        waiter.wait(FunctionName=FUNCTION_NAME, WaiterConfig={"Delay": 3, "MaxAttempts": 60})
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Function never became active: {e}")
        sys.exit(1)

    config = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)
    console.print(f"[green]✔[/green] Function is active — state is {config['State']}")
    return config


def create_or_update_rule(events) -> str:
    """Create the daily schedule rule, or update it in place. Returns the rule ARN.

    put_rule overwrites an existing rule of the same name, so this is idempotent
    and also repairs a rule whose schedule was changed by hand. Tags are accepted
    only on creation — EventBridge ignores them when updating.
    """
    try:
        rule_arn = events.put_rule(
            Name=RULE_NAME,
            ScheduleExpression=SCHEDULE_EXPRESSION,
            State="ENABLED",
            Description=f"Runs the Mercato wishlist price checker daily at {SCHEDULE_DESCRIPTION_IST}",
            Tags=[{"Key": k, "Value": v} for k, v in PROJECT_TAGS.items()],
        )["RuleArn"]
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to create schedule rule '{RULE_NAME}': {e}")
        sys.exit(1)

    console.print(
        f"[green]✔[/green] Schedule rule '{RULE_NAME}' set to "
        f"{SCHEDULE_EXPRESSION} — {SCHEDULE_DESCRIPTION_IST}"
    )
    return rule_arn


def add_invoke_permission(lambda_client, rule_arn: str) -> None:
    """Let EventBridge invoke this function, scoped to this one rule.

    SourceArn is the rule's own ARN rather than a wildcard, matching how
    setup_api_gateway.py scopes API Gateway's grant to a single route: without it,
    any rule in the account could trigger this function.
    """
    try:
        lambda_client.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=PERMISSION_STATEMENT_ID,
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        console.print(
            f"[green]✔[/green] Granted EventBridge permission to invoke '{FUNCTION_NAME}'"
        )
    except lambda_client.exceptions.ResourceConflictException:
        console.print(
            f"[yellow]![/yellow] Invoke permission '{PERMISSION_STATEMENT_ID}' "
            "already exists — skipping"
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to add invoke permission: {e}")
        sys.exit(1)


def attach_target(events, function_arn: str) -> None:
    """Point the rule at the function. put_targets replaces a target of the same Id."""
    try:
        response = events.put_targets(
            Rule=RULE_NAME,
            Targets=[{"Id": TARGET_ID, "Arn": function_arn}],
        )
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to attach '{FUNCTION_NAME}' to the rule: {e}")
        sys.exit(1)

    # put_targets returns 200 even when an individual target was rejected, so the
    # failure count has to be checked explicitly or a broken schedule reads as success.
    if response.get("FailedEntryCount", 0):
        for entry in response.get("FailedEntries", []):
            console.print(
                f"[red]✘[/red] Target '{entry.get('TargetId')}' rejected: "
                f"{entry.get('ErrorCode')} — {entry.get('ErrorMessage')}"
            )
        sys.exit(1)

    console.print(f"[green]✔[/green] Rule '{RULE_NAME}' now targets '{FUNCTION_NAME}'")


def print_summary(config: dict, env_vars: dict) -> None:
    size_mb = config["CodeSize"] / (1024 * 1024)
    console.print(f"[bold cyan]Deployed code size:[/bold cyan] {size_mb:.2f} MB")
    console.print(f"[bold cyan]Timeout / memory:[/bold cyan] {config['Timeout']}s / {config['MemorySize']} MB")
    console.print(f"[bold cyan]Last modified:[/bold cyan] {config['LastModified']}")
    console.print()

    notify_line = (
        f"Alerts publish to:\n  [green]{env_vars['SNS_TOPIC_ARN']}[/green]"
        if env_vars["SNS_TOPIC_ARN"]
        else "[yellow]SNS_TOPIC_ARN is unset — alerts will be logged, not emailed.[/yellow]"
    )

    console.print(
        Panel(
            f"[bold]{FUNCTION_NAME}[/bold] runs every day at "
            f"[bold]{SCHEDULE_DESCRIPTION_IST}[/bold] ({SCHEDULE_EXPRESSION}).\n\n"
            f"{notify_line}\n\n"
            "[dim]A triggered alert is consumed — its alert_threshold is removed from "
            "the\nwishlist item so one price drop does not notify every day. Test a run "
            "now with:\n"
            f"  aws lambda invoke --function-name {FUNCTION_NAME} build/out.json\n\n"
            "The response body lists every triggered alert — product titles, URLs and "
            "user\nids. It is written under build/ because that directory is already "
            "gitignored;\nkeep it there rather than dropping it in the repo root.[/dim]",
            title="[bold]Scheduled[/bold]",
            border_style="cyan",
        )
    )


def main() -> None:
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
    # Without this, deployment crashes mid-run and leaves the schedule half-wired.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()

    if not config["bucket_name"]:
        console.print("[red]✘[/red] S3_BUCKET_NAME is not set in .env — cannot deploy")
        sys.exit(1)

    console.rule(f"[bold]Deploying {FUNCTION_NAME}[/bold]")
    console.print(f"Region: {config['region']}\n")

    try:
        s3 = boto3.client("s3", region_name=config["region"])
        lambda_client = boto3.client("lambda", region_name=config["region"])
        events = boto3.client("events", region_name=config["region"])
        # IAM is global — no region, matching how deploy_lambda.py builds it.
        iam = boto3.client("iam")
    except NoCredentialsError:
        console.print("[red]✘[/red] No AWS credentials found. Run 'aws configure' first.")
        sys.exit(1)

    console.rule("Configuration")
    verify_env_vars(config["env_vars"], config["region"])

    console.rule("Package")
    verify_zip_exists()

    console.rule("Upload")
    upload_to_s3(s3, config["bucket_name"])

    console.rule("Function")
    created = create_function_if_missing(
        lambda_client, iam, config["bucket_name"], config["env_vars"]
    )

    if created:
        # CreateFunction already pulled the code we just uploaded and applied the
        # full configuration, so there is nothing to reconcile — only wait for the
        # function to finish initialising.
        function_config = wait_for_active(lambda_client)
    else:
        console.rule("Update function code")
        update_function_code(lambda_client, config["bucket_name"])
        wait_for_update(lambda_client)

        console.rule("Update function configuration")
        role_arn = get_execution_role_arn(iam)
        update_function_configuration(lambda_client, role_arn, config["env_vars"])
        function_config = wait_for_update(lambda_client)

    console.rule("Schedule")
    rule_arn = create_or_update_rule(events)
    add_invoke_permission(lambda_client, rule_arn)
    attach_target(events, function_config["FunctionArn"])

    console.rule("Summary")
    print_summary(function_config, config["env_vars"])


if __name__ == "__main__":
    main()
