"""Read and append verified operation history for local plans."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, cast


class JournalEvent(TypedDict):
    status: str
    operation_ids: list[str]
    after_hash: str | None


class JournalPlan(Protocol):
    @property
    def rows(self) -> list[dict[str, str]]: ...

    @property
    def foods(self) -> Mapping[str, object]: ...

    @property
    def plan_hash(self) -> str: ...

    @property
    def review_hash(self) -> str: ...

    @property
    def manifest_hash(self) -> str: ...


@dataclass
class FoodJournal:
    latest: JournalEvent | None = None
    verified_hash: str | None = None
    applied: dict[str, dict[str, str]] = field(
        default_factory=dict[str, dict[str, str]]
    )
    legacy: bool = False


def read_journal(root: Path, plan: JournalPlan) -> dict[str, FoodJournal]:
    path: Path = root / "journal.jsonl"
    states: dict[str, FoodJournal] = {}
    if not path.exists():
        return states
    lines: list[str] = path.read_text(encoding="utf-8").splitlines()
    rows: dict[str, dict[str, str]] = {row["operation_id"]: row for row in plan.rows}
    parsed: list[dict[str, object]] = []
    upgraded: dict[str, set[str]] = {}
    latest_line: dict[str, int] = {}
    for number, line in enumerate(lines, 1):
        try:
            candidate: object = cast(object, json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid journal entry on line {number}") from error
        if not isinstance(candidate, dict):
            raise TypeError(f"Invalid journal entry on line {number}")
        item: dict[str, object] = cast(dict[str, object], candidate)
        parsed.append(item)
        food_id_value: object = item.get("food_id")
        if isinstance(food_id_value, str):
            latest_line[food_id_value] = number
        if "operations" in item and item.get("status") == "verified":
            ids_value: object = item.get("operation_ids")
            if isinstance(food_id_value, str) and isinstance(ids_value, list):
                upgraded.setdefault(food_id_value, set()).update(
                    op for op in cast(list[object], ids_value) if isinstance(op, str)
                )
    for number, item in enumerate(parsed, 1):
        food_id: object = item.get("food_id")
        status: object = item.get("status")
        if (
            not isinstance(food_id, str)
            or food_id not in plan.foods
            or status
            not in {
                "attempt",
                "verified",
                "failed",
                "uncertain",
            }
        ):
            raise ValueError(f"Invalid journal entry on line {number}")
        if "operations" not in item:
            if (
                item.get("plan_hash") != plan.plan_hash
                or item.get("review_hash") != plan.review_hash
            ):
                # shortcut: legacy entries lack values; migrate before editing their review.
                ids_for_entry: object = item.get("operation_ids")
                if (
                    isinstance(ids_for_entry, list)
                    and all(
                        isinstance(op, str) for op in cast(list[object], ids_for_entry)
                    )
                    and set(cast(list[str], ids_for_entry))
                    <= upgraded.get(food_id, set())
                ):
                    continue
                raise ValueError(
                    "Legacy journal needs the original unchanged plan and review before further edits"
                )
            ids: object = item.get("operation_ids")
            if not isinstance(ids, list) or any(
                not isinstance(op, str) or op not in rows
                for op in cast(list[object], ids)
            ):
                raise ValueError(f"Invalid legacy journal operations on line {number}")
            operations: list[dict[str, str]] = [
                {
                    "operation_id": op,
                    "action": rows[op]["action"],
                    "proposed_value": rows[op]["proposed_value"],
                }
                for op in cast(list[str], ids)
            ]
        else:
            if item.get("manifest_hash") != plan.manifest_hash:
                raise ValueError(f"Journal export mismatch on line {number}")
            raw_operations: object = item["operations"]
            if not isinstance(raw_operations, list):
                raise ValueError(f"Invalid journal operations on line {number}")
            operations = []
            for raw_operation in cast(list[object], raw_operations):
                if not isinstance(raw_operation, dict):
                    raise TypeError(f"Invalid journal operations on line {number}")
                operation = cast(dict[str, object], raw_operation)
                if any(
                    not isinstance(operation.get(key), str)
                    for key in ("operation_id", "action", "proposed_value")
                ):
                    raise ValueError(f"Invalid journal operations on line {number}")
                operations.append(cast(dict[str, str], operation))
        if item.get("operation_ids") != [op["operation_id"] for op in operations]:
            raise ValueError(f"Journal operation IDs differ on line {number}")
        after_hash: object = item.get("after_hash")
        if after_hash is not None and not isinstance(after_hash, str):
            raise TypeError(f"Invalid journal after-hash on line {number}")
        deletion_verified: bool = (
            status == "verified"
            and after_hash is None
            and bool(operations)
            and all(
                operation["action"] == "merge_candidate"
                and operation["proposed_value"].startswith("delete:")
                for operation in operations
            )
        )
        if status == "verified" and not (
            isinstance(after_hash, str) or deletion_verified
        ):
            raise ValueError(
                f"Verified journal entry lacks after-hash on line {number}"
            )
        for operation in operations:
            op: str = operation["operation_id"]
            row: dict[str, str] | None = rows.get(op)
            if (
                status == "verified"
                or (
                    status in {"attempt", "uncertain"}
                    and number == latest_line[food_id]
                )
            ) and (
                row is None
                or row["food_id"] != food_id
                or row["decision"] != "approve"
                or row["action"] != operation["action"]
                or row["proposed_value"] != operation["proposed_value"]
            ):
                raise ValueError(
                    f"Applied operation {op} changed since journal verification"
                )
        state: FoodJournal = states.setdefault(food_id, FoodJournal())
        state.latest = {
            "status": cast(str, status),
            "operation_ids": [operation["operation_id"] for operation in operations],
            "after_hash": after_hash,
        }
        if "operations" not in item:
            state.legacy = True
        if status == "verified":
            if isinstance(after_hash, str):
                state.verified_hash = after_hash
            if "operations" in item and set(state.applied) <= {
                operation["operation_id"] for operation in operations
            }:
                state.legacy = False
            for operation in operations:
                state.applied[operation["operation_id"]] = operation
    return states


def applied_operations(root: Path, plan: JournalPlan) -> set[str]:
    return {op for state in read_journal(root, plan).values() for op in state.applied}


def upgrade_legacy_journal(
    root: Path, plan: JournalPlan, require_complete: bool = False
) -> None:
    for food_id, state in read_journal(root, plan).items():
        if (
            state.legacy
            and state.verified_hash is not None
            and state.latest is not None
            and state.latest["status"] not in {"attempt", "uncertain"}
        ):
            rows = [row for row in plan.rows if row["operation_id"] in state.applied]
            append_journal(
                root,
                plan,
                food_id,
                rows,
                "verified",
                state.verified_hash,
                state.verified_hash,
            )
        elif require_complete and state.legacy:
            raise ValueError(
                f"Resolve the legacy journal for food {food_id} with apply before editing the plan"
            )


def append_journal(
    root: Path,
    plan: JournalPlan,
    food_id: str,
    rows: list[dict[str, str]],
    status: str,
    before_hash: str,
    after_hash: str | None = None,
) -> None:
    entry: dict[str, object] = {
        "at": datetime.now(UTC).isoformat(),
        "plan_hash": plan.plan_hash,
        "review_hash": plan.review_hash,
        "manifest_hash": plan.manifest_hash,
        "food_id": food_id,
        "operation_ids": [row["operation_id"] for row in rows],
        "operations": [
            {
                "operation_id": row["operation_id"],
                "action": row["action"],
                "proposed_value": row["proposed_value"],
            }
            for row in rows
        ],
        "status": status,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    descriptor: int = os.open(
        root / "journal.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
    )
    with os.fdopen(descriptor, "ab") as output:
        _ = output.write(
            json.dumps(entry, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        output.flush()
        os.fsync(output.fileno())
