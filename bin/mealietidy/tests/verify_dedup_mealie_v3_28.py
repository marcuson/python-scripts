"""Verify food usage, merge, and safe deletion on disposable Mealie v3.28.0."""

import csv
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

from mealietidy.analyze import CSV_FIELDS, analyze_plan
from mealietidy.apply import apply_plan
from mealietidy.editor import make_server
from mealietidy.export import export_catalog
from verify_mealie_v3_28 import BASE_URL, request


def _usage_count(token: str, food_id: str, shopping: bool = False) -> int:
    if shopping:
        path: str = "/api/households/shopping/items?" + urlencode(
            {"queryFilter": f"foodId=={food_id}", "perPage": 1}
        )
    else:
        path = "/api/recipes?" + urlencode({"foods": food_id, "perPage": 1})
    result: object = request("GET", path, token=token)
    if not isinstance(result, dict):
        raise TypeError(f"Invalid reference count response: {path}")
    total: object = cast(dict[str, object], result).get("total")
    if not isinstance(total, int):
        raise TypeError(f"Invalid reference count response: {path}")
    return total


def _save_candidate_resolution(
    root: Path, participants: set[str], resolution: str
) -> dict[str, str]:
    review_path: Path = root / "review.csv"
    with review_path.open(newline="", encoding="utf-8") as source_file:
        rows: list[dict[str, str]] = list(csv.DictReader(source_file))
    match: dict[str, str] = next(
        row
        for row in rows
        if row["action"] == "merge_candidate"
        and {row["food_id"], row["duplicate_target_id"]} == participants
    )
    match["proposed_value"] = resolution
    match["decision"] = "approve"
    with review_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return match


