import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from dotenv import load_dotenv
from rich.console import Console

console = Console()

ROOT_DIR = Path(__file__).resolve().parent.parent
ZIP_PATH = ROOT_DIR / "build" / "mercato-lambda.zip"

DEFAULT_REGION = "ap-south-1"
FUNCTION_NAME = "mercato-agent"
S3_KEY = "lambda/mercato-lambda.zip"

ROLE_NAME = "mercato-lambda-role"
LAMBDA_RUNTIME = "python3.11"
LAMBDA_HANDLER = "lambda_handler.handler"
# The HTTP API integration in front of this function (infra/setup_api_gateway.py)
# has a hard, non-increasable 30s ceiling — an AWS-enforced maximum for every
# HTTP API, not a default that can be raised. A Lambda timeout of 60s let
# invocations run past that ceiling: the caller already had a 504 in hand, but
# the Lambda kept running (and billing for) Bedrock calls nobody would ever see
# the result of. 28s keeps a small margin under the 30s wall so the function
# times itself out first, instead of running to no purpose after the caller is
# already gone. This only matters on the API Gateway path — the CLI runs the
# agent in-process and is not bounded by this Lambda or this API at all.
LAMBDA_TIMEOUT = 28
LAMBDA_MEMORY_MB = 512

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

# Mirrors what was attached by hand when this function was first stood up. These
# are broad AWS-managed policies — fine for a research project, but worth
# narrowing to least-privilege before anything production-facing.
MANAGED_POLICY_ARNS = [
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
    "arn:aws:iam::aws:policy/AmazonS3FullAccess",
    "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
]

# IAM is eventually consistent: a brand-new role is not immediately assumable by
# Lambda, and CreateFunction fails with InvalidParameterValueException until it
# propagates. There is no waiter for this, so the create is retried instead.
ROLE_PROPAGATION_ATTEMPTS = 12
ROLE_PROPAGATION_DELAY = 5


def load_config() -> dict:
    # override=True so .env wins over anything already exported in the shell.
    # Without it an inherited AWS_REGION silently beats the .env value and the
    # function gets deployed to a region the rest of the stack is not in.
    load_dotenv(override=True)
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "bucket_name": os.getenv("S3_BUCKET_NAME", ""),
        "env_vars": {
            "S3_BUCKET_NAME": os.getenv("S3_BUCKET_NAME", ""),
            "DYNAMODB_WISHLIST_TABLE": os.getenv("DYNAMODB_WISHLIST_TABLE", "mercato-wishlist"),
            "DYNAMODB_SESSIONS_TABLE": os.getenv("DYNAMODB_SESSIONS_TABLE", "mercato-sessions"),
            "BEDROCK_MODEL_ID": os.getenv("BEDROCK_MODEL_ID", ""),
            "TAVILY_API_KEY": os.getenv("TAVILY_API_KEY", ""),
        },
    }


def verify_zip_exists() -> None:
    if not ZIP_PATH.exists():
        console.print(f"[red]✘[/red] No deployment package found at {ZIP_PATH}")
        console.print("Run [bold]python infra/build_lambda_package.py[/bold] first, then deploy.")
        sys.exit(1)

    size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    console.print(f"[green]✔[/green] Found deployment package ({size_mb:.2f} MB)")


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


def ensure_execution_role(iam) -> str:
    """Create the Lambda execution role and attach its policies, if not already present.

    Returns the role ARN.
    """
    try:
        role = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
            Description="Execution role for the Mercato agent Lambda function",
        )
        role_arn = role["Role"]["Arn"]
        console.print(f"[green]✔[/green] Created IAM role '{ROLE_NAME}'")
        created = True
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        console.print(f"[yellow]![/yellow] IAM role '{ROLE_NAME}' already exists — reusing it")
        created = False
    except (ClientError, BotoCoreError) as e:
        console.print(f"[red]✘[/red] Failed to create IAM role '{ROLE_NAME}': {e}")
        sys.exit(1)

    for policy_arn in MANAGED_POLICY_ARNS:
        try:
            # attach_role_policy is idempotent, so re-attaching on an existing role is safe.
            iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn=policy_arn)
            console.print(f"  [green]✔[/green] Attached {policy_arn.split('/')[-1]}")
        except (ClientError, BotoCoreError) as e:
            console.print(f"[red]✘[/red] Failed to attach {policy_arn}: {e}")
            sys.exit(1)

    if created:
        console.print("Waiting for the new role to propagate through IAM ...")

    return role_arn


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
                Description="Mercato agentic shopping assistant",
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


def create_lambda_function_if_missing(lambda_client, iam, bucket_name: str, env_vars: dict) -> bool:
    """Create the Lambda function, its execution role, and its policies if absent.

    Makes the deployment reproducible from a clean AWS account — previously the
    function had to be created by hand before this script would work.

    Returns True if the function was created, False if it already existed.
    """
    if function_exists(lambda_client):
        console.print(
            f"[yellow]![/yellow] Function '{FUNCTION_NAME}' already exists — "
            "skipping creation, will update its code instead"
        )
        return False

    console.print(f"Function '{FUNCTION_NAME}' not found — creating it from scratch")

    role_arn = ensure_execution_role(iam)
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
    except ClientError as e:
        console.print(f"[red]✘[/red] Failed to update '{FUNCTION_NAME}': {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Update submitted for '{FUNCTION_NAME}'")


def update_function_configuration(lambda_client, role_arn: str, env_vars: dict) -> None:
    """Reconcile an existing function's environment variables with .env.

    update_function_code only pushes code — a value rotated in .env (an API key,
    a table name) never reaches an already-existing function without this step,
    so a redeploy after editing .env would silently report success while leaving
    the function on stale configuration. Must follow the code update, not
    precede it: Lambda rejects a configuration change while a code update is
    still in progress.
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
        f"[green]✔[/green] Configuration update submitted ({len(env_vars)} env vars)"
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


def print_summary(config: dict) -> None:
    size_mb = config["CodeSize"] / (1024 * 1024)
    console.print(f"[bold cyan]Deployed code size:[/bold cyan] {size_mb:.2f} MB")
    console.print(f"[bold cyan]Last modified:[/bold cyan] {config['LastModified']}")


def main() -> None:
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
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
        iam = boto3.client("iam")
    except NoCredentialsError:
        console.print("[red]✘[/red] No AWS credentials found. Run 'aws configure' first.")
        sys.exit(1)

    console.rule("Package")
    verify_zip_exists()

    console.rule("Upload")
    upload_to_s3(s3, config["bucket_name"])

    console.rule("Function")
    created = create_lambda_function_if_missing(
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
        role_arn = ensure_execution_role(iam)
        update_function_configuration(lambda_client, role_arn, config["env_vars"])
        function_config = wait_for_update(lambda_client)

    console.rule("Summary")
    print_summary(function_config)


if __name__ == "__main__":
    main()
