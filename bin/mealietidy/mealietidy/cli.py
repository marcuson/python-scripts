"""Command-line entry point for catalog export, analysis, and application."""

import argparse
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import cast

from mealietidy.analyze import analyze_plan
from mealietidy.apply import apply_plan
from mealietidy.config import CONFIG_NAME, create_config, resolve_settings
from mealietidy.editor import edit_plan
from mealietidy.export import export_catalog


def main(argv: list[str] | None = None) -> int:
    version_text: str = f"mealietidy {version('mealietidy')}"
    parser = argparse.ArgumentParser(prog="mealietidy")
    parser.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Show the installed tool version",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser(
        "export",
        help="Discover the API and export every food without writing to Mealie",
    )
    export.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Show the installed tool version",
    )
    export.add_argument("--base-url", help="Mealie instance URL")
    export.add_argument("--token", help="Mealie API token")
    export.add_argument("--plan", help="Plan directory (default: ./mealietidy-plan)")
    export.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="HTTP timeout in seconds"
    )

    analyze = commands.add_parser(
        "analyze",
        help="Build a JSON/CSV review plan with optional AI suggestions",
        epilog="Provider settings: --provider-arg-NAME=VALUE (repeatable; accepted names depend on provider).",
    )
    analyze.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Show the installed tool version",
    )
    analyze.add_argument("--plan", help="Plan directory (default: ./mealietidy-plan)")
    analyze.add_argument(
        "--provider", help="Suggestion provider: none, codex, or gtranslate"
    )
    analyze.add_argument(
        "--refine",
        action=argparse.BooleanOptionalAction,
        help="Refine pending suggestions in an existing plan",
    )

    edit = commands.add_parser(
        "edit",
        help="Review foods in a local browser page and save progress to review.csv",
    )
    edit.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Show the installed tool version",
    )
    edit.add_argument("--plan", help="Plan directory (default: ./mealietidy-plan)")
    edit.add_argument("--base-url", help="Mealie instance URL for recipe usage counts")
    edit.add_argument("--token", help="Mealie API token for recipe usage counts")
    edit.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="HTTP timeout in seconds"
    )

    apply = commands.add_parser("apply", help="Validate and apply approved food edits")
    apply.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Show the installed tool version",
    )
    apply.add_argument("--base-url", help="Mealie instance URL used for export")
    apply.add_argument("--token", help="Mealie API token")
    apply.add_argument("--plan", help="Plan directory (default: ./mealietidy-plan)")
    apply.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="HTTP timeout in seconds"
    )
    apply.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        help="Preview and check without writing to Mealie",
    )

    config = commands.add_parser(
        "config", help="Create or update mealietidy.yaml in the current directory"
    )
    config.add_argument(
        "--version",
        action="version",
        version=version_text,
        help="Show the installed tool version",
    )

    args, unknown = parser.parse_known_args(argv)
    provider_args: dict[str, str] = {}
    for argument in unknown:
        if args.command != "analyze" or not argument.startswith("--provider-arg-"):
            parser.error("unrecognized argument: " + argument)
        name, separator, value = argument.removeprefix("--provider-arg-").partition("=")
        if not separator or not name or name in provider_args:
            parser.error(
                "provider arguments require unique --provider-arg-NAME=VALUE options"
            )
        provider_args[name] = value
    try:
        config_path: Path = Path.cwd() / CONFIG_NAME
        if args.command == "config":
            result: str = create_config(config_path)
            print(f"{result.capitalize()} configuration file: {config_path}")
            return 0
        cli_values: dict[str, object] = {
            name: getattr(args, name, None)
            for name in (
                "base_url",
                "token",
                "plan",
                "timeout",
                "provider",
                "dry_run",
                "refine",
            )
        }
        settings = resolve_settings(cli_values, provider_args, os.environ, config_path)
        if args.command == "export":
            if not settings.base_url:
                raise ValueError(
                    "Mealie base URL required: pass --base-url or configure base_url"
                )
            if not settings.token:
                raise ValueError(
                    "Mealie token required: pass --token or set MEALIETIDY_TOKEN"
                )
            count = export_catalog(
                settings.base_url, settings.token, settings.plan, settings.timeout
            )
            print(f"Exported {count} foods to {settings.plan}")
        elif args.command == "analyze":
            existing: bool = (settings.plan / "plan.json").exists()
            confirmed: bool = False
            if existing and not settings.refine:
                previous: object = json.loads(
                    (settings.plan / "plan.json").read_bytes()
                )
                old_provider: str = "unknown"
                if isinstance(previous, dict):
                    provider_value: object = cast(dict[str, object], previous).get(
                        "provider"
                    )
                    if isinstance(provider_value, str):
                        old_provider = provider_value
                try:
                    answer: str = input(
                        f"Plan already analyzed with {old_provider}. Reanalyze pending rows with {settings.provider}? [y/N]: "
                    )
                except EOFError:
                    answer = ""
                if answer.strip().lower() not in {"y", "yes"}:
                    raise RuntimeError(
                        "Analysis cancelled; use --refine to refine pending suggestions"
                    )
                confirmed = True
            operations, findings = analyze_plan(
                settings.plan,
                settings.provider,
                settings.provider_args,
                settings.refine,
                confirmed,
            )
            if existing:
                print(
                    f"Updated {operations} pending suggestion(s) in {settings.plan}; {findings} total findings"
                )
            else:
                print(
                    f"Created {operations} pending operations and {findings} findings in {settings.plan}"
                )
        elif args.command == "edit":
            edit_plan(
                settings.plan,
                settings.base_url,
                settings.token,
                settings.timeout,
            )
        else:
            if not settings.base_url:
                raise ValueError(
                    "Mealie base URL required: pass --base-url or configure base_url"
                )
            if not settings.token:
                raise ValueError(
                    "Mealie token required: pass --token or set MEALIETIDY_TOKEN"
                )
            applied, skipped = apply_plan(
                settings.base_url,
                settings.token,
                settings.plan,
                settings.timeout,
                settings.dry_run,
            )
            if not settings.dry_run:
                print(
                    f"Applied plan in {settings.plan}: {applied} food writes, {skipped} already verified"
                )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(f"mealietidy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
