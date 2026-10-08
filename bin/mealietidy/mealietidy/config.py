"""Load local CLI settings and merge a private configuration file."""

import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

from mealietidy.provider import PROVIDER_ARGUMENTS, create_provider

CONFIG_NAME: str = "mealietidy.yaml"
KEYS: frozenset[str] = frozenset(
    {
        "base_url",
        "token",
        "plan",
        "timeout",
        "provider",
        "provider_args",
        "dry_run",
        "refine",
    }
)
DEFAULTS: dict[str, object] = {
    "plan": "mealietidy-plan",
    "timeout": 20,
    "dry_run": False,
    "refine": False,
    "provider": "none",
    "provider_args": {},
}
PROVIDER_EXAMPLES: str = """# Provider examples. Comment out the active provider settings above and uncomment one block below.
# none is the default provider and takes no arguments.
provider: none
provider_args: {}

# codex provider:
# provider: codex
# provider_args:
#   model: "gpt-6-luna"
#   reasoning_effort: "low"
#   timeout: "120"

# gtranslate provider:
# provider: gtranslate
# provider_args:
#   google_api_key: "your Google Cloud API key"
#   timeout: "20"
"""
LEGACY_PROVIDER_EXAMPLES: tuple[str, ...] = (
    """# Provider argument examples. Replace the active provider_args value with one block.
# none provider: no arguments.
# provider_args: {}

# codex provider:
# provider_args:
#   model: "gpt-6-luna"
#   reasoning_effort: "low"
#   timeout: "120"

# gtranslate provider:
# provider_args:
#   google_api_key: "your Google Cloud API key"
#   timeout: "20"
""",
    """# Provider selection examples. Replace the active provider_args value with one block.
# none provider: no arguments.
# provider: none
# provider_args: {}

# codex provider:
# provider: codex
# provider_args:
#   model: "gpt-6-luna"
#   reasoning_effort: "low"
#   timeout: "120"

# gtranslate provider:
# provider: gtranslate
# provider_args:
#   google_api_key: "your Google Cloud API key"
#   timeout: "20"
""",
    """# Provider examples. Comment out the active provider settings above and uncomment one block below.
# none is the default provider and takes no arguments.

# codex provider:
# provider: codex
# provider_args:
#   model: "gpt-6-luna"
#   reasoning_effort: "low"
#   timeout: "120"

# gtranslate provider:
# provider: gtranslate
# provider_args:
#   google_api_key: "your Google Cloud API key"
#   timeout: "20"
""",
)
TEMPLATE: str = (
    """# Local mealietidy settings. CLI flags and environment variables override these values.
# base_url: https://mealie.example
# token: ""  # Prefer MEALIETIDY_TOKEN or MEALIE_TOKEN for secrets.
"""
    + yaml.safe_dump(
        {
            key: value
            for key, value in DEFAULTS.items()
            if key not in {"provider", "provider_args"}
        },
        sort_keys=False,
    )
    + "\n"
    + PROVIDER_EXAMPLES
)


@dataclass(frozen=True)
class Settings:
    base_url: str | None
    token: str | None
    plan: Path
    timeout: float
    provider: str
    provider_args: dict[str, str]
    dry_run: bool
    refine: bool = False


def read_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as stream:
            document: object = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid {path.name}: {error}") from error
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise TypeError(f"{path.name} must contain a YAML mapping")
    result: dict[str, object] = {}
    for key, value in cast("dict[object, object]", document).items():
        if not isinstance(key, str) or key not in KEYS:
            raise ValueError(f"Unknown setting in {path.name}: {key!r}")
        result[key] = value
    return result


def _pick(
    name: str,
    cli: Mapping[str, object],
    env: Mapping[str, str],
    config: Mapping[str, object],
) -> object:
    value: object = cli.get(name)
    if value is not None:
        return value
    environment_name: str = f"MEALIETIDY_{name.upper()}"
    if environment_name in env:
        return env[environment_name]
    if name == "token" and "MEALIE_TOKEN" in env:
        return env["MEALIE_TOKEN"]
    return config.get(name)


