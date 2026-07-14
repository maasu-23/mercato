import os
import sys

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

console = Console()

DEFAULT_REGION = "ap-south-1"

TAVILY_PLACEHOLDERS = {"", "your-tavily-api-key", "changeme", "your_tavily_api_key_here"}


def load_config() -> dict:
    load_dotenv()
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "wishlist_table": os.getenv("DYNAMODB_WISHLIST_TABLE", "mercato-wishlist"),
        "sessions_table": os.getenv("DYNAMODB_SESSIONS_TABLE", "mercato-sessions"),
        "bucket_name": os.getenv("S3_BUCKET_NAME", ""),
        "bedrock_model_id": os.getenv("BEDROCK_MODEL_ID", ""),
        "tavily_api_key": os.getenv("TAVILY_API_KEY", ""),
    }


def get_clients(region: str) -> dict:
    return {
        "dynamodb": boto3.client("dynamodb", region_name=region),
        "s3": boto3.client("s3", region_name=region),
        "bedrock": boto3.client("bedrock", region_name=region),
        "bedrock_runtime": boto3.client("bedrock-runtime", region_name=region),
    }


def create_dynamodb_table(dynamodb, table_name: str, sort_key_name: str) -> None:
    try:
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": sort_key_name, "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": sort_key_name, "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        console.print(f"[green]✔[/green] Created DynamoDB table '{table_name}'")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ResourceInUseException":
            console.print(f"[yellow]![/yellow] DynamoDB table '{table_name}' already exists — skipping")
        else:
            console.print(f"[red]✘[/red] Failed to create DynamoDB table '{table_name}': {e}")


def create_wishlist_table(dynamodb, table_name: str) -> None:
    create_dynamodb_table(dynamodb, table_name, "product_id")


def create_sessions_table(dynamodb, table_name: str) -> None:
    create_dynamodb_table(dynamodb, table_name, "timestamp")


def block_public_access(s3, bucket_name: str) -> None:
    try:
        s3.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        console.print(f"[green]✔[/green] Blocked all public access on '{bucket_name}'")
    except ClientError as e:
        console.print(f"[red]✘[/red] Failed to apply public access block on '{bucket_name}': {e}")


def create_s3_bucket(s3, bucket_name: str, region: str) -> None:
    if not bucket_name:
        console.print("[red]✘[/red] S3_BUCKET_NAME is not set in .env — cannot create bucket")
        return

    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        console.print(f"[green]✔[/green] Created S3 bucket '{bucket_name}'")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "BucketAlreadyOwnedByYou":
            console.print(f"[yellow]![/yellow] S3 bucket '{bucket_name}' already exists and is owned by you — skipping")
        elif code == "BucketAlreadyExists":
            console.print(
                f"[red]✘[/red] S3 bucket name '{bucket_name}' is already taken by another account. "
                "Please change S3_BUCKET_NAME in your .env file to something unique."
            )
            return
        else:
            console.print(f"[red]✘[/red] Failed to create S3 bucket '{bucket_name}': {e}")
            return

    block_public_access(s3, bucket_name)


def verify_bedrock_access(bedrock, bedrock_runtime, model_id: str) -> None:
    try:
        response = bedrock.list_foundation_models()
        model_ids = [m["modelId"] for m in response.get("modelSummaries", [])]
        match = any("claude-sonnet-4-5-20250929-v1:0" in m for m in model_ids)
        if match:
            console.print("[green]✔[/green] Bedrock access verified — claude-sonnet-4-5 is available in this region")
        else:
            console.print(
                "[yellow]![/yellow] Bedrock is reachable, but claude-sonnet-4-5 was not found in the model list. "
                "You may need to request model access in the Bedrock console."
            )
    except NoCredentialsError:
        console.print("[red]✘[/red] No AWS credentials found. Run 'aws configure' first.")
    except ClientError as e:
        console.print(f"[red]✘[/red] Failed to verify Bedrock access: {e}")

    try:
        bedrock_runtime.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            inferenceConfig={"maxTokens": 10},
        )
        console.print("[green]✔[/green] Bedrock invocation verified — account can use claude-sonnet-4-5")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"]["Message"]
        if error_code == "ResourceNotFoundException" and "use case details" in error_message:
            console.print(
                "[yellow]![/yellow] Bedrock invocation blocked — submit the Anthropic use case form in the "
                "Bedrock console under Model access. This can take up to 15 minutes to propagate."
            )
        else:
            console.print(f"[red]✘[/red] Bedrock invocation failed: {error_message}")
    except Exception as e:
        console.print(f"[red]✘[/red] Bedrock invocation failed: {e}")


def verify_tavily_key(tavily_api_key: str) -> None:
    if tavily_api_key.strip().lower() in TAVILY_PLACEHOLDERS:
        console.print("[red]✘[/red] TAVILY_API_KEY is missing or still a placeholder — set it in your .env file")
    else:
        console.print("[green]✔[/green] TAVILY_API_KEY is set")


def print_iam_permissions() -> None:
    table = Table(title="IAM permissions required to run this script")
    table.add_column("Action", style="cyan")
    table.add_column("Purpose", style="white")

    table.add_row("dynamodb:CreateTable", "Create the wishlist and sessions tables")
    table.add_row("s3:CreateBucket", "Create the session logs / artifacts bucket")
    table.add_row("s3:PutBucketPublicAccessBlock", "Lock the bucket down from public access")
    table.add_row("bedrock:ListFoundationModels", "Verify Claude Sonnet 4.5 is available")
    table.add_row(
        "bedrock:InvokeModel / bedrock-runtime:Converse",
        "Verify the account can actually invoke Claude Sonnet 4.5 (not just list it)",
    )

    console.print(table)


def main() -> None:
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
    # Without this, setup crashes mid-run and leaves infrastructure half-created.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()
    clients = get_clients(config["region"])

    console.rule("[bold]Mercato infrastructure setup[/bold]")
    console.print(f"Region: {config['region']}\n")

    console.rule("DynamoDB")
    create_wishlist_table(clients["dynamodb"], config["wishlist_table"])
    create_sessions_table(clients["dynamodb"], config["sessions_table"])

    console.rule("S3")
    create_s3_bucket(clients["s3"], config["bucket_name"], config["region"])

    console.rule("Bedrock")
    verify_bedrock_access(clients["bedrock"], clients["bedrock_runtime"], config["bedrock_model_id"])

    console.rule("Tavily")
    verify_tavily_key(config["tavily_api_key"])

    console.rule("IAM")
    print_iam_permissions()


if __name__ == "__main__":
    main()
