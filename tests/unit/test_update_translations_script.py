"""Operator contracts shared by the local and server workflow entry points."""

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from localize import workflow
from localize import workflow_publication as publication

REPO_ROOT = Path(__file__).resolve().parents[2]
CODERABBIT_MAX_REVIEWABLE_FILES = 100


def test_default_max_files_per_pr_stays_under_coderabbit_review_limit():
    assert workflow.DEFAULT_MAX_FILES_PER_PR == publication.DEFAULT_MAX_FILES_PER_PR
    assert 0 < publication.DEFAULT_MAX_FILES_PER_PR < CODERABBIT_MAX_REVIEWABLE_FILES


@pytest.mark.parametrize("limit", ["0", "-1", "100", "150"])
def test_oversized_or_invalid_batch_limits_fail_before_work(monkeypatch, limit):
    monkeypatch.setattr(workflow, "run", lambda *_: pytest.fail("invalid cap reached workflow"))
    assert workflow.main(["--max-files-per-pr", limit]) == 1


def test_env_example_documents_max_files_per_pr_override():
    env_example = (REPO_ROOT / "docker/.env.example").read_text()
    match = re.search(r"^MAX_FILES_PER_PR=(\d+)", env_example, re.MULTILINE)
    assert match is not None
    assert int(match.group(1)) == publication.DEFAULT_MAX_FILES_PER_PR
    assert "150 files" not in env_example


@pytest.mark.parametrize("head,title", [
    ("translation-updates-existing", "Update translations"),
    ("manual-review", "[PIPELINE-BLOCK] Await source review"),
])
def test_pending_pr_guard_blocks_existing_work(monkeypatch, capsys, head, title):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/gh")
    commands = []

    def query(*args, **kwargs):
        commands.append(args)
        return json.dumps([{"number": 42, "headRefName": head, "title": title}])

    monkeypatch.setattr(workflow, "command", query)
    assert workflow.pending_pr_guard("upstream/project") is True
    assert commands[0][commands[0].index("--repo") + 1] == "upstream/project"
    assert commands[0][commands[0].index("--author") + 1] == "@me"
    assert '"pending_pr_guard"' in capsys.readouterr().out


