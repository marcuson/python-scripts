#!/usr/bin/env python3

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _install_scripts_dir(bins_dir: Path):
    """Iterate through scripts in the 'marcuson_python_scripts' folder and install them with uv."""
    for script_dir in bins_dir.iterdir():
        if not script_dir.is_dir():
            continue

        print(f"Installing {script_dir.name}...")

        subprocess.run(
            ["uv", "tool", "install", "--force", script_dir.absolute()],
            check=True,
        )


def _try_install_scripts():
    """Check 'marcuson_python_scripts' folder location and install scripts."""

    python_exec_dir = Path(os.path.dirname(sys.executable))
    candidates = [
        python_exec_dir.parent / "mps",
        Path(__file__).parent.parent.parent / "data" / "mps",
    ]
    is_install_ok = False

    for candidate in candidates:
        print(f"Checking scripts folder in: {candidate}")

        if candidate.exists():
            print(f"Installing scripts from folder: {candidate}")

            _install_scripts_dir(candidate)
            is_install_ok = True
            break

    if not is_install_ok:
        print("Error: 'bin' folder not found.")
        sys.exit(1)


def install():
    if shutil.which("uv") is None:
        print("Error: 'uv' is not installed. Please install it and try again.")
        sys.exit(1)

    _try_install_scripts()
    print("All Python scripts were installed/updated successfully!")


if __name__ == "__main__":
    install()
