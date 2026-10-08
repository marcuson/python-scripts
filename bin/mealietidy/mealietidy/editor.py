"""Serve a local food review page that saves the existing CSV plan."""

import csv
import html
import io
import json
import os
import secrets
import tempfile
import webbrowser
from functools import cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from mealietidy.analyze import CSV_FIELDS
from mealietidy.export import (
    MealieClient,
    discover,
    sha256,
    substitution_reference_counts,
)
from mealietidy.journal import applied_operations, upgrade_legacy_journal
from mealietidy.review import ReviewConflictError, load_review

PAGE_SIZE: int = 20


@cache
def _asset(name: str) -> str:
    return (
        files("mealietidy").joinpath("editor_assets", name).read_text(encoding="utf-8")
    )


def _rows(data: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
    return list(reader)


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _render(
    root: Path,
    token: str,
    query: str,
    page: int,
    message: str = "",
    error: bool = False,
    overrides: dict[str, str] | None = None,
    show_applied: bool = False,
    conflict_ids: tuple[str, ...] = (),
    filter_ids: tuple[str, ...] = (),
    conflict_operation_id: str = "",
) -> str:
    reviewed = load_review(root)
    applied: set[str] = applied_operations(root, reviewed)
    review_bytes: bytes = (root / "review.csv").read_bytes()
    rows: list[dict[str, str]] = _rows(review_bytes)
    display_conflict_ids: tuple[str, ...] = conflict_ids or filter_ids
    foods: dict[str, dict[str, Any]] = reviewed.foods
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["food_id"], []).append(row)
    search_terms: list[str] = query.casefold().split()
    matches: list[str] = [
        food_id
        for food_id in grouped
        if (
            show_applied
            or any(
                row["operation_id"] not in applied
                and (overrides or {}).get(
                    f"decision.{row['operation_id']}", row["decision"]
                )
                == "pending"
                for row in grouped[food_id]
            )
        )
        and (not filter_ids or food_id in filter_ids)
        and all(
            term
            in " ".join(
                [
                    foods[food_id]["name"],
                    foods[food_id].get("pluralName") or "",
                    food_id,
                    *(
                        str(alias.get("name", ""))
                        for alias in foods[food_id].get("aliases", [])
                    ),
                    *(
                        text
                        for row in grouped[food_id]
                        for text in (
                            row["current_value"],
                            (overrides or {}).get(
                                f"value.{row['operation_id']}", row["proposed_value"]
                            ),
                            row["reason"],
                            row["reviewer_note"],
                        )
                    ),
                ]
            ).casefold()
            for term in search_terms
        )
    ]
    matches.sort(key=lambda food_id: (foods[food_id]["name"].casefold(), food_id))
    pages: int = max(1, (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 1), pages)
    shown: list[str] = matches[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    summary: dict[str, int] = {}
    for row in rows:
        decision: str = (overrides or {}).get(
            f"decision.{row['operation_id']}", row["decision"]
        )
        summary[decision] = summary.get(decision, 0) + 1
    food_fragments: list[str] = []
    for food_id in shown:
        food: dict[str, Any] = foods[food_id]
        food_rows: list[dict[str, str]] = grouped[food_id]
        counts: dict[str, int] = {"approve": 0, "reject": 0, "pending": 0}
        unsaved: bool = False
        for row in food_rows:
            op: str = row["operation_id"]
            decision: str = (overrides or {}).get(f"decision.{op}", row["decision"])
            counts[decision] = counts.get(decision, 0) + 1
            if op not in applied and any(
                (overrides or {}).get(f"{prefix}.{op}", row[field]) != row[field]
                for prefix, field in (
                    ("value", "proposed_value"),
                    ("decision", "decision"),
                    ("note", "reviewer_note"),
                )
            ):
                unsaved = True
        aliases: str = ", ".join(
            alias.get("name", "") for alias in food.get("aliases", [])
        )
        operations: list[str] = []
        for row in food_rows:
            op: str = row["operation_id"]
            if op in applied:
                if show_applied:
                    operations.append(
                        Template(_asset("applied.html")).substitute(
                            action=_escape(row["action"].replace("_", " ").title()),
                            value=_escape(row["proposed_value"]),
                        )
                    )
                continue
            value: str = (overrides or {}).get(f"value.{op}", row["proposed_value"])
            decision: str = (overrides or {}).get(f"decision.{op}", row["decision"])
            note: str = (overrides or {}).get(f"note.{op}", row["reviewer_note"])
            value_label: str = "Proposed value"
            usage_panel: str = ""
            value_control: str = (
                f"<input id='value-{_escape(op)}' name='value.{_escape(op)}' "
                f"data-saved='{_escape(row['proposed_value'])}' "
                f"value='{_escape(value)}' maxlength='255'>"
            )
            choices: tuple[str, ...] = ("pending", "approve", "reject")
            if row["action"] == "merge_candidate":
                value_label = "Conflict resolution"
                participants: tuple[str, str] = (
                    row["food_id"],
                    row["duplicate_target_id"],
                )
                usage_spans: str = " ".join(
                    f"<span data-food-id='{_escape(food_id)}' "
                    f"data-food-name='{_escape(reviewed.foods[food_id]['name'])}'>"
                    "Loading recipe count…</span>"
                    for food_id in participants
                )
                usage_panel = (
                    f"<div class='conflict-usage' data-food-ids="
                    f"'{_escape(','.join(participants))}'>{usage_spans}</div>"
                    "<small>Deletion is blocked unless recipe, shopping-list, and "
                    "substitution counts are all zero.</small>"
                )
                options_for_merge: list[str] = [
                    "<option value=''>Choose a resolution</option>"
                ]
                for retained_id in (row["food_id"], row["duplicate_target_id"]):
                    selected_attr: str = (
                        " selected" if value == f"merge:{retained_id}" else ""
                    )
                    options_for_merge.append(
                        f"<option value='merge:{_escape(retained_id)}'{selected_attr}>"
                        f"Merge into {_escape(reviewed.foods[retained_id]['name'])} "
                        "(keep its food fields)</option>"
                    )
                for deleted_id in (row["food_id"], row["duplicate_target_id"]):
                    selected_attr = (
                        " selected" if value == f"delete:{deleted_id}" else ""
                    )
                    options_for_merge.append(
                        f"<option value='delete:{_escape(deleted_id)}'{selected_attr}>"
                        f"Delete {_escape(reviewed.foods[deleted_id]['name'])} "
                        "(only when it has no references)</option>"
                    )
                value_control = (
                    f"<select id='value-{_escape(op)}' name='value.{_escape(op)}' "
                    f"data-saved='{_escape(row['proposed_value'])}'>"
                    + "".join(options_for_merge)
                    + "</select>"
                )
            elif (
                op == conflict_operation_id
                and row["action"] in {"rename", "add_alias"}
                and len(display_conflict_ids) == 2
                and food_id in display_conflict_ids
            ):
                other_id: str = next(
                    participant
                    for participant in display_conflict_ids
                    if participant != food_id
                )
                value_label = "Conflict resolution"
                options_for_resolution: list[str] = [
                    "<option value=''>Choose a resolution</option>"
                ]
                for retained_id in (food_id, other_id):
                    choice: str = f"resolve:merge:{other_id}:{retained_id}"
                    selected_attr: str = " selected" if value == choice else ""
                    options_for_resolution.append(
                        f"<option value='{_escape(choice)}'{selected_attr}>"
                        f"Merge into {_escape(foods[retained_id]['name'])} "
                        "(keep its food fields)</option>"
                    )
                for deleted_id in (food_id, other_id):
                    choice = f"resolve:delete:{other_id}:{deleted_id}"
                    selected_attr = " selected" if value == choice else ""
                    options_for_resolution.append(
                        f"<option value='{_escape(choice)}'{selected_attr}>"
                        f"Delete {_escape(foods[deleted_id]['name'])} "
                        "(only when it has no references)</option>"
                    )
                value_control = (
                    f"<select id='value-{_escape(op)}' name='value.{_escape(op)}' "
                    f"data-saved='{_escape(row['proposed_value'])}'>"
                    + "".join(options_for_resolution)
                    + "</select>"
                )
                usage_spans = " ".join(
                    f"<span data-food-id='{_escape(participant)}' "
                    f"data-food-name='{_escape(foods[participant]['name'])}'>"
                    "Loading usage counts…</span>"
                    for participant in display_conflict_ids
                )
                usage_panel = (
                    f"<div class='conflict-usage' data-food-ids="
                    f"'{_escape(','.join(display_conflict_ids))}'>{usage_spans}</div>"
                    "<small>Choose a decision below. A merge or deletion replaces "
                    "this conflicting proposal.</small>"
                )
            options: str = "".join(
                Template(_asset("option.html")).substitute(
                    value=choice,
                    operation_id=_escape(op),
                    saved_decision=_escape(row["decision"]),
                    selected=" checked" if decision == choice else "",
                    label=choice.title(),
                )
                for choice in choices
            )
            operations.append(
                Template(_asset("operation.html")).substitute(
                    action=_escape(row["action"].replace("_", " ").title()),
                    current=_escape(row["current_value"] or "—"),
                    reason=_escape(row["reason"]),
                    risk=_escape(row["risk"]),
                    operation_id=_escape(op),
                    saved_value=_escape(row["proposed_value"]),
                    value=_escape(value),
                    value_label=value_label,
                    value_control=value_control,
                    usage_panel=usage_panel,
                    saved_note=_escape(row["reviewer_note"]),
                    note=_escape(note),
                    hidden_attr=(
                        " hidden" if not show_applied and decision != "pending" else ""
                    ),
                    options=options,
                )
            )
        food_fragments.append(
            Template(_asset("food.html")).substitute(
                applied_count=str(
                    sum(row["operation_id"] in applied for row in food_rows)
                ),
                open_attr=" open"
                if counts["pending"] or (len(filter_ids) == 2 and food_id in filter_ids)
                else "",
                name=_escape(food["name"]),
                approved_count=str(counts["approve"]),
                rejected_count=str(counts["reject"]),
                pending_count=str(counts["pending"]),
                unsaved_hidden="" if unsaved else " hidden",
                food_id=_escape(food_id),
                plural=_escape(food.get("pluralName") or "—"),
                aliases=_escape(aliases or "—"),
                operations="".join(operations),
            )
        )
    links: list[str] = []
    for label, target in (("Previous", page - 1), ("Next", page + 1)):
        if 1 <= target <= pages:
            links.append(
                Template(_asset("nav_button.html")).substitute(
                    action=label.lower(), label=label
                )
            )
    page_options: str = "".join(
        f"<option value='{target}'{' selected' if target == page else ''}>{target}</option>"
        for target in range(1, pages + 1)
    )
    hidden_count: int = sum(
        row["operation_id"] in applied
        or (overrides or {}).get(f"decision.{row['operation_id']}", row["decision"])
        != "pending"
        for row in rows
    )
    conflict_details: str = ""
    conflict_usage: str = ""
    if len(display_conflict_ids) == 2 and all(
        food_id in foods for food_id in display_conflict_ids
    ):
        involved: str = ", ".join(
            f"{_escape(foods[food_id]['name'])} ({_escape(food_id)})"
            for food_id in display_conflict_ids
            if food_id in foods
        )
        conflict_details = f"<br><span>Conflicting foods: {involved}.</span>"
        if conflict_ids:
            conflict_query: str = _escape(
                urlencode(
                    {
                        "filter_ids": ",".join(display_conflict_ids),
                        "show_applied": "1",
                        "conflict_operation_id": conflict_operation_id,
                    }
                )
            )
            conflict_details += (
                f" <a href='?{conflict_query}' "
                "target='_blank' rel='noopener noreferrer'>"
                "Open only these foods in a new tab</a>"
            )
        names = "".join(
            f"<span data-food-id='{_escape(food_id)}' "
            f"data-food-name='{_escape(foods[food_id]['name'])}'>Loading recipe count…</span> "
            for food_id in display_conflict_ids
        )
        conflict_usage = (
            f"<div class='conflict-usage' data-food-ids="
            f"'{_escape(','.join(display_conflict_ids))}'>{names}</div>"
        )
    filter_input: str = (
        f"<input type='hidden' name='filter_ids' value='{_escape(','.join(filter_ids))}'>"
        if filter_ids
        else ""
    )
    return Template(_asset("page.html")).substitute(
        style=_asset("editor.css"),
        script=_asset("editor.js"),
        summary=(
            f"{summary.get('pending', 0)} pending · {summary.get('approve', 0)} approved · "
            f"{summary.get('reject', 0)} rejected operations"
        ),
        food_count=f"{len(matches)} foods · page {page} of {pages}",
        session_token=_escape(token),
        review_hash=sha256(review_bytes),
        query=_escape(query),
        page=str(page),
        page_options=page_options,
        conflict_details=conflict_details,
        conflict_usage=conflict_usage,
        filter_input=filter_input,
        conflict_operation_id=_escape(conflict_operation_id),
        show_applied=str(int(show_applied)),
        toggle_label="Hide completed"
        if show_applied
        else f"Show completed ({hidden_count})",
        status_class="error" if error else "success",
        status_hidden=" hidden" if not (message or conflict_details) else "",
        status_message=_escape(message),
        foods="".join(food_fragments),
        links="".join(links),
    )


def _stage(root: Path, fields: dict[str, list[str]], token: str) -> dict[str, str]:
    if any(len(values) != 1 for name, values in fields.items() if name != "food_id"):
        raise ValueError("Duplicate form field")
    form: dict[str, str] = {
        name: values[0] for name, values in fields.items() if name != "food_id"
    }
    if form.get("token") != token:
        raise ValueError("Invalid review session")
    path: Path = root / "review.csv"
    original: bytes = path.read_bytes()
    if form.get("review_hash") != sha256(original):
        raise ValueError(
            "review.csv changed since this page loaded; reload before saving"
        )
    rows: list[dict[str, str]] = _rows(original)
    applied: set[str] = applied_operations(root, load_review(root))
    food_ids: list[str] = fields.get("food_id", [])
    if len(food_ids) != len(set(food_ids)):
        raise ValueError("Duplicate food in review form")
    selected: list[dict[str, str]] = [row for row in rows if row["food_id"] in food_ids]
    if {row["food_id"] for row in selected} != set(food_ids):
        raise ValueError("Unknown food in review form")
    required: set[str] = {
        "token",
        "review_hash",
        "current_q",
        "q",
        "page",
        "action",
    }
    if "show_applied" in fields:
        required.add("show_applied")
    if "filter_ids" in fields:
        required.add("filter_ids")
    if "target_page" in fields:
        required.add("target_page")
    if "conflict_operation_id" in fields:
        required.add("conflict_operation_id")
    if food_ids:
        required.add("food_id")
    for row in selected:
        op: str = row["operation_id"]
        if op not in applied:
            required.update({f"value.{op}", f"decision.{op}", f"note.{op}"})
    if set(fields) != required:
        raise ValueError("Review form fields do not match the selected foods")
    if form["action"] not in {
        "save",
        "search",
        "previous",
        "next",
        "goto",
        "toggle_applied",
    }:
        raise ValueError("Unknown review action")
    if form["action"] == "goto" and "target_page" not in form:
        raise ValueError("Page selection is missing")
    return {
        name: value
        for name, value in form.items()
        if name.startswith(("value.", "decision.", "note."))
    }


def _save(
    root: Path,
    fields: dict[str, list[str]],
    token: str,
    drafts: dict[str, str] | None = None,
) -> tuple[str, int, str]:
    updates: dict[str, str] = _stage(root, fields, token)
    pending: dict[str, str] = {**(drafts or {}), **updates}
    reviewed = load_review(root)
    applied: set[str] = applied_operations(root, reviewed)
    if any(name.rsplit(".", 1)[-1] in applied for name in pending):
        raise ValueError("Applied operations are read-only; reload the review page")
    rows: list[dict[str, str]] = _rows((root / "review.csv").read_bytes())
    for row in rows:
        op: str = row["operation_id"]
        for field, prefix in (
            ("proposed_value", "value"),
            ("decision", "decision"),
            ("reviewer_note", "note"),
        ):
            if f"{prefix}.{op}" in pending:
                row[field] = pending[f"{prefix}.{op}"]
    candidate: bytes = _csv_bytes(rows)
    load_review(root, candidate)
    upgrade_legacy_journal(root, reviewed, require_complete=True)
    path: Path = root / "review.csv"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=root, prefix=".review-", delete=False
        ) as output:
            temporary = output.name
            os.chmod(temporary, 0o600)
            output.write(candidate)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    try:
        page: int = int(fields["page"][0])
    except ValueError:
        page = 1
    return fields["current_q"][0], page, "Saved all reviewed edits"


