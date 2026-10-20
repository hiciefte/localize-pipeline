"""A campaign adds only gated locales to the already reviewed German patch."""

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from localize.properties_parser import parse_properties_file
from localize.translation_memory import TranslationMemory, write_translation_memory, load_translation_memory_strict
from localize import translate_localization_files as runtime

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("campaign", ROOT / "scripts/run-bisq1-campaign.py")
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    repo = state / "bisq"
    resources = repo / campaign.RESOURCE_PATH
    resources.mkdir(parents=True)
    (resources / "displayStrings.properties").write_text("label=Account {0}\nstable=Wallet\n")
    for locale in campaign.profile_locales():
        (resources / campaign.filename(locale)).write_text("label=Old {0}\nstable=Wallet\n")
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Campaign test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "Baseline")
    git(repo, "checkout", "-b", campaign.BRANCH)
    git(repo, "remote", "add", "origin", campaign.UPSTREAM)
    german = resources / campaign.filename("de")
    german.write_text("label=Konto {0}\nstable=Wallet\n")
    git(repo, "add", str(german))
    monkeypatch.setattr(campaign, "FROZEN_HEAD", git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(campaign, "GERMAN_SHA", campaign.sha(german))
    source = parse_properties_file(str(resources / "displayStrings.properties"))[1]
    ledger = {campaign.filename("de"): runtime.build_file_key_ledger(
        source, parse_properties_file(str(german))[1])}
    runtime.save_translation_key_ledger(str(state / "ledger.json"), ledger)
    memory = TranslationMemory()
    memory.record("Account {0}", "Konto {0}", locale="de", format_id="java_properties")
    write_translation_memory(state / "memory.json", memory)
    with sqlite3.connect(state / "run_safeguards.sqlite3") as db:
        db.execute("CREATE TABLE requests (created REAL NOT NULL, model TEXT NOT NULL)")
        db.executemany("INSERT INTO requests VALUES (0, 'old')", [(), (), ()])
    return state


def start(state):
    directory = state / "campaigns" / "test"
    return directory, campaign.create_campaign(state, directory, ["cs", "fr"])


def candidate(state, directory, manifest, locale="cs"):
    attempt = directory / "runs" / (locale + "-test")
    repo = attempt / "bisq"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(state / "bisq"), str(repo)], check=True)
    resources = repo / campaign.RESOURCE_PATH
    target = resources / campaign.filename(locale)
    target.write_text("label=Ucet {0}\nstable=Wallet\n")
    git(repo, "add", str(target))
    report = attempt / "report"
    report.mkdir()
    source = parse_properties_file(str(resources / "displayStrings.properties"))[1]
    targets = parse_properties_file(str(target))[1]
    runtime.save_translation_key_ledger(str(report / "ledger.json"), {campaign.filename(locale): runtime.build_file_key_ledger(source, targets)})
    memory = TranslationMemory()
    memory.record("Account {0}", "Ucet {0}", locale=locale, format_id="java_properties")
    write_translation_memory(report / "memory.json", memory)
    (report / "quality.json").write_text(json.dumps({"blocking": False, "source_identical": {"changed_entries_count": 1, "checked_entries_count": 1, "expected_source_identical_count": 0}}))
    (report / "validation.json").write_text(json.dumps({"semantic_review": {"attempted": True, "failed_batches": 0, "failed_locales": []}}))
    return attempt, report


def test_start_pins_complete_content_and_one_lifetime_budget(state):
    directory, manifest = start(state)
    assert manifest["request_ceiling"] == 3003
    assert manifest["initial_request_count"] == 3
    assert manifest["accepted"]["de"]["status"] == "independent_reviewed"
    assert set(manifest["targets"]) == set(campaign.profile_locales())
    assert campaign.load_manifest(directory) == manifest
    campaign.verify_parent(state, manifest)
    assert campaign.request_count(state) == 3


@pytest.mark.parametrize("changed", ["source", "german", "other_locale", "index", "ledger"])
def test_resume_refuses_unapproved_drift(state, changed):
    directory, manifest = start(state)
    repo = state / "bisq"
    if changed == "ledger":
        (state / "ledger.json").write_text("{}")
    else:
        name = {"source": "displayStrings.properties", "german": campaign.filename("de"),
                "other_locale": campaign.filename("fr"), "index": campaign.filename("cs")}[changed]
        target = repo / campaign.RESOURCE_PATH / name
        target.write_text(target.read_text() + "extra=Changed\n")
        if changed == "index": git(repo, "add", str(target))
    with pytest.raises(RuntimeError): campaign.verify_parent(state, manifest)


