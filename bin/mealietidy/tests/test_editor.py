"""Local browser review and durable progress checks."""

import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mealietidy.analyze import analyze_plan
from mealietidy.editor import _render, _save, _stage, make_server  # pyright: ignore[reportPrivateUsage]
from mealietidy.export import export_catalog, json_bytes, sha256
from mealietidy.journal import append_journal, read_journal
from mealietidy.review import ReviewConflictError, load_review
from test_phase1 import FakeMealie


class EditorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root: Path = Path(self.temp.name) / "mealietidy-plan"
        fake = FakeMealie()
        with patch("mealietidy.export.MealieClient", return_value=fake):
            export_catalog(fake.base_url, "token", self.root, 10)
        analyze_plan(self.root)
        self.token: str = "test-session-token"

    def _form(self, *food_ids: str) -> dict[str, list[str]]:
        review_bytes: bytes = (self.root / "review.csv").read_bytes()
        rows: list[dict[str, str]] = list(
            csv.DictReader(io.StringIO(review_bytes.decode("utf-8")))
        )
        form: dict[str, list[str]] = {
            "token": [self.token],
            "conflict_operation_id": [""],
            "food_id": list(food_ids),
            "review_hash": [sha256(review_bytes)],
            "current_q": [""],
            "q": [""],
            "page": ["1"],
            "action": ["save"],
        }
        for row in rows:
            if row["food_id"] in food_ids:
                op: str = row["operation_id"]
                form[f"value.{op}"] = [row["proposed_value"]]
                form[f"decision.{op}"] = [row["decision"]]
                form[f"note.{op}"] = [row["reviewer_note"]]
        return form

    def test_save_and_resume_without_mealie_writes(self) -> None:
        page: str = _render(self.root, self.token, "", 1)
        self.assertEqual(page.count("name='action' value='save'>Save</button>"), 1)
        self.assertIn("Search foods", page)
        self.assertIn("Name, alias, proposal, or ID", page)
        form = self._form("a", "b")
        rename_a: str = next(key for key in form if key.startswith("value."))
        rename_b: str = next(
            key
            for key in form
            if key.startswith("value.") and key != rename_a and key.endswith("000004")
        )
        form[rename_a] = ["Pomodoro"]
        form[f"decision.{rename_a.removeprefix('value.')}"] = ["approve"]
        form[rename_b] = ["Pomodorino"]
        form[f"decision.{rename_b.removeprefix('value.')}"] = ["approve"]
        query, page_number, message = _save(self.root, form, self.token)
        self.assertEqual(message, "Saved all reviewed edits")
        self.assertEqual((query, page_number), ("", 1))
        reviewed = load_review(self.root)
        self.assertEqual(reviewed.summary["by_decision"]["approve"], 2)
        self.assertIn("Pomodoro", _render(self.root, "new-session-token", "", 1))
        self.assertIn("Pomodorino", _render(self.root, "new-session-token", "", 1))

    def test_food_sections_show_decision_counts_and_unsaved_edits(self) -> None:
        page: str = _render(self.root, self.token, "", 1)
        self.assertIn("<div class='sticky-top'><header>", page)
        self.assertIn("<div class='header-top'><h1>mealietidy review</h1>", page)
        self.assertIn("<div class='header-controls'><div class='search'>", page)
        self.assertNotIn("Edits follow you between pages", page)
        self.assertRegex(page, r"</header></div>\s*<details class='food'")
        self.assertIn("new ResizeObserver(updateStickyHeight)", page)
        self.assertIn("<div class='actions'>", page)
        self.assertIn("id='target-page' name='target_page'", page)
        self.assertIn("id='save-status' class='success' role='status' hidden", page)
        self.assertIn("await fetch(location.href", page)
        self.assertIn("${location.pathname}usage?food_ids=", page)
        self.assertIn(
            'status.innerHTML = page.querySelector("#save-status")?.innerHTML || message',
            page,
        )
        self.assertEqual(page.count("<details class='food'"), 2)
        self.assertEqual(page.count("data-unsaved hidden"), 2)
        self.assertIn("data-count='pending'>Pending 3</span>", page)
        self.assertIn("data-count='pending'>Pending 4</span>", page)
        edited: str = _render(
            self.root,
            self.token,
            "",
            1,
            overrides={"value.op-000001": "Pomodoro", "decision.op-000001": "approve"},
        )
        self.assertIn("data-count='approve'>Approved 1</span>", edited)
        self.assertIn("data-count='pending'>Pending 2</span>", edited)
        self.assertEqual(edited.count("data-unsaved>Unsaved"), 1)
        self.assertEqual(edited.count("data-unsaved hidden"), 1)
        self.assertIn("data-saved='' value='Pomodoro'", edited)
        self.assertIn("<script nonce='test-session-token'>", edited)

    def test_food_without_pending_operations_starts_collapsed(self) -> None:
        form: dict[str, list[str]] = self._form("a")
        for name in form:
            if name.startswith("decision."):
                form[name] = ["reject"]
        _save(self.root, form, self.token)
        page: str = _render(self.root, self.token, "", 1)
        self.assertNotIn("<h2>Tomato</h2>", page)
        self.assertRegex(
            page, r"data-applied-count='0' open>\s*<summary>\s*<h2>tomato</h2>"
        )
        self.assertEqual(page.count("data-unsaved hidden"), 1)
        self.assertNotIn("name='value.op-000001'", page)

    def test_show_completed_includes_rejected_operations(self) -> None:
        form: dict[str, list[str]] = self._form("a")
        for name in form:
            if name.startswith("decision."):
                form[name] = ["reject"]
        _save(self.root, form, self.token)
        page: str = _render(self.root, self.token, "", 1, show_applied=True)
        self.assertIn("<h2>Tomato</h2>", page)
        self.assertIn("name='value.op-000001'", page)

    def test_rejected_operation_is_hidden_while_pending_food_remains(self) -> None:
        form: dict[str, list[str]] = self._form("a")
        form["decision.op-000001"] = ["reject"]
        _save(self.root, form, self.token)
        page: str = _render(self.root, self.token, "", 1)
        self.assertIn("<h2>Tomato</h2>", page)
        self.assertEqual(page.count("<div class='row' hidden>"), 1)
        self.assertIn("name='value.op-000001'", page)
        self.assertIn("name='value.op-000002'", page)

    def test_conflict_details_show_names_and_link_to_filtered_foods(self) -> None:
        page: str = _render(
            self.root,
            self.token,
            "",
            1,
            message="Cross-food collision",
            error=True,
            conflict_ids=("a", "b"),
            conflict_operation_id="op-000001",
        )
        self.assertIn("Conflicting foods: Tomato (a), tomato (b)", page)
        self.assertIn("filter_ids=a%2Cb&amp;show_applied=1", page)
        self.assertIn("target='_blank' rel='noopener noreferrer'", page)
        self.assertIn("class='conflict-usage' data-food-ids='a,b'", page)
        self.assertIn("Loading recipe count…", page)
        self.assertIn("value='delete:a'", page)
        self.assertIn("value='delete:b'", page)
        self.assertIn("value='resolve:merge:b:a'", page)
        self.assertIn("value='resolve:delete:b:b'", page)
        self.assertIn("conflict_operation_id=op-000001", page)
        filtered: str = _render(
            self.root,
            self.token,
            "",
            1,
            show_applied=True,
            filter_ids=("a",),
        )
        self.assertEqual(filtered.count("<details class='food'"), 1)
        self.assertIn("name='filter_ids' value='a'", filtered)
        conflict_page: str = _render(
            self.root,
            self.token,
            "",
            1,
            show_applied=True,
            filter_ids=("a", "b"),
        )
        self.assertEqual(conflict_page.count("data-applied-count='0' open"), 2)
        self.assertIn("Conflict resolution", conflict_page)
        self.assertIn("value='delete:a'", conflict_page)

    def test_conflict_error_includes_involved_food_ids(self) -> None:
        form: dict[str, list[str]] = self._form("a")
        form["value.op-000001"] = ["tomato"]
        form["decision.op-000001"] = ["approve"]
        with self.assertRaises(ReviewConflictError) as raised:
            _save(self.root, form, self.token)
        self.assertEqual(raised.exception.food_ids, ("a", "b"))
        self.assertEqual(raised.exception.operation_id, "op-000001")

    def test_conflict_resolution_can_be_saved_from_rename_operation(self) -> None:
        form: dict[str, list[str]] = self._form("a", "b")
        form["value.op-000001"] = ["resolve:merge:b:a"]
        form["decision.op-000001"] = ["approve"]

        _save(self.root, form, self.token)

        reviewed = load_review(self.root)
        resolution: dict[str, str] = next(
            row for row in reviewed.resolutions if row["operation_id"] == "op-000001"
        )
        self.assertEqual(resolution["action"], "merge_candidate")
        self.assertEqual(resolution["duplicate_target_id"], "b")
        self.assertEqual(resolution["proposed_value"], "merge:a")
        self.assertNotIn("a", reviewed.groups)
        self.assertEqual(
            next(row for row in reviewed.rows if row["operation_id"] == "op-000001"),
            resolution,
        )
        append_journal(self.root, reviewed, "a", [resolution], "attempt", "before")
        read_journal(self.root, reviewed)

    def test_stale_and_invalid_edits_leave_review_untouched(self) -> None:
        form = self._form("a", "b")
        original: bytes = (self.root / "review.csv").read_bytes()
        rename: str = next(key for key in form if key.startswith("value."))
        operation_id: str = rename.removeprefix("value.")
        form[f"decision.{operation_id}"] = ["approve"]
        with self.assertRaisesRegex(ValueError, "empty proposed_value"):
            _save(self.root, form, self.token)
        self.assertEqual((self.root / "review.csv").read_bytes(), original)
        form[rename] = ["Pomodoro"]
        (self.root / "review.csv").write_bytes(original + b"\n")
        changed: bytes = (self.root / "review.csv").read_bytes()
        with self.assertRaisesRegex(ValueError, "changed since this page loaded"):
            _save(self.root, form, self.token)
        self.assertEqual((self.root / "review.csv").read_bytes(), changed)

    def test_invalid_edit_on_one_food_blocks_all_page_edits(self) -> None:
        form = self._form("a", "b")
        original: bytes = (self.root / "review.csv").read_bytes()
        rename_a: str = next(key for key in form if key.startswith("value."))
        form[rename_a] = ["Pomodoro"]
        form[f"decision.{rename_a.removeprefix('value.')}"] = ["approve"]
        form["decision.op-000004"] = ["approve"]
        with self.assertRaisesRegex(ValueError, "empty proposed_value"):
            _save(self.root, form, self.token)
        self.assertEqual((self.root / "review.csv").read_bytes(), original)

    def test_can_save_staged_edits_after_search_with_no_results(self) -> None:
        first = self._form("a")
        rename: str = next(key for key in first if key.startswith("value."))
        first[rename] = ["Pomodoro"]
        first[f"decision.{rename.removeprefix('value.')}"] = ["approve"]
        drafts: dict[str, str] = _stage(self.root, first, self.token)
        empty: dict[str, list[str]] = {
            "token": [self.token],
            "review_hash": first["review_hash"],
            "current_q": ["no-such-food"],
            "q": ["no-such-food"],
            "page": ["1"],
            "action": ["save"],
        }
        self.assertIn(
            "name='action' value='save'>Save</button>",
            _render(self.root, self.token, "no-such-food", 1),
        )
        _save(self.root, empty, self.token, drafts)
        self.assertEqual(load_review(self.root).summary["by_decision"]["approve"], 1)

    def test_search_matches_all_words_and_staged_proposals(self) -> None:
        reordered_words: str = _render(self.root, self.token, "tomatoes tomato", 1)
        self.assertEqual(reordered_words.count("<details class='food'"), 1)
        self.assertIn("<h2>Tomato</h2>", reordered_words)

        proposed_value: str = _render(
            self.root,
            self.token,
            "pomodoro",
            1,
            overrides={"value.op-000001": "Pomodoro"},
        )
        self.assertEqual(proposed_value.count("<details class='food'"), 1)
        self.assertIn("<h2>Tomato</h2>", proposed_value)

    def test_applied_row_is_hidden_and_read_only(self) -> None:
        form = self._form("a")
        form["value.op-000001"] = ["Pomodoro"]
        form["decision.op-000001"] = ["approve"]
        _save(self.root, form, self.token)
        reviewed = load_review(self.root)
        append_journal(
            self.root,
            reviewed,
            "a",
            reviewed.groups["a"],
            "verified",
            "before",
            "after",
        )
        page = _render(self.root, self.token, "", 1)
        self.assertNotIn("name='value.op-000001'", page)
        self.assertIn("name='value.op-000002'", page)
        shown = _render(self.root, self.token, "", 1, show_applied=True)
        self.assertIn("Applied per journal", shown)
        self.assertNotIn("name='value.op-000001'", shown)
        forged = self._form("a")
        with self.assertRaisesRegex(ValueError, "Review form fields do not match"):
            _save(self.root, forged, self.token)
        for prefix in ("value", "decision", "note"):
            del forged[f"{prefix}.op-000001"]
        forged["value.op-000002"] = ["Pomodori"]
        forged["decision.op-000002"] = ["approve"]
        _save(self.root, forged, self.token)
        self.assertEqual(len(load_review(self.root).groups["a"]), 2)

    def test_local_http_page_saves_review(self) -> None:
        with patch("mealietidy.editor.PAGE_SIZE", 1):
            try:
                server, url = make_server(self.root)
            except PermissionError:
                self.skipTest("Loopback sockets are unavailable in this sandbox")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(url, timeout=3) as response:
                    self.assertIn(
                        "script-src 'nonce-",
                        response.headers["Content-Security-Policy"],
                    )
                    self.assertIn(
                        "connect-src 'self'",
                        response.headers["Content-Security-Policy"],
                    )
                    self.assertIn(
                        "name='action' value='save'>Save</button>",
                        response.read().decode(),
                    )
                first = self._form("a")
                first["token"] = [url.split("/")[3]]
                first["action"] = ["goto"]
                first["target_page"] = ["2"]
                rename_a: str = next(key for key in first if key.startswith("value."))
                first[rename_a] = ["Pomodoro"]
                first[f"decision.{rename_a.removeprefix('value.')}"] = ["approve"]
                request = Request(
                    url,
                    data=urlencode(first, doseq=True).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with urlopen(request, timeout=3) as response:
                    navigation_page: str = response.read().decode()
                    self.assertIn("Edits retained in this session", navigation_page)
                    self.assertIn("page 2 of 2", navigation_page)
                self.assertEqual(
                    load_review(self.root).summary["by_decision"].get("approve", 0),
                    0,
                )
                second = self._form("b")
                second["token"] = [url.split("/")[3]]
                second["page"] = ["2"]
                rename_b: str = next(key for key in second if key.startswith("value."))
                second[rename_b] = ["Pomodorino"]
                second[f"decision.{rename_b.removeprefix('value.')}"] = ["approve"]
                request = Request(
                    url,
                    data=urlencode(second, doseq=True).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with urlopen(request, timeout=3) as response:
                    saved_page: str = response.read().decode()
                    self.assertIn("Saved all reviewed edits", saved_page)
                    self.assertIn(
                        f"name='review_hash' value='{sha256((self.root / 'review.csv').read_bytes())}'",
                        saved_page,
                    )
                self.assertEqual(
                    load_review(self.root).summary["by_decision"]["approve"], 2
                )
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()

    def test_local_http_conflict_shows_names_and_filtered_link(self) -> None:
        try:
            server, url = make_server(self.root)
        except PermissionError:
            self.skipTest("Loopback sockets are unavailable in this sandbox")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            form: dict[str, list[str]] = self._form("a")
            form["token"] = [url.split("/")[3]]
            form["value.op-000001"] = ["tomato"]
            form["decision.op-000001"] = ["approve"]
            request = Request(
                url,
                data=urlencode(form, doseq=True).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            page: str = raised.exception.read().decode()
            self.assertIn("Conflicting foods: Tomato (a), tomato (b)", page)
            self.assertIn("filter_ids=a%2Cb&amp;show_applied=1", page)
        finally:
            server.shutdown()
            thread.join(2)
            server.server_close()

    def test_local_recipe_usage_rediscovers_missing_capability_server_side(
        self,
    ) -> None:
        capability_path: Path = self.root / "discovery/capabilities.json"
        capability_path.write_bytes(json_bytes({}))
        manifest_path: Path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"]["discovery/capabilities.json"] = sha256(
            capability_path.read_bytes()
        )
        manifest_bytes: bytes = json_bytes(manifest)
        manifest_path.write_bytes(manifest_bytes)
        plan_path: Path = self.root / "plan.json"
        plan = json.loads(plan_path.read_bytes())
        plan["manifest_hash"] = sha256(manifest_bytes)
        plan["source_files"] = manifest["files"]
        plan_path.write_bytes(json_bytes(plan))

        class FakeCountClient:
            def __init__(self, base_url: str, token: str, timeout: float) -> None:
                self.token: str = token

            def get_json(self, path: str) -> dict[str, Any]:
                if path.startswith("/api/foods?"):
                    return {
                        "total": 2,
                        "total_pages": 1,
                        "items": [
                            {"id": "a", "substitutions": []},
                            {"id": "b", "substitutions": []},
                        ],
                    }
                count: int = 3 if "foods=a" in path or "foodId%3D%3Da" in path else 7
                return {"total": count}

        current_discovery: dict[str, object] = {
            "mealie_version": manifest["mealie_version"],
            "openapi_hash": manifest["openapi_hash"],
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
            "food_list": {
                "method": "GET",
                "path": "/api/foods",
                "pagination": ["page", "perPage"],
            },
        }
        mismatched_discovery: dict[str, object] = {
            **current_discovery,
            "openapi_hash": "different-openapi-hash",
        }
        with (
            patch("mealietidy.editor.MealieClient", FakeCountClient),
            patch(
                "mealietidy.editor.discover",
                side_effect=[
                    (current_discovery, b"{}"),
                    (mismatched_discovery, b"{}"),
                ],
            ) as discover_mock,
        ):
            try:
                server, url = make_server(self.root, mealie_token="private-token")
            except PermissionError:
                self.skipTest("Loopback sockets are unavailable in this sandbox")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urlopen(url + "usage?food_ids=a%2Cb", timeout=3) as response:
                    result = json.load(response)
                    self.assertEqual(
                        response.headers.get_content_type(), "application/json"
                    )
                self.assertEqual(
                    result,
                    {
                        "a": {
                            "recipes": 3,
                            "shopping_items": 3,
                            "substitution_links": 0,
                        },
                        "b": {
                            "recipes": 7,
                            "shopping_items": 7,
                            "substitution_links": 0,
                        },
                    },
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(url + "usage?food_ids=a%2Cb", timeout=3)
                self.assertEqual(raised.exception.code, 503)
                self.assertIn(
                    "differs from this plan",
                    raised.exception.read().decode("utf-8"),
                )
                self.assertEqual(discover_mock.call_count, 2)
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
