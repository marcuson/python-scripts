"""Exercise apply against the disposable, seeded Mealie v3.28.0 instance."""

import csv
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from mealietidy.analyze import CSV_FIELDS, analyze_plan
from mealietidy.apply import apply_plan
from mealietidy.export import export_catalog
from verify_mealie_v3_28 import BASE_URL, request


def main() -> None:
    credentials: dict[str, str] = {
        "username": os.getenv("MEALIE_TEST_USERNAME", "changeme@example.com"),
        "password": os.getenv("MEALIE_TEST_PASSWORD", "MyPassword"),
    }
    token: str = request("POST", "/api/auth/token", credentials, form=True)[
        "access_token"
    ]
    suffix: str = uuid4().hex[:10]
    food = request(
        "POST",
        "/api/foods",
        {
            "name": f"mealietidy test {suffix}",
            "description": "Preserve this description",
            "extras": {"probe": "preserve"},
            "aliases": [{"name": f"test alias {suffix}"}],
        },
        token=token,
    )
    food_id: str = food["id"]
    with tempfile.TemporaryDirectory() as directory:
        root: Path = Path(directory) / "mealietidy-plan"
        export_catalog(BASE_URL, token, root, 20)
        analyze_plan(root)
        review: Path = root / "review.csv"
        with review.open(newline="", encoding="utf-8") as source:
            rows: list[dict[str, str]] = list(csv.DictReader(source))
        for row in rows:
            if row["food_id"] != food_id:
                continue
            values: dict[str, str] = {
                "rename": f"prova mealietidy {suffix}",
                "add_alias": f"alternate test alias {suffix}",
            }
            if row["action"] in values:
                row["proposed_value"] = values[row["action"]]
                row["decision"] = "approve"
        with review.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        assert apply_plan(BASE_URL, token, root, 20, True) == (0, 0)
        before = request("GET", f"/api/foods/{food_id}", token=token)
        assert before["name"] == f"mealietidy test {suffix}"
        assert apply_plan(BASE_URL, token, root, 20, False) == (1, 0)
        after = request("GET", f"/api/foods/{food_id}", token=token)
        assert after["name"] == f"prova mealietidy {suffix}"
        assert {item["name"] for item in after["aliases"]} == {
            f"test alias {suffix}",
            f"alternate test alias {suffix}",
        }
        assert after["description"] == "Preserve this description"
        assert after["extras"] == {"probe": "preserve"}
        for row in rows:
            if row["food_id"] == food_id and row["action"] == "set_plural":
                row["proposed_value"] = f"prove mealietidy {suffix}"
                row["decision"] = "approve"
        with review.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        assert apply_plan(BASE_URL, token, root, 20, False) == (1, 0)
        after = request("GET", f"/api/foods/{food_id}", token=token)
        assert after["name"] == f"prova mealietidy {suffix}"
        assert after["pluralName"] == f"prove mealietidy {suffix}"
        assert {item["name"] for item in after["aliases"]} == {
            f"test alias {suffix}",
            f"alternate test alias {suffix}",
        }
        assert after["description"] == "Preserve this description"
        assert after["extras"] == {"probe": "preserve"}
        assert apply_plan(BASE_URL, token, root, 20, False) == (0, 1)
    print(
        "Mealie v3.28.0 apply preview, incremental updates, preservation, and resume: PASS"
    )


if __name__ == "__main__":
    main()
