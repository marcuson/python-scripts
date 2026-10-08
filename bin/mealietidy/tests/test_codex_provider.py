"""Offline checks for Codex suggestions and their review boundaries."""

import csv
import io
import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mealietidy.analyze import analyze_plan
from mealietidy.config import resolve_settings
from mealietidy.export import export_catalog
from mealietidy.provider import PROVIDERS, SuggestionProvider, create_provider
from mealietidy.provider.codex_provider import DEFAULT_MODEL, CodexProvider
from mealietidy.review import load_review
from test_phase1 import FakeMealie


class CodexProviderTests(unittest.TestCase):
    def test_registered_provider_uses_shared_analysis_path(self) -> None:
        class StubProvider:
            name: str = "stub"
            metadata: Mapping[str, str | None] = {"model": "test-model"}

            def suggest(
                self, foods: list[dict[str, Any]], context: dict[str, Any]
            ) -> tuple[list[dict[str, Any]], list[list[str]]]:
                return [
                    {
                        "food_id": "a",
                        "action": "rename",
                        "value": "pomodoro",
                        "language": "it",
                        "rationale": "test suggestion",
                        "uncertainty": "low",
                        "related_candidate_ids": [],
                    }
                ], []

        def create_stub(_root: Path, _args: Mapping[str, str]) -> StubProvider:
            return StubProvider()

        fake: FakeMealie = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                export_catalog(fake.base_url, "token", root, 10)
            with patch.dict(PROVIDERS, {"stub": create_stub}):
                settings = resolve_settings(
                    {"provider": "stub"}, {}, {}, root / "mealietidy.yaml"
                )
                self.assertEqual(settings.provider, "stub")
                analyze_plan(root, "stub")
            plan: dict[str, Any] = json.loads((root / "plan.json").read_text())
            self.assertEqual(plan["provider"], "stub")
            self.assertEqual(plan["provider_info"], {"model": "test-model"})
            self.assertEqual(
                next(
                    item
                    for item in plan["operations"]
                    if item["food_id"] == "a" and item["action"] == "rename"
                )["proposed_value"],
                "pomodoro",
            )

    def test_previous_schema_plan_remains_readable(self) -> None:
        fake: FakeMealie = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                export_catalog(fake.base_url, "token", root, 10)
            analyze_plan(root)
            plan_path: Path = root / "plan.json"
            plan: dict[str, Any] = json.loads(plan_path.read_text())
            plan["schema_version"] = "1.1.0"
            plan.pop("provider_info")
            plan["model"] = None
            plan_path.write_text(json.dumps(plan))
            output: io.StringIO = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(load_review(root).groups, {})
            self.assertIn("older plan schema 1.1.0", output.getvalue())

    def test_codex_suggestions_remain_pending_and_send_minimal_data(self) -> None:
        fake: FakeMealie = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                export_catalog(fake.base_url, "secret-token", root, 10)

            calls: list[tuple[list[str], str]] = []

            def respond(command: list[str], **kwargs: Any) -> object:
                prompt: str = kwargs["input"]
                calls.append((command, prompt))
                response: Path = Path(
                    command[command.index("--output-last-message") + 1]
                )
                response.write_text(
                    json.dumps(
                        {
                            "suggestions": [
                                {
                                    "food_id": "a",
                                    "action": "rename",
                                    "value": "pomodoro",
                                    "language": "it",
                                    "rationale": "Italian name for tomato",
                                    "uncertainty": "low",
                                    "related_candidate_ids": [],
                                }
                            ]
                        }
                    )
                )
                self.assertEqual(kwargs["cwd"], response.parent)
                self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
                self.assertNotIn("CODEX_API_KEY", kwargs["env"])
                return type("Result", (), {"returncode": 0})()

            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "api-secret"}),
                patch(
                    "mealietidy.provider.codex_provider.subprocess.run",
                    side_effect=respond,
                ),
            ):
                self.assertEqual(analyze_plan(root, "codex"), (7, 4))

            self.assertEqual(len(calls), 1)
            command, prompt = calls[0]
            self.assertIn("--sandbox", command)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
            self.assertIn("--skip-git-repo-check", command)
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[command.index("--model") + 1], DEFAULT_MODEL)
            self.assertNotIn("secret-token", prompt)
            self.assertNotIn("extras", prompt)
            plan: dict[str, Any] = json.loads((root / "plan.json").read_text())
            self.assertEqual(plan["provider"], "codex")
            self.assertEqual(plan["provider_info"], {"model": DEFAULT_MODEL})
            self.assertEqual(plan["schema_version"], "1.5.0")
            rename: dict[str, Any] = next(
                item
                for item in plan["operations"]
                if item["food_id"] == "a" and item["action"] == "rename"
            )
            self.assertEqual(rename["proposed_value"], "pomodoro")
            self.assertEqual(rename["decision"], "pending")
            self.assertEqual(rename["uncertainty"], "low")
            with (root / "review.csv").open(newline="", encoding="utf-8") as stream:
                rows: list[dict[str, str]] = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["proposed_value"], "pomodoro")
            self.assertEqual(rows[0]["decision"], "pending")
            self.assertEqual(load_review(root).groups, {})

    def test_cache_reuses_validated_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider: CodexProvider = CodexProvider(Path(directory), None)
            batch: list[dict[str, Any]] = [{"id": "a", "name": "Tomato"}]
            with patch.object(
                provider, "_run", return_value={"suggestions": []}
            ) as run:
                self.assertEqual(provider.suggest(batch, {}), ([], []))
                self.assertEqual(provider.suggest(batch, {}), ([], []))
            run.assert_called_once()

    def test_reasoning_effort_reaches_cli_and_separates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            commands: list[list[str]] = []

            def respond(command: list[str], **_kwargs: Any) -> object:
                commands.append(command)
                response: Path = Path(
                    command[command.index("--output-last-message") + 1]
                )
                response.write_text('{"suggestions": []}', encoding="utf-8")
                return type("Result", (), {"returncode": 0})()

            foods: list[dict[str, Any]] = [{"id": "a", "name": "Tomato"}]
            with patch(
                "mealietidy.provider.codex_provider.subprocess.run",
                side_effect=respond,
            ):
                for effort in ("low", "low", "high"):
                    provider: CodexProvider = CodexProvider(
                        root, "test-model", reasoning_effort=effort
                    )
                    self.assertEqual(provider.suggest(foods, {}), ([], []))
            self.assertEqual(len(commands), 2)
            self.assertEqual(
                [command[command.index("--config") + 1] for command in commands],
                ['model_reasoning_effort="low"', 'model_reasoning_effort="high"'],
            )
            self.assertTrue(
                all(
                    command[command.index("--model") + 1] == "test-model"
                    for command in commands
                )
            )

    def test_progress_counts_failed_batches_too(self) -> None:
        fake: FakeMealie = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                export_catalog(fake.base_url, "token", root, 10)
            output: io.StringIO = io.StringIO()
            with (
                patch("mealietidy.provider.codex_provider.BATCH_SIZE", 1),
                patch(
                    "mealietidy.provider.codex_provider.CodexProvider._run",
                    side_effect=[{"suggestions": []}, RuntimeError("test failure")],
                ),
                redirect_stderr(output),
            ):
                analyze_plan(root, "codex")
            progress: str = output.getvalue()
            self.assertIn("Codex: 0/2 foods processed", progress)
            self.assertIn("Codex: 1/2 foods processed", progress)
            self.assertIn("Codex: 2/2 foods processed", progress)
            self.assertIn("Codex: processing foods 2-2 of 2", progress)
            self.assertIn("Codex suggestions unavailable: test failure", progress)

    def test_invalid_output_leaves_pending_rows(self) -> None:
        fake: FakeMealie = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                export_catalog(fake.base_url, "token", root, 10)
            invalid: dict[str, object] = {
                "suggestions": [
                    {
                        "food_id": "unknown",
                        "action": "rename",
                        "value": "bad",
                        "language": "it",
                        "rationale": "unknown source",
                        "uncertainty": "low",
                        "related_candidate_ids": [],
                    }
                ]
            }
            with patch(
                "mealietidy.provider.codex_provider.CodexProvider._run",
                return_value=invalid,
            ):
                operations, findings = analyze_plan(root, "codex")
            self.assertEqual(operations, 7)
            self.assertEqual(findings, 5)
            plan: dict[str, Any] = json.loads((root / "plan.json").read_text())
            self.assertTrue(
                all(not item["proposed_value"] for item in plan["operations"])
            )
            self.assertEqual(plan["findings"][-1]["kind"], "provider_error")

    def test_cli_failure_reports_status_without_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider: CodexProvider = CodexProvider(Path(directory), None)
            failed: object = type(
                "Result", (), {"returncode": 17, "stderr": "private diagnostic"}
            )()
            output: io.StringIO = io.StringIO()
            with (
                patch(
                    "mealietidy.provider.codex_provider.subprocess.run",
                    return_value=failed,
                ),
                redirect_stderr(output),
            ):
                self.assertEqual(
                    provider.suggest([{"id": "a", "name": "Tomato"}], {}),
                    ([], [["a"]]),
                )
            self.assertIn("status 17", output.getvalue())
            self.assertNotIn("private diagnostic", output.getvalue())
            self.assertFalse((Path(directory) / "suggestions-cache").exists())

    def test_codex_provider_arguments_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / "mealietidy.yaml"
            path.write_text(
                "provider: codex\nprovider_args:\n  reasoning_effort: high\n",
                encoding="utf-8",
            )
            settings = resolve_settings(
                {"provider": "codex"},
                {"model": "example-model", "reasoning-effort": "low", "timeout": "40"},
                {"MEALIETIDY_PROVIDER_ARG_REASONING_EFFORT": "medium"},
                path,
            )
            self.assertEqual(settings.provider_args["model"], "example-model")
            provider: SuggestionProvider = create_provider(
                "codex", Path(directory), settings.provider_args
            )
            self.assertEqual(provider.metadata["model"], "example-model")
            self.assertEqual(provider.metadata["reasoning_effort"], "low")
            env_settings = resolve_settings(
                {"provider": "codex"},
                {},
                {"MEALIETIDY_PROVIDER_ARG_REASONING_EFFORT": "medium"},
                path,
            )
            self.assertEqual(env_settings.provider_args["reasoning-effort"], "medium")
            yaml_settings = resolve_settings({"provider": "codex"}, {}, {}, path)
            self.assertEqual(yaml_settings.provider_args["reasoning-effort"], "high")
            with self.assertRaisesRegex(ValueError, "Unknown codex provider argument"):
                resolve_settings({"provider": "codex"}, {"secret": "x"}, {}, path)
            with self.assertRaisesRegex(ValueError, "positive number"):
                resolve_settings({"provider": "codex"}, {"timeout": "0"}, {}, path)
            with self.assertRaisesRegex(ValueError, "reasoning-effort"):
                resolve_settings(
                    {"provider": "codex"}, {"reasoning-effort": "fast"}, {}, path
                )


if __name__ == "__main__":
    unittest.main()
