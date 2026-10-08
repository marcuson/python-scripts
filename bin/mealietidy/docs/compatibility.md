# Mealie API compatibility

## Verified local target

| Property | Observed value |
| --- | --- |
| Mealie version | `v3.28.0` (reported by `/openapi.json`) |
| Container image | `ghcr.io/mealie-recipes/mealie:v3.28.0`, digest `sha256:8b02290f4d1806f02acac6f25f6d48a3c965612fda1f8e914d5af533276f8688` |
| Test URL | `http://127.0.0.1:19925` (loopback only) |
| Database | SQLite under `/app/data` on `tmpfs` |
| Access | Direct localhost port mapping; no reverse proxy |
| API documentation | `GET /docs` returns 200 and points to `/openapi.json` |
| OpenAPI SHA-256 | `90e19aa713ab4ba15352627aca7dc37290f868b213eb65a3e1564f3b7a7ff292` |
| Test date | 2026-10-13 UTC |

Docker inspection showed no Docker volume mounts and one `tmpfs` mount at `/app/data`. After creating test foods and a recipe, `docker compose down` followed by a fresh `up` yielded zero foods and zero recipes.

The current test setup runs `scripts/seed_mealie_v3_28.py` after each fresh start. It calls Mealie's built-in `POST /api/groups/seeders/foods` endpoint with `locale: en-US`; on 2026-10-14 the pinned v3.28.0 image created 580 Foods. A second seed attempt was rejected to avoid duplicates. The one-time API verification passed on this seeded catalog and added two dummy Foods. The read-only phase 1 commands then exported all 582 Foods and generated 1,746 pending review rows. Seeding is local environment setup, separate from mealietidy's read-only `export`. The initial one-time write verification below was performed before this seeded setup was adopted.

## Discovered operations

| Purpose | Operation | Observed or declared shape |
| --- | --- | --- |
| Token | `POST /api/auth/token` | Form fields `username`, `password`; food routes use an OAuth2 bearer token. |
| List foods | `GET /api/foods` | Query `page` (default 1), `perPage` (default 50); response `IngredientFoodPagination` with `items`, `total`, `total_pages`, `next`, and `previous`. |
| Read food | `GET /api/foods/{item_id}` | Response `IngredientFood-Output`; path ID is UUID4. |
| Create food | `POST /api/foods` | Request `CreateIngredientFood`; response `IngredientFood-Output`, HTTP 201. |
| Update food | `PUT /api/foods/{item_id}` | Request `CreateIngredientFood`; response `IngredientFood-Output`, HTTP 200. |
| Recipe reference probe | `POST /api/recipes`, `GET/PUT /api/recipes/{slug}` | Used only in the disposable test to link a recipe ingredient to a food ID. |
| Merge foods | `PUT /api/foods/merge` | Request `MergeFood` with `fromFood` and `toFood`; redirects recipe and shopping-list references to `toFood`, then removes `fromFood`. |
| Delete food | `DELETE /api/foods/{item_id}` | Response 200. Mealie also removes recipe associations and incoming substitution relationships, so mealietidy permits deletion only when checked reference counts are zero. |
| Recipe usage | `GET /api/recipes?foods=<food_id>&perPage=1` | Response `total` gives the number of recipes using the food. |
| Shopping-list usage | `GET /api/households/shopping/items?queryFilter=foodId==<food_id>&perPage=1` | Response `total` gives the number of shopping-list items using the food. |
| Parse ingredient | `POST /api/parser/ingredient` | Request `IngredientRequest`; response `ParsedIngredient`. |

`CreateIngredientFood` requires `name` and allows `id`, `pluralName`, `description`, `extras`, `labelId`, `aliases`, `substitutions`, and `householdsWithIngredientFood`. Aliases have a `name` only; Mealie does not expose alias language metadata here. Food output also includes a label object and timestamps. No food usage count was found in the list or detail schema. The OpenAPI document declares validation responses (HTTP 422); a food list request without a token returned HTTP 401. Insufficient-permission behavior was not exercised.

## One-time write verification

`tests/verify_mealie_v3_28.py` was run only against the disposable Compose instance, separately from the future read-only `export` command. It created two dummy foods, including an alias, plural, description, extra property, and substitution relation, plus a recipe with an ingredient referencing one food ID. It then reread that food, changed its name and plural using a complete writable payload, and reread it again. Its ID, description, extras, alias, substitution, and recipe ingredient reference remained intact. The brute ingredient parser resolved the same food ID through an existing alias before and after that update.

A separate minimal `PUT` containing only `name` reset the omitted plural, description, extras, aliases, and substitutions. Treat this endpoint as **replacement**, not a safe partial update: reread the food and send every current writable field with the approved changes.

The version and OpenAPI hash above identify the only verified write target. Discovery of another instance may allow read-only export, but this record does not authorize writes when its version or hash differs.

## Apply integration probe

`tests/verify_apply_mealie_v3_28.py` runs against the same disposable, freshly seeded version and OpenAPI hash. It exports the catalog, creates an approved three-operation review for one dummy food, verifies that `--dry-run` leaves Mealie unchanged, applies one grouped update, checks the new name, plural, aliases, description, and extras, then reruns the plan and verifies that it makes no second write. The test uses a temporary plan directory and is included in `uv run poe test`.

## Deduplication verification

`tests/verify_dedup_mealie_v3_28.py` creates two duplicate foods, a recipe, and a shopping-list item that reference the source. It verifies both usage-count endpoints and the local editor's same-origin count route, then performs a dry run and an approved merge. Mealie redirects both references to the retained food and removes the source. The test also verifies that mealietidy preserves source-only aliases, the retained food's name stays unchanged, and rerunning the plan is idempotent. This merge behavior is enabled only for the version and OpenAPI hash in this record.

The integration test also approves deletion of an unused duplicate and verifies that a duplicate with a recipe reference is blocked before the DELETE request. Before deleting, mealietidy checks recipe and shopping-list usage endpoints and scans all current food pages for incoming and outgoing substitution links; it repeats those checks immediately before deletion. It verifies the selected food is gone and the other candidate is unchanged. The API's deletion of recipe and substitution references makes deletion unsafe whenever any of these counts are nonzero. The merge endpoint does not preserve source-only aliases or substitution relationships; mealietidy copies source aliases first and blocks a merge when any catalog substitution link is defined on the source or points to it. It rechecks those links immediately before writing. Compatibility with other versions or OpenAPI hashes remains unverified.
