"""Validate, preview, and apply approved food edits with a resumable journal."""

import copy
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode

from mealietidy.analyze import normalize
from mealietidy.export import (
    MealieClient,
    MealieWriteError,
    discover,
    json_bytes,
    sha256,
    substitution_reference_counts,
)
from mealietidy.journal import (
    FoodJournal,
    JournalEvent,
    upgrade_legacy_journal,
)
from mealietidy.journal import (
    append_journal as _append,
)
from mealietidy.journal import (
    read_journal as _journal,
)
from mealietidy.review import ReviewedPlan, load_review


def _hash(food: dict[str, Any]) -> str:
    return sha256(json_bytes(food))


def _read_food(client: MealieClient, template: str, food_id: str) -> dict[str, Any]:
    path: str = template.replace("{item_id}", quote(food_id, safe=""))
    value: object = client.get_json(path)
    if not isinstance(value, dict):
        raise TypeError(f"Invalid food detail for {food_id}")
    food = cast(dict[str, Any], value)
    if food.get("id") != food_id:
        raise ValueError(f"Invalid or changed food detail for {food_id}")
    return food


def _desired(food: dict[str, Any], rows: list[dict[str, str]]) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(food)
    for row in rows:
        value: str = row["proposed_value"]
        if row["action"] == "rename":
            result["name"] = value
        elif row["action"] == "set_plural":
            result["pluralName"] = value
        elif row["action"] == "add_alias":
            terms: set[str] = {normalize(result["name"])}
            terms.update(
                normalize(alias["name"]) for alias in result.get("aliases", [])
            )
            if normalize(value) not in terms:
                result.setdefault("aliases", []).append({"name": value})
    return result


def _matches(actual: dict[str, Any], desired: dict[str, Any]) -> bool:
    return {key: value for key, value in actual.items() if key != "updatedAt"} == {
        key: value for key, value in desired.items() if key != "updatedAt"
    }


def _report(root: Path, report: dict[str, Any]) -> None:
    folder: Path = root / "reports"
    folder.mkdir(mode=0o700, exist_ok=True)
    content: bytes = json_bytes(report)
    text: str = "\n".join(
        [
            f"Approved changes: {report['approved_changes']}",
            f"Pending food writes: {report['pending_foods']}",
            f"Already verified foods: {report['skipped_foods']}",
            f"Plan SHA-256: {report['plan_hash']}",
            f"Review SHA-256: {report['review_hash']}",
            f"OpenAPI SHA-256: {report['openapi_hash']}",
            f"Summary: {json.dumps(report['summary'], sort_keys=True)}",
            *(
                (
                    f"merge {item['operation_id']}: {item['source_food_id']} -> "
                    f"{item['retained_food_id']}; recipes {item['recipe_counts']}; "
                    f"shopping items {item['shopping_item_counts']}"
                )
                if item.get("action") == "merge"
                else (
                    f"delete {item['operation_id']}: {item['status']}; "
                    f"food {item['deleted_food_id']} "
                    f"(references: {item['reference_counts']})"
                )
                if item.get("action") == "delete"
                else (
                    f"{item['food_id']}: {item['status']}; "
                    + ", ".join(
                        f"{change['action']}: {change['from']!r} -> {change['to']!r}"
                        for change in item["changes"]
                    )
                )
                for item in report["foods"]
            ),
            *(
                f"VERIFIED merge {item['operation_id']}: recipe counts "
                f"{item['recipe_counts_before']} -> {item['recipe_counts_after']}; "
                f"shopping item counts {item['shopping_counts_before']} -> "
                f"{item['shopping_counts_after']}"
                for item in report.get("merge_results", [])
            ),
            *(
                f"VERIFIED delete {item['operation_id']}: deleted "
                f"{item['deleted_food_id']} with no recipe, shopping-list, or "
                "substitution references"
                for item in report.get("delete_results", [])
            ),
            *(f"BLOCKED: {reason}" for reason in report["blocked"]),
            "",
        ]
    )
    for name, data in (
        ("preflight.json", content),
        ("preflight.txt", text.encode("utf-8")),
    ):
        target: Path = folder / name
        temporary: Path = folder / (name + ".tmp")
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb"
        ) as output:
            output.write(data)
        os.replace(temporary, target)


def _payload(food: dict[str, Any], properties: set[str]) -> dict[str, Any]:
    result: dict[str, Any] = {key: food[key] for key in properties if key in food}
    result["aliases"] = [{"name": alias["name"]} for alias in food.get("aliases", [])]
    result["substitutions"] = [
        {key: item[key] for key in ("substituteFoodId", "note") if key in item}
        for item in food.get("substitutions", [])
    ]
    return result


