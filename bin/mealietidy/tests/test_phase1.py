"""Offline checks for the read-only phase."""

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mealietidy.analyze import analyze_plan, normalize
from mealietidy.export import export_catalog, normalize_base_url


class FakeMealie:
    def __init__(self) -> None:
        self.base_url: str = "http://127.0.0.1:19925"
        self.calls: list[str] = []
        self.change_on_second_pass: bool = False
        self.no_openapi: bool = False
        self.list_reads: int = 0
        self.foods: list[dict[str, Any]] = [
            {
                "id": "a",
                "name": "Tomato",
                "pluralName": None,
                "aliases": [{"name": "tomatoes"}],
                "substitutions": [],
                "extras": {},
            },
            {
                "id": "b",
                "name": "tomato",
                "pluralName": None,
                "aliases": [],
                "substitutions": [],
                "extras": {},
            },
        ]
        self.openapi: bytes = json.dumps(
            {
                "info": {"version": "v3.28.0"},
                "paths": {
                    "/api/foods": {
                        "get": {
                            "parameters": [
                                {"name": "page", "in": "query"},
                                {"name": "perPage", "in": "query"},
                            ],
                            "responses": {
                                "200": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "$ref": "#/components/schemas/IngredientFoodPagination"
                                            }
                                        }
                                    }
                                }
                            },
                            "security": [{"OAuth2PasswordBearer": []}],
                        }
                    },
                    "/api/foods/{item_id}": {
                        "get": {
                            "responses": {
                                "200": {
                                    "content": {
                                        "application/json": {
                                            "schema": {
                                                "$ref": "#/components/schemas/IngredientFood-Output"
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "put": {
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CreateIngredientFood"
                                        }
                                    }
                                }
                            }
                        },
                    },
                },
            }
        ).encode()

    def get_bytes(self, path: str) -> bytes:
        self.calls.append("GET " + path)
        if path == "/docs":
            return b"url: '/openapi.json'"
        if path == "/openapi.json" and not self.no_openapi:
            return self.openapi
        raise RuntimeError("OpenAPI unavailable")

    def get_json(self, path: str) -> dict[str, Any]:
        self.calls.append("GET " + path)
        parsed = urlsplit(path)
        if parsed.path == "/api/foods":
            self.list_reads += 1
            page = int(parse_qs(parsed.query)["page"][0])
            items: list[dict[str, Any]] = (
                [self.foods[page - 1]] if page <= len(self.foods) else []
            )
            if self.change_on_second_pass and self.list_reads > 2:
                items = [{**items[0], "name": "changed"}]
            return {
                "page": page,
                "per_page": 50,
                "total": len(self.foods),
                "total_pages": len(self.foods),
                "items": items,
            }
        return next(
            food for food in self.foods if parsed.path == "/api/foods/" + food["id"]
        )


class Phase1Tests(unittest.TestCase):
    def test_export_then_analyze(self) -> None:
        fake = FakeMealie()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mealietidy.export.MealieClient", return_value=fake),
        ):
            root = Path(directory) / "mealietidy-plan"
            self.assertEqual(
                export_catalog(fake.base_url, "private-test-token", root, 10), 2
            )
            self.assertEqual(fake.list_reads, 4)
            self.assertTrue(all(call.startswith("GET ") for call in fake.calls))
            self.assertEqual(analyze_plan(root), (7, 4))
            plan = json.loads((root / "plan.json").read_text())
            with (root / "review.csv").open(newline="", encoding="utf-8") as review:
                rows = list(csv.DictReader(review))
            self.assertEqual(plan["schema_version"], "1.5.0")
            self.assertEqual(plan["provider"], "none")
            self.assertEqual(plan["provider_info"], {})
            self.assertEqual(len(rows), 7)
            self.assertTrue(all(row["decision"] == "pending" for row in rows))
            self.assertTrue(all(row["proposed_value"] == "" for row in rows))
            self.assertEqual(sum(row["action"] == "merge_candidate" for row in rows), 1)
            self.assertEqual(plan["operations"][0]["risk"], "high")
            self.assertNotIn(
                "private-test-token",
                "".join(path.read_text() for path in root.rglob("*") if path.is_file()),
            )
            with self.assertRaises(FileExistsError):
                analyze_plan(root)
            with self.assertRaises(FileExistsError):
                export_catalog(fake.base_url, "private-test-token", root, 10)

    def test_openapi_failure_stops_before_catalog_reads(self) -> None:
        fake = FakeMealie()
        fake.no_openapi = True
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mealietidy.export.MealieClient", return_value=fake),
        ):
            root = Path(directory) / "mealietidy-plan"
            with self.assertRaises(RuntimeError):
                export_catalog(fake.base_url, "token", root, 10)
            self.assertEqual(fake.calls, ["GET /docs", "GET /openapi.json"])
            self.assertFalse(root.exists())

    def test_changed_catalog_stops_without_artifacts(self) -> None:
        fake = FakeMealie()
        fake.change_on_second_pass = True
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mealietidy.export.MealieClient", return_value=fake),
        ):
            root = Path(directory) / "mealietidy-plan"
            with self.assertRaises(ValueError):
                export_catalog(fake.base_url, "token", root, 10)
            self.assertFalse(root.exists())

    def test_analysis_rejects_modified_export(self) -> None:
        fake = FakeMealie()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mealietidy.export.MealieClient", return_value=fake),
        ):
            root = Path(directory) / "mealietidy-plan"
            export_catalog(fake.base_url, "token", root, 10)
            snapshot = root / "snapshot/foods.json"
            snapshot.write_text(snapshot.read_text().replace("Tomato", "Changed"))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                analyze_plan(root)
            self.assertFalse((root / "plan.json").exists())

    def test_copied_export_can_be_analyzed_without_git(self) -> None:
        fake = FakeMealie()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("mealietidy.export.MealieClient", return_value=fake),
        ):
            original = Path(directory) / "first" / "mealietidy-plan"
            copied = Path(directory) / "second" / "mealietidy-plan"
            export_catalog(fake.base_url, "token", original, 10)
            shutil.copytree(original, copied)
            self.assertEqual(analyze_plan(copied), (7, 4))
            self.assertTrue((copied / "review.csv").is_file())

    def test_normalization_and_url_guards(self) -> None:
        self.assertEqual(normalize("  Caffe\u0300   D'ORZO  "), "caffè d'orzo")
        self.assertEqual(
            normalize_base_url("http://127.0.0.1:19925/"), "http://127.0.0.1:19925"
        )
        for url in (
            "http://example.com",
            "https://user:secret@example.com",
            "https://example.com/?token=x",
            "https://example.com:bad",
        ):
            with self.assertRaises(ValueError):
                normalize_base_url(url)


if __name__ == "__main__":
    unittest.main()
