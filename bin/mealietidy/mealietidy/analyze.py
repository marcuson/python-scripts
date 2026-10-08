"""Build a review plan from verified foods and optional suggestions."""

import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from difflib import SequenceMatcher
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

from mealietidy.export import json_bytes, sha256, write_new
from mealietidy.provider import SuggestionProvider, create_provider

CSV_FIELDS: tuple[str, ...] = (
    "operation_id",
    "food_id",
    "action",
    "current_value",
    "proposed_value",
    "language",
    "duplicate_target_id",
    "evidence",
    "reason",
    "risk",
    "decision",
    "reviewer_note",
)


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def read_verified(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = cast(dict[str, Any], json.loads(manifest_bytes))
    for relative, expected in manifest["files"].items():
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError("Export manifest contains an unsafe relative path")
        if sha256((root / relative).read_bytes()) != expected:
            raise ValueError(f"Export checksum mismatch: {relative}")
    snapshot_bytes = (root / "snapshot/foods.json").read_bytes()
    if sha256(snapshot_bytes) != manifest["snapshot_hash"]:
        raise ValueError("Snapshot checksum mismatch")
    food_values: object = json.loads(snapshot_bytes)["foods"]
    if not isinstance(food_values, list):
        raise TypeError("Export food count does not match its manifest")
    food_list = cast(list[object], food_values)
    if len(food_list) != manifest["record_count"]:
        raise ValueError("Export food count does not match its manifest")
    foods: list[dict[str, Any]] = []
    for value in food_list:
        if not isinstance(value, dict):
            raise TypeError("Snapshot contains an invalid food")
        food = cast(dict[str, Any], value)
        if not isinstance(food.get("id"), str) or not isinstance(food.get("name"), str):
            raise TypeError("Snapshot contains an invalid food")
        if sha256(json_bytes(food)) != manifest["record_hashes"].get(food["id"]):
            raise ValueError(f"Food checksum mismatch: {food['id']}")
        foods.append(food)
    return manifest, foods


def analyze_plan(
    root: Path,
    provider_name: str = "none",
    provider_args: Mapping[str, str] | None = None,
    refine: bool = False,
    confirmed: bool = False,
) -> tuple[int, int]:
    if (root / "plan.json").exists() or (root / "review.csv").exists():
        if not (root / "plan.json").is_file() or not (root / "review.csv").is_file():
            raise FileExistsError(
                "Plan and review must both exist for another analysis"
            )
        if not refine and not confirmed:
            raise FileExistsError(
                "Plan already exists; use --refine or confirm a new analysis"
            )
        return _reanalyze_plan(root, provider_name, provider_args, refine)
    if refine:
        raise FileNotFoundError("--refine requires an existing plan and review.csv")
    print(f"Verifying exported catalog in {root}...", file=sys.stderr, flush=True)
    manifest, foods = read_verified(root)
    print(
        f"Preparing review operations for {len(foods)} foods...",
        file=sys.stderr,
        flush=True,
    )
    arguments: Mapping[str, str] = provider_args or {}
    provider: SuggestionProvider = create_provider(provider_name, root, arguments)
    operations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    seen_terms: dict[str, list[tuple[str, str]]] = {}

    def add_operation(
        food: dict[str, Any], action: str, current: str, reason: str, target: str = ""
    ) -> None:
        operations.append(
            {
                "operation_id": f"op-{len(operations) + 1:06d}",
                "food_id": food["id"],
                "action": action,
                "current_value": current,
                "proposed_value": "",
                "language": "",
                "duplicate_target_id": target,
                "evidence": f"catalog analysis; {provider.name} provider",
                "reason": reason,
                "risk": "high",
                "classification": "needs_review",
                "decision": "pending",
                "reviewer_note": "",
                "record_hash": manifest["record_hashes"][food["id"]],
            }
        )

    ordered_foods: list[dict[str, Any]] = sorted(foods, key=lambda item: item["id"])
    for index, food in enumerate(ordered_foods, start=1):
        if index == 1 or index % 25 == 0 or index == len(foods):
            print(
                f"Preparing food operations: {index}/{len(foods)}",
                file=sys.stderr,
                flush=True,
            )
        food_id: str = food["id"]
        name: str = food["name"]
        normalized_name = normalize(name)
        names[food_id] = normalized_name
        add_operation(
            food,
            "rename",
            name,
            "Check the canonical Italian name manually; no translation was generated",
        )
        plural = food.get("pluralName")
        if plural is not None and not isinstance(plural, str):
            raise ValueError(f"Invalid pluralName for food {food_id}")
        add_operation(
            food, "set_plural", plural or "", "Check the full Italian plural manually"
        )
        if not plural:
            findings.append(
                {"kind": "missing_plural", "food_ids": [food_id], "value": name}
            )
        add_operation(
            food, "add_alias", "", "Enter an alias only if it denotes this exact food"
        )
        aliases_value: object = food.get("aliases", [])
        if not isinstance(aliases_value, list):
            raise TypeError(f"Invalid aliases for food {food_id}")
        aliases = cast(list[Any], aliases_value)
        local_terms: set[str] = set()
        terms: list[tuple[str, object]] = [("name", name)]
        for alias in aliases:
            if not isinstance(alias, dict):
                raise TypeError(f"Invalid alias for food {food_id}")
            terms.append(("alias", cast(dict[str, Any], alias).get("name")))
        for kind, term in terms:
            if not isinstance(term, str):
                raise TypeError(f"Invalid alias for food {food_id}")
            normalized = normalize(term)
            if not normalized:
                findings.append(
                    {"kind": "empty_term", "food_ids": [food_id], "value": term}
                )
                continue
            if normalized in local_terms:
                findings.append(
                    {"kind": "repeated_term", "food_ids": [food_id], "value": term}
                )
            local_terms.add(normalized)
            seen_terms.setdefault(normalized, []).append((food_id, kind))

    for term, sources in sorted(seen_terms.items()):
        involved = sorted({food_id for food_id, _ in sources})
        if len(involved) > 1:
            findings.append(
                {
                    "kind": "cross_food_collision",
                    "food_ids": involved,
                    "value": term,
                    "sources": sources,
                }
            )

    by_id = {food["id"]: food for food in foods}
    ids = sorted(by_id)
    print(
        f"Checking duplicate names across {len(ids)} foods...",
        file=sys.stderr,
        flush=True,
    )
    for index, source_id in enumerate(ids):
        if index == 0 or (index + 1) % 25 == 0 or index + 1 == len(ids):
            print(
                f"Checking duplicate names: {index + 1}/{len(ids)}",
                file=sys.stderr,
                flush=True,
            )
        for target_id in ids[index + 1 :]:
            left, right = names[source_id], names[target_id]
            if not left or not right:
                continue
            equal = left == right
            similar = (
                not equal
                and min(len(left), len(right)) >= 5
                and SequenceMatcher(None, left, right).ratio() >= 0.92
            )
            if equal or similar:
                reason = (
                    "Equal normalized names"
                    if equal
                    else "Similar spelling; culinary equivalence is unverified"
                )
                add_operation(
                    by_id[target_id],
                    "merge_candidate",
                    by_id[target_id]["name"],
                    reason,
                    source_id,
                )
                findings.append(
                    {
                        "kind": "possible_duplicate",
                        "food_ids": [source_id, target_id],
                        "signal": "normalized_equal" if equal else "lexical_similarity",
                    }
                )

    catalog: list[dict[str, Any]] = [
        {
            "id": food["id"],
            "name": food["name"],
            "pluralName": food.get("pluralName"),
            "aliases": [alias["name"] for alias in food.get("aliases", [])],
        }
        for food in foods
    ]
    print(
        f"Requesting suggestions from {provider.name} for {len(catalog)} foods...",
        file=sys.stderr,
        flush=True,
    )
    try:
        suggestions, failed_batches = provider.suggest(
            catalog, {"normalization": "NFC, whitespace, casefold"}
        )
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(
            f"mealietidy: warning: {provider.name} suggestions unavailable: {error}",
            file=sys.stderr,
        )
        suggestions, failed_batches = [], [[food["id"] for food in foods]]
    findings.extend(
        {"kind": "provider_error", "food_ids": food_ids} for food_ids in failed_batches
    )
    occupied: dict[str, set[str]] = {}
    for food in foods:
        for term in [
            food["name"],
            *(alias["name"] for alias in food.get("aliases", [])),
        ]:
            occupied.setdefault(normalize(term), set()).add(food["id"])
    by_action: dict[tuple[str, str], dict[str, Any]] = {
        (operation["food_id"], operation["action"]): operation
        for operation in operations
    }
    for suggestion in suggestions:
        food_id: str = suggestion["food_id"]
        action: str = suggestion["action"]
        value: str = suggestion["value"]
        operation: dict[str, Any] = by_action[(food_id, action)]
        existing: str = operation["current_value"]
        collision: bool = bool(occupied.get(normalize(value), set()) - {food_id})
        if normalize(value) == normalize(existing) or collision:
            findings.append(
                {"kind": "rejected_suggestion", "food_ids": [food_id], "action": action}
            )
            continue
        if action == "add_alias" and normalize(value) in {
            normalize(term)
            for term in [
                by_id[food_id]["name"],
                *(alias["name"] for alias in by_id[food_id].get("aliases", [])),
            ]
        }:
            continue
        operation["proposed_value"] = value
        operation["language"] = suggestion["language"]
        operation["evidence"] = f"{provider.name} suggestion"
        operation["reason"] = suggestion["rationale"]
        operation["uncertainty"] = suggestion["uncertainty"]
        operation["related_candidate_ids"] = suggestion["related_candidate_ids"]
        if action in {"rename", "add_alias"}:
            occupied.setdefault(normalize(value), set()).add(food_id)

    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(
        {field: operation[field] for field in CSV_FIELDS} for operation in operations
    )
    plan: dict[str, object] = {
        "schema_version": "1.5.0",
        "tool_version": version("mealietidy"),
        "provider": provider.name,
        "provider_info": dict(provider.metadata),
        "normalization": ["Unicode NFC", "collapse whitespace", "casefold"],
        "created_at": datetime.now(UTC).isoformat(),
        "exported_at": manifest["exported_at"],
        "snapshot_hash": manifest["snapshot_hash"],
        "openapi_hash": manifest["openapi_hash"],
        "source_files": manifest["files"],
        "manifest_hash": hashlib.sha256(
            (root / "manifest.json").read_bytes()
        ).hexdigest(),
        "operations": operations,
        "findings": findings,
        "summary": {
            "by_risk": {"high": len(operations)},
            "by_decision": {"pending": len(operations)},
            "by_action": {
                action: sum(operation["action"] == action for operation in operations)
                for action in ("rename", "set_plural", "add_alias", "merge_candidate")
            },
        },
    }
    print("Writing plan.json and review.csv...", file=sys.stderr, flush=True)
    write_new(root / "plan.json", json_bytes(plan))
    write_new(root / "review.csv", csv_buffer.getvalue().encode("utf-8"))
    return len(operations), len(findings)


def _reanalyze_plan(
    root: Path,
    provider_name: str,
    provider_args: Mapping[str, str] | None,
    refine: bool,
) -> tuple[int, int]:
    from mealietidy.journal import upgrade_legacy_journal
    from mealietidy.review import load_review

    reviewed = load_review(root)
    upgrade_legacy_journal(root, reviewed, require_complete=True)
    original_plan: bytes = (root / "plan.json").read_bytes()
    original_review: bytes = (root / "review.csv").read_bytes()
    plan: dict[str, Any] = cast(dict[str, Any], json.loads(original_plan))
    reader = csv.DictReader(io.StringIO(original_review.decode("utf-8"), newline=""))
    rows: list[dict[str, str]] = [cast(dict[str, str], row) for row in reader]
    by_row: dict[str, dict[str, str]] = {row["operation_id"]: row for row in rows}
    eligible: dict[tuple[str, str], dict[str, Any]] = {}
    candidates: dict[str, list[dict[str, str]]] = {}
    for operation in plan["operations"]:
        row = by_row[operation["operation_id"]]
        if (
            row["decision"] != "pending"
            or row["proposed_value"] != operation["proposed_value"]
            or row["reviewer_note"]
            or operation["action"] == "merge_candidate"
        ):
            continue
        food_id: str = operation["food_id"]
        eligible[(food_id, operation["action"])] = operation
        if refine:
            candidates.setdefault(food_id, []).append(
                {
                    "action": operation["action"],
                    "value": row["proposed_value"],
                    "language": row["language"],
                    "reason": row["reason"],
                }
            )
    catalog: list[dict[str, Any]] = []
    for food_id, food in reviewed.foods.items():
        actions: list[str] = [
            action for candidate_id, action in eligible if candidate_id == food_id
        ]
        if not actions:
            continue
        item: dict[str, Any] = {
            "id": food_id,
            "name": food["name"],
            "pluralName": food.get("pluralName"),
            "aliases": [alias["name"] for alias in food.get("aliases", [])],
            "allowed_actions": actions,
        }
        if refine:
            item["pending_suggestions"] = candidates.get(food_id, [])
        catalog.append(item)
    if not catalog:
        print("No pending foods need suggestions.", file=sys.stderr, flush=True)
        return 0, len(plan["findings"])
    provider: SuggestionProvider = create_provider(
        provider_name, root, provider_args or {}
    )
    print(
        f"Refining pending suggestions with {provider.name} for {len(catalog)} foods...",
        file=sys.stderr,
        flush=True,
    )
    try:
        suggestions, failed_batches = provider.suggest(
            catalog, {"normalization": "NFC, whitespace, casefold", "refine": refine}
        )
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(
            f"mealietidy: warning: {provider.name} suggestions unavailable: {error}",
            file=sys.stderr,
        )
        suggestions, failed_batches = [], [[item["id"] for item in catalog]]
    print("Updating plan.json and review.csv...", file=sys.stderr, flush=True)
    findings: list[dict[str, Any]] = plan["findings"]
    findings.extend(
        {"kind": "provider_error", "food_ids": food_ids} for food_ids in failed_batches
    )
    occupied: dict[str, set[str]] = {}
    for food_id, food in reviewed.foods.items():
        for value in [
            food["name"],
            *(alias["name"] for alias in food.get("aliases", [])),
        ]:
            occupied.setdefault(normalize(value), set()).add(food_id)
    for row in rows:
        if row["action"] in {"rename", "add_alias"} and row["proposed_value"]:
            occupied.setdefault(normalize(row["proposed_value"]), set()).add(
                row["food_id"]
            )
    changed: int = 0
    for suggestion in suggestions:
        food_id: str = suggestion["food_id"]
        action: str = suggestion["action"]
        operation = eligible.get((food_id, action))
        if operation is None:
            findings.append(
                {"kind": "rejected_suggestion", "food_ids": [food_id], "action": action}
            )
            continue
        value: str = suggestion["value"]
        if (
            normalize(value) == normalize(operation["current_value"])
            or occupied.get(normalize(value), set()) - {food_id}
            or action == "add_alias"
            and normalize(value)
            in {
                normalize(term)
                for term in [
                    reviewed.foods[food_id]["name"],
                    *(
                        alias["name"]
                        for alias in reviewed.foods[food_id].get("aliases", [])
                    ),
                ]
            }
        ):
            findings.append(
                {"kind": "rejected_suggestion", "food_ids": [food_id], "action": action}
            )
            continue
        operation["proposed_value"] = value
        operation["language"] = suggestion["language"]
        operation["evidence"] = f"{provider.name} suggestion"
        operation["reason"] = suggestion["rationale"]
        operation["uncertainty"] = suggestion["uncertainty"]
        operation["related_candidate_ids"] = suggestion["related_candidate_ids"]
        row = by_row[operation["operation_id"]]
        for field in ("proposed_value", "language", "evidence", "reason"):
            row[field] = operation[field]
        if action in {"rename", "add_alias"}:
            occupied.setdefault(normalize(value), set()).add(food_id)
        changed += 1
    plan["provider"] = provider.name
    plan["provider_info"] = dict(provider.metadata)
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    new_plan: bytes = json_bytes(plan)
    new_review: bytes = csv_buffer.getvalue().encode("utf-8")
    if (root / "plan.json").read_bytes() != original_plan or (
        root / "review.csv"
    ).read_bytes() != original_review:
        raise RuntimeError("Plan or review changed during analysis; retry")
    with tempfile.TemporaryDirectory(prefix=".analysis-", dir=root) as temporary:
        staged: Path = Path(temporary)
        (staged / "plan.json").write_bytes(new_plan)
        (staged / "review.csv").write_bytes(new_review)
        (staged / "previous-plan.json").write_bytes(original_plan)
        os.chmod(staged / "plan.json", 0o600)
        os.chmod(staged / "review.csv", 0o600)
        os.chmod(staged / "previous-plan.json", 0o600)
        os.replace(staged / "plan.json", root / "plan.json")
        try:
            os.replace(staged / "review.csv", root / "review.csv")
        except OSError:
            os.replace(staged / "previous-plan.json", root / "plan.json")
            raise
    return changed, len(findings)
