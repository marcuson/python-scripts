# Marcuson Python scripts

## How to install

```bash
uvx https://github.com/marcuson/python-scripts.git
```

## Development

### Useful commands quicksheet

```bash

# Poe the Poet task runner
uv run poe <task name>

# Cleaning
uv cache clean
uv run poe clean
uv run poe clean-all

# Launch this to automatically recreate the bin_list array of script to install by walking the "bin" directory
uv run poe build

uv run --no-project scripts/clean.py # Low level purge in case Poe tasks are not working
```