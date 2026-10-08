"""Provider contract and registry for optional food suggestions."""

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from mealietidy.provider.codex_provider import CodexProvider
from mealietidy.provider.gtranslate_provider import GTranslateProvider


class SuggestionProvider(Protocol):
    name: str

    @property
    def metadata(self) -> Mapping[str, str | None]: ...

    def suggest(
        self, foods: list[dict[str, Any]], context: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[list[str]]]: ...


class NoneProvider:
    name: str = "none"
    metadata: Mapping[str, str | None] = {}

    def suggest(
        self, foods: list[dict[str, Any]], context: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[list[str]]]:
        return [], []


def _none_provider(root: Path, arguments: Mapping[str, str]) -> SuggestionProvider:
    if arguments:
        raise ValueError("provider 'none' accepts no --provider-arg-* options")
    return NoneProvider()


def _codex_provider(root: Path, arguments: Mapping[str, str]) -> SuggestionProvider:
    unknown: set[str] = set(arguments) - {"model", "reasoning-effort", "timeout"}
    if unknown:
        raise ValueError(f"Unknown codex provider argument: {min(unknown)}")
    model: str | None = arguments.get("model")
    if model is not None and (not model or any(char.isspace() for char in model)):
        raise ValueError("codex model must be a nonempty name without whitespace")
    reasoning_effort: str | None = arguments.get("reasoning-effort")
    if reasoning_effort is not None and reasoning_effort not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }:
        raise ValueError("codex reasoning-effort must be a supported effort level")
    try:
        timeout: float = float(arguments.get("timeout", "120"))
    except ValueError as error:
        raise ValueError("codex timeout must be a positive number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("codex timeout must be a positive number")
    return CodexProvider(root, model, timeout, reasoning_effort)


def _gtranslate_provider(
    root: Path, arguments: Mapping[str, str]
) -> SuggestionProvider:
    unknown: set[str] = set(arguments) - {"google-api-key", "timeout"}
    if unknown:
        raise ValueError(f"Unknown gtranslate provider argument: {min(unknown)}")
    api_key: str | None = arguments.get("google-api-key")
    if (
        not api_key
        or not api_key.isascii()
        or any(not char.isprintable() or char.isspace() for char in api_key)
    ):
        raise ValueError("gtranslate requires provider argument google-api-key")
    try:
        timeout: float = float(arguments.get("timeout", "20"))
    except ValueError as error:
        raise ValueError("gtranslate timeout must be a positive number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("gtranslate timeout must be a positive number")
    return GTranslateProvider(root, api_key, timeout)


PROVIDERS: dict[str, Callable[[Path, Mapping[str, str]], SuggestionProvider]] = {
    "none": _none_provider,
    "codex": _codex_provider,
    "gtranslate": _gtranslate_provider,
}
PROVIDER_ARGUMENTS: dict[str, frozenset[str]] = {
    "none": frozenset(),
    "codex": frozenset({"model", "reasoning-effort", "timeout"}),
    "gtranslate": frozenset({"google-api-key", "timeout"}),
}


def create_provider(
    name: str, root: Path, arguments: Mapping[str, str]
) -> SuggestionProvider:
    factory = PROVIDERS.get(name)
    if factory is None:
        raise ValueError(f"Unknown provider: {name!r}")
    return factory(root, arguments)