def test_candidate_requires_exact_source_hydrated_coverage_and_gate(state):
    directory, manifest = start(state)
    attempt, report = candidate(state, directory, manifest)
    proof = campaign.verify_candidate(state, manifest, "cs", attempt, report)
    assert proof["changed_keys"] == ["label"]
    assert proof["coverage"] == 1
    (report / "quality.json").write_text('{"blocking": false, "source_identical": {"changed_entries_count": 0}}')
    with pytest.raises(RuntimeError, match="coverage"):
        campaign.verify_candidate(state, manifest, "cs", attempt, report)
    assert campaign.load_manifest(directory)["accepted"] == manifest["accepted"]


def test_promotion_is_additive_preserves_german_and_memory_conflicts(state):
    directory, manifest = start(state)
    attempt, report = candidate(state, directory, manifest)
    german = (state / "bisq" / campaign.RESOURCE_PATH / campaign.filename("de")).read_bytes()
    memory = load_translation_memory_strict(state / "memory.json")
    memory.record("Account {0}", "Different", locale="cs", format_id="java_properties")
    write_translation_memory(state / "memory.json", memory)
    # This is the approved starting memory, before candidate promotion.
    manifest["state_hashes"] = campaign.state_hashes(state)
    campaign.save_manifest(directory, manifest)
    proof = campaign.verify_candidate(state, manifest, "cs", attempt, report)
    campaign.promote(state, directory, manifest, "cs", attempt, report, proof)
    accepted = campaign.load_manifest(directory)
    campaign.verify_parent(state, accepted)
    assert accepted["accepted"]["cs"]["status"] == "pipeline_validated"
    assert accepted["accepted"]["de"]["status"] == "independent_reviewed"
    assert (state / "bisq" / campaign.RESOURCE_PATH / campaign.filename("de")).read_bytes() == german
    assert set(json.loads((state / "ledger.json").read_text())["files"]) == {campaign.filename("de"), campaign.filename("cs")}
    memory = load_translation_memory_strict(state / "memory.json")
    assert memory.lookup("Account {0}", locale="de", format_id="java_properties") == "Konto {0}"
    assert memory.lookup("Account {0}", locale="cs", format_id="java_properties") is None
    assert campaign.request_count(state) == 3
    assert set(git(state / "bisq", "diff", "--cached", "--name-only").splitlines()) == {
        campaign.RESOURCE_PATH + "/" + campaign.filename("de"),
        campaign.RESOURCE_PATH + "/" + campaign.filename("cs"),
    }


def test_journal_recovers_owned_partial_promotion_without_refunding_requests(state):
    directory, manifest = start(state)
    attempt, report = candidate(state, directory, manifest)
    proof = campaign.verify_candidate(state, manifest, "cs", attempt, report)
    journal = campaign.prepare_promotion(state, directory, manifest, "cs", attempt, report, proof)
    target = state / "bisq" / campaign.RESOURCE_PATH / campaign.filename("cs")
    target.write_bytes((journal / "target.after").read_bytes())
    campaign.atomic_write(campaign.index_path(state / "bisq"), (journal / "index.after").read_bytes())
    with sqlite3.connect(state / "run_safeguards.sqlite3") as db: db.execute("INSERT INTO requests VALUES (0, 'failed')")
    campaign.recover_promotion(state, directory)
    campaign.verify_parent(state, manifest)
    assert campaign.request_count(state) == 4
    assert "cs" not in campaign.load_manifest(directory)["accepted"]
    assert (journal / "journal.json").is_file()


def test_journal_refuses_to_overwrite_human_changes(state):
    directory, manifest = start(state)
    attempt, report = candidate(state, directory, manifest)
    proof = campaign.verify_candidate(state, manifest, "cs", attempt, report)
    campaign.prepare_promotion(state, directory, manifest, "cs", attempt, report, proof)
    german = state / "bisq" / campaign.RESOURCE_PATH / campaign.filename("de")
    german.write_text("human=Preserve me\n")
    with pytest.raises(RuntimeError): campaign.recover_promotion(state, directory)
    assert german.read_text() == "human=Preserve me\n"


