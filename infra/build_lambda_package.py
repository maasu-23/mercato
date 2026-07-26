"""Build a Lambda deployment package for one of Mercato's two functions.

Both functions ship the same thing — the agent package and its dependencies —
and differ only in which handler module sits at the package root, so they are
built by the same code path against the BUILD_TARGETS table below rather than by
two scripts that would drift apart the first time a dependency is bumped.

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

# The price checker imports agent.agent to price each item, so it needs the whole
# agent package and every dependency the agent Lambda has. One list, both targets.
RUNTIME_DEPENDENCIES = [
    "boto3==1.37.0",
    "langgraph==0.2.74",
    "langchain-core==0.3.40",
    "langchain-aws==0.2.14",
    "tavily-python==0.3.9",
    "httpx==0.27.0",
]

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
    },
    "price-checker": {
        "handler_file": "price_checker_handler.py",
        "package_dir": BUILD_DIR / "price_checker_package",
        "zip_path": BUILD_DIR / "mercato-price-checker.zip",
        "description": "the scheduled price alert Lambda",
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
    console.print(f"Installing runtime dependencies: {', '.join(RUNTIME_DEPENDENCIES)}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(target["package_dir"]),
            *LAMBDA_PLATFORM_ARGS,
            *RUNTIME_DEPENDENCIES,
        ],
        check=True,
    )
    console.print("[green]✔[/green] Runtime dependencies installed")


def copy_agent_code(target: dict) -> None:
    shutil.copytree(ROOT_DIR / "agent", target["package_dir"] / "agent")
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
