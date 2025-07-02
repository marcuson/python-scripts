from importlib.resources import files
import os
import shutil
import sys
import typer
from organize.cli import cli as organize_cli

from .config import cli as cli_config

cli = typer.Typer()

cli.add_typer(cli_config, name="config")


def _get_config_path(dir: str) -> str:
    config_path = os.path.join(dir, "config.yaml")

    if not os.path.isfile(config_path):
        config_path = files("organizemarc").joinpath("data/config.yaml")

    return config_path


def _prep_env(dir: str) -> None:
    os.environ["ORGANIZE_ROOT_DIR"] = dir


def _check_deps() -> None:
    if shutil.which("pdftotext") is None:
        raise Exception(
            "pdftotext is not installed, please install it and add to path (e.g. via Uniget)."
        )


def _check_if_no_utf8() -> None:
    if not sys.flags.utf8_mode:
        print(
            "UTF8 mode is not enabled; PDF parsing might fail silently, especially on Windows. To enable it, set the env var PYTHONUTF8=1 *before* running this script."
        )
        if typer.confirm("Do you want to continue anyway?", default=False):
            typer.echo("Continuing...")
        else:
            typer.echo("Aborting.")
            sys.exit(0)


def _prep(dir: str) -> str:
    _check_if_no_utf8()
    _check_deps()
    _prep_env(dir)
    config_path = _get_config_path(dir)
    return config_path


@cli.command()
def sim(dir: str):
    config_path = _prep(dir)
    organize_cli(f"sim {config_path}")


@cli.command()
def run(dir: str):
    config_path = _prep(dir)
    organize_cli(f"run {config_path}")


@cli.command()
def raw():
    _check_deps()
    organize_cli(sys.argv[1:])


if __name__ == "__main__":
    cli()