def test_child_config_binds_shared_counter_and_fixed_limits(state):
    directory, manifest = start(state)
    attempt = directory / "runs" / "cs-config"
    report = attempt / "report"
    report.mkdir(parents=True)
    job = campaign.load_job()
    path = campaign.write_child_config(job, attempt / "bisq", attempt, ["cs"], report, state, manifest)
    import yaml
    config = yaml.safe_load(path.read_text())
    settings = config["run_safeguards"]
    assert Path(settings["state_path"]).resolve() == (state / "run_safeguards.sqlite3").resolve()
    assert settings["max_requests_total"] == settings["max_requests_per_24h"] == 3003
    assert settings["sample_max_requests"] == 60
    assert config["supported_locales"] == [{"code": "cs", "name": "Czech"}]


def write_completed_candidate(attempt, locale):
    repo = attempt / "bisq"
    resources = repo / campaign.RESOURCE_PATH
    target = resources / campaign.filename(locale)
    target.write_text("label=Translated {0}\nstable=Wallet\n")
    git(repo, "add", str(target))
    report = attempt / "report"
    report.mkdir()
    source = parse_properties_file(str(resources / "displayStrings.properties"))[1]
    targets = parse_properties_file(str(target))[1]
    runtime.save_translation_key_ledger(str(report / "ledger.json"), {campaign.filename(locale): runtime.build_file_key_ledger(source, targets)})
    write_translation_memory(report / "memory.json", TranslationMemory())
    (report / "quality.json").write_text(json.dumps({"blocking": False, "source_identical": {"changed_entries_count": 1, "checked_entries_count": 1, "expected_source_identical_count": 0}}))
    (report / "validation.json").write_text(json.dumps({"semantic_review": {"attempted": True, "failed_batches": 0, "failed_locales": []}}))
    (attempt / "latest-run.txt").write_text(str(report))
    return report


def test_loop_resumes_failed_locale_without_rerunning_accepted_outputs(state):
    directory, manifest = start(state)
    calls = []
    def worker(state, directory, manifest, locale, attempt):
        calls.append(locale)
        with sqlite3.connect(state / "run_safeguards.sqlite3") as db: db.execute("INSERT INTO requests VALUES (0, 'synthetic')")
        if locale == "fr": raise RuntimeError("Synthetic provider failure")
        return write_completed_candidate(attempt, locale)
    assert campaign.execute(state, directory, worker=worker) == ["fr"]
    assert calls == ["cs", "fr"]
    assert campaign.load_manifest(directory)["accepted"]["cs"]["status"] == "pipeline_validated"
    calls.clear()
    def retry(state, directory, manifest, locale, attempt):
        calls.append(locale)
        return write_completed_candidate(attempt, locale)
    assert campaign.execute(state, directory, worker=retry) == []
    assert calls == ["fr"]
    assert campaign.load_manifest(directory)["request_ceiling"] == 3003
    assert campaign.request_count(state) == 5
    campaign.verify_parent(state, campaign.load_manifest(directory))


def test_completed_report_reused_even_before_attempt_receipt_was_saved(state):
    directory, manifest = start(state)
    attempt = campaign.prepare_attempt(state, directory, manifest, "cs")
    write_completed_candidate(attempt, "cs")
    manifest["locales"] = ["cs"]
    campaign.save_manifest(directory, manifest)
    origin = json.loads((directory / "origin.json").read_text())
    origin["locales"] = ["cs"]
    campaign.write_json(directory / "origin.json", origin)
    def forbidden(*args): raise AssertionError("Already gated output must not request a new child")
    assert campaign.execute(state, directory, worker=forbidden) == []
    assert "cs" in campaign.load_manifest(directory)["accepted"]
    assert campaign.request_count(state) == 3


@pytest.mark.parametrize("tamper", ["request_ceiling", "initial_request_count", "source_hash", "locales"])
def test_resume_immutable_allowance_and_source_binding(state, tamper):
    directory, manifest = start(state)
    manifest[tamper] = 9999 if tamper != "locales" else ["cs"]
    campaign.save_manifest(directory, manifest)
    with pytest.raises(RuntimeError, match="Immutable"):
        campaign.execute(state, directory, worker=lambda *args: None)
    assert campaign.request_count(state) == 3


