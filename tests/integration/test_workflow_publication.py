"""Real Git isolation with fake signing and GitHub/network boundaries."""

import json
from pathlib import Path
import subprocess

import pytest

from localize import workflow_publication as publication


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True,
    ).stdout.strip()


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / "literal $HOME ' publication"
    root.mkdir()
    repo = root / "candidate"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Publication Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    folder = repo / "l10n"
    folder.mkdir()
    for name in ("first.properties", "second.properties", "third.properties"):
        (folder / name).write_text("key=old\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "Seed")
    base = git(repo, "rev-parse", "HEAD")
    fork_bare = root / "fork.git"
    upstream_bare = root / "upstream.git"
    git(root, "clone", "-q", "--bare", str(repo), str(fork_bare))
    git(root, "clone", "-q", "--bare", str(repo), str(upstream_bare))
    git(repo, "remote", "add", "origin", "git@github.com:translator/project.git")
    git(repo, "remote", "add", "upstream", "https://github.com/upstream/project.git")
    (folder / "first.properties").write_text("key=first translation\n", encoding="utf-8")
    (folder / "second.properties").unlink()
    (folder / "third.properties").write_text("key=third translation\n", encoding="utf-8")
    (folder / "new space.properties").write_text("key=new translation\n", encoding="utf-8")
    git(repo, "add", "--all", "l10n")
    files = git(repo, "diff", "--cached", "--name-only", "--no-renames").splitlines()
    report_dir = root / "reports"
    report_dir.mkdir()
    (report_dir / "translation_quality_report.json").write_text(json.dumps({
        "blocking": False, "status_state": "success", "status_description": "Checks passed",
    }), encoding="utf-8")
    (report_dir / "translation_quality_report.md").write_text("All checks passed.\n", encoding="utf-8")
    config_path = root / "config.yaml"
    config_path.write_text(f"input_folder: {repo / 'l10n'}\n", encoding="utf-8")
    commands = []
    statuses = {}
    pulls = {}
    pull_branches = {}
    probe_heads = set()
    faults = {}
    original_run = publication._run

    def fake_boundary(argv, *, cwd, input_text=None):
        commands.append(list(argv))
        if argv[0] == "git":
            if argv[1] == "ls-remote":
                actual = list(argv)
                actual[3] = str({"origin": fork_bare, "upstream": upstream_bare}[argv[3]])
                return original_run(actual, cwd=cwd, input_text=input_text)
            if argv[1] == "commit":
                assert "-S" in argv, "Every publication commit must require signing"
                if faults.get("sign"):
                    raise publication.PublicationError("Signing failed")
                actual = ["git", "-c", "commit.gpgsign=false", *[item for item in argv[1:] if item != "-S"]]
                return original_run(actual, cwd=cwd, input_text=input_text)
            if argv[1] == "commit-tree":
                assert "-S" in argv
                if faults.get("probe"):
                    raise publication.PublicationError("Signing probe failed")
                head = original_run(["git", *[item for item in argv[1:] if item != "-S"]], cwd=cwd, input_text=input_text)
                probe_heads.add(head.strip())
                return head
            if argv[1] == "verify-commit":
                if (faults.get("verify") and argv[2] not in probe_heads) or faults.get("probe_verify"):
                    raise publication.PublicationError("Signature verification failed")
                return ""
            if argv[1] == "push":
                assert argv[2] == "origin"
                assert not any("force" in arg for arg in argv)
                if faults.get("push"):
                    raise publication.PublicationError("Push failed")
                return original_run(["git", "push", str(fork_bare), *argv[3:]], cwd=cwd)
            return original_run(argv, cwd=cwd, input_text=input_text)
        if argv[:3] == ["gh", "auth", "status"]:
            return ""
        if argv[:2] == ["gh", "api"]:
            endpoint = argv[2]
            if endpoint == "repos/translator/project":
                return json.dumps({"full_name": "translator/project", "permissions": {"push": not faults.get("permission")}})
            if endpoint == "repos/upstream/project":
                return json.dumps({"full_name": "upstream/project"})
            if "/statuses/" in endpoint:
                head = endpoint.rsplit("/", 1)[1]
                values = dict(item.split("=", 1) for item in argv if "=" in item)
                statuses[head] = values
                return "{}"
            if "/commits/" in endpoint and endpoint.endswith("/status"):
                head = endpoint.split("/")[-2]
                return json.dumps({"statuses": [] if faults.get("status") else [statuses[head]]})
            raise AssertionError(f"Unexpected fake API endpoint: {endpoint}")
        if argv[:3] == ["gh", "pr", "create"]:
            branch = argv[argv.index("--head") + 1]
            if faults.get("pr") and branch.endswith(str(faults["pr"])):
                raise publication.PublicationError("PR creation failed")
            assert argv[argv.index("--repo") + 1] == "upstream/project"
            assert argv[argv.index("--base") + 1] == "main"
            assert branch.startswith("translator:codex/test")
            body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
            assert base in body
            assert "AI-assisted" in body
            url = f"https://github.com/upstream/project/pull/{len(pulls) + 1}"
            pulls[url] = {"url": url, "headRefOid": git(cwd, "rev-parse", "HEAD"), "baseRefOid": base, "baseRefName": "main"}
            pull_branches[url] = branch.split(":", 1)[1]
            if faults.get("lost_pr_result"):
                raise publication.PublicationError("Lost PR creation response")
            return url + "\n"
        if argv[:3] == ["gh", "pr", "list"]:
            branch = argv[argv.index("--head") + 1]
            return json.dumps([
                {"url": url, "headRepositoryOwner": {"login": "translator"}}
                for url in pulls if pull_branches[url] == branch
            ])
        if argv[:3] == ["gh", "pr", "view"]:
            return json.dumps(pulls[argv[3]])
        if len(argv) > 2 and argv[1:3] == ["-m", "localize.guardian.quality_report_publication"]:
            if faults.get("guardian"):
                raise publication.PublicationError("Guardian reporting failed")
            return "{}"
        raise AssertionError(f"Unexpected command at fake boundary: {argv[:3]}")

    monkeypatch.setattr(publication, "_run", fake_boundary)
    monkeypatch.setattr(publication.time, "sleep", lambda _: None)
    monkeypatch.delenv("GUARDIAN_QUALITY_REPORT_ENABLED", raising=False)
    original_patch = git(repo, "diff", "--cached", "--binary", "--full-index", "--no-renames")
    return {
        "repo": repo, "base": base, "files": files, "report_dir": report_dir,
        "config_path": config_path, "commands": commands, "faults": faults,
        "fork_bare": fork_bare, "upstream_bare": upstream_bare, "patch": original_patch,
    }


def publish(case, **kwargs):
    return publication.publish(
        **{key: case[key] for key in ("repo", "base", "files", "report_dir", "config_path")},
        upstream="upstream/project", fork="translator/project", branch="codex/test",
        target_branch="main", **kwargs,
    )


def assert_candidate_preserved(case):
    assert git(case["repo"], "rev-parse", "HEAD") == case["base"]
    assert git(case["repo"], "diff", "--cached", "--binary", "--full-index", "--no-renames") == case["patch"]
    assert not git(case["repo"], "diff", "--name-only")


def receipt(case, batch):
    return json.loads((case["report_dir"] / "publication" / f"batch-{batch}" / "receipt.json").read_text())


def test_batches_are_independent_signed_and_preserve_candidate(case):
    urls = publish(case, max_files_per_pr=2)
    assert urls == [f"https://github.com/upstream/project/pull/{number}" for number in (1, 2)]
    assert_candidate_preserved(case)
    for number in (1, 2):
        data = receipt(case, number)
        assert data["state"] == "complete"
        assert data["quality_status_verified"] is True
        head = data["head"]
        assert git(case["repo"], "rev-parse", head + "^") == case["base"]
        assert git(case["repo"], "diff", "--name-only", "--no-renames", case["base"], head).splitlines() == data["files"]
        assert git(case["fork_bare"], "rev-parse", f"codex/test-{number}") == head
    assert len([cmd for cmd in case["commands"] if cmd[:2] == ["git", "verify-commit"]]) == 3


@pytest.mark.parametrize("fault", ["sign", "verify", "push"])
def test_signature_or_push_failure_retains_candidate_and_never_falls_back(case, fault):
    case["faults"][fault] = True
    with pytest.raises(publication.PublicationError) as error:
        publish(case)
    assert error.value.published_urls == []
    assert_candidate_preserved(case)
    assert receipt(case, 1)["state"] == "failed"
    commits = [cmd for cmd in case["commands"] if cmd[:2] == ["git", "commit"]]
    assert len(commits) == 1 and "-S" in commits[0]
    assert not [cmd for cmd in case["commands"] if cmd[:3] == ["gh", "pr", "create"]]
    if fault != "push":
        assert not [cmd for cmd in case["commands"] if cmd[:2] == ["git", "push"]]


def test_second_batch_failure_preserves_first_pr_and_has_no_cross_batch_leak(case):
    case["faults"]["pr"] = 2
    with pytest.raises(publication.PublicationError) as error:
        publish(case, max_files_per_pr=2)
    assert error.value.published_urls == ["https://github.com/upstream/project/pull/1"]
    assert_candidate_preserved(case)
    assert receipt(case, 1)["state"] == "complete"
    second = receipt(case, 2)
    assert second["state"] == "failed"
    assert git(case["repo"], "rev-parse", second["head"] + "^") == case["base"]
    assert git(case["repo"], "diff", "--name-only", "--no-renames", case["base"], second["head"]).splitlines() == case["files"][2:]
    assert git(case["fork_bare"], "rev-parse", "codex/test-2") == second["head"]


def test_status_verification_failure_reports_created_pr(case):
    case["faults"]["status"] = True
    with pytest.raises(publication.PublicationError) as error:
        publish(case)
    assert error.value.published_urls == ["https://github.com/upstream/project/pull/1"]
    assert receipt(case, 1)["url"] == error.value.published_urls[0]
    assert "quality_status_verified" not in receipt(case, 1)
    assert_candidate_preserved(case)


@pytest.mark.parametrize("mode", ["wrong_origin", "wrong_push", "wrong_upstream", "credential_url"])
def test_remote_mismatch_fails_before_auth_or_publication(case, mode):
    if mode == "wrong_push":
        git(case["repo"], "remote", "set-url", "--push", "origin", "git@github.com:other/project.git")
    else:
        remote = "upstream" if mode == "wrong_upstream" else "origin"
        url = "https://secret@github.com/translator/project.git" if mode == "credential_url" else "https://github.com/other/project.git"
        git(case["repo"], "remote", "set-url", remote, url)
    with pytest.raises(publication.PublicationError):
        publish(case)
    assert not any(cmd[0] == "gh" for cmd in case["commands"])
    assert_candidate_preserved(case)


def test_blocking_quality_report_prevents_network(case):
    (case["report_dir"] / "translation_quality_report.json").write_text(json.dumps({"blocking": True, "status_state": "failure"}))
    with pytest.raises(publication.PublicationError, match="passing"):
        publish(case)
    assert not any(cmd[0] == "gh" or cmd[:2] == ["git", "ls-remote"] for cmd in case["commands"])
    assert_candidate_preserved(case)


def test_extra_staged_file_prevents_publication(case):
    (case["repo"] / "unrelated.txt").write_text("must not publish\n")
    git(case["repo"], "add", "unrelated.txt")
    with pytest.raises(publication.PublicationError, match="exactly match"):
        publish(case)
    assert not any(cmd[0] == "gh" for cmd in case["commands"])
    assert "unrelated.txt" in git(case["repo"], "diff", "--cached", "--name-only")


def test_changed_upstream_base_prevents_publication(case):
    git(case["upstream_bare"], "update-ref", "-d", "refs/heads/main")
    with pytest.raises(publication.PublicationError, match="Upstream target moved"):
        publish(case)
    assert not any(cmd[:2] == ["git", "commit"] for cmd in case["commands"])
    assert_candidate_preserved(case)


def test_permission_preflight_does_not_modify_candidate(case):
    case["faults"]["permission"] = True
    with pytest.raises(publication.PublicationError, match="cannot push"):
        publication.preflight(case["repo"], upstream="upstream/project", fork="translator/project")
    assert_candidate_preserved(case)


def test_guardian_hook_uses_exact_batch_head_and_worktree(case, monkeypatch):
    monkeypatch.setenv("GUARDIAN_QUALITY_REPORT_ENABLED", "true")
    publish(case)
    calls = [cmd for cmd in case["commands"] if cmd[1:3] == ["-m", "localize.guardian.quality_report_publication"]]
    assert len(calls) == 1
    call = calls[0]
    data = receipt(case, 1)
    worktree = case["report_dir"] / "publication" / "batch-1" / "worktree"
    assert call[call.index("--expected-head") + 1] == data["head"]
    assert Path(call[call.index("--repo-root") + 1]) == worktree
    assert Path(call[call.index("--input-folder") + 1]) == worktree / "l10n"


def test_empty_candidate_needs_no_credentials(case):
    case["files"] = []
    assert publish(case) == []
    assert not case["commands"]


@pytest.mark.parametrize("fault", ["probe", "probe_verify"])
def test_preflight_proves_signing_before_run(case, fault):
    case["faults"][fault] = True
    with pytest.raises(publication.PublicationError):
        publication.preflight(case["repo"], upstream="upstream/project", fork="translator/project")
    assert not any(cmd[:2] in (["git", "worktree"], ["git", "push"]) for cmd in case["commands"])
    assert_candidate_preserved(case)


def test_retry_completes_failed_second_batch_without_recommit_or_duplicate_pr(case):
    case["faults"]["pr"] = 2
    with pytest.raises(publication.PublicationError):
        publish(case, max_files_per_pr=2)
    original_heads = [receipt(case, number)["head"] for number in (1, 2)]
    case["faults"].clear()
    assert publish(case, max_files_per_pr=2) == [f"https://github.com/upstream/project/pull/{number}" for number in (1, 2)]
    assert original_heads == [receipt(case, number)["head"] for number in (1, 2)]
    assert len([cmd for cmd in case["commands"] if cmd[:2] == ["git", "commit"]]) == 2
    assert len([cmd for cmd in case["commands"] if cmd[:2] == ["git", "push"]]) == 2
    assert_candidate_preserved(case)


def test_retry_recovers_lost_pr_response_without_duplicate(case):
    case["faults"]["lost_pr_result"] = True
    with pytest.raises(publication.PublicationError):
        publish(case)
    assert "url" not in receipt(case, 1)
    case["faults"].clear()
    assert publish(case) == ["https://github.com/upstream/project/pull/1"]
    assert len([cmd for cmd in case["commands"] if cmd[:3] == ["gh", "pr", "create"]]) == 1
    assert_candidate_preserved(case)


def test_successful_publication_retry_is_read_only_on_github(case):
    urls = publish(case)
    case["commands"].clear()
    assert publish(case) == urls
    assert not any(cmd[:2] == ["git", "push"] or cmd[:3] == ["gh", "pr", "create"] for cmd in case["commands"])
    assert not any(cmd[:2] == ["gh", "api"] and "/statuses/" in cmd[2] for cmd in case["commands"])


def test_precommit_hook_cannot_publish_unreviewed_content(case):
    hook = case["repo"] / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'key=unreviewed change\\n' > l10n/first.properties\ngit add -- l10n/first.properties\n")
    hook.chmod(0o755)
    with pytest.raises(publication.PublicationError) as error:
        publish(case)
    assert "Signed commit differs" in str(error.value.__cause__)
    assert not any(cmd[:2] == ["git", "push"] for cmd in case["commands"])
    assert_candidate_preserved(case)


def test_oversized_batch_limit_is_rejected(case):
    with pytest.raises(publication.PublicationError, match="reviewable"):
        publish(case, max_files_per_pr=100)
    assert not case["commands"]


def test_pr_body_reads_actual_shared_workflow_summary_paths(case):
    logs = case["report_dir"] / "logs"
    logs.mkdir()
    (logs / "translation_summary.json").write_text(json.dumps({"changed_values_count": 23, "translation_memory_reused_count": 19}))
    (logs / "token_usage_summary.json").write_text(json.dumps({"totals": {"total_tokens": 321, "estimated_cost_usd": 0.00321}}))
    publish(case)
    body = (case["report_dir"] / "publication/batch-1/pr-body.md").read_text()
    assert '"changed_values_count": 23' in body
    assert '"translation_memory_reused_count": 19' in body
    assert '"estimated_cost_usd": 0.00321' in body


def test_publication_and_retry_preserve_crlf_property_bytes(case):
    path = case["repo"] / "l10n/first.properties"
    path.write_bytes(b"key=translation with CRLF\r\n")
    git(case["repo"], "add", "--", "l10n/first.properties")
    urls = publish(case)
    assert publish(case) == urls
    worktree = case["report_dir"] / "publication/batch-1/worktree"
    assert (worktree / "l10n/first.properties").read_bytes() == path.read_bytes()
    assert b"translation with CRLF\r\n" in (case["report_dir"] / "publication/batch-1/candidate.patch").read_bytes()
