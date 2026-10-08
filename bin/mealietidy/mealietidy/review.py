"""Validate an edited review against its immutable export and plan."""

import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from mealietidy.analyze import CSV_FIELDS, normalize, read_verified
from mealietidy.export import sha256

SCHEMA_VERSION: tuple[int, int, int] = (1, 5, 0)
EDITABLE: frozenset[str] = frozenset({"proposed_value", "decision", "reviewer_note"})
ACTIONS: frozenset[str] = frozenset(
    {"rename", "set_plural", "add_alias", "merge_candidate"}
)


class ReviewConflictError(ValueError):
    def __init__(self, operation_id: str, food_id: str, others: set[str]) -> None:
        self.operation_id: str = operation_id
        self.food_ids: tuple[str, ...] = tuple(
            dict.fromkeys((food_id, *sorted(others)))
        )
        super().__init__(
            f"Cross-food collision for {operation_id} on food {food_id}: {sorted(others)}"
        )


@dataclass(frozen=True)
class ReviewedPlan:
    root: Path
    manifest: dict[str, Any]
    foods: dict[str, dict[str, Any]]
    groups: dict[str, list[dict[str, str]]]
    resolutions: list[dict[str, str]]
    rows: list[dict[str, str]]
    summary: dict[str, dict[str, int]]
    plan_hash: str
    review_hash: str
    manifest_hash: str


