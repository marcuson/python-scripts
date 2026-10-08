"""Offline checks for Google Translation suggestions and cost boundaries."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from mealietidy.analyze import analyze_plan
from mealietidy.cli import main
from mealietidy.config import resolve_settings
from mealietidy.export import export_catalog
from mealietidy.provider.gtranslate_provider import GTranslateProvider
from test_phase1 import FakeMealie


class GTranslateProviderTests(unittest.TestCase):
    def test_plan_uses_only_names_and_keeps_translation_pending(self) -> None:
        fake: FakeMealie = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                export_catalog(fake.base_url, "mealie-secret", root, 10)
            api_response: bytes = json.dumps(
                {
                    "data": {
                        "translations": [
                            {
                                "translatedText": "pomodoro",
                                "detectedSourceLanguage": "en",
                            },
                            {
                                "translatedText": "tomato",
                                "detectedSourceLanguage": "it",
                            },
                        ]
                    }
                }
            ).encode()
            with patch(
                "mealietidy.provider.gtranslate_provider.http.client.HTTPSConnection"
            ) as factory:
                connection: MagicMock = factory.return_value
                connection.getresponse.return_value.status = 200
                connection.getresponse.return_value.read.return_value = api_response
                analyze_plan(root, "gtranslate", {"google-api-key": "google-secret"})
            factory.assert_called_once_with("translation.googleapis.com", timeout=20.0)
            args, kwargs = connection.request.call_args
            self.assertEqual(args[:2], ("POST", "/language/translate/v2"))
            self.assertEqual(
                json.loads(kwargs["body"]),
                {
                    "q": ["Tomato", "tomato"],
                    "target": "it",
                    "format": "text",
                    "model": "nmt",
                },
            )
            self.assertEqual(kwargs["headers"]["X-goog-api-key"], "google-secret")
            plan: dict[str, Any] = json.loads((root / "plan.json").read_text())
            self.assertEqual(plan["provider"], "gtranslate")
            self.assertEqual(plan["provider_info"], {"model": "nmt"})
            rename: dict[str, Any] = next(
                item
                for item in plan["operations"]
                if item["food_id"] == "a" and item["action"] == "rename"
            )
            self.assertEqual(rename["proposed_value"], "pomodoro")
            self.assertEqual(rename["decision"], "pending")
            self.assertEqual(rename["uncertainty"], "high")
            self.assertNotIn("google-secret", (root / "plan.json").read_text())
            self.assertNotIn("mealie-secret", (root / "plan.json").read_text())
            self.assertTrue(
                all(
                    "google-secret" not in path.read_text()
                    for path in (root / "suggestions-cache").iterdir()
                )
            )

    def test_cache_prevents_repeat_request_and_run_cap_blocks_large_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider: GTranslateProvider = GTranslateProvider(
                Path(directory), "google-secret"
            )
            response: bytes = json.dumps(
                {
                    "data": {
                        "translations": [
                            {
                                "translatedText": "pomodoro",
                                "detectedSourceLanguage": "en",
                            }
                        ]
                    }
                }
            ).encode()
            with patch(
                "mealietidy.provider.gtranslate_provider.http.client.HTTPSConnection"
            ) as factory:
                connection: MagicMock = factory.return_value
                connection.getresponse.return_value.status = 200
                connection.getresponse.return_value.read.return_value = response
                food: list[dict[str, Any]] = [{"id": "a", "name": "Tomato"}]
                self.assertEqual(provider.suggest(food, {})[1], [])
                self.assertEqual(provider.suggest(food, {})[1], [])
                self.assertEqual(factory.call_count, 1)
                with patch("mealietidy.provider.gtranslate_provider.BATCH_SIZE", 1):
                    _, failures = provider.suggest(
                        [
                            {"id": str(index), "name": "x" * 4_499 + str(index)}
                            for index in range(4)
                        ],
                        {},
                    )
                self.assertEqual(failures, [["3"]])
                self.assertEqual(factory.call_count, 4)

    def test_api_failure_does_not_expose_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider: GTranslateProvider = GTranslateProvider(
                Path(directory), "google-secret"
            )
            output: io.StringIO = io.StringIO()
            with (
                patch(
                    "mealietidy.provider.gtranslate_provider.http.client.HTTPSConnection"
                ) as factory,
                redirect_stderr(output),
            ):
                connection: MagicMock = factory.return_value
                connection.getresponse.return_value.status = 403
                suggestions, failures = provider.suggest(
                    [{"id": "a", "name": "Tomato"}], {}
                )
            self.assertEqual(suggestions, [])
            self.assertEqual(failures, [["a"]])
            self.assertIn("HTTP 403", output.getvalue())
            self.assertNotIn("google-secret", output.getvalue())
            self.assertFalse((Path(directory) / "suggestions-cache").exists())

    def test_key_is_required_and_can_come_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / "mealietidy.yaml"
            with self.assertRaisesRegex(
                ValueError, "requires provider argument google-api-key"
            ):
                resolve_settings({"provider": "gtranslate"}, {}, {}, path)
            with self.assertRaisesRegex(
                ValueError, "requires provider argument google-api-key"
            ):
                resolve_settings(
                    {"provider": "gtranslate"},
                    {"google-api-key": "bad\nkey"},
                    {},
                    path,
                )
            settings = resolve_settings(
                {"provider": "gtranslate"},
                {},
                {"MEALIETIDY_PROVIDER_ARG_GOOGLE_API_KEY": "google-secret"},
                path,
            )
            self.assertEqual(settings.provider_args["google-api-key"], "google-secret")
            path.write_text(
                "provider: gtranslate\nprovider_args:\n  google_api_key: yaml-secret\n"
            )
            self.assertEqual(
                resolve_settings({}, {}, {}, path).provider_args["google-api-key"],
                "yaml-secret",
            )
            with self.assertRaisesRegex(
                ValueError, "Unknown gtranslate provider argument"
            ):
                resolve_settings(
                    {"provider": "gtranslate"}, {"api_key": "old-secret"}, {}, path
                )
            path.write_text(
                "provider: gtranslate\nprovider_args:\n"
                "  google_api_key: first\n  google-api-key: second\n"
            )
            with self.assertRaisesRegex(
                ValueError, "Duplicate provider argument in YAML: google-api-key"
            ):
                resolve_settings({}, {}, {}, path)

    def test_cli_google_api_key_overrides_environment_and_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / "mealietidy.yaml").write_text(
                f"provider: gtranslate\nplan: {root / 'plan'}\n"
                "provider_args:\n  google_api_key: yaml-secret\n"
            )
            with (
                patch("mealietidy.cli.Path.cwd", return_value=root),
                patch.dict(
                    os.environ,
                    {"MEALIETIDY_PROVIDER_ARG_GOOGLE_API_KEY": "env-secret"},
                    clear=True,
                ),
                patch("mealietidy.cli.analyze_plan", return_value=(0, 0)) as analyze,
            ):
                self.assertEqual(
                    main(["analyze", "--provider-arg-google-api-key=cli-secret"]), 0
                )
            analyze.assert_called_once_with(
                root / "plan",
                "gtranslate",
                {"google-api-key": "cli-secret"},
                False,
                False,
            )


if __name__ == "__main__":
    unittest.main()
