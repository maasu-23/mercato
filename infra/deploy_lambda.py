import os
import sys
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


def load_config() -> dict:
    load_dotenv()
    return {
        "region": os.getenv("AWS_REGION", DEFAULT_REGION),
        "bucket_name": os.getenv("S3_BUCKET_NAME", ""),
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
    except NoCredentialsError:
        console.print("[red]✘[/red] No AWS credentials found. Run 'aws configure' first.")
        sys.exit(1)

    console.rule("Package")
    verify_zip_exists()

    console.rule("Upload")
    upload_to_s3(s3, config["bucket_name"])

    console.rule("Update function code")
    update_function_code(lambda_client, config["bucket_name"])
    function_config = wait_for_update(lambda_client)

    console.rule("Summary")
    print_summary(function_config)


if __name__ == "__main__":
    main()
