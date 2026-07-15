# Marcuson Python scripts

## How to install

```bash
uvx https://github.com/marcuson/python-scripts.git
```

## Development

### Useful commands quicksheet

```bash
uv cache clean # Clean uv build cache
uv build
uv build --clear

uv run --no-project scripts/<script-name> # Something like task runner
uv run --no-project scripts/build.py
uv run --no-project scripts/clean.py
```