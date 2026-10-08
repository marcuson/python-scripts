# Guide for AI agents working on mealietidy

Follow the repository root `AGENTS.md` as well as this file. Write code, comments, and documentation in English.

## Source of truth

- Read [docs/specs.md](docs/specs.md) before changing behavior. Keep it aligned with implementation and user decisions.
- This directory is an independent Python project. Keep its entry point and dependencies in `pyproject.toml`; use `uv` for development and local installation.
- `export`, `analyze`, `edit`, and `apply` are implemented. `analyze --provider codex` requests optional AI suggestions through the local Codex CLI. `edit` saves local reviews; `apply` writes only approved edits on a verified compatible instance. Automatic merges remain unsupported.

## Runtime boundaries

- The Git repository versions source code only. Catalog exports, plans, reviews, reports, and journals belong in local directories outside the source tree. The installed tool must work without Git or a source checkout.
- `export`, `analyze`, `edit`, and `apply --dry-run` must not write to Mealie. The one-time API update test uses only the disposable local Mealie v3.28.0 instance defined by `compose.test.yaml`.
- Seed every fresh local test database with Mealie's built-in `en-US` Foods seeder through `scripts/seed_mealie_v3_28.py` before integration tests; never substitute a hand-made default catalog.
- Discover the running instance's OpenAPI document before relying on food endpoints or update behavior. Do not infer compatibility from another Mealie version.
- Apply only approved rows after validating the review and current state. Group operations by food, preserve unrelated fields, journal results, and stop on conflicts or uncertain writes. Reject approved merge candidates before all writes.
- The default `none` provider flags items for review but does not generate Italian names or translation suggestions. The optional `codex` and `gtranslate` providers only fill pending suggestions; do not send Mealie credentials or private URLs to them. `gtranslate` sends only food names to Google Cloud Translation Basic and must respect its local request limit. Keep providers behind the strategy interface in the specification.

## Working checks

- Use the smallest useful validation for each change. Test risky behavior against mocks and the disposable local instance when relevant; never use production data for fixtures or destructive tests.
- Run the complete local suite with `uv run poe test`; put future discoverable Python tests in `tests/test_*.py`.
- Keep `README.md` accurate for humans, including the distinction between implemented and planned behavior. Update `docs/specs.md` when a decision changes the contract.
- Do not run installation commands merely to check a routine edit; they replace an installed tool. Follow the root repository guide when adding this tool to the generated tool list.
