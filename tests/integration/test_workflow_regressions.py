"""Review and transport edges that must hold for both launchers."""

import json
from pathlib import Path

import pytest

from localize import workflow, workflow_publication
from tests.integration.test_unified_workflow import (
    HARNESS, git, latest_report, make_project, run_workflow,
)


def test_semantic_reversal_removes_the_previously_staged_draft(tmp_path):
    project = make_project(tmp_path)
    original = project.paths["de"].read_bytes()
    injection = r'''
original_engine = workflow.run_engine
original_semantic = workflow.run_semantic
before = {}
def engine(*args, **kwargs):
    result = original_engine(*args, **kwargs)
    config = workflow.yaml.safe_load(args[0].read_text())
    target = Path(config["input_folder"]) / "app_de.properties"
    before[target] = target.read_bytes()
    target.write_text("label=Kontodaten\ngreeting=Hallo {0}\n")
    return result
def semantic(*args, **kwargs):
    for target, content in before.items():
        target.write_bytes(content)
    # Review the restored candidate; it has no diff and uses no provider.
    workflow.git(args[1], "add", "--", *map(str, before))
    result = original_semantic(*args, **kwargs)
    # Reproduce a staged draft vs corrected worktree, as an ordinary semantic
    # reviewer writes corrections without owning Git staging.
    for target, content in before.items():
        target.write_text("label=Kontodaten\ngreeting=Hallo {0}\n")
        workflow.git(args[1], "add", "--", str(target))
        target.write_bytes(content)
    return result
workflow.run_engine = engine
workflow.run_semantic = semantic
'''
    harness = HARNESS.replace("raise SystemExit(workflow.main", injection + "\nraise SystemExit(workflow.main")
    result, _ = run_workflow(project, "--locales", "de", harness=harness)
    assert result.returncode == 0, result.stdout + result.stderr
    assert project.paths["de"].read_bytes() == original
    assert git(project.repo, "status", "--porcelain") == ""
    report = latest_report(project)
    assert (report / "translations.patch").read_bytes() == b""
    assert json.loads((report / "receipt.json").read_text())["changed_files"] == []


@pytest.mark.parametrize("server", [False, True])
def test_publishing_selects_transport_for_the_available_authentication(tmp_path, monkeypatch, server):
    project = make_project(tmp_path)
    git(project.repo, "remote", "add", "origin", "https://github.com/upstream/project.git")
    monkeypatch.delenv("DEPLOY_KEY_PATH", raising=False)
    if server:
        monkeypatch.setattr(workflow, "ROOT", Path("/app"))
    calls = []
    monkeypatch.setattr(workflow_publication, "preflight", lambda repo, **kw: calls.append((repo, kw)))
    workflow.prepare_publication(project.repo, "upstream/project", "fork/project")
    expected = "git@github.com:fork/project.git" if server else "https://github.com/fork/project.git"
    assert git(project.repo, "remote", "get-url", "origin") == expected
    assert git(project.repo, "remote", "get-url", "upstream") == "https://github.com/upstream/project.git"
    if not server:
        assert git(project.repo, "config", "--local", "credential.https://github.com.helper") == "!gh auth git-credential"
    assert calls == [(project.repo, {"upstream": "upstream/project", "fork": "fork/project"})]
