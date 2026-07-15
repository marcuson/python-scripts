import shutil
from pathlib import Path

from const import data_scripts_dir, repo_root

# -- Build artifacts


def _clean_build_artifacts(dir_path: Path):
    print(f"Cleaning build artifacts of '{dir_path}'...")
    shutil.rmtree(dir_path / "dist", ignore_errors=True)


def clean_installer():
    _clean_build_artifacts(repo_root)


def clean_scripts():
    for script_dir in data_scripts_dir.iterdir():
        if not script_dir.is_dir():
            continue

        _clean_build_artifacts(script_dir)


# -- .venv


def _clean_venv(dir_path: Path):
    print(f"Cleaning .venv of '{dir_path}'...")
    shutil.rmtree(dir_path / ".venv", ignore_errors=True)


def clean_venv_scripts():
    for script_dir in data_scripts_dir.iterdir():
        if not script_dir.is_dir():
            continue

        _clean_venv(script_dir)


# -- Manual entrypoint cleanup
if __name__ == "__main__":
    clean_installer()
    clean_scripts()
    clean_venv_scripts()