def _schema_version(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Plan has an invalid schema_version")
    major, minor, patch = (int(part) for part in value.split("."))
    found: tuple[int, int, int] = (major, minor, patch)
    if major != SCHEMA_VERSION[0]:
        raise ValueError(f"Unsupported plan schema major version: {value}")
    if found > SCHEMA_VERSION:
        raise ValueError(f"Plan schema {value} is newer than this reader supports")
    if found < SCHEMA_VERSION:
        print(
            f"mealietidy: warning: older plan schema {value}; migration is not yet available",
            file=sys.stderr,
        )


def _validate_value(row: dict[str, str]) -> None:
    value: str = row["proposed_value"]
    if row["decision"] not in {"pending", "approve", "reject"}:
        raise ValueError(
            f"Invalid decision for {row['operation_id']}: {row['decision']!r}"
        )
    if value.startswith("resolve:"):
        parts: list[str] = value.split(":")
        if (
            row["action"] not in {"rename", "add_alias"}
            or len(parts) != 4
            or parts[1] not in {"merge", "delete"}
            or not parts[2]
            or not parts[3]
        ):
            raise ValueError(f"Invalid conflict resolution for {row['operation_id']}")
    if (
        row["decision"] == "approve"
        and row["action"] != "merge_candidate"
        and not normalize(value)
    ):
        raise ValueError(
            f"Approved operation {row['operation_id']} has an empty proposed_value"
        )
    if (
        row["decision"] == "approve"
        and row["action"] == "merge_candidate"
        and not value
    ):
        raise ValueError(
            f"Approved merge_candidate {row['operation_id']} has no selected resolution"
        )
    if row["action"] == "merge_candidate" and value:
        mode, separator, selected_id = value.partition(":")
        participants: set[str] = {row["food_id"], row["duplicate_target_id"]}
        if (
            not separator
            or mode not in {"merge", "delete"}
            or selected_id not in participants
        ):
            raise ValueError(
                f"Invalid conflict resolution for {row['operation_id']}; choose a merge or food to delete"
            )
    if value and (len(value) > 255 or any(ord(char) < 32 for char in value)):
        raise ValueError(
            f"Invalid proposed_value for {row['operation_id']}: maximum 255 characters, no controls"
        )
    if value and not normalize(value):
        raise ValueError(f"Invalid blank proposed_value for {row['operation_id']}")


def _conflict_resolution(
    row: dict[str, str], foods: dict[str, dict[str, Any]]
) -> dict[str, str] | None:
    mode, separator, remainder = row["proposed_value"].partition(":")
    if mode != "resolve":
        return None
    mode, separator, remainder = remainder.partition(":")
    other_id, separator, selected_id = remainder.partition(":")
    participants: set[str] = {row["food_id"], other_id}
    if (
        not separator
        or mode not in {"merge", "delete"}
        or other_id not in foods
        or other_id == row["food_id"]
        or selected_id not in participants
    ):
        raise ValueError(f"Invalid conflict resolution for {row['operation_id']}")
    return {
        **row,
        "action": "merge_candidate",
        "duplicate_target_id": other_id,
        "proposed_value": f"{mode}:{selected_id}",
    }


def _validate_baseline(
    operation: dict[str, Any], food: dict[str, Any], hashes: dict[str, Any]
) -> None:
    food_id: str = operation["food_id"]
    action: str = operation["action"]
    if action not in ACTIONS:
        raise ValueError(f"Unknown plan action: {action}")
    if operation.get("record_hash") != hashes.get(food_id):
        raise ValueError(f"Plan record hash does not match export for food {food_id}")
    current: object = (
        food["name"]
        if action in {"rename", "merge_candidate"}
        else food.get("pluralName") or ""
        if action == "set_plural"
        else ""
    )
    if operation["current_value"] != current:
        raise ValueError(f"Plan current value does not match export for food {food_id}")
    if action == "merge_candidate" and operation["duplicate_target_id"] not in hashes:
        raise ValueError(f"Unknown merge target for food {food_id}")


def _check_collisions(
    groups: dict[str, list[dict[str, str]]], foods: dict[str, dict[str, Any]]
) -> None:
    terms: dict[str, set[str]] = {}
    for food_id, food in foods.items():
        for term in [
            food["name"],
            *(alias["name"] for alias in food.get("aliases", [])),
        ]:
            terms.setdefault(normalize(term), set()).add(food_id)
    for food_id, rows in groups.items():
        for row in rows:
            if row["action"] not in {"rename", "add_alias"}:
                continue
            value: str = normalize(row["proposed_value"])
            others: set[str] = terms.get(value, set()) - {food_id}
            if others:
                raise ReviewConflictError(row["operation_id"], food_id, others)
            terms.setdefault(value, set()).add(food_id)


def load_review(root: Path, review_bytes: bytes | None = None) -> ReviewedPlan:
    manifest, food_list = read_verified(root)
    foods: dict[str, dict[str, Any]] = {food["id"]: food for food in food_list}
    manifest_bytes: bytes = (root / "manifest.json").read_bytes()
    plan_bytes: bytes = (root / "plan.json").read_bytes()
    if review_bytes is None:
        review_bytes = (root / "review.csv").read_bytes()
    value: object = json.loads(plan_bytes)
    if not isinstance(value, dict):
        raise TypeError("plan.json must contain an object")
    plan = cast(dict[str, Any], value)
    _schema_version(plan.get("schema_version"))
    if (
        plan.get("manifest_hash") != sha256(manifest_bytes)
        or plan.get("snapshot_hash") != manifest["snapshot_hash"]
        or plan.get("openapi_hash") != manifest["openapi_hash"]
        or plan.get("source_files") != manifest["files"]
    ):
        raise ValueError("Plan does not match the verified export manifest")
    operations_value: object = plan.get("operations")
    if not isinstance(operations_value, list):
        raise TypeError("Plan operations must be a list")
    baseline: dict[str, dict[str, Any]] = {}
    hashes: dict[str, Any] = manifest["record_hashes"]
    for raw in cast(list[object], operations_value):
        if not isinstance(raw, dict):
            raise TypeError("Plan contains an invalid operation")
        operation = cast(dict[str, Any], raw)
        if any(not isinstance(operation.get(field), str) for field in CSV_FIELDS):
            raise TypeError("Plan operation has an invalid review field")
        operation_id: str = operation["operation_id"]
        food_id: str = operation["food_id"]
        if not operation_id or operation_id in baseline or food_id not in foods:
            raise ValueError(f"Invalid or duplicate plan operation: {operation_id}")
        _validate_baseline(operation, foods[food_id], hashes)
        baseline[operation_id] = operation
    try:
        text: str = review_bytes.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if reader.fieldnames != list(CSV_FIELDS):
            raise ValueError("review.csv header differs from the plan schema")
        rows: list[dict[str, str]] = []
        for raw_row in reader:
            if None in raw_row or any(value is None for value in raw_row.values()):
                raise ValueError("review.csv contains an incomplete or extra column")
            rows.append(cast(dict[str, str], raw_row))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError("review.csv is not valid UTF-8 CSV") from error
    if len(rows) != len(baseline):
        raise ValueError("review.csv row count differs from plan.json")
    groups: dict[str, list[dict[str, str]]] = {}
    resolutions: list[dict[str, str]] = []
    effective_rows: list[dict[str, str]] = []
    seen: set[str] = set()
    summary: dict[str, dict[str, int]] = {
        "by_action": {},
        "by_decision": {},
        "by_risk": {},
    }
    for row in rows:
        operation_id = row["operation_id"]
        if operation_id in seen or operation_id not in baseline:
            raise ValueError(f"Added or duplicate review row: {operation_id}")
        seen.add(operation_id)
        source: dict[str, Any] = baseline[operation_id]
        for field in CSV_FIELDS:
            if field not in EDITABLE and row[field] != source[field]:
                raise ValueError(
                    f"Immutable review field {field} changed for {operation_id}"
                )
        _validate_value(row)
        resolution: dict[str, str] | None = _conflict_resolution(row, foods)
        effective_rows.append(resolution or row)
        effective_risk: str = (
            "high" if row["proposed_value"] != source["proposed_value"] else row["risk"]
        )
        if row["proposed_value"] != source["proposed_value"] and row["risk"] != "high":
            raise ValueError(
                f"Edited value exceeds baseline risk for {operation_id}; regenerate the plan"
            )
        for kind, label in (
            (
                "by_action",
                "merge_candidate" if resolution is not None else row["action"],
            ),
            ("by_decision", row["decision"]),
            ("by_risk", effective_risk),
        ):
            summary[kind][label] = summary[kind].get(label, 0) + 1
        if row["decision"] == "approve":
            if row["action"] == "merge_candidate":
                resolutions.append(row)
            elif resolution is not None:
                resolutions.append(resolution)
            else:
                groups.setdefault(row["food_id"], []).append(row)
    if seen != set(baseline):
        raise ValueError("review.csv is missing plan operations")
    for food_id, group in groups.items():
        for action in ("rename", "set_plural"):
            if sum(row["action"] == action for row in group) > 1:
                raise ValueError(f"Conflicting {action} operations for food {food_id}")
    _check_collisions(groups, foods)
    return ReviewedPlan(
        root=root,
        manifest=manifest,
        foods=foods,
        groups=groups,
        resolutions=resolutions,
        rows=effective_rows,
        summary=summary,
        plan_hash=sha256(plan_bytes),
        review_hash=sha256(review_bytes),
        manifest_hash=sha256(manifest_bytes),
    )
