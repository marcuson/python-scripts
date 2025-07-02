from importlib.resources import files
import shutil
from typing import Annotated
import typer

cli = typer.Typer()


@cli.command()
def export(file: Annotated[str, typer.Option()] = "./config.yaml"):
    config_path = files("organizemarc").joinpath("data/config.yaml")
    shutil.copyfile(config_path, file)