def _recipe_count(
    client: MealieClient, capability: dict[str, str], food_id: str
) -> int:
    path: str = capability["path"]
    if not path.startswith("/api/") or path.startswith("//"):
        raise ValueError("Invalid recipe usage API path")
    result: object = client.get_json(
        path + "?" + urlencode({capability["food_filter"]: food_id, "perPage": 1})
    )
    if not isinstance(result, dict):
        raise TypeError(f"Invalid recipe usage count for food {food_id}")
    count: object = cast(dict[str, object], result).get("total")
    if not isinstance(count, int):
        raise TypeError(f"Invalid recipe usage count for food {food_id}")
    if count < 0:
        raise ValueError(f"Invalid recipe usage count for food {food_id}")
    return count


def _shopping_count(
    client: MealieClient, capability: dict[str, str], food_id: str
) -> int:
    path: str = capability["path"]
    if not path.startswith("/api/") or path.startswith("//"):
        raise ValueError("Invalid shopping usage API path")
    query: str = f"foodId=={food_id}"
    result: object = client.get_json(
        path + "?" + urlencode({"queryFilter": query, "perPage": 1})
    )
    if not isinstance(result, dict):
        raise TypeError(f"Invalid shopping-list usage count for food {food_id}")
    count: object = cast(dict[str, object], result).get("total")
    if not isinstance(count, int):
        raise TypeError(f"Invalid shopping-list usage count for food {food_id}")
    if count < 0:
        raise ValueError(f"Invalid shopping-list usage count for food {food_id}")
    return count


def _merge_ids(row: dict[str, str]) -> tuple[str, str]:
    mode, separator, retained_id = row["proposed_value"].partition(":")
    participants: set[str] = {row["food_id"], row["duplicate_target_id"]}
    if mode != "merge" or not separator or retained_id not in participants:
        raise ValueError(f"Unsupported duplicate resolution for {row['operation_id']}")
    source_id: str = next(food_id for food_id in participants if food_id != retained_id)
    return source_id, retained_id


def _delete_id(row: dict[str, str]) -> str:
    mode, separator, deleted_id = row["proposed_value"].partition(":")
    participants: set[str] = {row["food_id"], row["duplicate_target_id"]}
    if mode != "delete" or not separator or deleted_id not in participants:
        raise ValueError(f"Unsupported duplicate resolution for {row['operation_id']}")
    return deleted_id