def main() -> None:
    credentials: dict[str, str] = {
        "username": os.getenv("MEALIE_TEST_USERNAME", "changeme@example.com"),
        "password": os.getenv("MEALIE_TEST_PASSWORD", "MyPassword"),
    }
    token: str = request("POST", "/api/auth/token", credentials, form=True)[
        "access_token"
    ]
    suffix: str = uuid4().hex[:10]
    source_name: str = f"Phase4 duplicate {suffix}"
    retained_name: str = f"phase4 duplicate {suffix}"
    source: dict[str, Any] = request(
        "POST",
        "/api/foods",
        {
            "name": source_name,
            "aliases": [{"name": f"phase4 source alias {suffix}"}],
        },
        token=token,
    )
    retained: dict[str, Any] = request(
        "POST", "/api/foods", {"name": retained_name}, token=token
    )
    source_id: str = source["id"]
    retained_id: str = retained["id"]

    recipe_slug: str = request(
        "POST", "/api/recipes", {"name": f"phase4 recipe {suffix}"}, token=token
    )
    recipe: dict[str, Any] = request("GET", f"/api/recipes/{recipe_slug}", token=token)
    recipe_payload: dict[str, object] = {
        key: recipe[key]
        for key in ("id", "userId", "householdId", "groupId", "name", "slug")
    }
    recipe_payload["recipeIngredient"] = [
        {"quantity": 1, "food": {"id": source_id, "name": source_name}}
    ]
    request("PUT", f"/api/recipes/{recipe_slug}", recipe_payload, token=token)

    shopping_list: dict[str, Any] = request(
        "POST",
        "/api/households/shopping/lists",
        {"name": f"phase4 list {suffix}"},
        token=token,
    )
    shopping_response: dict[str, Any] = request(
        "POST",
        "/api/households/shopping/items",
        {
            "shoppingListId": shopping_list["id"],
            "foodId": source_id,
            "display": source_name,
        },
        token=token,
    )
    shopping_item_id: str = shopping_response["createdItems"][0]["id"]

    with tempfile.TemporaryDirectory() as directory:
        root: Path = Path(directory) / "mealietidy-plan"
        export_catalog(BASE_URL, token, root, 30)
        analyze_plan(root)
        review_path: Path = root / "review.csv"
        with review_path.open(newline="", encoding="utf-8") as source_file:
            rows: list[dict[str, str]] = list(csv.DictReader(source_file))
        match = next(
            row
            for row in rows
            if row["action"] == "merge_candidate"
            and {row["food_id"], row["duplicate_target_id"]} == {source_id, retained_id}
        )
        match["proposed_value"] = f"merge:{retained_id}"
        match["decision"] = "approve"
        with review_path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        server, url = make_server(root, BASE_URL, token, 20)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            query: str = urlencode({"food_ids": f"{source_id},{retained_id}"})
            try:
                with urlopen(url + "usage?" + query, timeout=5) as response:
                    browser_counts: dict[str, dict[str, int]] = json.load(response)
            except HTTPError as error:
                raise RuntimeError(
                    f"Editor usage lookup returned HTTP {error.code}: "
                    f"{error.read().decode('utf-8')}"
                ) from error
            assert browser_counts[source_id] == {
                "recipes": 1,
                "shopping_items": 1,
                "substitution_links": 0,
            }
            assert browser_counts[retained_id] == {
                "recipes": 0,
                "shopping_items": 0,
                "substitution_links": 0,
            }
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

        assert _usage_count(token, source_id) == 1
        assert _usage_count(token, retained_id) == 0
        assert _usage_count(token, source_id, shopping=True) == 1
        assert _usage_count(token, retained_id, shopping=True) == 0
        assert apply_plan(BASE_URL, token, root, 30, True) == (0, 0)
        assert request("GET", f"/api/foods/{source_id}", token=token)["id"] == source_id
        assert apply_plan(BASE_URL, token, root, 30, False) == (1, 0)

        merged = request("GET", f"/api/foods/{retained_id}", token=token)
        assert merged["name"] == retained_name
        assert f"phase4 source alias {suffix}" in {
            alias["name"] for alias in merged["aliases"]
        }
        assert _usage_count(token, source_id) == 0
        assert _usage_count(token, retained_id) == 1
        assert _usage_count(token, source_id, shopping=True) == 0
        assert _usage_count(token, retained_id, shopping=True) == 1
        recipe_after = request("GET", f"/api/recipes/{recipe_slug}", token=token)
        assert recipe_after["recipeIngredient"][0]["food"]["id"] == retained_id
        shopping_after = request(
            "GET",
            f"/api/households/shopping/items/{shopping_item_id}",
            token=token,
        )
        assert shopping_after["foodId"] == retained_id
        assert apply_plan(BASE_URL, token, root, 30, False) == (0, 1)

    referenced_name: str = f"Phase4 referenced delete {suffix}"
    referenced_source: dict[str, Any] = request(
        "POST", "/api/foods", {"name": referenced_name}, token=token
    )
    referenced_retained: dict[str, Any] = request(
        "POST",
        "/api/foods",
        {"name": referenced_name.lower()},
        token=token,
    )
    unreferenced_name: str = f"Phase4 unused delete {suffix}"
    unreferenced_source: dict[str, Any] = request(
        "POST", "/api/foods", {"name": unreferenced_name}, token=token
    )
    unreferenced_retained: dict[str, Any] = request(
        "POST", "/api/foods", {"name": unreferenced_name.lower()}, token=token
    )
    referenced_recipe_slug: str = request(
        "POST",
        "/api/recipes",
        {"name": f"phase4 delete reference {suffix}"},
        token=token,
    )
    referenced_recipe: dict[str, Any] = request(
        "GET", f"/api/recipes/{referenced_recipe_slug}", token=token
    )
    referenced_recipe_payload: dict[str, object] = {
        key: referenced_recipe[key]
        for key in ("id", "userId", "householdId", "groupId", "name", "slug")
    }
    referenced_recipe_payload["recipeIngredient"] = [
        {
            "quantity": 1,
            "food": {
                "id": referenced_source["id"],
                "name": referenced_source["name"],
            },
        }
    ]
    request(
        "PUT",
        f"/api/recipes/{referenced_recipe_slug}",
        referenced_recipe_payload,
        token=token,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "mealietidy-plan"
        export_catalog(BASE_URL, token, root, 30)
        analyze_plan(root)
        _save_candidate_resolution(
            root,
            {referenced_source["id"], referenced_retained["id"]},
            f"delete:{referenced_source['id']}",
        )
        unused_row: dict[str, str] = _save_candidate_resolution(
            root,
            {unreferenced_source["id"], unreferenced_retained["id"]},
            f"delete:{unreferenced_source['id']}",
        )
        try:
            apply_plan(BASE_URL, token, root, 30, True)
        except ValueError as error:
            assert "recipes=1" in str(error)
        else:
            raise AssertionError("Deletion with a recipe reference was not blocked")
        assert (
            request("GET", f"/api/foods/{referenced_source['id']}", token=token)["id"]
            == referenced_source["id"]
        )
        with (root / "review.csv").open(newline="", encoding="utf-8") as source_file:
            rows = list(csv.DictReader(source_file))
        for row in rows:
            if row["operation_id"] == unused_row["operation_id"]:
                continue
            if row["action"] == "merge_candidate" and {
                row["food_id"],
                row["duplicate_target_id"],
            } == {referenced_source["id"], referenced_retained["id"]}:
                row["decision"] = "reject"
        with (root / "review.csv").open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        assert apply_plan(BASE_URL, token, root, 30, True) == (0, 0)
        assert apply_plan(BASE_URL, token, root, 30, False) == (1, 0)
        try:
            request("GET", f"/api/foods/{unreferenced_source['id']}", token=token)
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("Approved unreferenced food was not deleted")
        assert (
            request("GET", f"/api/foods/{unreferenced_retained['id']}", token=token)[
                "id"
            ]
            == unreferenced_retained["id"]
        )
        assert apply_plan(BASE_URL, token, root, 30, False) == (0, 1)
    print(
        "Mealie v3.28.0 reference counts, merge, blocked referenced delete, "
        "safe delete, and resume: PASS"
    )


if __name__ == "__main__":
    main()