def _text(name: str, value: object, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _boolean(name: str, value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in {"1", "true", "yes", "on"}:
            return True
        if value.lower() in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} must be true or false")


def _timeout(value: object) -> float:
    if value is None:
        return 20.0
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("timeout must be a positive number")
    try:
        timeout: float = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("timeout must be a positive number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    return timeout


def _provider_args(
    config: Mapping[str, object],
    env: Mapping[str, str],
    cli: Mapping[str, str],
    accepted: frozenset[str] | None,
) -> dict[str, str]:
    raw: object = config.get("provider_args", {})
    if not isinstance(raw, dict):
        raise TypeError("provider_args must map names to string values")
    result: dict[str, str] = {}
    for key, value in cast("dict[object, object]", raw).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("provider_args must map names to string values")
        name: str = key.replace("_", "-")
        if name in result:
            raise ValueError(f"Duplicate provider argument in YAML: {name}")
        if accepted is None or name in accepted:
            result[name] = value
    prefix: str = "MEALIETIDY_PROVIDER_ARG_"
    for key, value in env.items():
        if key.startswith(prefix):
            name: str = key.removeprefix(prefix).lower().replace("_", "-")
            if not name:
                raise ValueError("Provider argument name cannot be empty")
            if accepted is None or name in accepted:
                result[name] = value
    result.update(cli)
    return result


def resolve_settings(
    cli: Mapping[str, object],
    cli_provider_args: Mapping[str, str],
    env: Mapping[str, str],
    path: Path,
) -> Settings:
    config: dict[str, object] = read_config(path)
    plan: str | None = _text("plan", _pick("plan", cli, env, config), "mealietidy-plan")
    if not plan:
        raise ValueError("plan must be a nonempty path")
    provider: str | None = _text(
        "provider", _pick("provider", cli, env, config), "none"
    )
    if provider is None:
        raise ValueError("provider must be a nonempty name")
    configured_provider: object = _pick("provider", {}, env, config)
    if configured_provider is None:
        configured_provider = "none"
    accepted: frozenset[str] | None = (
        PROVIDER_ARGUMENTS.get(provider) if configured_provider != provider else None
    )
    provider_args: dict[str, str] = _provider_args(
        config, env, cli_provider_args, accepted
    )
    create_provider(provider, Path(plan), provider_args)
    return Settings(
        base_url=_text("base_url", _pick("base_url", cli, env, config)),
        token=_text("token", _pick("token", cli, env, config)),
        plan=Path(plan),
        timeout=_timeout(_pick("timeout", cli, env, config)),
        provider=provider,
        provider_args=provider_args,
        dry_run=_boolean("dry_run", _pick("dry_run", cli, env, config)),
        refine=_boolean("refine", _pick("refine", cli, env, config)),
    )


def create_config(path: Path) -> Literal["created", "updated", "unchanged"]:
    try:
        descriptor: int = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(TEMPLATE)
        return "created"

    original: str = path.read_text(encoding="utf-8")
    existing: dict[str, object] = read_config(path)
    if path.read_text(encoding="utf-8") != original:
        raise RuntimeError(f"{path.name} changed while updating it; retry")
    missing: dict[str, object] = {
        key: value for key, value in DEFAULTS.items() if key not in existing
    }
    legacy_examples_present: bool = False
    merge_base: str = original
    for legacy_examples in LEGACY_PROVIDER_EXAMPLES:
        if legacy_examples in merge_base:
            merge_base = merge_base.replace(legacy_examples, PROVIDER_EXAMPLES)
            legacy_examples_present = True
    example_marker: str = PROVIDER_EXAMPLES.splitlines()[0]
    marker_index: int = merge_base.find(example_marker)
    if marker_index >= 0:
        prefix: str = merge_base[:marker_index]
        for default_settings in (
            "provider: none\nprovider_args: {}\n\n",
            "provider: none\nprovider_args: {}\n",
        ):
            if prefix.endswith(default_settings):
                merge_base = (
                    prefix[: -len(default_settings)] + merge_base[marker_index:]
                )
                break
    examples_missing: bool = example_marker not in merge_base
    if not missing and not examples_missing and not legacy_examples_present:
        return "unchanged"
    suffix: str = yaml.safe_dump(missing, sort_keys=False) if missing else ""
    if examples_missing:
        provider_examples: str = PROVIDER_EXAMPLES
        if "provider" in existing:
            provider_examples = provider_examples.replace(
                "provider: none\nprovider_args: {}\n",
                "# provider: none\n# provider_args: {}\n",
            )
        suffix += "\n" + provider_examples
    lines: list[str] = merge_base.splitlines(keepends=True)
    end_index: int | None = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if lines[index].strip() and not lines[index].lstrip().startswith("#")
        ),
        None,
    )
    if end_index is not None and lines[end_index].split("#", 1)[0].strip() == "...":
        updated: str = (
            "".join(lines[:end_index]).rstrip("\r\n")
            + "\n"
            + suffix
            + "".join(lines[end_index:])
        )
    else:
        updated = merge_base.rstrip("\r\n") + "\n" + suffix
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".mealietidy-config-",
            delete=False,
        ) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            stream.write(updated)
        read_config(Path(temporary))
        if path.read_text(encoding="utf-8") != original:
            raise RuntimeError(f"{path.name} changed while updating it; retry")
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return "updated"
