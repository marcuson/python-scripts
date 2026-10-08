"""Configuration precedence and file creation behavior."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mealietidy.cli import main
from mealietidy.config import create_config, read_config, resolve_settings


class ConfigTests(unittest.TestCase):
    def test_cli_environment_yaml_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mealietidy.yaml"
            path.write_text(
                "base_url: https://yaml.example\n"
                "token: yaml-token\n"
                "plan: yaml-plan\n"
                "timeout: 8\n"
                "dry_run: true\n"
                "refine: true\n"
            )
            settings = resolve_settings(
                {
                    "base_url": "https://cli.example",
                    "plan": "cli-plan",
                    "dry_run": False,
                    "refine": False,
                },
                {},
                {
                    "MEALIETIDY_BASE_URL": "https://env.example",
                    "MEALIETIDY_TOKEN": "env-token",
                    "MEALIETIDY_TIMEOUT": "5",
                    "MEALIETIDY_DRY_RUN": "true",
                    "MEALIETIDY_REFINE": "true",
                },
                path,
            )
            self.assertEqual(settings.base_url, "https://cli.example")
            self.assertEqual(settings.token, "env-token")
            self.assertEqual(settings.plan, Path("cli-plan"))
            self.assertEqual(settings.timeout, 5.0)
            self.assertFalse(settings.dry_run)
            self.assertFalse(settings.refine)
            self.assertTrue(
                resolve_settings({}, {}, {"MEALIETIDY_REFINE": "true"}, path).refine
            )
            self.assertEqual(
                resolve_settings({}, {}, {"MEALIE_TOKEN": "legacy-token"}, path).token,
                "legacy-token",
            )

    def test_export_reads_current_directory_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mealietidy.yaml").write_text(
                "base_url: https://yaml.example\ntoken: yaml-token\nplan: local-plan\n"
            )
            with (
                patch("mealietidy.cli.Path.cwd", return_value=root),
                patch.dict(os.environ, {}, clear=True),
                patch("mealietidy.cli.export_catalog", return_value=2) as export,
            ):
                self.assertEqual(main(["export"]), 0)
            export.assert_called_once_with(
                "https://yaml.example", "yaml-token", Path("local-plan"), 20.0
            )
            with (
                patch("mealietidy.cli.Path.cwd", return_value=root),
                patch.dict(
                    os.environ,
                    {
                        "MEALIETIDY_BASE_URL": "https://env.example",
                        "MEALIETIDY_TOKEN": "env-token",
                    },
                    clear=True,
                ),
                patch("mealietidy.cli.export_catalog", return_value=2) as export,
            ):
                self.assertEqual(
                    main(["export", "--base-url", "https://cli.example"]), 0
                )
            export.assert_called_once_with(
                "https://cli.example", "env-token", Path("local-plan"), 20.0
            )

    def test_config_creates_private_file_without_opening_an_editor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mealietidy.yaml"
            with (
                patch("mealietidy.cli.Path.cwd", return_value=Path(directory)),
                patch.dict(os.environ, {"EDITOR": "false"}, clear=True),
                patch("subprocess.run") as editor,
            ):
                self.assertEqual(main(["config"]), 0)
                editor.assert_not_called()
            created: str = path.read_text()
            self.assertIn("# base_url:", created)
            self.assertIn("# codex provider:", created)
            self.assertIn('#   reasoning_effort: "low"', created)
            self.assertIn("# gtranslate provider:", created)
            self.assertIn('#   google_api_key: "your Google Cloud API key"', created)
            self.assertEqual(read_config(path)["provider"], "none")
            self.assertEqual(read_config(path)["provider_args"], {})
            self.assertIn("provider: none\nprovider_args: {}\n", created)
            self.assertEqual(read_config(path)["timeout"], 20)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text(
                "# Keep my settings\nbase_url: https://edited.example\n"
                "provider: gtranslate\nprovider_args:\n  google_api_key: secret\n"
                "dry_run: true\n"
            )
            with patch("mealietidy.cli.Path.cwd", return_value=Path(directory)):
                self.assertEqual(main(["config"]), 0)
            content: str = path.read_text()
            self.assertIn(
                "# Keep my settings\nbase_url: https://edited.example", content
            )
            self.assertIn("  google_api_key: secret\n", content)
            self.assertIn("# codex provider:", content)
            self.assertIn("# gtranslate provider:", content)
            self.assertEqual(read_config(path)["provider"], "gtranslate")
            self.assertEqual(read_config(path)["dry_run"], True)
            self.assertEqual(read_config(path)["plan"], "mealietidy-plan")
            self.assertEqual(read_config(path)["refine"], False)
            self.assertEqual(create_config(path), "unchanged")
            self.assertEqual(path.read_text(), content)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_config_adds_examples_to_complete_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / "mealietidy.yaml"
            path.write_text(
                "plan: mealietidy-plan\ntimeout: 20\nprovider: none\n"
                "provider_args: {}\ndry_run: false\nrefine: false\n"
                "# Operator comment\n"
            )
            self.assertEqual(create_config(path), "updated")
            content: str = path.read_text()
            self.assertIn("# Operator comment\n", content)
            self.assertEqual(content.count("# codex provider:"), 1)
            self.assertEqual(content.count("# gtranslate provider:"), 1)
            self.assertEqual(read_config(path)["provider_args"], {})
            self.assertEqual(create_config(path), "unchanged")
            self.assertEqual(path.read_text(), content)

    def test_switching_provider_ignores_other_providers_lower_priority_arguments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mealietidy.yaml"
            path.write_text(
                "provider: gtranslate\nprovider_args:\n"
                "  google_api_key: yaml-key\n  timeout: '42'\n"
            )
            settings = resolve_settings(
                {"provider": "codex"},
                {"model": "test-model"},
                {"MEALIETIDY_PROVIDER_ARG_GOOGLE_API_KEY": "env-key"},
                path,
            )
            self.assertEqual(
                settings.provider_args, {"timeout": "42", "model": "test-model"}
            )
            with self.assertRaisesRegex(ValueError, "Unknown codex provider argument"):
                resolve_settings(
                    {"provider": "codex"},
                    {"google-api-key": "explicit-key"},
                    {},
                    path,
                )

    def test_config_merges_before_yaml_end_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mealietidy.yaml"
            path.write_text("provider: codex\n... # end\n# operator comment\n")
            self.assertEqual(create_config(path), "updated")
            self.assertEqual(read_config(path)["provider"], "codex")
            self.assertEqual(read_config(path)["plan"], "mealietidy-plan")
            self.assertTrue(
                path.read_text().endswith("... # end\n# operator comment\n")
            )

    def test_invalid_config_and_provider_arguments_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mealietidy.yaml"
            path.write_text("unknown: value\n")
            with self.assertRaisesRegex(ValueError, "Unknown setting"):
                read_config(path)
            path.write_text("provider_args:\n  model: local\n")
            with self.assertRaisesRegex(ValueError, "accepts no"):
                resolve_settings({}, {}, {}, path)
            path.write_text("dry_run: false\n")
            with self.assertRaisesRegex(ValueError, "true or false"):
                resolve_settings({}, {}, {"MEALIETIDY_DRY_RUN": "maybe"}, path)
            path.write_text("'yes': true\n")
            with self.assertRaisesRegex(ValueError, "Unknown setting"):
                read_config(path)
            path.write_text("timeout: nope\n")
            self.assertEqual(create_config(path), "updated")
            self.assertEqual(read_config(path)["timeout"], "nope")
            path.write_text("provider_args: [\n")
            with self.assertRaisesRegex(ValueError, "Invalid mealietidy.yaml"):
                create_config(path)
            self.assertEqual(path.read_text(), "provider_args: [\n")


if __name__ == "__main__":
    unittest.main()
