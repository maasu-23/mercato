import os
import sys

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

console = Console()

DEFAULT_REGION = "ap-south-1"

API_NAME = "mercato-api"
FUNCTION_NAME = "mercato-agent"
ROUTE_KEY = "POST /chat"
ROUTE_PATH = "/chat"
STAGE_NAME = "$default"
PAYLOAD_FORMAT_VERSION = "2.0"
PERMISSION_STATEMENT_ID = "mercato-api-invoke"


def load_config() -> dict:
    load_dotenv()
    return {"region": os.getenv("AWS_REGION", DEFAULT_REGION)}


def get_clients(region: str) -> dict:
    return {
        "apigateway": boto3.client("apigatewayv2", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
        "sts": boto3.client("sts", region_name=region),
    }


def get_function_arn(lambda_client) -> str:
    try:
        arn = lambda_client.get_function_configuration(FunctionName=FUNCTION_NAME)["FunctionArn"]
    except ClientError as e:
        console.print(f"[red]✘[/red] Could not find Lambda function '{FUNCTION_NAME}': {e}")
        sys.exit(1)

    console.print(f"[green]✔[/green] Found Lambda function '{FUNCTION_NAME}'")
    return arn


def find_api(apigateway) -> dict | None:
    paginator = apigateway.get_paginator("get_apis")
    for page in paginator.paginate():
        for api in page["Items"]:
            if api["Name"] == API_NAME:
                return api
    return None


def create_or_get_api(apigateway) -> dict:
    existing = find_api(apigateway)
    if existing:
        console.print(f"[yellow]![/yellow] HTTP API '{API_NAME}' already exists — reusing it")
        return existing

    api = apigateway.create_api(Name=API_NAME, ProtocolType="HTTP")
    console.print(f"[green]✔[/green] Created HTTP API '{API_NAME}'")
    return api


def create_or_get_integration(apigateway, api_id: str, function_arn: str) -> str:
    existing = apigateway.get_integrations(ApiId=api_id)["Items"]
    for integration in existing:
        if integration.get("IntegrationUri") == function_arn:
            console.print("[yellow]![/yellow] Lambda integration already exists — reusing it")
            return integration["IntegrationId"]

    integration = apigateway.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=function_arn,
        # API Gateway always POSTs to the Lambda service, regardless of the
        # HTTP method the client used on the route.
        IntegrationMethod="POST",
        PayloadFormatVersion=PAYLOAD_FORMAT_VERSION,
    )
    console.print(
        f"[green]✔[/green] Created Lambda integration (payload format {PAYLOAD_FORMAT_VERSION})"
    )
    return integration["IntegrationId"]


def create_or_get_route(apigateway, api_id: str, integration_id: str) -> None:
    existing = apigateway.get_routes(ApiId=api_id)["Items"]
    for route in existing:
        if route["RouteKey"] == ROUTE_KEY:
            console.print(f"[yellow]![/yellow] Route '{ROUTE_KEY}' already exists — reusing it")
            return

    apigateway.create_route(
        ApiId=api_id,
        RouteKey=ROUTE_KEY,
        Target=f"integrations/{integration_id}",
    )
    console.print(f"[green]✔[/green] Created route '{ROUTE_KEY}'")


def create_or_get_stage(apigateway, api_id: str) -> None:
    try:
        apigateway.get_stage(ApiId=api_id, StageName=STAGE_NAME)
        console.print(f"[yellow]![/yellow] Stage '{STAGE_NAME}' already exists — reusing it")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "NotFoundException":
            console.print(f"[red]✘[/red] Failed to check stage '{STAGE_NAME}': {e}")
            sys.exit(1)

    apigateway.create_stage(ApiId=api_id, StageName=STAGE_NAME, AutoDeploy=True)
    console.print(f"[green]✔[/green] Created stage '{STAGE_NAME}' with auto-deploy enabled")


def add_invoke_permission(lambda_client, api_id: str, region: str, account_id: str) -> None:
    # Scoped to this API's /chat route only — not a blanket grant to API Gateway.
    source_arn = f"arn:aws:execute-api:{region}:{account_id}:{api_id}/*/*{ROUTE_PATH}"

    try:
        lambda_client.add_permission(
            FunctionName=FUNCTION_NAME,
            StatementId=PERMISSION_STATEMENT_ID,
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=source_arn,
        )
        console.print(f"[green]✔[/green] Granted API Gateway permission to invoke '{FUNCTION_NAME}'")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            console.print(
                f"[yellow]![/yellow] Invoke permission '{PERMISSION_STATEMENT_ID}' already exists — skipping"
            )
        else:
            console.print(f"[red]✘[/red] Failed to add invoke permission: {e}")
            sys.exit(1)


def print_invoke_url(api_endpoint: str) -> None:
    url = f"{api_endpoint}{ROUTE_PATH}"
    console.print(
        Panel(
            f"[bold cyan]POST[/bold cyan] {url}",
            title="Invoke URL",
            border_style="cyan",
        )
    )
    console.print(
        "[dim]This route is public — anyone with the URL can invoke the agent.[/dim]"
    )


def main() -> None:
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config = load_config()
    region = config["region"]

    console.rule("[bold]Mercato API Gateway setup[/bold]")
    console.print(f"Region: {region}\n")

    clients = get_clients(region)

    try:
        account_id = clients["sts"].get_caller_identity()["Account"]
    except NoCredentialsError:
        console.print("[red]✘[/red] No AWS credentials found. Run 'aws configure' first.")
        sys.exit(1)

    console.rule("Lambda")
    function_arn = get_function_arn(clients["lambda"])

    console.rule("HTTP API")
    api = create_or_get_api(clients["apigateway"])
    api_id = api["ApiId"]

    console.rule("Integration")
    integration_id = create_or_get_integration(clients["apigateway"], api_id, function_arn)

    console.rule("Route")
    create_or_get_route(clients["apigateway"], api_id, integration_id)

    console.rule("Stage")
    create_or_get_stage(clients["apigateway"], api_id)

    console.rule("Permissions")
    add_invoke_permission(clients["lambda"], api_id, region, account_id)

    console.rule("Done")
    print_invoke_url(api["ApiEndpoint"])


if __name__ == "__main__":
    main()