def _merge_target(
    retained: dict[str, Any],
    source: dict[str, Any],
    participant_ids: set[str],
    foods: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(retained)
    aliases: list[dict[str, str]] = [
        {"name": alias["name"]} for alias in retained.get("aliases", [])
    ]
    terms: set[str] = {normalize(retained["name"])}
    terms.update(normalize(alias["name"]) for alias in aliases)
    source_terms: list[str] = [
        source["name"],
        *(alias["name"] for alias in source.get("aliases", [])),
    ]
    for term in source_terms:
        normalized: str = normalize(term)
        if not normalized or normalized in terms:
            continue
        collision: bool = any(
            candidate_id not in participant_ids
            and normalized
            in {
                normalize(food["name"]),
                *(normalize(alias["name"]) for alias in food.get("aliases", [])),
            }
            for candidate_id, food in foods.items()
        )
        if collision:
            raise ValueError(
                f"Cannot merge food {source['id']}: alias {term!r} collides with another food"
            )
        aliases.append({"name": term})
        terms.add(normalized)
    result["aliases"] = aliases

    return result


def _validate_resolution_overlaps(plan: ReviewedPlan, applied: set[str]) -> None:
    occupied: set[str] = set()
    for row in plan.resolutions:
        pair: set[str] = {row["food_id"], row["duplicate_target_id"]}
        if occupied & pair:
            raise ValueError(
                f"Conflict resolution {row['operation_id']} overlaps another approved food operation"
            )
        occupied.update(pair)
        if pair & set(plan.groups):
            retained_id: str = row["proposed_value"].removeprefix("merge:")
            if (
                row["operation_id"] not in applied
                or not row["proposed_value"].startswith("merge:")
                or pair & set(plan.groups) != {retained_id}
            ):
                raise ValueError(
                    f"Conflict resolution {row['operation_id']} overlaps another approved food operation"
                )


def apply_plan(
    base_url: str, token: str, root: Path, timeout: float, dry_run: bool
) -> tuple[int, int]:
    if timeout <= 0:
        raise ValueError("--timeout must be greater than zero")
    plan: ReviewedPlan = load_review(root)
    upgrade_legacy_journal(root, plan)
    journal: dict[str, FoodJournal] = _journal(root, plan)
    _validate_resolution_overlaps(
        plan,
        {operation_id for state in journal.values() for operation_id in state.applied},
    )
    if not plan.groups and not plan.resolutions:
        raise ValueError("No approved operations in review.csv")
    client = MealieClient(base_url, token, timeout)
    if client.base_url != plan.manifest["base_url"]:
        raise ValueError("Mealie base URL differs from the export manifest")
    discovery, raw_openapi = discover(client)
    if (
        discovery["mealie_version"] != plan.manifest["mealie_version"]
        or discovery["openapi_hash"] != plan.manifest["openapi_hash"]
        or sha256(raw_openapi) != discovery["openapi_hash"]
        or discovery["write_compatibility"] != "verified"
    ):
        raise ValueError(
            "Mealie version or OpenAPI differs from the verified export; writes disabled"
        )
    merge_rows: list[dict[str, str]] = [
        row for row in plan.resolutions if row["proposed_value"].startswith("merge:")
    ]
    delete_rows: list[dict[str, str]] = [
        row for row in plan.resolutions if row["proposed_value"].startswith("delete:")
    ]
    if merge_rows and discovery.get("dedupe_compatibility") != "verified":
        raise ValueError(
            "Food merge is not verified for this Mealie version; no writes performed"
        )
    if delete_rows and discovery.get("delete_compatibility") != "verified":
        raise ValueError(
            "Food deletion is not verified for this Mealie version; no writes performed"
        )
    document = cast(dict[str, Any], json.loads(raw_openapi))
    properties: set[str] = set(
        document["components"]["schemas"]["CreateIngredientFood"]["properties"]
    )
    required: set[str] = {
        "name",
        "pluralName",
        "aliases",
        "substitutions",
        "extras",
        "description",
        "labelId",
        "householdsWithIngredientFood",
    }
    if not required <= properties:
        raise ValueError(
            "Verified food update schema is missing required writable fields"
        )
    template: str = discovery["food_detail"]["path"]
    merge_capability: object = discovery.get("food_merge")
    delete_capability: object = discovery.get("food_delete")
    recipe_capability: object = discovery.get("recipe_usage")
    shopping_capability: object = discovery.get("shopping_usage")
    food_list_capability: object = discovery.get("food_list")
    if plan.resolutions and not all(
        isinstance(capability, dict)
        for capability in (
            recipe_capability,
            shopping_capability,
        )
    ):
        raise ValueError(
            "Mealie reference-count API is unavailable; no writes performed"
        )
    if merge_rows and not isinstance(merge_capability, dict):
        raise ValueError("Mealie merge API is unavailable; no writes performed")
    if (merge_rows or delete_rows) and not isinstance(food_list_capability, dict):
        raise ValueError(
            "Mealie substitution-reference API is unavailable; no writes performed"
        )
    if delete_rows and not isinstance(delete_capability, dict):
        raise ValueError("Mealie delete API is unavailable; no writes performed")
    pending: dict[str, tuple[dict[str, Any], list[dict[str, str]]]] = {}
    skipped: int = 0
    blocked: list[str] = []
    food_reports: list[dict[str, Any]] = []
    pending_merges: list[dict[str, Any]] = []
    pending_deletions: list[dict[str, Any]] = []
    recovered_deletions: list[tuple[dict[str, str], str]] = []
    substitution_counts: dict[str, int] = {}
    if merge_rows or delete_rows:
        try:
            substitution_counts = substitution_reference_counts(
                client, cast(dict[str, Any], food_list_capability)
            )
        except (RuntimeError, ValueError, TypeError, KeyError) as error:
            raise ValueError(
                f"Cannot verify substitution references; no writes performed: {error}"
            ) from error
    for food_id, rows in sorted(plan.groups.items()):
        current: dict[str, Any] = _read_food(client, template, food_id)
        state: FoodJournal = journal.get(food_id, FoodJournal())
        new_rows: list[dict[str, str]] = [
            row for row in rows if row["operation_id"] not in state.applied
        ]
        current_hash: str = _hash(current)
        expected_hash: str = (
            state.verified_hash or plan.manifest["record_hashes"][food_id]
        )
        status: str = "pending"
        last: JournalEvent | None = state.latest
        if current_hash != expected_hash:
            if last and last["status"] in {"attempt", "uncertain"}:
                attempted: list[dict[str, str]] = [
                    row for row in rows if row["operation_id"] in last["operation_ids"]
                ]
                old_rows: list[dict[str, str]] = [
                    row for row in plan.rows if row["operation_id"] in state.applied
                ]
                if _matches(
                    current, _desired(plan.foods[food_id], old_rows + attempted)
                ):
                    _append(
                        root,
                        plan,
                        food_id,
                        attempted,
                        "verified",
                        expected_hash,
                        current_hash,
                    )
                    state.verified_hash = current_hash
                    state.applied.update(
                        {row["operation_id"]: row for row in attempted}
                    )
                    new_rows = [row for row in new_rows if row not in attempted]
                else:
                    status = "blocked"
            else:
                status = "blocked"
        if status == "pending" and not new_rows:
            status = "skipped"
            skipped += 1
        if status == "blocked":
            blocked.append(
                f"food {food_id} differs from its snapshot or verified journal state"
            )
        if status == "pending":
            pending[food_id] = (current, new_rows)
        changes: list[dict[str, str]] = [
            {
                "operation_id": row["operation_id"],
                "action": row["action"],
                "from": row["current_value"],
                "to": row["proposed_value"],
            }
            for row in new_rows
        ]
        food_reports.append(
            {
                "food_id": food_id,
                "status": status,
                "changes": changes,
                "preserved_fields": sorted(
                    set(current) - {"name", "pluralName", "aliases", "updatedAt"}
                ),
            }
        )
    recovered_merges: list[tuple[dict[str, str], str, str, str]] = []
    for row in plan.resolutions:
        state: FoodJournal = journal.get(row["food_id"], FoodJournal())
        operation_id: str = row["operation_id"]
        if operation_id in state.applied:
            skipped += 1
            continue
        if row["proposed_value"].startswith("delete:"):
            deleted_id: str = _delete_id(row)
            survivor_id: str = next(
                food_id
                for food_id in (row["food_id"], row["duplicate_target_id"])
                if food_id != deleted_id
            )
            deleted_path: str = template.replace(
                "{item_id}", quote(deleted_id, safe="")
            )
            survivor_path: str = template.replace(
                "{item_id}", quote(survivor_id, safe="")
            )
            deleted_value: object = client.get_json_or_none(deleted_path)
            survivor_value: object = client.get_json_or_none(survivor_path)
            if not isinstance(survivor_value, dict):
                blocked.append(
                    f"delete {operation_id}: remaining food {survivor_id} is missing"
                )
                continue
            survivor: dict[str, Any] = cast(dict[str, Any], survivor_value)
            if deleted_value is None:
                delete_attempted: bool = bool(
                    state.latest
                    and operation_id in state.latest["operation_ids"]
                    and state.latest["status"] in {"attempt", "uncertain"}
                )
                if (
                    delete_attempted
                    and _hash(survivor) == plan.manifest["record_hashes"][survivor_id]
                    and _recipe_count(
                        client, cast(dict[str, str], recipe_capability), deleted_id
                    )
                    == 0
                    and _shopping_count(
                        client, cast(dict[str, str], shopping_capability), deleted_id
                    )
                    == 0
                    and substitution_reference_counts(
                        client, cast(dict[str, Any], food_list_capability)
                    ).get(deleted_id, 0)
                    == 0
                ):
                    recovered_deletions.append((row, deleted_id))
                    skipped += 1
                else:
                    blocked.append(
                        f"delete {operation_id}: selected food {deleted_id} is missing without a recoverable journal attempt"
                    )
                continue
            if not isinstance(deleted_value, dict):
                blocked.append(f"delete {operation_id}: invalid food {deleted_id}")
                continue
            deleted: dict[str, Any] = cast(dict[str, Any], deleted_value)
            if (
                _hash(deleted) != plan.manifest["record_hashes"][deleted_id]
                or _hash(survivor) != plan.manifest["record_hashes"][survivor_id]
            ):
                blocked.append(
                    f"delete {operation_id}: conflict foods changed since export"
                )
                continue
            recipes: int = _recipe_count(
                client, cast(dict[str, str], recipe_capability), deleted_id
            )
            shopping_items: int = _shopping_count(
                client, cast(dict[str, str], shopping_capability), deleted_id
            )
            substitution_links: int = substitution_counts.get(deleted_id, 0)
            if recipes or shopping_items or substitution_links:
                blocked.append(
                    f"delete {operation_id}: food {deleted_id} still has references "
                    f"(recipes={recipes}, shopping_items={shopping_items}, "
                    f"substitution_links={substitution_links})"
                )
                continue
            pending_deletions.append(
                {
                    "row": row,
                    "deleted_id": deleted_id,
                    "survivor_id": survivor_id,
                    "food": deleted,
                    "survivor": survivor,
                    "recipes": recipes,
                    "shopping_items": shopping_items,
                    "substitution_links": substitution_links,
                }
            )
            food_reports.append(
                {
                    "operation_id": operation_id,
                    "action": "delete",
                    "deleted_food_id": deleted_id,
                    "survivor_food_id": survivor_id,
                    "status": "pending",
                    "reference_counts": {
                        "recipes": recipes,
                        "shopping_items": shopping_items,
                        "substitution_links": substitution_links,
                    },
                }
            )
            continue
        source_id, retained_id = _merge_ids(row)
        source_path: str = template.replace("{item_id}", quote(source_id, safe=""))
        retained_path: str = template.replace("{item_id}", quote(retained_id, safe=""))
        source_value: object = client.get_json_or_none(source_path)
        retained_value: object = client.get_json_or_none(retained_path)
        if not isinstance(retained_value, dict):
            blocked.append(
                f"merge {operation_id}: retained food {retained_id} is missing"
            )
            continue
        retained = cast(dict[str, Any], retained_value)
        participant_ids: set[str] = {source_id, retained_id}
        expected_retained = _merge_target(
            retained, plan.foods[source_id], participant_ids, plan.foods
        )
        if source_value is None:
            if _matches(retained, expected_retained):
                recovered_merges.append((row, source_id, retained_id, _hash(retained)))
                skipped += 1
                continue
            blocked.append(
                f"merge {operation_id}: source food {source_id} is missing and retained food differs"
            )
            continue
        if not isinstance(source_value, dict):
            blocked.append(f"merge {operation_id}: invalid source food {source_id}")
            continue
        source = cast(dict[str, Any], source_value)
        source_hash_matches: bool = (
            _hash(source) == plan.manifest["record_hashes"][source_id]
        )
        retained_is_original: bool = (
            _hash(retained) == plan.manifest["record_hashes"][retained_id]
        )
        retained_is_prepared: bool = _matches(retained, expected_retained)
        if not source_hash_matches or not (
            retained_is_original or retained_is_prepared
        ):
            blocked.append(
                f"merge {operation_id}: source or retained food changed since export"
            )
            continue
        source_substitution_links: int = substitution_counts.get(source_id, 0)
        if source_substitution_links:
            blocked.append(
                f"merge {operation_id}: source food {source['name']!r} ({source_id}) "
                f"has {source_substitution_links} catalog substitution link(s) defined "
                "on it or pointing to it from another food. Mealie's merge may "
                "lose these links. Resolve them in Mealie, then export and "
                "analyze a new plan"
            )
            continue
        recipe_info = cast(dict[str, str], recipe_capability)
        shopping_info = cast(dict[str, str], shopping_capability)
        source_recipes: int = _recipe_count(client, recipe_info, source_id)
        retained_recipes: int = _recipe_count(client, recipe_info, retained_id)
        source_shopping: int = _shopping_count(client, shopping_info, source_id)
        retained_shopping: int = _shopping_count(client, shopping_info, retained_id)
        pending_merges.append(
            {
                "row": row,
                "source_id": source_id,
                "retained_id": retained_id,
                "source": source,
                "retained": retained,
                "desired_retained": expected_retained,
                "source_recipes": source_recipes,
                "retained_recipes": retained_recipes,
                "source_shopping": source_shopping,
                "retained_shopping": retained_shopping,
            }
        )
        food_reports.append(
            {
                "operation_id": operation_id,
                "action": "merge",
                "source_food_id": source_id,
                "retained_food_id": retained_id,
                "status": "pending",
                "recipe_counts": {
                    source_id: source_recipes,
                    retained_id: retained_recipes,
                },
                "shopping_item_counts": {
                    source_id: source_shopping,
                    retained_id: retained_shopping,
                },
            }
        )
    upgrade_legacy_journal(root, plan)
    approved: int = sum(map(len, plan.groups.values())) + len(plan.resolutions)
    pending_writes: int = len(pending) + len(pending_merges) + len(pending_deletions)
    report: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "plan_hash": plan.plan_hash,
        "review_hash": plan.review_hash,
        "manifest_hash": plan.manifest_hash,
        "openapi_hash": discovery["openapi_hash"],
        "summary": plan.summary,
        "approved_changes": approved,
        "pending_foods": pending_writes,
        "skipped_foods": skipped,
        "foods": food_reports,
        "merge_results": [],
        "delete_results": [],
        "blocked": blocked,
    }
    _report(root, report)
    print(
        f"Preflight: {approved} approved change(s), {pending_writes} pending write(s), "
        f"{skipped} already verified; report: {root / 'reports/preflight.txt'}"
    )
    if (
        dry_run
        or not pending
        and not pending_merges
        and not pending_deletions
        and not recovered_merges
        and not recovered_deletions
    ):
        if blocked:
            raise ValueError("Preflight blocked: " + "; ".join(blocked))
        return 0, skipped
    applied: int = 0
    for row, source_id, retained_id, after_hash in recovered_merges:
        if dry_run:
            continue
        state = journal.get(row["food_id"], FoodJournal())
        before_hash: str = (
            state.latest["after_hash"]
            if state.latest and state.latest["after_hash"]
            else plan.manifest["record_hashes"][row["food_id"]]
        )
        _append(
            root,
            plan,
            row["food_id"],
            [row],
            "verified",
            before_hash,
            after_hash,
        )
    for row, deleted_id in recovered_deletions:
        _append(root, plan, row["food_id"], [row], "verified", "deleted", None)
        cast(list[dict[str, Any]], report["delete_results"]).append(
            {"operation_id": row["operation_id"], "deleted_food_id": deleted_id}
        )
        _report(root, report)
    delete_cap = cast(dict[str, str], delete_capability)
    for deletion in pending_deletions:
        row = cast(dict[str, str], deletion["row"])
        deleted_id = cast(str, deletion["deleted_id"])
        survivor_id = cast(str, deletion["survivor_id"])
        deleted = cast(dict[str, Any], deletion["food"])
        survivor = cast(dict[str, Any], deletion["survivor"])
        deleted_path = template.replace("{item_id}", quote(deleted_id, safe=""))
        survivor_path = template.replace("{item_id}", quote(survivor_id, safe=""))
        current_deleted = client.get_json_or_none(deleted_path)
        current_survivor = _read_food(client, template, survivor_id)
        if (
            not isinstance(current_deleted, dict)
            or _hash(cast(dict[str, Any], current_deleted)) != _hash(deleted)
            or _hash(current_survivor) != _hash(survivor)
        ):
            raise ValueError(
                f"Conflict foods changed after delete preflight for {row['operation_id']}; no further writes"
            )
        recipes = _recipe_count(
            client, cast(dict[str, str], recipe_capability), deleted_id
        )
        shopping_items = _shopping_count(
            client, cast(dict[str, str], shopping_capability), deleted_id
        )
        substitution_links = substitution_reference_counts(
            client, cast(dict[str, Any], food_list_capability)
        ).get(deleted_id, 0)
        if recipes or shopping_items or substitution_links:
            raise ValueError(
                f"Delete blocked for {deleted_id}: references appeared after preflight "
                f"(recipes={recipes}, shopping_items={shopping_items}, "
                f"substitution_links={substitution_links}); no further writes"
            )
        before_hash = _hash(deleted)
        _append(root, plan, row["food_id"], [row], "attempt", before_hash)
        try:
            client.delete(
                delete_cap["path"].replace("{item_id}", quote(deleted_id, safe=""))
            )
        except MealieWriteError as error:
            target_after: object = cast(
                object,
                client.get_json_or_none(deleted_path)
                if error.uncertain
                else current_deleted,
            )
            if target_after is not None:
                _append(
                    root,
                    plan,
                    row["food_id"],
                    [row],
                    "uncertain" if error.uncertain else "failed",
                    before_hash,
                )
                raise RuntimeError(
                    f"Delete failed for operation {row['operation_id']} and food {deleted_id}; no further writes"
                ) from error
        target_after = client.get_json_or_none(deleted_path)
        survivor_after = _read_food(client, template, survivor_id)
        recipes_after = _recipe_count(
            client, cast(dict[str, str], recipe_capability), deleted_id
        )
        shopping_after = _shopping_count(
            client, cast(dict[str, str], shopping_capability), deleted_id
        )
        substitutions_after = substitution_reference_counts(
            client, cast(dict[str, Any], food_list_capability)
        ).get(deleted_id, 0)
        if (
            target_after is not None
            or _hash(survivor_after) != _hash(survivor)
            or recipes_after
            or shopping_after
            or substitutions_after
        ):
            _append(
                root,
                plan,
                row["food_id"],
                [row],
                "uncertain",
                before_hash,
            )
            raise ValueError(
                f"Delete verification failed for operation {row['operation_id']}; inspect Mealie before retrying"
            )
        _append(root, plan, row["food_id"], [row], "verified", before_hash, None)
        cast(list[dict[str, Any]], report["delete_results"]).append(
            {
                "operation_id": row["operation_id"],
                "deleted_food_id": deleted_id,
                "reference_counts_before": {
                    "recipes": recipes,
                    "shopping_items": shopping_items,
                    "substitution_links": substitution_links,
                },
                "reference_counts_after": {
                    "recipes": recipes_after,
                    "shopping_items": shopping_after,
                    "substitution_links": substitutions_after,
                },
            }
        )
        _report(root, report)
        applied += 1
    merge_cap = cast(dict[str, str], merge_capability)
    recipe_cap = cast(dict[str, str], recipe_capability)
    shopping_cap = cast(dict[str, str], shopping_capability)
    for merge in pending_merges:
        row = cast(dict[str, str], merge["row"])
        source_id = cast(str, merge["source_id"])
        retained_id = cast(str, merge["retained_id"])
        source = cast(dict[str, Any], merge["source"])
        retained = cast(dict[str, Any], merge["retained"])
        desired_retained = cast(dict[str, Any], merge["desired_retained"])
        source_path = template.replace("{item_id}", quote(source_id, safe=""))
        retained_path = template.replace("{item_id}", quote(retained_id, safe=""))
        source_now = client.get_json_or_none(source_path)
        retained_now = _read_food(client, template, retained_id)
        if not isinstance(source_now, dict) or _hash(
            cast(dict[str, Any], source_now)
        ) != _hash(source):
            raise ValueError(
                f"Merge foods {source_id} or {retained_id} changed after preflight; no further writes"
            )
        if not (
            _matches(retained_now, retained) or _matches(retained_now, desired_retained)
        ):
            raise ValueError(
                f"Retained food {retained_id} changed after preflight; no further writes"
            )
        current_substitution_links: int = substitution_reference_counts(
            client, cast(dict[str, Any], food_list_capability)
        ).get(source_id, 0)
        if current_substitution_links:
            raise ValueError(
                f"Merge {row['operation_id']} blocked: {current_substitution_links} "
                f"catalog substitution link(s) now involve source food "
                f"{source['name']!r} ({source_id}). Resolve them in Mealie, "
                "then export and analyze a new plan; no further writes"
            )
        before_hash = sha256(json_bytes({"source": source, "retained": retained}))
        _append(root, plan, row["food_id"], [row], "attempt", before_hash)
        if not _matches(retained_now, desired_retained):
            try:
                client.put_json(retained_path, _payload(desired_retained, properties))
                retained_now = _read_food(client, template, retained_id)
            except (MealieWriteError, RuntimeError, ValueError, TypeError) as error:
                _append(root, plan, row["food_id"], [row], "uncertain", before_hash)
                raise RuntimeError(
                    f"Could not preserve source aliases on retained food {retained_id}; check Mealie before retrying"
                ) from error
            if not _matches(retained_now, desired_retained):
                _append(
                    root,
                    plan,
                    row["food_id"],
                    [row],
                    "uncertain",
                    before_hash,
                    _hash(retained_now),
                )
                raise ValueError(
                    f"Retained food {retained_id} differs after alias preservation; no further writes"
                )
        try:
            client.put_json(
                merge_cap["path"], {"fromFood": source_id, "toFood": retained_id}
            )
        except MealieWriteError as error:
            source_after: object = cast(
                object,
                client.get_json_or_none(source_path) if error.uncertain else source_now,
            )
            if source_after is not None:
                _append(
                    root,
                    plan,
                    row["food_id"],
                    [row],
                    "uncertain" if error.uncertain else "failed",
                    before_hash,
                )
                raise RuntimeError(
                    f"Merge failed for operation {row['operation_id']} ({source_id} -> {retained_id}); check Mealie before retrying"
                ) from error
        source_after = client.get_json_or_none(source_path)
        retained_after = _read_food(client, template, retained_id)
        source_recipe_count = _recipe_count(client, recipe_cap, source_id)
        retained_recipe_count = _recipe_count(client, recipe_cap, retained_id)
        source_shopping_count = _shopping_count(client, shopping_cap, source_id)
        retained_shopping_count = _shopping_count(client, shopping_cap, retained_id)
        expected_recipe_count = merge["source_recipes"] + merge["retained_recipes"]
        expected_shopping_count = merge["source_shopping"] + merge["retained_shopping"]
        if (
            source_after is not None
            or not _matches(retained_after, desired_retained)
            or source_recipe_count != 0
            or retained_recipe_count != expected_recipe_count
            or source_shopping_count != 0
            or retained_shopping_count != expected_shopping_count
        ):
            _append(
                root,
                plan,
                row["food_id"],
                [row],
                "uncertain",
                before_hash,
                _hash(retained_after),
            )
            raise ValueError(
                f"Merge verification failed for operation {row['operation_id']}; inspect food and reference counts before retrying"
            )
        _append(
            root,
            plan,
            row["food_id"],
            [row],
            "verified",
            before_hash,
            _hash(retained_after),
        )
        cast(list[dict[str, Any]], report["merge_results"]).append(
            {
                "operation_id": row["operation_id"],
                "source_food_id": source_id,
                "retained_food_id": retained_id,
                "recipe_counts_before": {
                    source_id: merge["source_recipes"],
                    retained_id: merge["retained_recipes"],
                },
                "recipe_counts_after": {
                    source_id: source_recipe_count,
                    retained_id: retained_recipe_count,
                },
                "shopping_counts_before": {
                    source_id: merge["source_shopping"],
                    retained_id: merge["retained_shopping"],
                },
                "shopping_counts_after": {
                    source_id: source_shopping_count,
                    retained_id: retained_shopping_count,
                },
            }
        )
        _report(root, report)
        applied += 1
    for food_id, (before, rows) in pending.items():
        if (
            sha256((root / "plan.json").read_bytes()) != plan.plan_hash
            or sha256((root / "review.csv").read_bytes()) != plan.review_hash
            or sha256((root / "manifest.json").read_bytes()) != plan.manifest_hash
        ):
            raise ValueError("Plan, review, or manifest changed during apply")
        current = _read_food(client, template, food_id)
        before_hash: str = _hash(before)
        if _hash(current) != before_hash:
            raise ValueError(
                f"Food {food_id} changed after preflight; no further writes"
            )
        desired = _desired(current, rows)
        if _matches(current, desired):
            _append(root, plan, food_id, rows, "verified", before_hash, before_hash)
            skipped += 1
            continue
        _append(root, plan, food_id, rows, "attempt", before_hash)
        path: str = template.replace("{item_id}", quote(food_id, safe=""))
        try:
            client.put_json(path, _payload(desired, properties))
        except MealieWriteError as error:
            if error.uncertain:
                try:
                    actual = _read_food(client, template, food_id)
                except (RuntimeError, ValueError, TypeError):
                    actual = None
                if actual is not None and _matches(actual, desired):
                    _append(
                        root,
                        plan,
                        food_id,
                        rows,
                        "verified",
                        before_hash,
                        _hash(actual),
                    )
                    applied += 1
                    continue
                _append(root, plan, food_id, rows, "uncertain", before_hash)
            else:
                _append(root, plan, food_id, rows, "failed", before_hash)
            ids: str = ", ".join(row["operation_id"] for row in rows)
            if error.status in {401, 403}:
                raise RuntimeError(
                    f"Mealie denied write permission for food {food_id}, operations {ids}"
                ) from error
            raise RuntimeError(
                f"Write failed or has uncertain outcome for food {food_id}, operations {ids}: {error}"
            ) from error
        try:
            actual = _read_food(client, template, food_id)
        except (RuntimeError, ValueError, TypeError) as error:
            _append(root, plan, food_id, rows, "uncertain", before_hash)
            raise RuntimeError(
                f"Cannot verify write for food {food_id}; stop and resume after checking Mealie"
            ) from error
        if not _matches(actual, desired):
            _append(root, plan, food_id, rows, "uncertain", before_hash, _hash(actual))
            raise ValueError(f"Food {food_id} differs after update; no further writes")
        _append(root, plan, food_id, rows, "verified", before_hash, _hash(actual))
        applied += 1
        time.sleep(0.1)
    upgrade_legacy_journal(root, plan)
    if blocked:
        raise ValueError(
            f"Applied {applied} independent food(s); preflight blocked: "
            + "; ".join(blocked)
        )
    return applied, skipped
