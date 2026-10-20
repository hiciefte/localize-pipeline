"""Exercise the shared workflow with real Git repositories and no paid requests.

The subprocess boundary is intentional: the legacy engine initializes module
globals at import time, while the suite's global fixtures replace those globals.
These tests exercise the configuration and state that an actual operator gets.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True,
    ).strip()


def make_project(tmp_path, name="desktop", layout="suffix"):
    project = tmp_path / name
    repo = project / "repo"
    resources = repo / "resources"
    profile = project / "profile"
    state = project / "state"
    profile.mkdir(parents=True)
    state.mkdir()
    source_values = {"label": "Account", "greeting": "Hello {0}"}
    translations = {
        "de": {"label": "Konto", "greeting": "Hallo {0}"},
        "fr": {"label": "Compte", "greeting": "Bonjour {0}"},
    }
    paths = {}
    for locale, values in {"en": source_values, **translations}.items():
        if layout == "suffix":
            path = resources / ("app.properties" if locale == "en" else f"app_{locale}.properties")
            content = "".join(f"{key}={value}\n" for key, value in values.items())
        else:
            path = resources / locale / "messages.json"
            content = json.dumps(values, ensure_ascii=False, indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        paths[locale] = path
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.name", "Workflow integration test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "Complete baseline")
    base = git(repo, "rev-parse", "HEAD")
    (profile / "glossary.json").write_text("{}\n")
    config = {
        "target_project_root": "/target_repo",
        "input_folder": "resources",
        "glossary_file_path": "glossary.json",
        "translation_source": "git",
        "localization_format": "java_properties" if layout == "suffix" else "json",
        "localization_layout": {"id": layout, "source_locale": "en"},
        "translation_file_filter_glob": "*.properties" if layout == "suffix" else "*.json",
        "process_all_files": True,
        "dry_run": False,
        "retranslate_identical_source_strings": False,
        "model_name": "gpt-4o-mini",
        "review_model_name": "gpt-4o-mini",
        "semantic_review": {"enabled": True, "model": "gpt-4o-mini"},
        "supported_locales": [
            {"code": "de", "name": "German"},
            {"code": "fr", "name": "French"},
        ],
        "run_safeguards": {
            "state_path": "logs/run_safeguards.sqlite3",
            "max_requests_per_24h": 12,
            "sample_files": 1,
            "sample_keys": 2,
            "sample_max_requests": 6,
        },
        "quality_gate": {
            "block_on_pipeline_warnings": True,
            "block_on_semantic_qa_findings": True,
        },
    }
    config_path = profile / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return SimpleNamespace(
        repo=repo, resources=resources, profile=profile, state=state,
        config=config_path, base=base, paths=paths, layout=layout,
    )


HARNESS = r'''
import json
from pathlib import Path
import sys
from localize import workflow

observe = Path(sys.argv[1])
failure = sys.argv[2]
events = []
def wrap(name):
    original = getattr(workflow, name)
    def invoke(*args, **kwargs):
        events.append(name)
        observe.write_text(json.dumps(events))
        if failure == name:
            return 1
        return original(*args, **kwargs)
    setattr(workflow, name, invoke)
for name in ("run_engine", "run_semantic", "run_quality"):
    wrap(name)
raise SystemExit(workflow.main(sys.argv[3:]))
'''


def workflow_environment():
    env = dict(os.environ)
    for key in ("TRANSLATOR_CONFIG_FILE", "LOCALIZE_DRY_RUN", "TRANSLATION_FILTER_GLOB",
                "REVIEW_MODEL_NAME", "LOCALIZE_ALLOW_RESET_LEDGER", "TRANSLATION_DIFF_BASE",
                "GH_TOKEN", "GITHUB_TOKEN", "HEALTHCHECK_URL", "UPSTREAM_REPO_NAME",
                "FORK_REPO_NAME", "FORK_REPO_URL", "TARGET_BRANCH_FOR_PR", "TARGET_REPO_DIR"):
        env.pop(key, None)
    env.update({"OPENAI_API_KEY": "test-only", "OPENAI_BASE_URL": "http://127.0.0.1:1"})
    return env


def run_workflow(project, *extra, failure="", harness=HARNESS):
    observed = project.state / "observed.json"
    result = subprocess.run(
        [sys.executable, "-c", harness, str(observed), failure,
         "--config", str(project.config), "--repo", str(project.repo),
         "--state-dir", str(project.state), "--base-ref", project.base,
         "--no-fetch", *extra],
        cwd=ROOT, env=workflow_environment(), text=True, capture_output=True, timeout=45,
    )
    events = json.loads(observed.read_text()) if observed.exists() else []
    return result, events


def latest_report(project):
    return Path((project.state / "latest-run.txt").read_text().strip())


@pytest.mark.parametrize("layout", ["suffix", "locale_directory"])
def test_full_local_run_checks_unchanged_files_without_publishing(tmp_path, layout):
    project = make_project(tmp_path, layout=layout)
    before = {locale: path.read_bytes() for locale, path in project.paths.items()}
    result, events = run_workflow(project, "--locales", "de")
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == ["run_engine", "run_semantic", "run_quality"]
    assert "Starting Git and Transifex validation" in result.stdout
    report = latest_report(project)
    assert json.loads((report / "translation_quality_report.json").read_text())["blocking"] is False
    assert json.loads((report / "logs/token_usage_summary.json").read_text())["totals"]["calls"] == 0
    assert (report / "translations.patch").read_bytes() == b""
    assert {locale: path.read_bytes() for locale, path in project.paths.items()} == before
    assert git(project.repo, "rev-parse", "HEAD") == project.base
    assert git(project.repo, "diff", "--cached", "--name-only") == ""
    resolved = yaml.safe_load((report / "config.yaml").read_text())
    assert Path(resolved["target_project_root"]) == project.repo
    assert Path(resolved["glossary_file_path"]) == project.profile / "glossary.json"
    assert Path(resolved["run_safeguards"]["state_path"]) == project.state / "run_safeguards.sqlite3"
    assert resolved["run_safeguards"]["max_requests_per_24h"] == 12
    assert [row["code"] for row in resolved["supported_locales"]] == ["de"]
    assert Path(resolved["translation_queue_folder"]).is_relative_to(report)
    assert Path(resolved["translated_queue_folder"]).is_relative_to(report)


@pytest.mark.parametrize("failed_stage", ["run_semantic", "run_quality"])
def test_final_check_failure_does_not_promote_accepted_state_or_reset_budget(tmp_path, failed_stage):
    project = make_project(tmp_path)
    from localize.translation_memory import TranslationMemory, write_translation_memory
    memory = TranslationMemory()
    memory.record("Account", "Konto", locale="de", format_id="java_properties")
    write_translation_memory(project.state / "memory.json", memory)
    # Establish accepted state using the actual engine before attempting a
    # failing run, so overwriting or resetting pre-existing files is detectable.
    result, _ = run_workflow(project)
    assert result.returncode == 0, result.stdout + result.stderr
    from localize.run_safeguards import RunSafeguards
    budget = RunSafeguards(project.state / "run_safeguards.sqlite3", 12, 1, 2, 6)
    budget.reserve("existing request")
    budget.accept("existing accepted output", {"label": "Konto"})
    protected = [project.state / "ledger.json", project.state / "memory.json"]
    before = {path: path.read_bytes() for path in protected}
    result, events = run_workflow(project, failure=failed_stage)
    assert result.returncode != 0
    assert "run_engine" in events and failed_stage in events
    assert {path: path.read_bytes() for path in protected} == before
    assert budget.request_count() == 1
    assert budget.lookup("existing accepted output") == {"label": "Konto"}
    assert git(project.repo, "rev-parse", "HEAD") == project.base


def test_dirty_checkout_refuses_before_engine_and_preserves_user_files(tmp_path):
    project = make_project(tmp_path)
    project.paths["de"].write_text("label=User translation\ngreeting=Hallo {0}\n")
    untracked = project.repo / "notes.txt"
    untracked.write_text("Uncommitted user notes\n")
    before = {path: path.read_bytes() for path in [project.paths["de"], untracked]}
    status = git(project.repo, "status", "--porcelain")
    result, events = run_workflow(project)
    assert result.returncode != 0
    assert events == []
    assert {path: path.read_bytes() for path in before} == before
    assert git(project.repo, "status", "--porcelain") == status
    assert not (project.state / "ledger.json").exists()
    assert not (project.state / "memory.json").exists()


def test_preflight_never_runs_engine_or_final_model_review(tmp_path):
    project = make_project(tmp_path)
    config = yaml.safe_load(project.config.read_text())
    config["translation_source"] = "transifex"
    project.config.write_text(yaml.safe_dump(config))
    before = {locale: path.read_bytes() for locale, path in project.paths.items()}
    result, events = run_workflow(project, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == []
    assert {locale: path.read_bytes() for locale, path in project.paths.items()} == before
    assert not (project.state / "ledger.json").exists()
    assert not (project.state / "memory.json").exists()
    assert git(project.repo, "rev-parse", "HEAD") == project.base


def test_profiles_keep_accepted_state_and_request_accounting_separate(tmp_path):
    desktop = make_project(tmp_path, "desktop", "suffix")
    mobile = make_project(tmp_path, "mobile", "locale_directory")
    from localize.run_safeguards import RunSafeguards
    for project, count in [(desktop, 1), (mobile, 2)]:
        budget = RunSafeguards(project.state / "run_safeguards.sqlite3", 12, 1, 2, 6)
        for _ in range(count):
            budget.reserve("previous request")
    result, _ = run_workflow(desktop, "--locales", "de")
    assert result.returncode == 0, result.stdout + result.stderr
    protected = {path: path.read_bytes() for path in desktop.state.rglob("*") if path.is_file()}
    result, _ = run_workflow(mobile, "--locales", "fr")
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path: path.read_bytes() for path in protected} == protected
    desktop_ledger = json.loads((desktop.state / "ledger.json").read_text())["files"]
    mobile_ledger = json.loads((mobile.state / "ledger.json").read_text())["files"]
    assert set(desktop_ledger) == {"app_de.properties"}
    assert set(mobile_ledger) == {"fr/messages.json"}
    for project, count in [(desktop, 1), (mobile, 2)]:
        with sqlite3.connect(project.state / "run_safeguards.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == count


@pytest.mark.parametrize("script,requested,publishes", [
    ("run-local-translation.sh", [], False),
    ("run-local-translation.sh", ["--publish"], True),
    ("update-translations.sh", [], True),
    ("update-translations.sh", ["--no-publish"], False),
])
def test_local_and_server_launch_the_same_workflow(tmp_path, script, requested, publishes):
    """Observe the process boundary; neither entry point may run its own engine."""
    project = make_project(tmp_path / "profile with spaces")
    calls = tmp_path / "python-calls.jsonl"
    python = tmp_path / "python-spy"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "with Path(os.environ['WORKFLOW_TEST_CALLS']).open('a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    python.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / script), "--config", str(project.config),
         "--repo", str(project.repo), "--state-dir", str(project.state),
         "--no-fetch", *requested],
        cwd=tmp_path,
        env={**os.environ, "LOCALIZE_PYTHON": str(python), "WORKFLOW_TEST_CALLS": str(calls)},
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(invocations) == 1
    arguments = invocations[0]
    assert arguments[:2] == ["-m", "localize.workflow"]
    assert arguments[arguments.index("--config") + 1] == str(project.config)
    assert arguments[arguments.index("--repo") + 1] == str(project.repo)
    assert arguments[arguments.index("--state-dir") + 1] == str(project.state)
    assert ("--publish" in arguments) is publishes
    assert not ({"--publish", "--no-publish"} <= set(arguments))


DETERMINISTIC_HARNESS = r'''
import json
from pathlib import Path
import sys
from localize import workflow

observed = Path(sys.argv[1])
events = []
def record(kind, key=None):
    events.append([kind, key])
    observed.write_text(json.dumps(events))
original_engine = workflow.run_engine

def deterministic_engine(*args, **kwargs):
    from localize import translate_localization_files as runtime
    from localize import translation_semantic_reviewer as reviewer
    async def draft(text, key, existing, sources, language, glossary, semaphore, limiter, index, *args):
        runtime.config.run_safeguards.reserve("deterministic-draft")
        record("draft", key)
        return index, "Neue Nachricht {0}", True
    async def review(**kwargs):
        runtime.config.run_safeguards.reserve("deterministic-review")
        record("review", kwargs["keys_to_review"])
        return {key: "Neue Nachricht {0}" for key in kwargs["keys_to_review"]}
    async def semantic(**kwargs):
        runtime.config.run_safeguards.reserve("deterministic-semantic")
        record("semantic", len(kwargs["changes"]))
        return []
    runtime.translate_text_async = draft
    runtime.holistic_review_async = review
    reviewer.review_translation_changes = semantic
    return original_engine(*args, **kwargs)
workflow.run_engine = deterministic_engine
raise SystemExit(workflow.main(sys.argv[3:]))
'''


def add_missing_translation(project):
    source = project.paths["en"]
    if project.layout == "suffix":
        source.write_text(source.read_text() + "new=New message {0}\n")
    else:
        content = json.loads(source.read_text())
        content["new"] = "New message {0}"
        source.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n")
    git(project.repo, "add", ".")
    git(project.repo, "-c", "commit.gpgsign=false", "commit", "-m", "Add English message")


@pytest.mark.parametrize("layout", ["suffix", "locale_directory"])
def test_full_pipeline_creates_and_reuses_a_reviewed_translation(tmp_path, layout):
    project = make_project(tmp_path, layout=layout)
    add_missing_translation(project)
    english = project.paths["en"].read_bytes()
    result, events = run_workflow(project, "--locales", "de", harness=DETERMINISTIC_HARNESS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {event[0] for event in events} == {"draft", "review", "semantic"}
    assert "Neue Nachricht {0}" in project.paths["de"].read_text()
    assert project.paths["en"].read_bytes() == english
    report = latest_report(project)
    patch = (report / "translations.patch").read_bytes()
    assert patch and b"Neue Nachricht {0}" in patch
    assert json.loads((report / "translation_quality_report.json").read_text())["blocking"] is False
    from localize.run_safeguards import RunSafeguards
    budget = RunSafeguards(project.state / "run_safeguards.sqlite3", 12, 1, 2, 6)
    count = budget.request_count()
    assert count == len(events)
    # Recreate the exact original input as after an interrupted copy-back. The
    # accepted candidate must skip draft/review, while the final sidecar still
    # reviews the resulting patch against the source.
    relative = project.paths["de"].relative_to(project.repo).as_posix()
    git(project.repo, "restore", "--staged", "--worktree", "--", relative)
    (project.state / "observed.json").unlink()
    result, repeated = run_workflow(project, "--locales", "de", harness=DETERMINISTIC_HARNESS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert repeated and all(event[0] == "semantic" for event in repeated)
    assert budget.request_count() == count + len(repeated)
    assert (latest_report(project) / "translations.patch").read_bytes() == patch


@pytest.mark.parametrize("held_lock", [None, "state", "repo"])
def test_saved_publish_uses_receipt_state_and_exact_patch_without_translation(tmp_path, held_lock):
    project = make_project(tmp_path)
    add_missing_translation(project)
    result, _ = run_workflow(project, "--locales", "de", harness=DETERMINISTIC_HARNESS)
    assert result.returncode == 0, result.stdout + result.stderr
    report = latest_report(project)
    patch = (report / "translations.patch").read_bytes()
    receipt_before = json.loads((report / "receipt.json").read_text())
    protected = {path: path.read_bytes() for path in [
        project.state / "ledger.json", project.state / "memory.json",
        project.state / "run_safeguards.sqlite3",
    ]}
    observation = tmp_path / "published.json"
    decoy = tmp_path / "wrong-state"
    harness = r'''
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from localize import workflow, workflow_publication

def forbidden(*args, **kwargs):
    raise AssertionError("Saved publication must not run translation or quality again")
workflow.run_engine = workflow.run_semantic = workflow.run_quality = forbidden
workflow.prepare_publication = lambda *args: None

def publish(repo, base, files, report, config, **kwargs):
    patch = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
    Path(sys.argv[1]).write_text(json.dumps({
        "repo": str(repo), "base": base, "files": files,
        "sha256": hashlib.sha256(patch).hexdigest(),
    }))
    return ["https://github.com/example/project/pull/1"]
workflow_publication.publish = publish
raise SystemExit(workflow.main(sys.argv[2:]))
'''
    handle = None
    if held_lock:
        lock_path = (project.state / "job.lock" if held_lock == "state"
                     else project.repo / ".git/localize-workflow.lock")
        handle = lock_path.open("a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(
            [sys.executable, "-c", harness, str(observation), "--publish-run", str(report),
             "--state-dir", str(decoy), "--repo", str(tmp_path / "wrong-repo"),
             "--config", str(tmp_path / "missing-profile.yaml"),
             "--upstream", "example/project", "--fork", "owner/project", "--target-branch", "master"],
            cwd=ROOT, env=workflow_environment(), text=True, capture_output=True, timeout=30,
        )
    finally:
        if handle:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
    assert result.returncode == (1 if held_lock else 0), result.stdout + result.stderr
    assert not decoy.exists()
    assert {path: path.read_bytes() for path in protected} == protected
    assert (report / "translations.patch").read_bytes() == patch
    if held_lock:
        assert "Another localization run holds" in result.stderr
        assert not observation.exists()
        assert json.loads((report / "receipt.json").read_text()) == receipt_before
    else:
        published = json.loads(observation.read_text())
        assert published["repo"] == str(project.repo)
        assert published["base"] == receipt_before["base"]
        assert published["files"] == receipt_before["changed_files"]
        assert published["sha256"] == hashlib.sha256(patch).hexdigest()
        assert json.loads((report / "receipt.json").read_text())["status"] == "published"


@pytest.mark.parametrize("problem", ["missing_branch", "missing_repositories", "signing"])
def test_publication_preflight_fails_before_translation(tmp_path, problem):
    project = make_project(tmp_path)
    extra = ["--publish"]
    harness = HARNESS
    if problem != "missing_branch":
        extra += ["--target-branch", "master"]
    if problem == "signing":
        git(project.repo, "remote", "add", "origin", "https://github.com/example/project.git")
        git(project.repo, "config", "gpg.format", "openpgp")
        git(project.repo, "config", "gpg.program", "/usr/bin/false")
        git(project.repo, "config", "user.signingkey", "integration-test-unavailable")
        extra += ["--upstream", "example/project", "--fork", "example/project"]
        injection = r'''
from localize import workflow_publication
original_run = workflow_publication._run
workflow.pending_pr_guard = lambda upstream, **kw: False

def fake_auth(argv, *, cwd, input_text=None):
    if argv[0] != "gh":
        return original_run(argv, cwd=cwd, input_text=input_text)
    if argv[1] == "auth":
        return ""
    return json.dumps({"full_name": "example/project", "permissions": {"push": True}})
workflow_publication._run = fake_auth
'''
        harness = HARNESS.replace("raise SystemExit(workflow.main", injection + "\nraise SystemExit(workflow.main")
    result, events = run_workflow(project, *extra, harness=harness)
    assert result.returncode == 1, result.stdout + result.stderr
    assert events == []
    assert ("commit-tree" in result.stderr if problem == "signing" else "Publishing requires" in result.stderr)
    assert git(project.repo, "rev-parse", "HEAD") == project.base
    assert git(project.repo, "status", "--porcelain") == ""
    assert not (project.state / "ledger.json").exists()
    assert not (project.state / "run_safeguards.sqlite3").exists()


def test_quoted_false_dry_run_performs_full_pipeline(tmp_path):
    project = make_project(tmp_path)
    config = yaml.safe_load(project.config.read_text())
    config["dry_run"] = "false"
    project.config.write_text(yaml.safe_dump(config))
    result, events = run_workflow(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events == ["run_engine", "run_semantic", "run_quality"]
    assert json.loads((latest_report(project) / "receipt.json").read_text())["status"] == "accepted"


@pytest.mark.parametrize("disabled", [False, "false"])
def test_disabled_semantic_review_refuses_before_engine(tmp_path, disabled):
    project = make_project(tmp_path)
    config = yaml.safe_load(project.config.read_text())
    config["semantic_review"]["enabled"] = disabled
    project.config.write_text(yaml.safe_dump(config))
    result, events = run_workflow(project)
    assert result.returncode == 1
    assert "requires semantic_review.enabled: true" in result.stderr
    assert events == []
    assert git(project.repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("other", ["memory", "budget"])
def test_colliding_persistent_state_paths_refuse_before_engine(tmp_path, other):
    project = make_project(tmp_path)
    config = yaml.safe_load(project.config.read_text())
    collision = project.state / "retained-state"
    collision.write_bytes(b"Existing persistent state must survive\n")
    config["translation_key_ledger_file_path"] = str(collision)
    if other == "memory":
        config["translation_memory_file_path"] = str(collision)
    else:
        config["run_safeguards"]["state_path"] = str(collision)
    project.config.write_text(yaml.safe_dump(config))
    result, events = run_workflow(project)
    assert result.returncode == 1
    assert "must have distinct paths" in result.stderr
    assert events == []
    assert collision.read_bytes() == b"Existing persistent state must survive\n"


@pytest.mark.parametrize("configured_path", [None, "logs/translation_memory.json", "/app/logs/translation_memory.json"])
def test_server_default_keeps_existing_translation_memory_path(tmp_path, monkeypatch, configured_path):
    from localize import workflow
    project = make_project(tmp_path)
    raw = yaml.safe_load(project.config.read_text())
    if configured_path is not None:
        raw["translation_memory_file_path"] = configured_path
    monkeypatch.setattr(workflow, "ROOT", Path("/app"))
    args = SimpleNamespace(repo=project.repo, locales=[])
    _, _, _, accepted, _ = workflow.prepare_config(
        args, project.config, raw, project.state, project.state / "runs/new-run",
    )
    assert accepted["memory.json"] == Path("/app/logs/translation_memory.json")
    assert not (project.state / "memory.json").exists()
