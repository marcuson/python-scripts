"""One-time write verification for the disposable Mealie v3.28.0 Compose instance."""

import hashlib
import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL: str = "http://127.0.0.1:19925"
OPENAPI_SHA256: str = "90e19aa713ab4ba15352627aca7dc37290f868b213eb65a3e1564f3b7a7ff292"


def request(
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    token: str | None = None,
    form: bool = False,
) -> Any:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload: bytes | None = None
    if data is not None:
        headers["Content-Type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
        payload = urlencode(data).encode() if form else json.dumps(data).encode()
    with urlopen(
        Request(BASE_URL + path, data=payload, headers=headers, method=method),
        timeout=20,
    ) as response:
        body = response.read()
        return (
            json.loads(body)
            if body and response.headers.get_content_type() == "application/json"
            else body
        )


def main() -> None:
    spec = request("GET", "/openapi.json")
    assert spec["info"]["version"] == "v3.28.0"
    with urlopen(BASE_URL + "/openapi.json", timeout=20) as response:
        assert hashlib.sha256(response.read()).hexdigest() == OPENAPI_SHA256
    assert b"/openapi.json" in request("GET", "/docs")
    for path, method in (
        ("/api/foods", "post"),
        ("/api/foods", "get"),
        ("/api/foods/{item_id}", "get"),
        ("/api/foods/{item_id}", "put"),
        ("/api/foods/{item_id}", "delete"),
    ):
        assert method in spec["paths"][path]

    credentials = {
        "username": os.getenv("MEALIE_TEST_USERNAME", "changeme@example.com"),
        "password": os.getenv("MEALIE_TEST_PASSWORD", "MyPassword"),
    }
    token = request("POST", "/api/auth/token", credentials, form=True)["access_token"]
    before_list = request("GET", "/api/foods?page=1&perPage=50", token=token)
    assert before_list["total"] > 0, "Run the built-in Foods seeder before this test"

    substitute = request(
        "POST", "/api/foods", {"name": "phase0 substitute"}, token=token
    )
    created = request(
        "POST",
        "/api/foods",
        {
            "name": "phase0 grain",
            "pluralName": "phase0 grains",
            "description": "Compatibility probe",
            "extras": {"mealietidyProbe": "preserve-me"},
            "aliases": [{"name": "phase0 cereal"}],
            "substitutions": [
                {"substituteFoodId": substitute["id"], "note": "Keep this relation"}
            ],
        },
        token=token,
    )
    food_id = created["id"]
    before = request("GET", f"/api/foods/{food_id}", token=token)
    assert before["id"] == food_id
    assert before["extras"] == {"mealietidyProbe": "preserve-me"}
    assert before["substitutions"][0]["substituteFoodId"] == substitute["id"]
    recipe_slug = request(
        "POST", "/api/recipes", {"name": "phase0 recipe"}, token=token
    )
    recipe = request("GET", f"/api/recipes/{recipe_slug}", token=token)
    recipe_payload = {
        key: recipe[key]
        for key in ("id", "userId", "householdId", "groupId", "name", "slug")
    }
    recipe_payload["recipeIngredient"] = [
        {"quantity": 1, "food": {"id": food_id, "name": before["name"]}}
    ]
    request(
        "PUT",
        f"/api/recipes/{recipe_slug}",
        recipe_payload,
        token=token,
    )
    recipe_before = request("GET", f"/api/recipes/{recipe_slug}", token=token)
    assert recipe_before["recipeIngredient"][0]["food"]["id"] == food_id

    parser_before = request(
        "POST",
        "/api/parser/ingredient",
        {"parser": "brute", "ingredient": "1 phase0 cereal"},
        token=token,
    )
    payload = {
        key: before[key]
        for key in spec["components"]["schemas"]["CreateIngredientFood"]["properties"]
        if key in before
    }
    payload["aliases"] = [{"name": alias["name"]} for alias in before["aliases"]]
    payload["substitutions"] = [
        {key: item[key] for key in ("substituteFoodId", "note") if key in item}
        for item in before["substitutions"]
    ]
    payload.update(name="phase0 grano", pluralName="phase0 grani")
    payload["aliases"].append({"name": "phase0 grain"})
    request("PUT", f"/api/foods/{food_id}", payload, token=token)
    after = request("GET", f"/api/foods/{food_id}", token=token)
    assert after["id"] == food_id
    assert after["name"] == "phase0 grano"
    assert after["pluralName"] == "phase0 grani"
    assert {alias["name"] for alias in after["aliases"]} == {
        "phase0 cereal",
        "phase0 grain",
    }
    for field in (
        "description",
        "extras",
        "labelId",
        "substitutions",
        "householdsWithIngredientFood",
    ):
        assert after[field] == before[field], f"Unexpected change to {field}"
    recipe_after = request("GET", f"/api/recipes/{recipe_slug}", token=token)
    assert recipe_after["recipeIngredient"][0]["food"]["id"] == food_id
    parser_after = request(
        "POST",
        "/api/parser/ingredient",
        {"parser": "brute", "ingredient": "1 phase0 cereal"},
        token=token,
    )
    assert parser_before["ingredient"]["food"]["id"] == food_id
    assert parser_after["ingredient"]["food"]["id"] == food_id
    request(
        "PUT", f"/api/foods/{food_id}", {"name": "phase0 minimal update"}, token=token
    )
    partial = request("GET", f"/api/foods/{food_id}", token=token)
    assert partial["pluralName"] is None
    assert partial["description"] == ""
    assert partial["extras"] == {}
    assert partial["aliases"] == []
    assert partial["substitutions"] == []
    print("Mealie v3.28.0 food update preserved fields and recipe reference: PASS")
    print("Parser result for an existing alias before/after update: unchanged")
    print("Minimal PUT resets omitted optional fields: verified")
    print(f"OpenAPI SHA-256: {OPENAPI_SHA256}")


if __name__ == "__main__":
    main()
