import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()

ROOT_DIR = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT_DIR / "build"
PACKAGE_DIR = BUILD_DIR / "lambda_package"
ZIP_PATH = BUILD_DIR / "mercato-lambda.zip"

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


def clean_build_dir() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    PACKAGE_DIR.mkdir(parents=True)
    console.print(f"[green]✔[/green] Clean build directory created at {PACKAGE_DIR}")


def install_dependencies() -> None:
    console.print(f"Installing runtime dependencies: {', '.join(RUNTIME_DEPENDENCIES)}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(PACKAGE_DIR),
            *LAMBDA_PLATFORM_ARGS,
            *RUNTIME_DEPENDENCIES,
        ],
        check=True,
    )
    console.print("[green]✔[/green] Runtime dependencies installed")


def copy_agent_code() -> None:
    shutil.copytree(ROOT_DIR / "agent", PACKAGE_DIR / "agent")
    console.print("[green]✔[/green] Copied agent/ into package")


def copy_lambda_handler() -> None:
    shutil.copy2(ROOT_DIR / "infra" / "lambda_handler.py", PACKAGE_DIR / "lambda_handler.py")
    console.print("[green]✔[/green] Copied infra/lambda_handler.py to package root")


def create_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    base_name = str(ZIP_PATH.with_suffix(""))
    shutil.make_archive(base_name, "zip", root_dir=PACKAGE_DIR)
    console.print(f"[green]✔[/green] Zipped package contents to {ZIP_PATH}")


def print_zip_size() -> None:
    size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    console.print(f"[bold cyan]Final package size:[/bold cyan] {size_mb:.2f} MB")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    console.rule("[bold]Building Mercato Lambda deployment package[/bold]")

    console.rule("Clean build directory")
    clean_build_dir()

    console.rule("Install dependencies")
    install_dependencies()

    console.rule("Copy application code")
    copy_agent_code()
    copy_lambda_handler()

    console.rule("Create zip archive")
    create_zip()
    print_zip_size()


if __name__ == "__main__":
    main()
