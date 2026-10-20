# Translation run safeguards

Bisq 1, Bisq 2 and Bisq Mobile enable the same safeguards in their profiles.
The safeguards do not change the configured models or relax quality checks.

## Validate a sample first

Before processing the full queue, the runner translates up to five selected
keys in each of three files with pending work. Placeholder-bearing and longer
messages come first. The sample uses isolated queue/output copies, the normal
draft and holistic review, serialization/placeholder/glossary validation,
configured semantic rules, and semantic AI review when enabled.

Any sample error, incomplete review or exhausted sample budget stops the run
before the full queue begins. The sample has a separate 60-request ceiling;
those requests also count toward the shared rolling limit. Sample success is
not a quality certificate for the remaining files: normal publication gates
still apply.

`logs/sample_validation_summary.json` records sample results.
`logs/run_stop.json` records a sample or budget stop. Exit status is nonzero,
so the shell runner cannot continue to PR publication or a success heartbeat.

## Resume accepted work

The persistent SQLite store keeps exact-input checkpoints for files that
passed draft/holistic processing and deterministic validation, plus semantic
review responses with no findings. Failed or partial file results and flagged
semantic reviews are not accepted checkpoints.

Identical retries reuse these results. The normal semantic and publication
gates still run; cached translation-stage output is not semantic approval.
The checkpoint fingerprint includes source and target content, selected keys,
models, glossary, policy and pipeline code. Changes to those inputs invalidate
the checkpoint. Existing translation memory can still supply drafts, which
then receive the current review and validation.

Bisq 1 keeps this store in its workspace across dated run directories, even
when the whole-run ledger has not advanced. Pending local changes still need
review before the runner can prepare that checkout again.

## Bound provider requests across restarts

| Profile | Maximum requests in a rolling 24 hours |
| --- | ---: |
| Bisq 1 | 1,000 |
| Bisq 2 | 2,000 |
| Bisq Mobile | 750 |

Every outbound chat-completion attempt reserves a request atomically before
sending. Drafts, holistic/semantic review, application retries and token-parameter
fallbacks share the limit. SDK retries are disabled for guarded OpenAI routes.
Failed requests and ambiguous timeouts consume a reservation. Cache hits do not.

The limit persists across process/container restarts and does not reset at
midnight. It counts requests observed since installation, not historical
account usage. It is a request limit, not a dollar cap; models, prompt sizes
and output lengths still affect cost.

```yaml
run_safeguards:
  state_path: /app/logs/run_safeguards.sqlite3
  max_requests_per_24h: 2000
  sample_files: 3
  sample_keys: 5
  sample_max_requests: 60
```

Use a separate persistent store for each pipeline. Docker profiles place it
in the existing mounted logs directory; Bisq 1's manual runner resolves it into
its workspace. Preserve this store during recovery. Removing it discards both
the rolling counter and accepted checkpoints.

After a stop, inspect the sample/usage reports, correct the cause, then rerun.
For a budget stop, wait for rolling capacity or deliberately adjust the profile
limit. Do not delete the store or silently loop full runs. Provider setup,
state-storage failures and invalid limits fail closed.
