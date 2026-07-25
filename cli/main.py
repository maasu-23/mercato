import hashlib
import sys

import click
from dotenv import load_dotenv

# override=True so .env wins over anything already exported in the shell —
# otherwise an inherited AWS_REGION points the CLI at a region with no tables.
load_dotenv(override=True)

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.agent import chat
from agent.tools.wishlist import fetch_wishlist

console = Console()

EXIT_COMMANDS = {"exit", "quit"}
WISHLIST_COMMAND = "wishlist"


def _resolve_user_id() -> str:
    """Derive the session user_id by hashing the caller's IAM ARN.

    Exits the process if AWS credentials are unavailable — every tool in the
    agent depends on this identity, so there is nothing useful to do without it.
    """
    try:
        arn = boto3.client("sts").get_caller_identity()["Arn"]
    except (NoCredentialsError, ClientError, BotoCoreError) as e:
        console.print(f"[red]✘ Could not determine your AWS identity:[/red] {e}")
        console.print("Run [bold]aws configure[/bold] to set up your credentials, then try again.")
        sys.exit(1)

    return hashlib.sha256(arn.encode("utf-8")).hexdigest()[:16]


def _print_welcome() -> None:
    console.print(
        Panel(
            "[bold cyan]Mercato[/bold cyan] — your agentic shopping assistant",
            border_style="cyan",
        )
    )


def _print_wishlist(user_id: str) -> None:
    """Fetch and render the user's wishlist, bypassing the agent loop."""
    try:
        items = fetch_wishlist(user_id)
    except Exception as e:
        console.print(f"[red]Could not load your wishlist:[/red] {e}")
        return

    if not items:
        console.print("[yellow]Your wishlist is empty.[/yellow]")
        return

    table = Table(title="Your Wishlist", border_style="magenta")
    table.add_column("Title", style="bold")
    table.add_column("Price", justify="right")
    table.add_column("Currency")
    table.add_column("Merchant")
    table.add_column("Saved At")

    for item in items:
        table.add_row(
            str(item.get("title", "")),
            str(item.get("price", "—")),
            str(item.get("currency", "")),
            str(item.get("merchant", "")),
            str(item.get("saved_at", "")),
        )

    console.print(table)


@click.command()
def main() -> None:
    """Start an interactive Mercato shopping session."""
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    _print_welcome()

    user_id = _resolve_user_id()

    console.print(f"[green]✔[/green] Ready — signed in as [dim]{user_id}[/dim]")
    console.print(
        "[dim]Type 'exit' or 'quit' to leave, type 'wishlist' to view saved items.[/dim]\n"
    )

    history: list = []

    while True:
        try:
            user_input = console.input("[bold green]>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[cyan]Goodbye — happy shopping![/cyan]")
            break

        if not user_input:
            continue

        command = user_input.lower()

        if command in EXIT_COMMANDS:
            console.print("[cyan]Goodbye — happy shopping![/cyan]")
            break

        if command == WISHLIST_COMMAND:
            _print_wishlist(user_id)
            continue

        try:
            reply, history = chat(user_input, history, user_id)
        except Exception as e:
            console.print(f"[red]Something went wrong talking to the agent:[/red] {e}")
            console.print("[dim]You can try again, or rephrase your request.[/dim]")
            continue

        console.print(Panel(reply, border_style="blue", title="Mercato"))


if __name__ == "__main__":
    main()
