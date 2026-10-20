"""Published candidates can advance only when upstream contains exact outputs."""
import hashlib
import json
import subprocess

import pytest

from localize import workflow_recovery as recovery


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).strip()


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").decode()


@pytest.fixture
def case(tmp_path, monkeypatch):
    upstream = tmp_path / "source"
    repo = tmp_path / "candidate"
    state = tmp_path / "state"
    report = state / "runs" / "accepted"
    report.mkdir(parents=True)
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    git(upstream, "config", "user.name", "Recovery test")
    git(upstream, "config", "user.email", "recovery@example.invalid")
    (upstream / "messages.properties").write_text("label=Security warning\n")
    (upstream / "messages_de.properties").write_bytes(b"label=Alt\r\n")
    (upstream / "obsolete_de.properties").write_text("label=Entfernen\n")
    base = commit(upstream, "Base")
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    (repo / "messages_de.properties").write_bytes("label=Sicherheitswarnung\r\n".encode())
    (repo / "obsolete_de.properties").unlink()
    git(repo, "add", "-A")
    patch = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
    (report / "translations.patch").write_bytes(patch)
    receipt = {
        "status": "published", "repo": str(repo), "base": base,
        "upstream": "upstream/project", "target_branch": "main",
        "changed_files": ["messages_de.properties", "obsolete_de.properties"],
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
    }
    (report / "receipt.json").write_text(json.dumps(receipt))
    (state / "published-run.json").write_text(json.dumps({"report_dir": str(report)}))

    def fetch_local(target, upstream_name, branch):
        assert upstream_name == "upstream/project"
        assert branch == "main"
        recovery._git(target, "fetch", "--no-tags", "origin", branch)
        return recovery._text(target, "rev-parse", "FETCH_HEAD")

    monkeypatch.setattr(recovery, "_fetch_upstream", fetch_local)
    return repo, state, report, upstream, base, patch


def merge_candidate(upstream):
    (upstream / "messages_de.properties").write_bytes("label=Sicherheitswarnung\r\n".encode())
    (upstream / "obsolete_de.properties").unlink()
    (upstream / "unrelated.txt").write_text("Another upstream change\n")
    return commit(upstream, "Squash reviewed translations and other changes")


def reconcile(repo, state):
    return recovery.reconcile_published(repo, state, upstream="upstream/project", target_branch="main")


def assert_candidate(repo, base, patch):
    assert git(repo, "rev-parse", "HEAD").decode() == base
    assert subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"]) == patch


def test_squash_merged_candidate_advances_without_losing_bytes_or_deletions(case):
    repo, state, report, upstream, base, patch = case
    head = merge_candidate(upstream)
    assert reconcile(repo, state) is True
    assert git(repo, "rev-parse", "HEAD").decode() == head
    assert git(repo, "status", "--porcelain") == b""
    assert (repo / "messages_de.properties").read_bytes() == "label=Sicherheitswarnung\r\n".encode()
    assert not (repo / "obsolete_de.properties").exists()
    assert (report / "translations.patch").read_bytes() == patch
    assert json.loads((report / "receipt.json").read_text())["reconciled_head"] == head
    assert json.loads((state / "published-run.json").read_text())["reconciled_head"] == head
    assert reconcile(repo, state) is False


def test_pending_candidate_is_retained_when_not_merged(case):
    repo, state, report, upstream, base, patch = case
    with pytest.raises(RuntimeError, match="Upstream"):
        reconcile(repo, state)
    assert_candidate(repo, base, patch)
    assert "reconciled_head" not in json.loads((report / "receipt.json").read_text())


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_unrecognized_local_changes_are_preserved(case, kind):
    repo, state, _, upstream, base, patch = case
    merge_candidate(upstream)
    path = repo / ("notes.txt" if kind == "untracked" else "messages_de.properties")
    path.write_text("Human work must remain\n")
    if kind == "staged":
        git(repo, "add", path.name)
    before = git(repo, "status", "--porcelain")
    staged = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
    with pytest.raises(RuntimeError):
        reconcile(repo, state)
    assert path.read_text() == "Human work must remain\n"
    assert git(repo, "status", "--porcelain") == before
    assert_candidate(repo, base, staged)