def make_server(
    root: Path,
    base_url: str | None = None,
    mealie_token: str | None = None,
    timeout: float = 20,
) -> tuple[HTTPServer, str]:
    reviewed = load_review(root)
    upgrade_legacy_journal(root, reviewed, require_complete=True)
    token: str = secrets.token_urlsafe(24)
    path: str = f"/{token}/"
    drafts: dict[str, str] = {}
    mealie_client: MealieClient | None = None
    if mealie_token:
        manifest = json.loads((root / "manifest.json").read_bytes())
        plan_url: object = (
            cast(dict[str, object], manifest).get("base_url")
            if isinstance(manifest, dict)
            else None
        )
        if not isinstance(plan_url, str):
            raise ValueError("Plan manifest has no Mealie base URL")
        if (
            base_url
            and MealieClient(base_url, mealie_token, timeout).base_url != plan_url
        ):
            raise ValueError("Mealie base URL differs from the export manifest")
        mealie_client = MealieClient(plan_url, mealie_token, timeout)

    class Handler(BaseHTTPRequestHandler):
        def _reply(
            self,
            status: int,
            content: str,
            content_type: str = "text/html; charset=utf-8",
        ) -> None:
            data: bytes = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{token}'; connect-src 'self'; form-action 'self'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            return (
                urlsplit(self.path).path in {path, path + "usage"}
                and isinstance(self.server, HTTPServer)
                and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
            )

        def do_GET(self) -> None:
            if not self._authorized():
                self._reply(404, "Not found")
                return
            parameters: dict[str, list[str]] = parse_qs(
                urlsplit(self.path).query, keep_blank_values=True
            )
            if urlsplit(self.path).path == path + "usage":
                if mealie_client is None:
                    self._reply(
                        503,
                        json.dumps({"error": "Mealie credentials are not configured"}),
                        "application/json; charset=utf-8",
                    )
                    return
                try:
                    reviewed_plan = load_review(root)
                    ids: list[str] = list(
                        dict.fromkeys(
                            filter(None, parameters.get("food_ids", [""])[0].split(","))
                        )
                    )
                    if len(ids) != 2 or any(
                        food_id not in reviewed_plan.foods for food_id in ids
                    ):
                        raise ValueError(
                            "A conflict must contain two foods from this plan"
                        )
                    capabilities: dict[str, Any] = json.loads(
                        (root / "discovery/capabilities.json").read_bytes()
                    )
                    recipe_capability: object = capabilities.get("recipe_usage")
                    shopping_capability: object = capabilities.get("shopping_usage")
                    food_list: object = capabilities.get("food_list")
                    if not all(
                        isinstance(capability, dict)
                        for capability in (
                            recipe_capability,
                            shopping_capability,
                            food_list,
                        )
                    ):
                        current_discovery, _ = discover(mealie_client)
                        if (
                            current_discovery["mealie_version"]
                            != reviewed_plan.manifest["mealie_version"]
                            or current_discovery["openapi_hash"]
                            != reviewed_plan.manifest["openapi_hash"]
                        ):
                            raise ValueError(
                                "Mealie discovery differs from this plan; re-export before loading recipe counts"
                            )
                        recipe_capability = current_discovery.get("recipe_usage")
                        shopping_capability = current_discovery.get("shopping_usage")
                        food_list = current_discovery.get("food_list")
                    if not all(
                        isinstance(capability, dict)
                        for capability in (
                            recipe_capability,
                            shopping_capability,
                            food_list,
                        )
                    ):
                        raise TypeError(
                            "Recipe, shopping-list, or substitution usage is not available in this Mealie OpenAPI document"
                        )
                    recipe_info = cast(dict[str, str], recipe_capability)
                    shopping_info = cast(dict[str, str], shopping_capability)
                    counts: dict[str, dict[str, int]] = {}
                    substitution_counts = substitution_reference_counts(
                        mealie_client, cast(dict[str, Any], food_list)
                    )
                    for food_id in ids:
                        recipe_result: object = mealie_client.get_json(
                            recipe_info["path"]
                            + "?"
                            + urlencode(
                                {recipe_info["food_filter"]: food_id, "perPage": 1}
                            )
                        )
                        shopping_result: object = mealie_client.get_json(
                            shopping_info["path"]
                            + "?"
                            + urlencode(
                                {
                                    "queryFilter": f"foodId=={food_id}",
                                    "perPage": 1,
                                }
                            )
                        )
                        for label, result in (
                            ("recipe", recipe_result),
                            ("shopping-list", shopping_result),
                        ):
                            if (
                                not isinstance(result, dict)
                                or not isinstance(
                                    cast(dict[str, object], result).get("total"), int
                                )
                                or result["total"] < 0
                            ):
                                raise ValueError(
                                    f"Mealie returned an invalid {label} count"
                                )
                        counts[food_id] = {
                            "recipes": cast(dict[str, int], recipe_result)["total"],
                            "shopping_items": cast(dict[str, int], shopping_result)[
                                "total"
                            ],
                            "substitution_links": substitution_counts.get(food_id, 0),
                        }
                    self._reply(
                        200,
                        json.dumps(counts),
                        "application/json; charset=utf-8",
                    )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                ) as error:
                    self._reply(
                        503,
                        json.dumps({"error": str(error)}),
                        "application/json; charset=utf-8",
                    )
                return
            if urlsplit(self.path).path != path:
                self._reply(404, "Not found")
                return
            query: str = parameters.get("q", [""])[0]
            try:
                page: int = int(parameters.get("page", ["1"])[0])
            except ValueError:
                page = 1
            try:
                show_applied: bool = parameters.get("show_applied", ["0"])[0] == "1"
                filter_ids: tuple[str, ...] = tuple(
                    filter(None, parameters.get("filter_ids", [""])[0].split(","))
                )
                self._reply(
                    200,
                    _render(
                        root,
                        token,
                        query,
                        page,
                        overrides=drafts,
                        show_applied=show_applied,
                        filter_ids=filter_ids,
                        conflict_operation_id=parameters.get(
                            "conflict_operation_id", [""]
                        )[0],
                    ),
                )
            except (OSError, ValueError, TypeError, KeyError) as error:
                self._reply(500, f"Review unavailable: {_escape(error)}")

        def do_POST(self) -> None:
            if not self._authorized() or urlsplit(self.path).path != path:
                self._reply(404, "Not found")
                return
            if self.headers.get("Content-Type") != "application/x-www-form-urlencoded":
                self._reply(415, "Unsupported form content type")
                return
            fields: dict[str, list[str]] = {}
            try:
                length: int = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 262144:
                    raise ValueError("Review form is too large")
                fields = parse_qs(
                    self.rfile.read(length).decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=1000,
                )
                drafts.update(_stage(root, fields, token))
                action: str = fields["action"][0]
                show_applied = fields.get("show_applied", ["0"])[0] == "1"
                filter_ids: tuple[str, ...] = tuple(
                    filter(None, fields.get("filter_ids", [""])[0].split(","))
                )
                try:
                    page = int(fields["page"][0])
                except ValueError:
                    page = 1
                query = fields["current_q"][0]
                if action == "save":
                    query, page, message = _save(root, fields, token, drafts)
                    drafts.clear()
                elif action == "search":
                    query = fields["q"][0]
                    page = 1
                    message = (
                        "Edits retained in this session; press Save to write review.csv"
                    )
                elif action == "toggle_applied":
                    show_applied = not show_applied
                    page = 1
                    message = "Applied operations are read-only and recorded in the local journal"
                elif action == "goto":
                    try:
                        page = int(fields["target_page"][0])
                    except ValueError as error:
                        raise ValueError("Invalid page selection") from error
                    message = (
                        "Edits retained in this session; press Save to write review.csv"
                    )
                else:
                    page += 1 if action == "next" else -1
                    message = (
                        "Edits retained in this session; press Save to write review.csv"
                    )
                self._reply(
                    200,
                    _render(
                        root,
                        token,
                        query,
                        page,
                        message,
                        overrides=drafts,
                        show_applied=show_applied,
                        filter_ids=filter_ids,
                        conflict_operation_id=fields.get("conflict_operation_id", [""])[
                            0
                        ],
                    ),
                )
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                UnicodeDecodeError,
            ) as error:
                form: dict[str, str] = {
                    name: values[0] for name, values in fields.items()
                }
                try:
                    page = int(form.get("page", "1"))
                except ValueError:
                    page = 1
                conflict_ids: tuple[str, ...] = (
                    error.food_ids if isinstance(error, ReviewConflictError) else ()
                )
                conflict_operation_id: str = (
                    error.operation_id
                    if isinstance(error, ReviewConflictError)
                    else form.get("conflict_operation_id", "")
                )
                filter_ids: tuple[str, ...] = tuple(
                    filter(None, form.get("filter_ids", "").split(","))
                )
                self._reply(
                    400,
                    _render(
                        root,
                        token,
                        form.get("current_q", ""),
                        page,
                        str(error),
                        True,
                        {**drafts, **form},
                        form.get("show_applied") == "1",
                        conflict_ids,
                        filter_ids,
                        conflict_operation_id,
                    ),
                )

    server: HTTPServer = HTTPServer(("127.0.0.1", 0), Handler)
    url: str = f"http://127.0.0.1:{server.server_address[1]}{path}"
    return server, url


def edit_plan(
    root: Path,
    base_url: str | None = None,
    mealie_token: str | None = None,
    timeout: float = 20,
) -> None:
    server, url = make_server(root, base_url, mealie_token, timeout)
    print(f"Review page: {url}", flush=True)
    print(
        "Use Save to write all reviewed edits. Press Ctrl+C to stop the local server.",
        flush=True,
    )
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
