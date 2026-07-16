#!/usr/bin/env python3

import shutil
import subprocess
import sys
from pathlib import Path

from marcuson_python_scripts import bin_list

from . import pkg_utils


def _install_local_bins_dir(root_dir: Path):
    """Iterate through scripts in the 'bin' folder and install them with uv."""
    bins_dir = root_dir / "bin"

    if not bins_dir.exists():
        print(f"Error: '{bins_dir}' folder not found.")
        sys.exit(1)

    for bin_dir_name in bin_list.bins:
        bin_dir = bins_dir / bin_dir_name

        print(f"Installing {bin_dir.name}...")

        if not bin_dir.is_dir():
            print(f"Warning: cannot find {bin_dir.name}!")
            print("")
            continue

        subprocess.run(
            ["uv", "tool", "install", "--force", bin_dir.absolute()],
            check=True,
        )
        print("")


def _install_git_bins_dir(git_url: str):
    """Iterate through scripts in the 'bin' folder and install them with uv."""
    for bin_dir_name in bin_list.bins:
        print(f"Installing {bin_dir_name}...")

        full_url = f"{git_url}#subdirectory=bin/{bin_dir_name}"
        subprocess.run(
            ["uv", "tool", "install", "--force", full_url],
            check=True,
        )
        print("")


def install():
    pkg_meta = pkg_utils.get_pkg_metadata()
    if pkg_meta.is_local_exec:
        print("Local exection detected.")
        print(f"Pkg installed: {pkg_meta.is_installed}")
        print("")

    if shutil.which("uv") is None:
        print("Error: 'uv' is not installed. Please install it and try again.")
        sys.exit(1)

    if pkg_meta.is_local_exec:
        _install_local_bins_dir(Path.from_uri(pkg_meta.dist_uri))
    else:
        _install_git_bins_dir(pkg_meta.dist_uri)

    print("")
    print("All Python scripts were installed/updated successfully!")


if __name__ == "__main__":
    install()