@pytest.mark.parametrize("kind", ["file", "deletion", "mode"])
def test_upstream_must_match_each_reviewed_result_exactly(case, kind):
    repo, state, _, upstream, base, patch = case
    merge_candidate(upstream)
    if kind == "file":
        (upstream / "messages_de.properties").write_text("label=Different review result\n")
    elif kind == "deletion":
        (upstream / "obsolete_de.properties").write_text("Keep this file\n")
    else:
        (upstream / "messages_de.properties").chmod(0o755)
    commit(upstream, "Modify candidate result upstream")
    with pytest.raises(RuntimeError):
        reconcile(repo, state)
    assert_candidate(repo, base, patch)


def test_failed_fast_forward_restores_the_original_staged_candidate(case, monkeypatch):
    repo, state, _, upstream, base, patch = case
    merge_candidate(upstream)
    original = recovery._git

    def fail_merge(target, *args, **kwargs):
        if args[0] == "merge":
            raise RuntimeError("Simulated merge failure")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(recovery, "_git", fail_merge)
    with pytest.raises(RuntimeError, match="recorded patch is retained"):
        reconcile(repo, state)
    assert_candidate(repo, base, patch)
    assert (repo / "messages_de.properties").read_bytes() == "label=Sicherheitswarnung\r\n".encode()
    assert not (repo / "obsolete_de.properties").exists()


@pytest.mark.parametrize("status", ["accepted", "running", "failed"])
def test_only_completed_publication_can_reconcile(case, status):
    repo, state, report, upstream, base, patch = case
    merge_candidate(upstream)
    path = report / "receipt.json"
    data = json.loads(path.read_text())
    data["status"] = status
    path.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="not the recorded published candidate"):
        reconcile(repo, state)
    assert_candidate(repo, base, patch)


def test_missing_pointer_does_not_touch_unknown_dirty_work(case):
    repo, state, _, _, base, patch = case
    (state / "published-run.json").unlink()
    assert reconcile(repo, state) is False
    assert_candidate(repo, base, patch)


def test_tampered_recorded_patch_cannot_be_applied(case):
    repo, state, report, upstream, base, patch = case
    merge_candidate(upstream)
    (report / "translations.patch").write_bytes(patch + b"tampered\n")
    with pytest.raises(RuntimeError, match="differs"):
        reconcile(repo, state)
    assert_candidate(repo, base, patch)


def test_remote_identity_is_verified_before_fetch(tmp_path, monkeypatch):
    # Exercise the real boundary separately; the fixture uses a local Git source.
    calls = []

    def git_result(repo, *args, **kwargs):
        calls.append(args)
        if args[0] == "check-ref-format":
            return b""
        if args == ("remote",):
            return b"upstream\norigin\n"
        if args[:3] == ("remote", "get-url", "--all"):
            return b"https://github.com/unexpected/repository.git\n"
        pytest.fail("must not fetch an unverified source")

    monkeypatch.setattr(recovery, "_git", git_result)
    with pytest.raises(RuntimeError, match="does not match"):
        recovery._fetch_upstream(tmp_path, "upstream/project", "main")
    assert not any(args[0] == "fetch" for args in calls)


@pytest.mark.parametrize("malformed", [{}, {"report_dir": None}, {"report_dir": []}])
def test_malformed_pointer_fails_without_touching_candidate(case, malformed):
    repo, state, _, _, base, patch = case
    (state / "published-run.json").write_text(json.dumps(malformed))
    with pytest.raises(RuntimeError, match="report directory"):
        reconcile(repo, state)
    assert_candidate(repo, base, patch)