def test_pending_pr_guard_does_not_swallow_gh_failures(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/gh")

    def failed_query(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(workflow, "command", failed_query)
    with pytest.raises(subprocess.CalledProcessError):
        workflow.pending_pr_guard("upstream/project")


def test_pending_pr_guard_does_not_block_unrelated_pr(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/gh")
    monkeypatch.setattr(workflow, "command", lambda *args, **kwargs: json.dumps([
        {"number": 43, "headRefName": "fix-typo", "title": "Repair unrelated typo"},
    ]))
    assert workflow.pending_pr_guard("upstream/project") is False


def test_publishing_checks_pending_prs_with_gh_login_without_env_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/gh")
    monkeypatch.setattr(workflow, "command", lambda *args, **kwargs: json.dumps([
        {"number": 42, "headRefName": "translation-updates-existing", "title": "Translations"},
    ]))
    assert workflow.pending_pr_guard("upstream/project", required=True) is True


def test_git_source_needs_neither_transifex_cli_nor_source_pull(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow.shutil, "which", lambda *_: pytest.fail("tx lookup for git source"))
    monkeypatch.setattr(workflow.subprocess, "run", lambda *_a, **_k: pytest.fail("tx run for git source"))
    workflow.pull_source({"translation_source": "git"}, tmp_path)


@pytest.mark.parametrize("pull_sources", [False, True])
def test_transifex_pull_is_bounded_and_source_pull_is_opt_in(monkeypatch, tmp_path, pull_sources):
    monkeypatch.setenv("TX_PULL_TIMEOUT", "123")
    monkeypatch.delenv("TX_PULL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/tx")
    calls = []
    monkeypatch.setattr(workflow.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    workflow.pull_source({"translation_source": "transifex", "supported_locales": ["de", "fr"], "pull_source_files_from_transifex": pull_sources}, tmp_path)
    argv, options = calls[0]
    assert argv[0][:2] == ["tx", "pull"]
    assert "-t" in argv[0] and "-f" in argv[0] and "--use-git-timestamps" in argv[0]
    assert ("-s" in argv[0]) is pull_sources
    assert argv[0][argv[0].index("-l") + 1] == "de,fr"
    assert options["cwd"] == tmp_path
    assert options["timeout"] == 123
    assert options["check"] is True


@pytest.mark.parametrize("timeout", ["0", "-1", "invalid"])
def test_transifex_invalid_timeout_cannot_start_a_pull(monkeypatch, tmp_path, timeout):
    monkeypatch.setenv("TX_PULL_TIMEOUT", timeout)
    monkeypatch.delenv("TX_PULL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/tx")
    monkeypatch.setattr(workflow.subprocess, "run", lambda *_a, **_k: pytest.fail("unbounded pull started"))
    with pytest.raises(ValueError):
        workflow.pull_source({"translation_source": "transifex", "supported_locales": ["de"]}, tmp_path)


def test_transifex_timeout_is_not_treated_as_a_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "/test/tx")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(workflow.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        workflow.pull_source({"translation_source": "transifex", "supported_locales": ["de"]}, tmp_path)


def _config_case(tmp_path):
    repo = tmp_path / "target"
    (repo / "i18n").mkdir(parents=True)
    config_path = tmp_path / "profile/config.yaml"
    config_path.parent.mkdir()
    (config_path.parent / "glossary.json").write_text("{}")
    config = {"target_project_root": str(repo), "input_folder": "i18n",
              "supported_locales": [{"code": "de", "name": "German"}], "dry_run": False}
    config_path.write_text(yaml.safe_dump(config))
    return repo, config_path, config


@pytest.mark.parametrize("source,expected", [(None, "transifex"), ("GIT", "git"), ("Transifex", "transifex")])
def test_translation_source_defaults_and_normalization(tmp_path, source, expected):
    _, path, config = _config_case(tmp_path)
    if source is not None:
        config["translation_source"] = source
    result, *_ = workflow.prepare_config(SimpleNamespace(repo=None, locales=[]), path, config,
                                        tmp_path / "state", tmp_path / "report")
    assert result["translation_source"] == expected


def test_unknown_translation_source_fails_instead_of_falling_back(tmp_path):
    _, path, config = _config_case(tmp_path)
    config["translation_source"] = "gti"
    with pytest.raises(ValueError, match="translation_source"):
        workflow.prepare_config(SimpleNamespace(repo=None, locales=[]), path, config,
                                tmp_path / "state", tmp_path / "report")


def test_dry_run_environment_override_prevents_source_models_and_publication(monkeypatch, tmp_path):
    repo, path, _ = _config_case(tmp_path)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    state = tmp_path / "state"
    monkeypatch.setenv("LOCALIZE_DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATION_DIFF_BASE", raising=False)
    monkeypatch.setattr(workflow, "prepare_checkout", lambda *_a, **_k: ("a" * 40, "main"))
    monkeypatch.setattr(workflow, "discover", lambda *_a: ({"messages_de.properties": object()}, set()))
    from localize import app_config
    monkeypatch.setattr(app_config, "validate_config", lambda *_a, **_k: [])
    for name in ("pending_pr_guard", "pull_source", "run_engine", "run_semantic", "run_quality", "publish_receipt"):
        monkeypatch.setattr(workflow, name, lambda *_a, _name=name, **_k: pytest.fail(f"dry run called {_name}"))
    assert workflow.main(["--config", str(path), "--state-dir", str(state), "--publish"]) == 0
    plans = list((state / "runs").glob("*/plan.json"))
    assert len(plans) == 1
    assert json.loads(plans[0].read_text())["model_calls"] == 0
    assert not (state / "source-baseline.txt").exists()


def test_quality_audit_scope_override_is_forwarded(monkeypatch, tmp_path):
    from localize import translation_quality_gate
    calls = []
    monkeypatch.setenv("TRANSLATION_QUALITY_AUDIT_SCOPE", "full")
    monkeypatch.setattr(translation_quality_gate, "main", lambda args: calls.append(args) or 0)
    assert workflow.run_quality(tmp_path / "config.yaml", tmp_path, tmp_path / "i18n",
                                ["i18n/messages_de.properties"], tmp_path / "report") == 0
    argv = calls[0]
    assert argv[argv.index("--audit-scope") + 1] == "full"


def test_structured_events_preserve_field_boundaries(capsys):
    workflow.event("example", count=0, source="transifex", path="folder with spaces")
    line = capsys.readouterr().out.strip()
    assert line.startswith("PIPELINE_EVENT ")
    assert json.loads(line.removeprefix("PIPELINE_EVENT ")) == {
        "event": "example", "count": 0, "source": "transifex", "path": "folder with spaces",
    }


def test_pr_body_includes_real_run_summary_and_all_stage_costs(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    summary = {"changed_values_count": 8, "candidate_keys_count": 10,
               "model_translation_keys_count": 6, "model_translation_failed_count": 0,
               "translation_memory_reused_count": 4, "source_identical_skipped_count": 2}
    usage = {"totals": {"estimated_cost_usd": 1.25},
             "stages": {"semantic_review": {"totals": {"total_tokens": 200}}}}
    (logs / "translation_summary.json").write_text(json.dumps(summary))
    (logs / "token_usage_summary.json").write_text(json.dumps(usage))
    body = publication._body(tmp_path, base="a" * 40, files=["i18n/messages_de.properties"], batch=1, total=1)
    for key, value in summary.items():
        assert f'"{key}": {value}' in body
    assert '"estimated_cost_usd": 1.25' in body
    assert '"semantic_review"' in body


def test_pr_body_has_a_bounded_size(tmp_path):
    (tmp_path / "translation_quality_report.md").write_text("quality detail " * 8000)
    body = publication._body(tmp_path, base="a" * 40, files=["i18n/messages_de.properties"], batch=1, total=1)
    assert len(body) < 65536
    assert "truncated" in body


def test_health_check_reads_github_token_from_main_or_mobile_install():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text()

    assert (
        'for env_file in "$INSTALL_ROOT/docker/.env" "$MOBILE_INSTALL_ROOT/docker/.env"'
        in script
    )
    assert 'if [ -z "$GITHUB_TOKEN" ] && [ -f "$env_file" ]; then' in script


def test_health_check_alerts_on_stale_completed_cron_runs():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text()

    assert "cron_log_files()" in script
    assert "combine_cron_logs()" in script
    assert 'gzip -cd -- "$file"' in script
    assert (
        'MAX_CRON_SUCCESS_AGE_SECONDS="${MAX_CRON_SUCCESS_AGE_SECONDS:-93600}"'
        in script
    )
    assert 'local max_success_age_sec="$3"' in script
    assert "last completed run is too old" in script
    assert (
        'check_cron_log "Main service" "$main_log" "$MAX_CRON_SUCCESS_AGE_SECONDS"'
        in script
    )
    assert (
        'check_cron_log "Mobile app service" "$mobile_log" "$MAX_CRON_SUCCESS_AGE_SECONDS"'
        in script
    )


def test_health_check_verifies_job_heartbeat_attempts():
    script = (REPO_ROOT / "scripts" / "check-translation-services.sh").read_text()

    assert "Warning: Health check ping failed" in script
    assert "did not attempt a heartbeat" in script
    assert "Sending heartbeat to health check URL" in script
