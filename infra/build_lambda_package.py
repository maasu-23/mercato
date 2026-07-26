"""Build a Lambda deployment package for one of Mercato's two functions.

The two functions no longer ship the same thing. The agent Lambda carries the
whole agent package and its tool dependencies; the price checker carries a
Bedrock client and nothing else, because it deliberately does not run the
tool-bound agent loop (see infra/price_checker_handler.get_current_price). What
each one gets is declared in the BUILD_TARGETS table below and built by one
shared code path, rather than by two scripts that would drift apart the first
time a dependency is bumped.

    python infra/build_lambda_package.py                # agent (default)
    python infra/build_lambda_package.py price-checker
    python infra/build_lambda_package.py all

Each target gets its own staging directory and its own zip, so building one does
not clobber the other's artifact.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()

ROOT_DIR = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT_DIR / "build"

# What both targets need to talk to Bedrock.
#
# langchain-core is a transitive dependency of langchain-aws, not a direct import
# of the price checker. It is pinned here anyway so both packages resolve to the
# same version rather than to whatever the range allows on the day.
BEDROCK_DEPENDENCIES = [
    "boto3==1.37.0",
    "langchain-aws==0.2.14",
    "langchain-core==0.3.40",
]

# The agent Lambda adds the graph loop and the tools' own dependencies: langgraph
# for the loop, tavily-python for web_search, httpx for ucp_query.
AGENT_DEPENDENCIES = [
    *BEDROCK_DEPENDENCIES,
    "langgraph==0.2.74",
    "tavily-python==0.3.9",
    "httpx==0.27.0",
]

# The price checker adds nothing — it imports boto3 and ChatBedrockConverse, and
# that is the whole list. It does not import the agent package at all.
#
# This is a consequence of the prompt injection fix rather than an optimisation:
# the checker feeds untrusted wishlist text to a model with no tools bound, so it
# has no reason to carry langgraph, tavily-python or httpx. Keeping them out means
# a future edit that reintroduces the tool loop fails at build time instead of
# quietly restoring the vulnerability.
PRICE_CHECKER_DEPENDENCIES = [*BEDROCK_DEPENDENCIES]

# Lambda runs Linux/x86_64 on CPython 3.11. Without these, pip resolves wheels for
# whatever machine runs this build, and packages with compiled extensions
# (pydantic-core, numpy, orjson, tiktoken) fail to import inside Lambda.
LAMBDA_PLATFORM_ARGS = [
    "--platform",
    "manylinux2014_x86_64",
    "--python-version",
    "3.11",
    "--implementation",
    "cp",
    "--only-binary=:all:",
]

BUILD_TARGETS = {
    "agent": {
        "handler_file": "lambda_handler.py",
        "package_dir": BUILD_DIR / "lambda_package",
        "zip_path": BUILD_DIR / "mercato-lambda.zip",
        "description": "the API Gateway-fronted agent Lambda",
        "dependencies": AGENT_DEPENDENCIES,
        # lambda_handler.py does `from agent.agent import chat`.
        "include_agent_package": True,
    },
    "price-checker": {
        "handler_file": "price_checker_handler.py",
        "package_dir": BUILD_DIR / "price_checker_package",
        "zip_path": BUILD_DIR / "mercato-price-checker.zip",
        "description": "the scheduled price alert Lambda",
        "dependencies": PRICE_CHECKER_DEPENDENCIES,
        # price_checker_handler.py imports nothing from agent/, so shipping it
        # would put agent/tools/*.py — whose module-level imports of tavily and
        # httpx are no longer satisfied by this package — inside the zip as dead,
        # unimportable code. Leave it out.
        "include_agent_package": False,
    },
}

DEFAULT_TARGET = "agent"


def clean_build_dir(target: dict) -> None:
    """Reset this target's staging directory and drop its previous zip.

    Scoped to the one target rather than wiping BUILD_DIR wholesale: the two
    packages live side by side, and clearing the shared parent would silently
    delete the other function's artifact mid-build.
    """
    package_dir = target["package_dir"]

    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)

    if target["zip_path"].exists():
        target["zip_path"].unlink()

    console.print(f"[green]✔[/green] Clean build directory created at {package_dir}")


def install_dependencies(target: dict) -> None:
    dependencies = target["dependencies"]
    console.print(f"Installing runtime dependencies: {', '.join(dependencies)}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(target["package_dir"]),
            *LAMBDA_PLATFORM_ARGS,
            *dependencies,
        ],
        check=True,
    )
    console.print("[green]✔[/green] Runtime dependencies installed")


def copy_agent_code(target: dict) -> None:
    """Copy agent/ into the package, for the targets whose handler imports it.

    __pycache__ is excluded: it holds .pyc files compiled by whatever Python
    built the package, which are dead weight inside a Linux Lambda runtime.
    """
    if not target["include_agent_package"]:
        console.print("[dim]Skipped agent/ — this handler does not import it[/dim]")
        return

    shutil.copytree(
        ROOT_DIR / "agent",
        target["package_dir"] / "agent",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    console.print("[green]✔[/green] Copied agent/ into package")


def copy_lambda_handler(target: dict) -> None:
    """Place this target's handler module at the package root.

    Lambda resolves the configured handler ("<module>.handler") against the root
    of the zip, so the file has to sit there rather than under infra/.
    """
    handler_file = target["handler_file"]
    shutil.copy2(ROOT_DIR / "infra" / handler_file, target["package_dir"] / handler_file)
    console.print(f"[green]✔[/green] Copied infra/{handler_file} to package root")


def create_zip(target: dict) -> None:
    zip_path = target["zip_path"]
    if zip_path.exists():
        zip_path.unlink()
    base_name = str(zip_path.with_suffix(""))
    shutil.make_archive(base_name, "zip", root_dir=target["package_dir"])
    console.print(f"[green]✔[/green] Zipped package contents to {zip_path}")


def print_zip_size(target: dict) -> None:
    size_mb = target["zip_path"].stat().st_size / (1024 * 1024)
    console.print(f"[bold cyan]Final package size:[/bold cyan] {size_mb:.2f} MB")


def build(target_name: str) -> None:
    target = BUILD_TARGETS[target_name]

    console.rule(f"[bold]Building '{target_name}' — {target['description']}[/bold]")

    console.rule("Clean build directory")
    clean_build_dir(target)

    console.rule("Install dependencies")
    install_dependencies(target)

    console.rule("Copy application code")
    copy_agent_code(target)
    copy_lambda_handler(target)

    console.rule("Create zip archive")
    create_zip(target)
    print_zip_size(target)


def parse_args() -> list[str]:
    """Resolve the command line to the list of target names to build."""
    parser = argparse.ArgumentParser(
        description="Build a Mercato Lambda deployment package.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=DEFAULT_TARGET,
        choices=[*BUILD_TARGETS, "all"],
        help=f"which package to build (default: {DEFAULT_TARGET})",
    )
    args = parser.parse_args()

    return list(BUILD_TARGETS) if args.target == "all" else [args.target]


def main() -> None:
    # Windows defaults stdout to cp1252, which cannot encode the glyphs below.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    for target_name in parse_args():
        build(target_name)


if __name__ == "__main__":
    main()
