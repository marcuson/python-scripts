"""Read-only Mealie discovery and food catalog export."""

import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class MealieWriteError(RuntimeError):
    def __init__(
        self, path: str, status: int | None, uncertain: bool, method: str = "PUT"
    ) -> None:
        self.status: int | None = status
        self.uncertain: bool = uncertain
        self.method: str = method
        detail: str = f"HTTP {status}" if status is not None else "connection failure"
        super().__init__(f"{method} {path} failed: {detail}")


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("--base-url has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError(
            "--base-url must be an HTTP(S) URL without embedded credentials"
        )
    if (
        parsed.query
        or parsed.fragment
        or (port is None and parsed.netloc.endswith(":"))
    ):
        raise ValueError(
            "--base-url must not contain a query, fragment, or invalid port"
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("HTTPS is required except for a local loopback test instance")
    return value.strip().rstrip("/")


class MealieClient:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
        self.base_url: str = normalize_base_url(base_url)
        self.token: str = token
        self.timeout: float = timeout
        self.opener = build_opener(NoRedirect())

    def get_bytes(self, path: str) -> bytes:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"Unsafe API path: {path}")
        request = Request(
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.read()
            except HTTPError as error:
                if error.code in {429, 502, 503, 504} and attempt < 2:
                    time.sleep(0.25 * 2**attempt)
                    continue
                raise RuntimeError(f"GET {path} returned HTTP {error.code}") from error
            except URLError as error:
                if attempt < 2:
                    time.sleep(0.25 * 2**attempt)
                    continue
                raise RuntimeError(f"GET {path} failed: {error.reason}") from error
        raise RuntimeError(f"GET {path} failed after retries")

    def get_json(self, path: str) -> Any:
        try:
            return json.loads(self.get_bytes(path))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"GET {path} did not return valid JSON") from error

    def get_json_or_none(self, path: str) -> Any | None:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"Unsafe API path: {path}")
        request = Request(
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError(f"GET {path} returned HTTP {error.code}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"GET {path} did not return valid JSON") from error

    def put_json(self, path: str, value: dict[str, Any]) -> None:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"Unsafe API path: {path}")
        request = Request(
            self.base_url + path,
            data=json_bytes(value),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response.read()
        except HTTPError as error:
            raise MealieWriteError(
                path, error.code, error.code == 429 or error.code >= 500
            ) from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise MealieWriteError(path, None, True) from error

    def delete(self, path: str) -> None:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError(f"Unsafe API path: {path}")
        request = Request(
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method="DELETE",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                response.read()
        except HTTPError as error:
            raise MealieWriteError(
                path, error.code, error.code == 429 or error.code >= 500, "DELETE"
            ) from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise MealieWriteError(path, None, True, "DELETE") from error


def _response_ref(operation: dict[str, Any]) -> str | None:
    return (
        operation.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref")
    )


def discover(client: MealieClient) -> tuple[dict[str, Any], bytes]:
    docs = client.get_bytes("/docs")
    match = re.search(rb"\burl:\s*['\"]([^'\"]+)['\"]", docs)
    if match is None:
        raise ValueError("/docs does not identify an OpenAPI document")
    openapi_path = match.group(1).decode("utf-8")
    if (
        not openapi_path.startswith("/")
        or openapi_path.startswith("//")
        or "?" in openapi_path
    ):
        raise ValueError("/docs points to an unsupported OpenAPI location")
    raw_openapi = client.get_bytes(openapi_path)
    try:
        document = json.loads(raw_openapi)
        paths: object = document["paths"]
        version: object = document["info"]["version"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The instance returned an invalid OpenAPI document") from error
    if not isinstance(paths, dict) or not isinstance(version, str):
        raise TypeError("The OpenAPI document has invalid paths or version")
    api_paths = cast(dict[str, Any], paths)
    list_paths = [
        path
        for path, methods in api_paths.items()
        if isinstance(methods, dict)
        and _response_ref(cast(dict[str, Any], methods).get("get", {}))
        == "#/components/schemas/IngredientFoodPagination"
    ]
    if len(list_paths) != 1:
        raise ValueError(
            "Cannot identify one unambiguous food list operation in OpenAPI"
        )
    list_path = list_paths[0]
    detail_paths = [
        path
        for path, methods in api_paths.items()
        if path.startswith(list_path + "/{")
        and path.endswith("}")
        and isinstance(methods, dict)
        and _response_ref(cast(dict[str, Any], methods).get("get", {}))
        == "#/components/schemas/IngredientFood-Output"
    ]
    if len(detail_paths) != 1:
        raise ValueError(
            "Cannot identify one unambiguous food detail operation in OpenAPI"
        )
    detail_path = detail_paths[0]
    parameters = cast(
        list[dict[str, Any]], api_paths[list_path]["get"].get("parameters", [])
    )
    names = {
        parameter.get("name")
        for parameter in parameters
        if parameter.get("in") == "query"
    }
    if not {"page", "perPage"} <= names:
        raise ValueError("The food list lacks known page/perPage pagination parameters")
    recipe_lists: list[tuple[str, dict[str, Any]]] = []
    for path, methods in api_paths.items():
        if not isinstance(methods, dict):
            continue
        operation_value: object = cast(dict[str, Any], methods).get("get")
        if not isinstance(operation_value, dict):
            continue
        operation = cast(dict[str, Any], operation_value)
        recipe_parameters: set[object] = {
            cast(dict[str, object], parameter).get("name")
            for parameter in cast(list[object], operation.get("parameters", []))
            if isinstance(parameter, dict)
            and cast(dict[str, object], parameter).get("in") == "query"
        }
        if {"foods", "page", "perPage"} <= recipe_parameters:
            recipe_lists.append((path, operation))
    recipe_usage: dict[str, str] | None = None
    recipe_lists = [
        candidate for candidate in recipe_lists if candidate[0] == "/api/recipes"
    ]
    if len(recipe_lists) == 1:
        recipe_path, recipe_operation = recipe_lists[0]
        if (
            _response_ref(recipe_operation)
            == "#/components/schemas/PaginationBase_RecipeSummary_"
        ):
            recipe_usage = {
                "method": "GET",
                "path": recipe_path,
                "food_filter": "foods",
            }
    shopping_usage: dict[str, str] | None = None
    shopping_operation: object = api_paths.get("/api/households/shopping/items", {})
    if isinstance(shopping_operation, dict):
        shopping_get: object = cast(dict[str, Any], shopping_operation).get("get")
        if isinstance(shopping_get, dict):
            shopping_get_typed = cast(dict[str, Any], shopping_get)
            shopping_names: set[object] = {
                cast(dict[str, object], parameter).get("name")
                for parameter in cast(
                    list[object], shopping_get_typed.get("parameters", [])
                )
                if isinstance(parameter, dict)
                and cast(dict[str, object], parameter).get("in") == "query"
            }
            if {"queryFilter", "page", "perPage"} <= shopping_names and _response_ref(
                shopping_get_typed
            ) == "#/components/schemas/ShoppingListItemPagination":
                shopping_usage = {
                    "method": "GET",
                    "path": "/api/households/shopping/items",
                    "food_filter": "queryFilter",
                }
    update_operation: dict[str, Any] = api_paths[detail_path].get("put", {})
    update_ref = (
        update_operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref")
    )
    openapi_hash = sha256(raw_openapi)
    compatibility = cast(
        dict[str, Any],
        json.loads(
            resources.files("mealietidy")
            .joinpath("compatibility/v3_28_0.json")
            .read_text()
        ),
    )
    write_verified = (
        version == compatibility["mealie_version"]
        and openapi_hash == compatibility["openapi_sha256"]
        and update_ref == "#/components/schemas/CreateIngredientFood"
    )
    merge_operation: dict[str, Any] = api_paths.get("/api/foods/merge", {}).get(
        "put", {}
    )
    merge_schema: object = (
        merge_operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref")
    )
    dedupe_compatibility: object = compatibility.get("deduplication", {})
    merge_compatibility = (
        isinstance(dedupe_compatibility, dict)
        and cast(dict[str, Any], dedupe_compatibility).get("merge") == "verified"
        and version == compatibility["mealie_version"]
        and openapi_hash == compatibility["openapi_sha256"]
        and merge_schema == "#/components/schemas/MergeFood"
        and recipe_usage is not None
        and shopping_usage is not None
    )
    delete_operation: object = api_paths[detail_path].get("delete")
    delete_compatibility = (
        isinstance(dedupe_compatibility, dict)
        and cast(dict[str, Any], dedupe_compatibility).get("delete") == "verified"
        and version == compatibility["mealie_version"]
        and openapi_hash == compatibility["openapi_sha256"]
        and isinstance(delete_operation, dict)
        and "200" in cast(dict[str, Any], delete_operation).get("responses", {})
        and recipe_usage is not None
        and shopping_usage is not None
    )
    discovery: dict[str, Any] = {
        "mealie_version": version,
        "openapi_path": openapi_path,
        "openapi_hash": openapi_hash,
        "food_list": {
            "method": "GET",
            "path": list_path,
            "pagination": ["page", "perPage"],
        },
        "food_detail": {"method": "GET", "path": detail_path},
        "food_update": {
            "method": "PUT",
            "path": detail_path,
            "request_schema": update_ref,
        }
        if update_ref
        else None,
        "recipe_usage": recipe_usage,
        "shopping_usage": shopping_usage,
        "food_merge": {
            "method": "PUT",
            "path": "/api/foods/merge",
            "request_schema": merge_schema,
        }
        if merge_schema == "#/components/schemas/MergeFood"
        else None,
        "food_delete": {"method": "DELETE", "path": detail_path}
        if isinstance(delete_operation, dict)
        and "200" in cast(dict[str, Any], delete_operation).get("responses", {})
        else None,
        "write_compatibility": "verified" if write_verified else "unverified",
        "dedupe_compatibility": "verified" if merge_compatibility else "unverified",
        "delete_compatibility": "verified" if delete_compatibility else "unverified",
        "authentication": api_paths[list_path]["get"].get("security", []),
    }
    return discovery, raw_openapi


def substitution_reference_counts(
    client: MealieClient, food_list: dict[str, Any]
) -> dict[str, int]:
    path: object = food_list.get("path")
    pagination: object = food_list.get("pagination")
    if (
        not isinstance(path, str)
        or not path.startswith("/api/")
        or path.startswith("//")
        or not isinstance(pagination, list)
        or not {"page", "perPage"} <= set(cast(list[object], pagination))
    ):
        raise ValueError("Invalid Mealie food-list capability")
    counts: dict[str, int] = {}
    seen_ids: set[str] = set()
    page: int = 1
    total: int | None = None
    total_pages: int | None = None
    while total_pages is None or page <= total_pages:
        result: object = client.get_json(
            path + "?" + urlencode({"page": page, "perPage": 50})
        )
        if not isinstance(result, dict):
            raise TypeError("Invalid food-list response while checking substitutions")
        response = cast(dict[str, object], result)
        page_total: object = response.get("total")
        pages: object = response.get("total_pages")
        items: object = response.get("items")
        if (
            not isinstance(page_total, int)
            or page_total < 0
            or not isinstance(pages, int)
            or pages < 0
            or not isinstance(items, list)
            or (total is not None and total != page_total)
            or (total_pages is not None and total_pages != pages)
        ):
            raise ValueError(
                "Invalid food-list pagination while checking substitutions"
            )
        total = page_total
        total_pages = pages
        for item_value in cast(list[object], items):
            if not isinstance(item_value, dict):
                raise TypeError("Invalid food in substitution reference scan")
            item = cast(dict[str, Any], item_value)
            source_id: object = item.get("id")
            substitutions: object = item.get("substitutions")
            if (
                not isinstance(source_id, str)
                or source_id in seen_ids
                or not isinstance(substitutions, list)
            ):
                raise ValueError("Food response lacks substitution reference data")
            seen_ids.add(source_id)
            for relation_value in cast(list[object], substitutions):
                if not isinstance(relation_value, dict):
                    raise TypeError("Invalid substitution relationship in food list")
                relation: dict[str, object] = cast(dict[str, object], relation_value)
                target_id: object = relation.get("substituteFoodId")
                note: object = relation.get("note")
                if (target_id is not None and not isinstance(target_id, str)) or (
                    target_id is None and (not isinstance(note, str) or not note.strip())
                ):
                    raise TypeError("Substitution relationship lacks a food ID or note")
                counts[source_id] = counts.get(source_id, 0) + 1
                if isinstance(target_id, str) and target_id != source_id:
                    counts[target_id] = counts.get(target_id, 0) + 1
        if pages == 0 or page >= pages:
            break
        page += 1
    if len(seen_ids) != total:
        raise ValueError("Food-list substitution scan did not complete")
    return counts


def _food_pages(client: MealieClient, path: str) -> tuple[list[dict[str, Any]], int]:
    foods: list[dict[str, Any]] = []
    total: int | None = None
    total_pages: int | None = None
    page = 1
    while True:
        print(f"Reading food list page {page}...", file=sys.stderr, flush=True)
        result: object = client.get_json(
            path + "?" + urlencode({"page": page, "perPage": 50})
        )
        if not isinstance(result, dict):
            raise TypeError(f"Food page {page} has an invalid response")
        data = cast(dict[str, Any], result)
        if not isinstance(data.get("items"), list):
            raise TypeError(f"Food page {page} has an invalid response")
        page_total: object = data.get("total")
        page_count: object = data.get("total_pages")
        if not isinstance(page_total, int) or not isinstance(page_count, int):
            raise TypeError(f"Food page {page} has invalid pagination totals")
        if total is None:
            total, total_pages = page_total, page_count
            if total < 0 or total_pages < 0 or (total > 0 and total_pages == 0):
                raise ValueError("Food pagination has inconsistent totals")
        elif page_total != total or page_count != total_pages:
            raise ValueError("Food catalog changed during pagination")
        if data.get("page") != page:
            raise ValueError(f"Food page {page} returned an unexpected page number")
        for item in cast(list[object], data["items"]):
            if not isinstance(item, dict):
                raise TypeError(f"Food page {page} contains a non-object item")
            foods.append(cast(dict[str, Any], item))
        if total_pages is None:
            raise ValueError("Food pagination has no page count")
        if page >= max(1, total_pages):
            break
        page += 1
    ids = [food.get("id") for food in foods]
    if any(not isinstance(food_id, str) or not food_id for food_id in ids):
        raise ValueError("A food page contains a missing or invalid ID")
    if len(set(ids)) != len(ids) or len(foods) != total:
        raise ValueError("Food export has duplicate IDs or an inconsistent count")
    return foods, total


def write_new(path: Path, content: bytes) -> None:
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as output:
        output.write(content)


def export_catalog(base_url: str, token: str, root: Path, timeout: float) -> int:
    if timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    client = MealieClient(base_url, token, timeout)
    print("Discovering Mealie API...", file=sys.stderr, flush=True)
    discovery, raw_openapi = discover(client)
    print(
        f"Discovery complete: Mealie {discovery['mealie_version']}; "
        f"food endpoint {discovery['food_list']['path']}",
        file=sys.stderr,
        flush=True,
    )
    print("Listing foods...", file=sys.stderr, flush=True)
    listed, total = _food_pages(client, discovery["food_list"]["path"])
    details: list[dict[str, Any]] = []
    detail_template: str = discovery["food_detail"]["path"]
    print(f"Fetching details for {total} foods...", file=sys.stderr, flush=True)
    for index, listed_food in enumerate(listed, start=1):
        print(f"Fetching food details: {index}/{total}", file=sys.stderr, flush=True)
        food_id: str = listed_food["id"]
        detail_value: object = client.get_json(
            detail_template.replace("{item_id}", quote(food_id, safe=""))
        )
        if not isinstance(detail_value, dict):
            raise TypeError(f"Food detail differs from the listed ID: {food_id}")
        detail = cast(dict[str, Any], detail_value)
        if detail.get("id") != food_id:
            raise ValueError(f"Food detail differs from the listed ID: {food_id}")
        if any(detail.get(key) != value for key, value in listed_food.items()):
            raise ValueError(f"Food changed between list and detail reads: {food_id}")
        details.append(detail)
    if total:
        print(file=sys.stderr, flush=True)
    print(
        "Verifying the catalog did not change during export...",
        file=sys.stderr,
        flush=True,
    )
    listed_again, total_again = _food_pages(client, discovery["food_list"]["path"])
    if total_again != total or listed_again != listed:
        raise ValueError(
            "Food catalog changed during export; retry from a fresh plan directory"
        )
    details.sort(key=lambda food: food["id"])
    snapshot = json_bytes({"foods": details})
    discovery_data = json_bytes(discovery)
    files: dict[str, bytes] = {
        "discovery/openapi.json": raw_openapi,
        "discovery/capabilities.json": discovery_data,
        "snapshot/foods.json": snapshot,
    }
    for food in details:
        files[f"snapshot/records/{sha256(food['id'].encode('utf-8'))}.json"] = (
            json_bytes(food)
        )
    manifest: dict[str, object] = {
        "exported_at": datetime.now(UTC).isoformat(),
        "base_url": client.base_url,
        "mealie_version": discovery["mealie_version"],
        "openapi_hash": discovery["openapi_hash"],
        "record_count": total,
        "page_total": total,
        "snapshot_hash": sha256(snapshot),
        "files": {path: sha256(content) for path, content in files.items()},
        "record_hashes": {food["id"]: sha256(json_bytes(food)) for food in details},
    }
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise FileExistsError(f"Plan directory already contains files: {root}")
    print(
        f"Writing {len(files) + 1} plan files to {root}...",
        file=sys.stderr,
        flush=True,
    )
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise PermissionError(f"Plan directory must be private (mode 0700): {root}")
    (root / "discovery").mkdir(mode=0o700)
    (root / "snapshot").mkdir(mode=0o700)
    (root / "snapshot/records").mkdir(mode=0o700)
    for relative, content in files.items():
        write_new(root / relative, content)
    write_new(root / "manifest.json", json_bytes(manifest))
    return total
