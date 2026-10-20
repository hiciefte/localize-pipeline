# How to Run the Translation Service Locally

`run-local-translation.sh` runs the full workflow used by server jobs. Both
launchers call `localize.workflow`, including source preparation, a bounded
sample, translation and holistic review, final semantic review, and the
quality gate. A local run leaves a patch for review by default. Publishing is
an explicit option.

## Run Without Docker

From the project root, create the virtual environment and install dependencies:

```bash
python3.11 -m venv venv
venv/bin/pip install -r requirements-dev.txt
```

Provide `OPENAI_API_KEY` through your environment or local `.env`; never commit
it. Transifex profiles also need `TX_TOKEN` and the `tx` CLI. GitHub publishing
needs `gh`, repository access, and working Git commit signing. A local run that
does not publish does not require a publishing identity.

Select any config file or profile directory. The included named profiles are
`bisq1`, `bisq` (Bisq 2), and `bisq-mobile`:

```bash
./run-local-translation.sh --profile bisq1 --check
./run-local-translation.sh profiles/bisq1 --locales de
./run-local-translation.sh profiles/bisq-mobile
./run-local-translation.sh path/to/config.yaml --repo /path/to/clean/checkout
```

Without a positional path or `--profile`, the launcher uses
`TRANSLATOR_CONFIG_FILE` if set, otherwise the checkout's `config.yaml`.
Relative config arguments resolve from the directory where you invoke the
script. Profile-relative glossary and configuration paths remain relative to
the original profile.

`--check` inspects the run without Transifex pulls or model calls. It may fetch
Git; add `--no-fetch` to inspect the available checkout without fetching. A
normal run makes real API calls and edits the selected translation files.

Useful options:

| Option | Purpose |
|---|---|
| `--repo PATH` | Select the target Git checkout. Existing local changes are preserved by refusing an unsafe run. |
| `--state-dir PATH` | Select persistent job state and run reports. Reuse the same directory across retries. |
| `--locales de fr` | Limit the run to configured locales. |
| `--base-ref SHA` | Supply a source baseline when initializing incremental translation state. |
| `--check` | Prepare and inspect without pulling Transifex or calling models. |
| `--no-fetch` | Use locally available Git history. |
| `--publish` | Publish a successful run as signed commits and PRs. |
| `--publish-run PATH` | Publish a previously successful run from its saved report without regenerating translations. |

The selected profile still controls models, glossary, locale layout, semantic
policy and request limits. The full workflow requires semantic review to be
enabled. Profiles without safeguards receive a persistent request budget and
bounded sample defaults; explicit profile limits remain in effect. Selecting a
different profile does not bypass the workflow's final checks. See [the Bisq 1 guide](bisq1-local.md) for its project
policy and existing-state migration details.

## Review, Publish And Retry

After a successful run, inspect its saved patch, quality report and Git diff.
Then publish that candidate using the run directory reported by the launcher:

```bash
./run-local-translation.sh --publish-run /absolute/path/to/successful/run
```

Or run and publish in one invocation:

```bash
./run-local-translation.sh --profile bisq1 --fork your-account/bisq --publish
```

Bundled profiles identify the upstream repository and base branch. Select your
fork with `--fork owner/repository` or `FORK_REPO_NAME`; custom profiles can also
provide `--upstream owner/repository` and `--target-branch main`.

Publishing requires successful quality checks and a matching reviewed candidate.
It fails if signing fails; there is no unsigned fallback. Existing pending PRs
prevent repeated publication runs.

Local state defaults to `.localize/<profile identity>/` in the pipeline checkout,
unless `--state-dir` or `LOCALIZE_STATE_DIR` selects another location. Explicit
ledger, memory and budget paths from operator configs are preserved; container
placeholder paths are resolved for local execution.

Persistent state records accepted outputs and consumed requests. Each attempt
has separate working ledger, memory and reports; failed checks do not promote
its ledger or memory to accepted state. Request consumption is retained even
when a run fails. Reuse the same state after fixing a problem, and keep failed
reports for diagnosis. Do not delete budget state or create a fresh directory
to bypass a request limit.

After publication, the reviewed candidate remains available locally. Once all
of its PR batches are merged, the next normal run verifies that upstream contains
the exact reviewed files before advancing the checkout. If a maintainer changed
those files, or you edited the local candidate, the workflow preserves the local
work and stops for review.

The old `scripts/run-bisq1-local.py` entry point is a compatibility wrapper for
the shared workflow. `localize run` is the lower-level engine command and does
not perform the surrounding repository, final-check and publishing stages.

## Run With Docker

Docker runs the same workflow and adds the server's runtime environment.
`update-translations.sh` publishes by default; pass `--no-publish` for a local
review run.

Create `docker/.env` and the key files described in
[new-project-deployment.md](new-project-deployment.md). By default, Compose
reads the deploy key from `secrets/deploy_key/id_ed25519` and the signing key
from `secrets/gpg_bot_key/bot_secret_key.asc`; `DEPLOY_KEY_FILE` and
`GPG_BOT_KEY_FILE` can select other host paths.

The keys are read-only runtime secrets. Compose mounts them under
`/run/secrets/` only when the container starts, and the entrypoint installs or
imports them for that transient run. They are not stored in the image.

From the project root:

```bash
export DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1
docker compose --env-file docker/.env -f docker/docker-compose.yml build
docker compose --env-file docker/.env -f docker/docker-compose.yml run -T --rm translator /app/update-translations.sh --no-publish
```

Omit `/app/update-translations.sh --no-publish` to use the normal server default
that publishes successful runs. `-T` disables pseudo-TTY allocation; add
`--interactive=false` when a script must not pass stdin to the container.
