"""Apply validation, write safety, and CLI help checks."""

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mealietidy.analyze import CSV_FIELDS, analyze_plan
from mealietidy.apply import _validate_resolution_overlaps, apply_plan  # pyright: ignore[reportPrivateUsage]
from mealietidy.cli import main
from mealietidy.config import Settings
from mealietidy.export import (
    MealieClient,
    MealieWriteError,
    export_catalog,
    substitution_reference_counts,
)
from mealietidy.review import load_review
from test_phase1 import FakeMealie


class WritableMealie(FakeMealie):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.recipe_counts: dict[str, int] = {}
        self.shopping_counts: dict[str, int] = {}
        self.denied: bool = False
        self.timeout_after_write: bool = False
        document: dict[str, Any] = json.loads(self.openapi)
        document["components"] = {
            "schemas": {
                "CreateIngredientFood": {
                    "properties": {
                        key: {}
                        for key in (
                            "name",
                            "pluralName",
                            "aliases",
                            "substitutions",
                            "extras",
                            "description",
                            "labelId",
                            "householdsWithIngredientFood",
                        )
                    }
                }
            }
        }
        self.openapi = json.dumps(document).encode()

    def put_json(self, path: str, value: dict[str, Any]) -> None:
        if self.denied:
            raise MealieWriteError(path, 403, False)
        self.puts.append(value)
        food: dict[str, Any] = next(
            item for item in self.foods if path.endswith("/" + item["id"])
        )
        food.update({key: item for key, item in value.items() if key in food})
        if self.timeout_after_write:
            raise MealieWriteError(path, None, True)

    def get_json(self, path: str) -> dict[str, Any]:
        parsed = urlsplit(path)
        if parsed.path == "/api/recipes":
            food_id: str = parse_qs(parsed.query).get("foods", [""])[0]
            return {"total": self.recipe_counts.get(food_id, 0)}
        if parsed.path == "/api/households/shopping/items":
            query: str = parse_qs(parsed.query).get("queryFilter", [""])[0]
            food_id: str = query.removeprefix("foodId==")
            return {"total": self.shopping_counts.get(food_id, 0)}
        return super().get_json(path)

    def get_json_or_none(self, path: str) -> dict[str, Any] | None:
        try:
            return self.get_json(path)
        except StopIteration:
            return None

    def delete(self, path: str) -> None:
        food_id: str = path.rsplit("/", maxsplit=1)[-1]
        self.deletes.append(food_id)
        self.foods = [food for food in self.foods if food["id"] != food_id]


