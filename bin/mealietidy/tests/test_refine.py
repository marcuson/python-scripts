"""Repeated provider passes preserve reviewed work."""

import csv
import io
import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Self, cast
from unittest.mock import patch

from mealietidy.analyze import CSV_FIELDS, analyze_plan
from mealietidy.cli import main
from mealietidy.config import Settings
from mealietidy.export import export_catalog
from mealietidy.provider import PROVIDERS
from mealietidy.review import load_review
from tests.test_phase1 import FakeMealie


def suggestion(food_id: str, action: str, value: str) -> dict[str, object]:
    return {
        "food_id": food_id,
        "action": action,
        "value": value,
        "language": "it",
        "rationale": "provider proposal",
        "uncertainty": "medium",
        "related_candidate_ids": [],
    }


class StubProvider:
    metadata: Mapping[str, str | None] = {}

    def __init__(self, name: str, values: list[dict[str, object]]) -> None:
        self.name: str = name
        self.values: list[dict[str, object]] = values
        self.seen: list[dict[str, object]] = []

    def __call__(self, _root: Path, _args: Mapping[str, str]) -> Self:
        return self

    def suggest(
        self, foods: list[dict[str, object]], _context: dict[str, object]
    ) -> tuple[list[dict[str, object]], list[list[str]]]:
        self.seen = foods
        return self.values, []


def rows_at(root: Path) -> list[dict[str, str]]:
    with (root / "review.csv").open(newline="", encoding="utf-8") as stream:
        return cast(list[dict[str, str]], list(csv.DictReader(stream)))


def save_rows(root: Path, rows: list[dict[str, str]]) -> None:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _ = (root / "review.csv").write_text(output.getvalue())


class RefineTests(unittest.TestCase):
    def test_refine_changes_only_untouched_pending_rows(self) -> None:
        fake = FakeMealie()
        first = StubProvider("first", [suggestion("b", "set_plural", "Pomodori")])
        second = StubProvider(
            "second",
            [
                suggestion("a", "rename", "Ignored approval"),
                suggestion("a", "set_plural", "Ignored rejection"),
                suggestion("b", "rename", "Ignored manual edit"),
                suggestion("b", "set_plural", "Pomodori rossi"),
            ],
        )
        third = StubProvider(
            "third", [suggestion("b", "set_plural", "Pomodori maturi")]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                _ = export_catalog(fake.base_url, "test-token", root, 10)
            with patch.dict(
                PROVIDERS,
                {
                    "first": first,
                    "second": second,
                    "third": third,
                },
            ):
                _ = analyze_plan(root, "first")
                rows = rows_at(root)
                for row in rows:
                    if (row["food_id"], row["action"]) == ("a", "rename"):
                        row["proposed_value"] = "Pomodoro"
                        row["decision"] = "approve"
                    elif (row["food_id"], row["action"]) == ("a", "set_plural"):
                        row["decision"] = "reject"
                    elif (row["food_id"], row["action"]) == ("b", "rename"):
                        row["proposed_value"] = "Pomodoro manuale"
                        row["reviewer_note"] = "Keep this"
                save_rows(root, rows)
                before = rows_at(root)
                self.assertEqual(analyze_plan(root, "second", refine=True)[0], 1)
            after = rows_at(root)
            for old, new in zip(before, after, strict=True):
                if (old["food_id"], old["action"]) != ("b", "set_plural"):
                    self.assertEqual(new, old)
                else:
                    self.assertEqual(new["proposed_value"], "Pomodori rossi")
            food: dict[str, object] = next(
                item for item in second.seen if item["id"] == "b"
            )
            pending: list[dict[str, str]] = cast(
                list[dict[str, str]], food["pending_suggestions"]
            )
            self.assertEqual(pending[0]["value"], "Pomodori")
            self.assertNotIn("rename", cast(list[str], food["allowed_actions"]))
            self.assertEqual(load_review(root).summary["by_decision"]["approve"], 1)
            plan: dict[str, object] = cast(
                dict[str, object], json.loads((root / "plan.json").read_text())
            )
            self.assertEqual(plan["provider"], "second")
            with patch.dict(PROVIDERS, {"third": third}):
                self.assertEqual(analyze_plan(root, "third", confirmed=True)[0], 1)
            self.assertNotIn("pending_suggestions", third.seen[0])
            self.assertEqual(rows_at(root)[0], before[0])
            self.assertEqual(load_review(root).summary["by_decision"]["approve"], 1)

    def test_analyze_existing_plan_requires_explicit_confirmation(self) -> None:
        fake = FakeMealie()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plan"
            with patch("mealietidy.export.MealieClient", return_value=fake):
                _ = export_catalog(fake.base_url, "test-token", root, 10)
            _ = analyze_plan(root)
            original = (root / "review.csv").read_bytes()
            settings = Settings(None, None, root, 20, "none", {}, False)
            with (
                patch("mealietidy.cli.resolve_settings", return_value=settings),
                patch("builtins.input", return_value="no"),
                patch("mealietidy.cli.analyze_plan") as analyze,
            ):
                self.assertEqual(main(["analyze"]), 1)
                analyze.assert_not_called()
            self.assertEqual((root / "review.csv").read_bytes(), original)
            with (
                patch("mealietidy.cli.resolve_settings", return_value=settings),
                patch("builtins.input", return_value="y") as prompt,
            ):
                self.assertEqual(main(["analyze"]), 0)
                self.assertIn("[y/N]", prompt.call_args.args[0])
            refined_settings = Settings(None, None, root, 20, "none", {}, False, True)
            with (
                patch("mealietidy.cli.resolve_settings", return_value=refined_settings),
                patch(
                    "builtins.input", side_effect=AssertionError("unexpected prompt")
                ),
            ):
                self.assertEqual(main(["analyze", "--refine"]), 0)


if __name__ == "__main__":
    _ = unittest.main()
