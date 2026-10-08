# Marcuson Python scripts

A collection of small Python command-line tools. The root package installs the tools in `bin/` as separate `uv` tools. Currently it includes `hellopython`, which prints a greeting.

## Install

Install [uv](https://docs.astral.sh/uv/) first. The packages require Python 3.12 or newer.

```bash
uvx https://github.com/marcuson/python-scripts.git
```

`uvx` runs this repository's installer in a temporary environment. The installer then installs each tool in `bin/` persistently with `uv tool install --force`. Running the command again updates those tools and replaces their existing installations.

## Use

Make sure the `uv` tool executable directory is on your `PATH`, then run the currently available command:

```bash
hellopython
```

## Develop

The repository selects Python 3.14 via `.python-version`. Each directory in `bin/` is an independent package with its own `pyproject.toml` and lockfile. The root package provides the installer and the Poe tasks below.

To install just one tool from a local checkout for development, run this from the repository root:

```bash
uv tool install --force --editable ./bin/hellopython
```

Replace `hellopython` with another directory under `bin/` as needed. Editable installation links the tool to its source, so Python code changes take effect without reinstalling. `--force` replaces an existing installation of that tool; rerun the command after changing its package metadata or dependencies.

```bash
uv run poe build      # Regenerate marcuson_python_scripts/bin_list.py and build the root package
uv run poe clean      # Remove root and tool dist/ directories
uv run poe clean-all  # Also remove tool .venv directories
```

When adding a tool, create its package under `bin/`, define its command in that package's `[project.scripts]`, then run `uv run poe build` and include the updated `bin_list.py`. There is no tracked test suite; check the affected command directly.

For cleanup without Poe, run `uv run --no-project python -m scripts.clean` from the repository root to remove the same build artifacts and tool virtual environments as `clean-all`. `uv cache clean` also clears uv's cache, but is separate from the repository cleanup tasks.