def test_child_loads_canonical_env_and_effective_budget_without_api(state, monkeypatch):
    directory, manifest = start(state)
    attempt = campaign.prepare_attempt(state, directory, manifest, "cs")
    (state / ".env").write_text("OPENAI_API_KEY=synthetic-campaign-key\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    job = campaign.load_job()
    def run(args, child_state):
        import os, yaml
        assert os.environ["OPENAI_API_KEY"] == "synthetic-campaign-key"
        job.prepare_checkout(attempt / "bisq")
        report = attempt / "config-test"
        report.mkdir()
        config_path = job.write_config(attempt / "bisq", child_state, args.locales, report)
        assert yaml.safe_load(config_path.read_text())["run_safeguards"]["max_requests_total"] == 3003
    job.run = run
    monkeypatch.setattr(campaign, "load_job", lambda: job)
    campaign.run_child(attempt / "child.json")
    assert campaign.request_count(state) == 3


def test_promotion_write_failure_rolls_back_partial_state(state, monkeypatch):
    directory, manifest = start(state)
    attempt, report = candidate(state, directory, manifest)
    proof = campaign.verify_candidate(state, manifest, "cs", attempt, report)
    original = campaign.atomic_write
    failed = []
    def fail_memory(path, data):
        if Path(path) == state / "memory.json" and not failed:
            failed.append(True)
            raise OSError("Synthetic interrupted memory write")
        return original(path, data)
    monkeypatch.setattr(campaign, "atomic_write", fail_memory)
    with pytest.raises(OSError, match="interrupted"):
        campaign.promote(state, directory, manifest, "cs", attempt, report, proof)
    campaign.verify_parent(state, manifest)
    assert "cs" not in campaign.load_manifest(directory)["accepted"]
    assert campaign.request_count(state) == 3


def test_index_metadata_edits_are_not_overwritten_by_recovery(state):
    directory, manifest = start(state)
    attempt, report = candidate(state, directory, manifest)
    proof = campaign.verify_candidate(state, manifest, "cs", attempt, report)
    campaign.prepare_promotion(state, directory, manifest, "cs", attempt, report, proof)
    git(state / "bisq", "update-index", "--assume-unchanged", campaign.RESOURCE_PATH + "/" + campaign.filename("de"))
    before = campaign.index_path(state / "bisq").read_bytes()
    with pytest.raises(RuntimeError, match="Index bytes"):
        campaign.recover_promotion(state, directory)
    assert campaign.index_path(state / "bisq").read_bytes() == before


def test_canonical_lock_descriptor_is_forwarded_to_paid_child(state, monkeypatch):
    import fcntl
    directory, manifest = start(state)
    observed = []
    original = subprocess.run
    def fake_run(command, **kwargs):
        if "--child" not in command: return original(command, **kwargs)
        spec = json.loads(Path(command[-1]).read_text())
        observed.append(kwargs["pass_fds"])
        write_completed_candidate(Path(spec["attempt"]), spec["locale"])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(campaign.subprocess, "run", fake_run)
    with (state / "job.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert campaign.execute(state, directory, lock_fd=lock.fileno()) == []
        assert observed == [(lock.fileno(),), (lock.fileno(),)]


@pytest.mark.parametrize("change", ["origin", "branch", "effective_model"])
def test_parent_identity_and_effective_model_context_are_bound(state, monkeypatch, change):
    directory, manifest = start(state)
    if change == "origin": git(state / "bisq", "remote", "set-url", "origin", "https://example.invalid/other.git")
    elif change == "branch": git(state / "bisq", "checkout", "-b", "unapproved")
    else: monkeypatch.setenv("REVIEW_MODEL_NAME", "unapproved-model")
    with pytest.raises(RuntimeError): campaign.verify_parent(state, manifest)


def test_lock_survives_parent_descriptor_close_while_child_runs(tmp_path):
    import fcntl
    import time
    lock_path = tmp_path / "job.lock"
    ready = tmp_path / "ready"
    lock = lock_path.open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    process = subprocess.Popen([sys.executable, "-c", "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(20)", str(ready)], pass_fds=(lock.fileno(),))
    try:
        for _ in range(100):
            if ready.exists(): break
            time.sleep(0.01)
        assert ready.exists()
        lock.close()  # Simulate parent termination; child retains the inherited descriptor.
        with lock_path.open("a") as contender:
            with pytest.raises(BlockingIOError): fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        lock.close()
        process.terminate()
        process.wait(timeout=5)
    with lock_path.open("a") as contender: fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