def edit_review(root: Path, edits: dict[tuple[str, str], str]) -> None:
    path: Path = root / "review.csv"
    with path.open(newline="", encoding="utf-8") as source:
        rows: list[dict[str, str]] = list(csv.DictReader(source))
    for row in rows:
        for (food_id, action), value in edits.items():
            if row["food_id"] == food_id and row["action"] == action:
                row["proposed_value"] = value
                row["decision"] = "approve"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root: Path = Path(self.temp.name) / "mealietidy-plan"
        self.fake = WritableMealie()
        with patch("mealietidy.export.MealieClient", return_value=self.fake):
            export_catalog(self.fake.base_url, "token", self.root, 10)
        analyze_plan(self.root)
        self.schema: bytes = self.fake.openapi
        self.discovery: dict[str, Any] = {
            "mealie_version": "v3.28.0",
            "openapi_hash": json.loads((self.root / "manifest.json").read_text())[
                "openapi_hash"
            ],
            "write_compatibility": "verified",
            "food_detail": {"path": "/api/foods/{item_id}"},
        }

    def run_apply(self, dry_run: bool = False) -> tuple[int, int]:
        with (
            patch("mealietidy.apply.MealieClient", return_value=self.fake),
            patch(
                "mealietidy.apply.discover", return_value=(self.discovery, self.schema)
            ),
        ):
            return apply_plan(self.fake.base_url, "token", self.root, 10, dry_run)

    def test_dry_run_grouped_write_and_resume(self) -> None:
        edit_review(
            self.root,
            {
                ("a", "rename"): "Pomodoro",
                ("a", "set_plural"): "Pomodori",
                ("a", "add_alias"): "pomodoro rosso",
            },
        )
        self.assertEqual(self.run_apply(True), (0, 0))
        self.assertEqual(self.fake.puts, [])
        report = json.loads((self.root / "reports/preflight.json").read_text())
        self.assertEqual(report["approved_changes"], 3)
        self.assertEqual(report["pending_foods"], 1)
        self.assertEqual(self.run_apply(), (1, 0))
        self.assertEqual(len(self.fake.puts), 1)
        self.assertEqual(self.fake.foods[0]["name"], "Pomodoro")
        self.assertEqual(self.fake.foods[0]["extras"], {})
        self.assertEqual(self.run_apply(), (0, 1))
        self.assertEqual(len(self.fake.puts), 1)
        journal = (self.root / "journal.jsonl").read_text()
        self.assertIn('"status": "verified"', journal)

    def test_later_approval_on_same_food_uses_verified_state(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.assertEqual(self.run_apply(), (1, 0))
        edit_review(self.root, {("a", "set_plural"): "Pomodori"})
        self.assertEqual(self.run_apply(), (1, 0))
        self.assertEqual(len(self.fake.puts), 2)
        self.assertEqual(self.fake.foods[0]["name"], "Pomodoro")
        self.assertEqual(self.fake.foods[0]["pluralName"], "Pomodori")
        self.assertEqual(self.run_apply(), (0, 1))

    def test_later_approval_on_other_food_skips_verified_food(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.run_apply()
        edit_review(self.root, {("b", "rename"): "Cipollino"})
        self.assertEqual(self.run_apply(), (1, 1))
        self.assertEqual(len(self.fake.puts), 2)

    def test_refining_pending_rows_preserves_applied_operations(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.run_apply()
        analyze_plan(self.root, refine=True)
        self.assertEqual(self.run_apply(), (0, 1))

    def test_legacy_journal_is_upgraded_before_refinement(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.run_apply()
        journal_path: Path = self.root / "journal.jsonl"
        entries: list[dict[str, Any]] = [
            json.loads(line) for line in journal_path.read_text().splitlines()
        ]
        for entry in entries:
            entry.pop("operations")
            entry.pop("manifest_hash")
        journal_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        analyze_plan(self.root, refine=True)
        edit_review(self.root, {("a", "set_plural"): "Pomodori"})
        self.assertEqual(self.run_apply(), (1, 0))

    def test_uncertain_legacy_journal_blocks_review_changes(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        reviewed = load_review(self.root)
        entry: dict[str, object] = {
            "plan_hash": reviewed.plan_hash,
            "review_hash": reviewed.review_hash,
            "food_id": "a",
            "operation_ids": [reviewed.groups["a"][0]["operation_id"]],
            "status": "uncertain",
        }
        (self.root / "journal.jsonl").write_text(json.dumps(entry) + "\n")
        with self.assertRaisesRegex(ValueError, "Resolve the legacy journal"):
            analyze_plan(self.root, refine=True)

    def test_applied_operation_cannot_be_changed(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.run_apply()
        path: Path = self.root / "review.csv"
        for old, new in (("Pomodoro", "Pomodorino"), ("approve", "reject")):
            original: str = path.read_text()
            path.write_text(original.replace(old, new, 1))
            with self.assertRaisesRegex(ValueError, "Applied operation"):
                self.run_apply()
            path.write_text(original)
        self.assertEqual(len(self.fake.puts), 1)

    def test_conflicted_food_does_not_block_independent_food(self) -> None:
        edit_review(
            self.root, {("a", "rename"): "Pomodoro", ("b", "rename"): "Cipollino"}
        )
        self.fake.foods[0]["extras"] = {"other": "edit"}
        with self.assertRaisesRegex(ValueError, "Applied 1 independent food"):
            self.run_apply()
        self.assertEqual(len(self.fake.puts), 1)
        self.assertEqual(self.fake.foods[1]["name"], "Cipollino")
        report = json.loads((self.root / "reports/preflight.json").read_text())
        self.assertEqual(report["pending_foods"], 1)
        self.assertEqual(len(report["blocked"]), 1)

    def test_partial_apply_exits_nonzero(self) -> None:
        edit_review(
            self.root, {("a", "rename"): "Pomodoro", ("b", "rename"): "Cipollino"}
        )
        self.fake.foods[0]["extras"] = {"other": "edit"}
        settings = Settings(
            base_url=self.fake.base_url,
            token="token",
            plan=self.root,
            timeout=10,
            provider="none",
            provider_args={},
            dry_run=False,
        )
        with (
            patch("mealietidy.cli.resolve_settings", return_value=settings),
            patch("mealietidy.apply.MealieClient", return_value=self.fake),
            patch(
                "mealietidy.apply.discover", return_value=(self.discovery, self.schema)
            ),
            patch("sys.stderr", new_callable=io.StringIO) as errors,
        ):
            self.assertEqual(main(["apply"]), 1)
        self.assertIn("Applied 1 independent food", errors.getvalue())
        self.assertEqual(len(self.fake.puts), 1)

    def test_rejects_immutable_edits_before_network(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        path = self.root / "review.csv"
        path.write_text(path.read_text().replace("Tomato", "Changed", 1))
        with patch("mealietidy.apply.MealieClient") as client:
            with self.assertRaisesRegex(ValueError, "Immutable review field"):
                apply_plan(self.fake.base_url, "token", self.root, 10, True)
            client.assert_not_called()

    def test_approved_merge_is_blocked_before_network(self) -> None:
        edit_review(self.root, {("b", "merge_candidate"): "merge:a"})
        with (
            patch(
                "mealietidy.apply.discover",
                return_value=(self.discovery, self.schema),
            ),
            patch("mealietidy.apply.MealieClient", return_value=self.fake),
            self.assertRaisesRegex(ValueError, "Food merge is not verified"),
        ):
            apply_plan(self.fake.base_url, "token", self.root, 10, False)
        self.assertEqual(self.fake.puts, [])

    def test_merge_blocks_incoming_substitution_with_clear_message(self) -> None:
        self._enable_verified_delete()
        self.discovery.update(
            {
                "dedupe_compatibility": "verified",
                "food_merge": {"method": "PUT", "path": "/api/foods/merge"},
            }
        )
        self.fake.foods[0]["substitutions"] = [
            {"substituteFoodId": "b", "note": None}
        ]
        self.root = Path(self.temp.name) / "linked-plan"
        with patch("mealietidy.export.MealieClient", return_value=self.fake):
            export_catalog(self.fake.base_url, "token", self.root, 10)
        analyze_plan(self.root)
        edit_review(self.root, {("b", "merge_candidate"): "merge:a"})
        with self.assertRaisesRegex(
            ValueError,
            r"source food 'tomato' \(b\) has 1 catalog substitution link\(s\).*"
            r"Resolve them in Mealie, then export and analyze a new plan",
        ):
            self.run_apply(dry_run=True)
        self.assertEqual(self.fake.puts, [])

    def test_substitution_reference_scan_accepts_note_only_links(self) -> None:
        self.fake.foods[0]["substitutions"] = [
            {"substituteFoodId": None, "note": "Use water"}
        ]
        self.fake.foods[1]["substitutions"] = [
            {"substituteFoodId": "a", "note": None}
        ]
        counts: dict[str, int] = substitution_reference_counts(
            cast(MealieClient, self.fake),
            {"path": "/api/foods", "pagination": ["page", "perPage"]},
        )
        self.assertEqual(counts, {"a": 2, "b": 1})

    def test_merge_rechecks_substitutions_before_writing(self) -> None:
        self._enable_verified_delete()
        self.discovery.update(
            {
                "dedupe_compatibility": "verified",
                "food_merge": {"method": "PUT", "path": "/api/foods/merge"},
            }
        )
        edit_review(self.root, {("b", "merge_candidate"): "merge:a"})
        with (
            patch(
                "mealietidy.apply.substitution_reference_counts",
                side_effect=[{}, {"b": 1}],
            ),
            self.assertRaisesRegex(
                ValueError,
                r"1 catalog substitution link\(s\) now involve source food 'tomato' \(b\)",
            ),
        ):
            self.run_apply()
        self.assertEqual(self.fake.puts, [])

    def test_applied_merge_allows_new_operations_on_retained_food(self) -> None:
        edit_review(
            self.root,
            {("b", "merge_candidate"): "merge:a", ("a", "set_plural"): "Tomatoes"},
        )
        reviewed = load_review(self.root)
        merge_id: str = reviewed.resolutions[0]["operation_id"]
        with self.assertRaisesRegex(ValueError, "overlaps another approved"):
            _validate_resolution_overlaps(reviewed, set())

        _validate_resolution_overlaps(reviewed, {merge_id})

    def _enable_verified_delete(self) -> None:
        self.discovery.update(
            {
                "delete_compatibility": "verified",
                "food_delete": {
                    "method": "DELETE",
                    "path": "/api/foods/{item_id}",
                },
                "food_list": {
                    "method": "GET",
                    "path": "/api/foods",
                    "pagination": ["page", "perPage"],
                },
                "recipe_usage": {
                    "method": "GET",
                    "path": "/api/recipes",
                    "food_filter": "foods",
                },
                "shopping_usage": {
                    "method": "GET",
                    "path": "/api/households/shopping/items",
                    "food_filter": "queryFilter",
                },
            }
        )

    def test_approved_delete_of_unreferenced_food_is_journaled_and_idempotent(
        self,
    ) -> None:
        self._enable_verified_delete()
        edit_review(self.root, {("b", "merge_candidate"): "delete:b"})
        self.assertEqual(self.run_apply(dry_run=True), (0, 0))
        self.assertEqual(self.fake.deletes, [])
        self.assertEqual(self.run_apply(), (1, 0))
        self.assertEqual(self.fake.deletes, ["b"])
        self.assertEqual([food["id"] for food in self.fake.foods], ["a"])
        entry = json.loads((self.root / "journal.jsonl").read_text().splitlines()[-1])
        self.assertEqual(entry["status"], "verified")
        self.assertIsNone(entry["after_hash"])
        self.assertEqual(self.run_apply(), (0, 1))
        self.assertEqual(self.fake.deletes, ["b"])

    def test_delete_is_blocked_when_recipe_shopping_or_substitution_references_exist(
        self,
    ) -> None:
        self._enable_verified_delete()
        self.fake.recipe_counts["b"] = 1
        self.fake.shopping_counts["b"] = 2
        self.fake.foods.append(
            {
                "id": "c",
                "name": "Unrelated",
                "pluralName": None,
                "aliases": [],
                "substitutions": [{"substituteFoodId": "b", "note": "related"}],
                "extras": {},
            }
        )
        edit_review(self.root, {("b", "merge_candidate"): "delete:b"})
        with self.assertRaisesRegex(
            ValueError,
            "recipes=1, shopping_items=2, substitution_links=1",
        ):
            self.run_apply(dry_run=True)
        self.assertEqual(self.fake.deletes, [])

    def test_concurrent_change_blocks_write(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.fake.foods[0]["extras"] = {"other": "edit"}
        with self.assertRaisesRegex(ValueError, "Preflight blocked"):
            self.run_apply()
        self.assertEqual(self.fake.puts, [])

    def test_permission_denial_is_journaled(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.fake.denied = True
        with self.assertRaisesRegex(RuntimeError, "denied write permission for food a"):
            self.run_apply()
        self.assertIn('"status": "failed"', (self.root / "journal.jsonl").read_text())

    def test_timeout_after_write_is_reconciled(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        self.fake.timeout_after_write = True
        self.assertEqual(self.run_apply(), (1, 0))
        self.assertEqual(self.run_apply(), (0, 1))
        self.assertEqual(len(self.fake.puts), 1)

    def test_help_describes_each_flag(self) -> None:
        for command, flags in (
            ("export", ("--base-url", "--token", "--plan", "--timeout", "--version")),
            (
                "analyze",
                (
                    "--plan",
                    "--provider",
                    "--refine",
                    "--provider-arg-NAME",
                    "--version",
                ),
            ),
            ("edit", ("--plan", "--version")),
            (
                "apply",
                (
                    "--base-url",
                    "--token",
                    "--plan",
                    "--timeout",
                    "--dry-run",
                    "--version",
                ),
            ),
            ("config", ("--version",)),
        ):
            with (
                self.subTest(command=command),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                with self.assertRaises(SystemExit) as raised:
                    main([command, "--help"])
                self.assertEqual(raised.exception.code, 0)
                for flag in flags:
                    self.assertIn(flag, output.getvalue())
                self.assertIn("Show the installed tool version", output.getvalue())
                if command in {"export", "apply"}:
                    self.assertIn("Mealie API token", output.getvalue())
                    self.assertIn("HTTP timeout in seconds", output.getvalue())
                if command == "apply":
                    self.assertIn(
                        "Preview and check without writing", output.getvalue()
                    )
                    self.assertNotIn("--yes", output.getvalue())

    def test_apply_needs_no_confirmation_flag_or_prompt(self) -> None:
        edit_review(self.root, {("a", "rename"): "Pomodoro"})
        with patch("builtins.input", side_effect=AssertionError("unexpected prompt")):
            self.assertEqual(self.run_apply(), (1, 0))
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["apply", "--yes"])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_dry_run_omits_final_write_summary(self) -> None:
        for dry_run in (True, False):
            settings = Settings(
                base_url=self.fake.base_url,
                token="token",
                plan=self.root,
                timeout=10,
                provider="none",
                provider_args={},
                dry_run=dry_run,
            )
            with (
                self.subTest(dry_run=dry_run),
                patch("mealietidy.cli.resolve_settings", return_value=settings),
                patch("mealietidy.cli.apply_plan", return_value=(1, 0)) as apply,
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                self.assertEqual(main(["apply"]), 0)
                apply.assert_called_once_with(
                    self.fake.base_url, "token", self.root, 10, dry_run
                )
                self.assertEqual(
                    output.getvalue(),
                    ""
                    if dry_run
                    else f"Applied plan in {self.root}: 1 food writes, 0 already verified\n",
                )


if __name__ == "__main__":
    unittest.main()
