"""Seed a fresh Mealie v3.28.0 instance with Mealie's own foods."""

import json
import os
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL: str = os.getenv("MEALIE_SEED_BASE_URL", "http://127.0.0.1:19925")
SEED_LOCALE: str = "en-US"


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
        timeout=60,
    ) as response:
        return json.load(response)


def main() -> None:
    for _ in range(30):
        try:
            openapi = request("GET", "/openapi.json")
            break
        except (URLError, ConnectionResetError):
            time.sleep(1)
    else:
        raise RuntimeError(f"Mealie did not become ready at {BASE_URL}")

    if openapi["info"]["version"] != "v3.28.0":
        raise RuntimeError("The Mealie instance is not v3.28.0")
    if "post" not in openapi["paths"].get("/api/groups/seeders/foods", {}):
        raise RuntimeError("Mealie's built-in Foods seeder is unavailable")

    credentials = {
        "username": os.getenv("MEALIE_TEST_USERNAME", "changeme@example.com"),
        "password": os.getenv("MEALIE_TEST_PASSWORD", "MyPassword"),
    }
    token = request("POST", "/api/auth/token", credentials, form=True)["access_token"]
    before = request("GET", "/api/foods?page=1&perPage=1", token=token)
    if before["total"] != 0:
        if os.getenv("MEALIE_SEED_SKIP_EXISTING") == "1":
            print(f"Skipping seeding: {before['total']} Foods already present")
            return
        raise RuntimeError(
            "Foods are already present; start with a fresh Compose instance"
        )

    result = request(
        "POST", "/api/groups/seeders/foods", {"locale": SEED_LOCALE}, token=token
    )
    if result.get("error"):
        raise RuntimeError(f"Mealie Foods seeding failed: {result['message']}")
    after = request("GET", "/api/foods?page=1&perPage=1", token=token)
    if not isinstance(after["total"], int) or after["total"] <= 0:
        raise RuntimeError("Mealie reported success but no Foods were seeded")
    print(f"Seeded {after['total']} Mealie Foods ({SEED_LOCALE})")


if __name__ == "__main__":
    main()
