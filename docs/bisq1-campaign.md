# Bisq 1 additive campaign (2026-10-20 pilot)

`scripts/run-bisq1-campaign.py` is scoped to the approved pilot snapshot:
upstream `2dab5f8f81d6937f8bc7f6308908894339fa0734` and the independently
reviewed German file with SHA-256
`c136811bf6e49ec4e5cbfcf03c869d5f489552d965272bc069039966d4cce1f3`.
It adds other locales to that staged patch without committing or publishing.
It deliberately refuses a different starting patch. A future source snapshot or
German review needs an explicit new approval and corresponding implementation.

Use the signed installed snapshot containing the coordinator and lifetime-budget
support. For example, with its venv Python and canonical state directory:

```sh
python scripts/run-bisq1-campaign.py --work-dir /absolute/canonical/state \
  --locales fr ja ru cs es fa it pl pt-br pt th vi zh-hans zh-hant --prepare-only
python scripts/run-bisq1-campaign.py --work-dir /absolute/canonical/state \
  --resume /absolute/canonical/state/campaigns/CAMPAIGN_ID
```

The prepare step makes no model calls. It freezes all target hashes, English and
upstream HEAD, staged index tree, profile/glossary/runtime context, locale list
and the existing all-time SQLite request count. `origin.json` retains the fixed
source/context/allowance fields; `manifest.json` advances accepted locale state.
Do not start another campaign to obtain a new allowance. This pilot has one
explicit authorization for **3,000 additional requests**, including retries,
sample calls and semantic review. The captured ceiling is prior all-time count
plus 3,000 (3,358 when the prior count is 358), retained on every resume. Each
child uses that absolute lifetime ceiling and the same daily ceiling against
the original canonical `run_safeguards.sqlite3`. The normal profile's daily
1,000 limit and sample limit of 60 remain unchanged.

The parent holds canonical `job.lock`. Its descriptor is deliberately inherited
by paid children, so parent interruption does not release the lock while a child
is still running. A locale runs in its own clean sparse candidate checkout,
pinned to the frozen commit without fetching or merging newer English. The
canonical `.env` is loaded silently in the child; credentials are not copied
into the candidate or report. Canonical ledger/memory seed that attempt, while
queues and generated report state stay inside it.

The existing runner performs sample, draft, holistic, structural, semantic and
quality checks. Before promotion the coordinator independently matches actual
changed keys to English-hydrated staged entries, quality coverage (including
expected source-identical exemptions), completed semantic status and final
serialized ledger hashes. Only the current locale's file is staged in the
canonical checkout. Its ledger entries replace only that filename's entries;
its memory entries merge only within that locale, preserving conflicts and
other locales. German bytes and every unrelated target remain bound to the
manifest throughout.

Each promotion has saved before/after target, index, ledger, memory and manifest
snapshots under `promotions/`. On interruption, resume rolls back incomplete
owned writes without touching request history, or recognizes a completed
promotion. Unexpected human edits cause a stop instead of being overwritten.
Completed gated candidate reports are reused before creating a new child,
including reports left before an attempt receipt was saved. Other failures stay
isolated with reports and provisional checkpoints available for retry.

Reports live under `campaigns/ID/runs/LOCALE-TIMESTAMP/`; `execution.log` captures
child output and `latest-run.txt` points to its usual runner report directory.
New accepted locales are `pipeline_validated`; the already reviewed German
locale is `independent_reviewed`. These are distinct states. Independent meaning
review and a final combined-patch review are still required before a future PR.

Passing `profiles/bisq1/config.yaml` directly to `run-local-translation.sh` is not
an equivalent recovery or campaign command. The raw profile has placeholder
paths and does not load the canonical local key, historical English baseline,
accepted state, approved staged-patch manifest or shared campaign allowance.
An older main checkout may also lack the model compatibility, strict source
hydration, candidate checkpoint and lifetime-budget fixes. Use the approved
installed runner/coordinator so those operational guarantees are present.
