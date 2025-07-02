from tomlkit import parse, dumps
from pathlib import Path


def delete() -> None:
    with open("pyproject.toml", "r", encoding="utf-8") as f:
        content = f.read()

    data = parse(content)
    data["project"]["dependencies"] = []

    with open("pyproject.toml", "w", encoding="utf-8") as f:
        f.write(dumps(data))


def generate() -> None:
    deps = []
    clis = {}
    for item in Path("tools").iterdir():
        if item.is_dir():
            deps.append(
                f"{item.name} @ git+https://github.com/marcuson/python-scripts.git#subdirectory=tools/{item.name}"
            )
            clis[item.name] = f"{item.name}.cli.main:cli"

    with open("pyproject.toml", "r", encoding="utf-8") as f:
        content = f.read()

    data = parse(content)
    data["project"]["dependencies"] = deps
    data["project"]["scripts"] = clis

    with open("pyproject.toml", "w", encoding="utf-8") as f:
        f.write(dumps(data))
