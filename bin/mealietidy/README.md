# mealietidy

`mealietidy` is a local Python tool for reviewing and cleaning up the food catalog of a self-hosted Mealie instance. The intended workflow exports the catalog, prepares a reviewable plan for Italian names, plurals, aliases, and possible duplicates, and applies only changes approved in that plan.

The full behavior and safety rules are in [the specification](docs/specs.md).

## Current status

Phases 0 through 4 are implemented. The CLI exports foods, prepares a review plan, optionally requests provider suggestions, previews approved edits, and applies reviewed food updates and explicit conflict resolutions on a verified compatible Mealie instance. Packaged merge and zero-reference deletion compatibility was established on the disposable v3.28.0 target; the operator has also manually confirmed the workflow on the actual v3.28.0 instance. Runtime version and OpenAPI hash checks still apply; see [docs/compatibility.md](docs/compatibility.md).

## Install locally

From this directory, with `uv` available and Python 3.12 or newer:

```sh
uv tool install .
```

The installed command is `mealietidy`. It runs without Git or a source checkout. Install the package on the computer that can reach Mealie; keep generated data in a separate local directory.

## Configure

Run `mealietidy config` in the directory where you will run the tool. It creates a private `mealietidy.yaml` with the current defaults, or adds missing default settings to an existing file while keeping its values and comments. The final two settings select the provider and its arguments; `none` is active by default with an empty argument mapping. Commented `codex` and `gtranslate` examples each include their matching `provider` setting immediately before `provider_args`. The command reports whether the file was created, updated, or already complete. Edit the file yourself with any tool you prefer. Malformed YAML is reported without changing the file.

Use flat YAML keys such as:

```yaml
base_url: https://mealie.example
plan: mealietidy-plan
timeout: 20
provider: none
```

You may also set `token`, `dry_run`, and `provider_args` (a mapping of names to string values). CLI flags override environment variables, which override YAML. Environment names are `MEALIETIDY_BASE_URL`, `MEALIETIDY_TOKEN`, `MEALIETIDY_PLAN`, `MEALIETIDY_TIMEOUT`, `MEALIETIDY_PROVIDER`, and `MEALIETIDY_DRY_RUN`; provider arguments use `MEALIETIDY_PROVIDER_ARG_<NAME>`. The existing `MEALIE_TOKEN` variable remains a fallback for `MEALIETIDY_TOKEN`. Boolean environment values accept `true`/`false`, `yes`/`no`, `on`/`off`, and `1`/`0`. `--help` and `--version` are CLI actions, not configuration values. Keep `mealietidy.yaml` outside the source repository if it contains a token.

Remove any old `yes` key from `mealietidy.yaml`; it is no longer a setting.

## Export and analyze

Run from a local data directory outside the source repository. By default, all artifacts go into `./mealietidy-plan` relative to the current directory. Use the same `--plan DIR` on each command to choose another location. An export refuses to overwrite a nonempty plan directory.

```sh
export MEALIE_TOKEN='your token'
mealietidy export --base-url https://mealie.example
mealietidy analyze --provider none
```

`export` reads `/docs`, fetches the running instance's OpenAPI document, then reads every food page and each food detail using GET requests. It saves the raw OpenAPI, discovered capabilities, per-food JSON, a combined snapshot, and a checksum manifest. If the catalog changes during export, it stops without creating a usable plan. Exported foods and plans are local files, not Git history or a full Mealie backup.

`analyze` checks the export checksums and creates `plan.json` and `review.csv`. The default `none` provider identifies missing plurals, repeated terms, cross-food collisions, and possible duplicate names. It creates pending, high-risk review rows for names, plurals, and aliases, with empty proposed values. It does not translate or invent Italian text. Similar names are only possible duplicate candidates; no merge is applied.

To run another provider on the same plan, use `mealietidy analyze --provider codex --refine`. Codex sees existing suggestions for untouched pending operations and may improve them. Approved and rejected rows, and pending rows whose value or note you edited, stay unchanged. A second `analyze` without `--refine` asks for confirmation with `[y/N]` (press `y` to continue); it requests fresh suggestions for eligible pending rows using the original food data. In either mode, suggestions omitted by the provider keep their existing values. `refine: true` in `mealietidy.yaml` or `MEALIETIDY_REFINE=true` also enables refinement. When `--provider` overrides a different configured provider, arguments for the old provider from YAML or environment are ignored; explicit CLI provider arguments are still checked.

