"""Exercise the production translation fetch commands against real Git remotes."""

import subprocess

import pytest


from localize.workflow import prepare_checkout


def _run(command, *, cwd, check=True):
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=check
    )



@pytest.fixture
def retagged_remote(tmp_path):
    remote = tmp_path / "remote.git"
    publisher = tmp_path / "publisher"
    target = tmp_path / "target"
    _run(["git", "init", "--bare", str(remote)], cwd=tmp_path)
    _run(["git", "init", "-b", "main", str(publisher)], cwd=tmp_path)
    _run(["git", "config", "user.name", "Fetch Test"], cwd=publisher)
    _run(["git", "config", "user.email", "fetch@example.invalid"], cwd=publisher)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=publisher)
    _run(["git", "commit", "--allow-empty", "-m", "Original release"], cwd=publisher)
    original = _run(["git", "rev-parse", "HEAD"], cwd=publisher).stdout.strip()
    _run(["git", "tag", "v2.1.14"], cwd=publisher)
    _run(["git", "remote", "add", "origin", str(remote)], cwd=publisher)
    _run(["git", "push", "origin", "main", "--tags"], cwd=publisher)
    _run(["git", "clone", "-b", "main", str(remote), str(target)], cwd=tmp_path)
    # A persisted repository may already have tags enabled; CLI must override it.
    _run(["git", "config", "remote.origin.tagOpt", "--tags"], cwd=target)
    _run(["git", "commit", "--allow-empty", "-m", "Updated branch"], cwd=publisher)
    latest = _run(["git", "rev-parse", "HEAD"], cwd=publisher).stdout.strip()
    _run(["git", "tag", "-f", "v2.1.14"], cwd=publisher)
    _run(["git", "push", "--force", "origin", "main", "--tags"], cwd=publisher)
    return target, original, latest


def test_translation_fetch_refreshes_branch_despite_retagged_release(retagged_remote, monkeypatch):
    target, original, latest = retagged_remote
    for name in ("UPSTREAM_REPO_NAME", "TARGET_BRANCH_FOR_PR", "FORK_REPO_URL"):
        monkeypatch.delenv(name, raising=False)
    head, branch = prepare_checkout(target, {"default_branch": "main"})
    assert head == latest
    assert branch == "main"
    assert _run(["git", "rev-parse", "origin/main"], cwd=target).stdout.strip() == latest
    assert _run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip() == latest
    assert _run(["git", "rev-parse", "v2.1.14"], cwd=target).stdout.strip() == original


def test_required_translation_fetch_stops_on_unreachable_remote(retagged_remote, tmp_path, monkeypatch):
    target, original, _ = retagged_remote
    for name in ("UPSTREAM_REPO_NAME", "TARGET_BRANCH_FOR_PR", "FORK_REPO_URL"):
        monkeypatch.delenv(name, raising=False)
    _run(["git", "remote", "set-url", "origin", str(tmp_path / "missing.git")], cwd=target)
    with pytest.raises(RuntimeError, match="failed"):
        prepare_checkout(target, {"default_branch": "main"})
    assert _run(["git", "rev-parse", "HEAD"], cwd=target).stdout.strip() == original
    assert _run(["git", "status", "--porcelain"], cwd=target).stdout == ""