To request suggestions, install Codex CLI separately and sign in with `codex login` using a ChatGPT account. Then run:

```sh
mealietidy analyze --provider codex
```

During analysis, the terminal shows the current batch and a `processed/total` food counter after each batch, including batches that fail.

Selecting `codex` explicitly allows the food names, existing plurals, and alias names in the export to be sent to Codex for cloud processing. No Mealie token, instance URL, recipe, or other food fields are included in the prompt. The local Codex process runs in a temporary directory with read-only sandboxing and returns suggestions through a JSON schema. Suggestions are checked for valid IDs, fields, length, and catalog collisions before appearing as pending values in the review; they are never approved automatically. Codex failures leave those rows empty and add a `provider_error` finding. A valid batch response is cached under `suggestions-cache/` in the private plan directory, keyed by its inputs, prompt version, and selected model. The default model is `gpt-6-luna`, chosen for fast, focused tasks. To select another model, use `--provider-arg-model=MODEL` (for example, `--provider-arg-model=gpt-6.1-sol`); use `--provider-arg-reasoning-effort=low` to select the reasoning effort, or `--provider-arg-timeout=SECONDS` to change the default 120-second limit per batch. When reasoning effort is omitted, Codex uses its built-in default for the selected model; supported levels vary by model. These arguments also work through `provider_args` in YAML and `MEALIETIDY_PROVIDER_ARG_MODEL` / `MEALIETIDY_PROVIDER_ARG_REASONING_EFFORT` / `MEALIETIDY_PROVIDER_ARG_TIMEOUT` environment variables. In YAML, write `provider_args: {reasoning_effort: low}`. Codex uses the signed-in account's plan and limits; the CLI does not require an OpenAI API key. Usage can consume included limits or account credits. OpenAI's data retention and training controls depend on the ChatGPT account and workspace; check them before analyzing private names. Read-only sandboxing prevents writes by model-run commands, but is not a complete barrier against reading other local files. [Codex authentication](https://learn.chatgpt.com/docs/auth), [usage and pricing](https://learn.chatgpt.com/docs/pricing), [data controls](https://help.openai.com/en/articles/7730893-data-controls-in-chatgpt)

To use Google Cloud Translation Basic v2, enable the Cloud Translation API and billing in a dedicated Google Cloud project, create an API key restricted to that API, and set it in the environment:

```sh
export MEALIETIDY_PROVIDER_ARG_GOOGLE_API_KEY='your Google Cloud API key'
mealietidy analyze --provider gtranslate
```

`gtranslate` also accepts `--provider-arg-google-api-key=KEY` or `provider_args: {google_api_key: KEY}` in `mealietidy.yaml`. Underscores in YAML provider argument names map to hyphens in CLI names. It sends only food names to Google and proposes Italian canonical names for manual review. It leaves plurals, aliases, and merges to the operator. Suggestions remain pending and have high uncertainty because short food names can translate ambiguously. Valid responses are cached under the private plan directory; the API key is excluded from the plan and cache. The provider accepts `--provider-arg-timeout=SECONDS` (default 20) and limits new requests to 16,000 input characters per run; skipped foods remain pending. [Google's pricing](https://cloud.google.com/products/translate/pricing) currently covers the first 500,000 NMT characters per month with a shared $10 credit, but billing must be enabled and usage beyond that can incur charges. To keep a dedicated project within the free allowance even in a 31-day month, set its [general-model daily character quota](https://docs.cloud.google.com/translate/quotas) to at most 16,000. The tool's run limit alone cannot account for other clients or repeated runs. Google says it holds submitted text briefly in memory for translation and does not use it to improve the model; Basic uses a global endpoint. See [Google's data usage FAQ](https://docs.cloud.google.com/translate/data-usage).

Run `mealietidy edit` from the same directory, or pass `--plan DIR`. It opens a local browser page with search and 20 foods per page. The compact review controls span the page; foods appear in two columns on wide screens and one column on narrower screens. Foods with no Pending operations start collapsed, showing only their name and chips; expand them to review their operations. Its heading stays visible below the review controls while you scroll through that food. Each food shows counts of approved, rejected, and pending operations; an **Unsaved** chip appears when its edits differ from `review.csv`. Edit proposed values and notes, choose `Pending`, `Approve`, or `Reject`, and move between pages as needed. The editor retains edits across its Search, Previous, and Next controls. Press **Save** once to write all edits accumulated in the current editor session to `review.csv`, including edits on other pages. With JavaScript enabled, Save uses `fetch` and shows the result without reloading the page. Saved work remains available when you run `mealietidy edit` later. Applied operations are hidden by default; **Show applied** displays them read-only according to the local journal. The page is served only on `127.0.0.1` and never writes to Mealie. Recipe-use counts for duplicate candidates are fetched through the local server when `edit` receives `--base-url` and `--token`; older plans missing this discovery field are checked against the live Mealie OpenAPI before counts are requested. Without credentials, counts appear unavailable and review stays offline. Keep the terminal running while reviewing. You can still edit `review.csv` directly; only `proposed_value`, `decision`, and `reviewer_note` may change, and verified operations must retain their value and decision. Unsaved edits are lost if the editor process stops or you leave the page without using its controls.

Run `mealietidy apply --base-url https://mealie.example --dry-run` to validate the review and inspect `reports/preflight.json` and `reports/preflight.txt` under the plan directory. The preview makes no changes to Mealie. To apply the same reviewed plan, run `mealietidy apply --base-url https://mealie.example`; the command repeats all checks and then writes approved changes without another prompt. A previous dry run is optional. `apply` groups newly approved changes by food, makes one PUT per food, verifies the result, and records operation values and outcomes in `journal.jsonl`. You can approve more rows and apply again, including rows for a previously updated food; verified operations are skipped. For plans applied with an older mealietidy release, open the unchanged review with `mealietidy edit` before adding approvals so the older journal can be upgraded. A changed food is reported as blocked while independent approved foods can still be applied; the command exits nonzero for a partial result. For a merge, select the food to retain in the editor; the source name and aliases are added to its aliases, and recipe and shopping-list references are checked before and after the merge. For deletion, select the food to remove; it is allowed only when current recipe, shopping-list, and substitution reference counts are all zero, and the command verifies those counts again before deleting. Merges and deletions require the exact tested Mealie version and OpenAPI hash. A merge source with substitutions or aliases colliding with another food is blocked.

You may pass the token with `--token`; it overrides the environment and YAML values, but a command-line token can appear in shell history or process listings. Use `--timeout SECONDS` on `export` or `apply` when necessary. HTTP is accepted only for localhost; remote instances require HTTPS. Keep plan files private and outside Git. The operator is responsible for backups before applying changes.

Development verification uses disposable Mealie v3.28.0 instances. The actual instance URL will be supplied after implementation; API compatibility must be verified before writes are enabled there.

## Manual development instance

From this directory, start Mealie and seed its built-in `en-US` Foods catalog:

```sh
docker compose up -d
docker compose logs seed
```

Open `http://127.0.0.1:19926` after the seed service reports success. The seed populates each fresh Compose instance and skips an existing catalog if you run `docker compose up` again. Mealie's default local administrator credentials are `changeme@example.com` and `MyPassword`.

When finished, run `docker compose down`. The database lives only in the Mealie container's `tmpfs`; no Mealie data volume is mounted, so removing the containers discards all changes. Starting again creates a fresh database and seeds it again. This manual instance has its own Compose project and port, separate from automated tests.

## Run the tests

From this directory, with `uv` and Docker Compose available:

```sh
uv run poe test
uv run ruff check .
```

The command resets the disposable instance from `compose.test.yaml`, starts Mealie v3.28.0, invokes [Mealie's built-in Foods seeder](https://mealie.io/documentation/getting-started/faq/) with the `en-US` locale, discovers every `tests/test_*.py` test, runs the API verification and `apply` integration probe, and stops Compose even if a test fails. Add future automated tests as `tests/test_*.py` so the same command discovers them. Mealie's SQLite data lives on `tmpfs` with no data volumes; each run starts with a fresh catalog. The local seeding check created 580 Foods. The integration scripts use Mealie's disposable default administrator credentials unless `MEALIE_TEST_USERNAME` and `MEALIE_TEST_PASSWORD` are set. The test instance's API matrix, OpenAPI hash, and known limits are in [docs/compatibility.md](docs/compatibility.md).
