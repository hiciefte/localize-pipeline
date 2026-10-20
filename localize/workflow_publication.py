"""Publish a reviewed candidate without changing its checkout or index.

The shared workflow owns translation and quality checks. This module consumes
its staged candidate and passing report, then publishes independent, signed
batches from the exact reviewed base. Failed publication leaves both the
candidate and per-batch evidence available for inspection.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

import yaml


DEFAULT_MAX_FILES_PER_PR = 90


class PublicationError(RuntimeError):
    """A publication failure, including URLs created before the failure."""

    def __init__(self, message: str, *, published_urls: list[str] | None = None):
        super().__init__(message)
        self.published_urls = list(published_urls or [])


def _run(argv: list[str], *, cwd: Path, input_text: str | None = None) -> str:
    result = subprocess.run(
        argv, cwd=cwd, input=input_text.encode("utf-8") if input_text is not None else None,
        capture_output=True,
        timeout=180, check=False,
    )
    if result.returncode:
        # Git/gh output may contain authenticated URLs. Keep it out of reports.
        raise PublicationError(f"Publication command failed: {argv[0]} {argv[1]}")
    # Preserve CRLF bytes inside Git patches; universal-newline conversion
    # would silently publish different property-file line endings.
    return result.stdout.decode("utf-8")


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).strip()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise PublicationError("Repository must be an explicit GitHub owner/name")
    return value.casefold()


def _remote_repository(url: str) -> str:
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:"):]
    else:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"https", "ssh"}
            or parsed.hostname != "github.com"
            or parsed.password or parsed.query or parsed.fragment or parsed.port
            or (parsed.scheme == "https" and parsed.username)
            or (parsed.scheme == "ssh" and parsed.username != "git")
        ):
            raise PublicationError("Publication remotes must be credential-free GitHub URLs")
        path = parsed.path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return _repository_name(path)


def _check_remotes(repo: Path, *, upstream: str, fork: str) -> None:
    for remote, expected in (("origin", fork), ("upstream", upstream)):
        for mode in ([], ["--push"]):
            urls = _git(repo, "remote", "get-url", *mode, "--all", remote).splitlines()
            if len(urls) != 1 or _remote_repository(urls[0]) != _repository_name(expected):
                raise PublicationError(f"{remote} remote does not match the configured repository")


def preflight(repo: Path, *, upstream: str, fork: str) -> None:
    """Check publication identity/auth before a potentially expensive run."""
    repo = Path(repo).resolve()
    _check_remotes(repo, upstream=upstream, fork=fork)
    _git(repo, "var", "GIT_AUTHOR_IDENT")
    _git(repo, "var", "GIT_COMMITTER_IDENT")
    _run(["gh", "auth", "status", "--hostname", "github.com"], cwd=repo)
    permissions = json.loads(_run(["gh", "api", f"repos/{fork}"], cwd=repo))
    if permissions.get("full_name", "").casefold() != _repository_name(fork):
        raise PublicationError("GitHub returned an unexpected fork repository")
    if permissions.get("permissions", {}).get("push") is not True:
        raise PublicationError("Authenticated GitHub account cannot push to the configured fork")
    upstream_info = json.loads(_run(["gh", "api", f"repos/{upstream}"], cwd=repo))
    if upstream_info.get("full_name", "").casefold() != _repository_name(upstream):
        raise PublicationError("GitHub returned an unexpected upstream repository")
    # An unattached empty-tree commit proves the configured signer works without
    # changing branches, the working tree or the staged candidate.
    empty_tree = _run(["git", "mktree"], cwd=repo, input_text="").strip()
    probe = _git(repo, "commit-tree", "-S", empty_tree, "-m", "Verify localization signing")
    _git(repo, "verify-commit", probe)


def _paths(output: str) -> set[str]:
    return {item for item in output.split("\0") if item}


def _patch(repo: Path, base: str, files: list[str]) -> str:
    return _run([
        "git", "diff", "--cached", "--binary", "--full-index", "--no-ext-diff",
        "--no-textconv", "--no-renames", base, "--", *[f":(literal){path}" for path in files],
    ], cwd=repo)


def _check_candidate(repo: Path, base: str, files: list[str]) -> str:
    if _git(repo, "rev-parse", "HEAD") != base:
        raise PublicationError("Candidate HEAD differs from the reviewed base")
    if _git(repo, "diff", "--name-only", "-z", "--"):
        raise PublicationError("Candidate contains unstaged changes")
    if _git(repo, "ls-files", "--others", "--exclude-standard", "-z"):
        raise PublicationError("Candidate contains untracked files")
    staged = _paths(_run([
        "git", "diff", "--cached", "--name-only", "--no-renames", "-z", base, "--",
    ], cwd=repo))
    if staged != set(files):
        raise PublicationError("Staged paths do not exactly match the reviewed file list")
    for line in _git(repo, "ls-files", "--stage", "--", *[f":(literal){path}" for path in files]).splitlines():
        if line.split(" ", 1)[0] not in {"100644", "100755"}:
            raise PublicationError("Candidate contains a symlink or non-file entry")
    return _patch(repo, base, files)


def _remote_head(repo: Path, remote: str, branch: str) -> str:
    output = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
    return output.split()[0] if output else ""


def _body(report_dir: Path, *, base: str, files: list[str], batch: int, total: int) -> str:
    sections = [
        f"Update {len(files)} localization files (batch {batch}/{total}) against "
        f"reviewed source `{base}`. All files in this batch passed the shared "
        "translation, semantic review and quality gate workflow.",
        "These translations and reviews are AI-assisted. Native-speaker review "
        "and rendered UI/layout testing are not implied by the automated checks.",
    ]
    report_md = report_dir / "translation_quality_report.md"
    if report_md.is_file():
        sections.append("### Whole-run quality report\n\n" + report_md.read_text(encoding="utf-8"))
    for filename, heading in (
        ("translation_summary.json", "Whole-run translation summary"),
        ("token_usage_summary.json", "Whole-run token usage and estimated cost"),
    ):
        path = report_dir / "logs" / filename
        if not path.is_file():
            path = report_dir / filename
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            # These summaries contain counts/model names, not environment values.
            sections.append(f"### {heading}\n\n```json\n" + json.dumps(value, indent=2) + "\n```")
    body = "\n\n".join(sections)
    if len(body) > 60000:
        body = body[:60000] + "\n\n<!-- Whole-run report truncated to fit GitHub limits. -->"
    return body + "\n"


def _publish_status(repo: Path, *, fork: str, head: str, url: str, description: str) -> None:
    _run([
        "gh", "api", f"repos/{fork}/statuses/{head}", "-f", "state=success",
        "-f", "context=translation-quality-gate", "-f", f"description={description[:140]}",
        "-f", f"target_url={url}",
    ], cwd=repo)
    _verify_status(repo, fork=fork, head=head, url=url)


def _verify_status(repo: Path, *, fork: str, head: str, url: str) -> None:
    for attempt in range(5):
        status = json.loads(_run(["gh", "api", f"repos/{fork}/commits/{head}/status"], cwd=repo))
        matches = [item for item in status.get("statuses", []) if item.get("context") == "translation-quality-gate"]
        if matches and matches[0].get("state") == "success" and matches[0].get("target_url") == url:
            return
        if attempt < 4:
            time.sleep(0.5 * (2 ** attempt))
    raise PublicationError("Published translation-quality-gate status could not be verified")


def _find_pr(repo: Path, *, upstream: str, fork: str, branch: str) -> str | None:
    pulls = json.loads(_run([
        "gh", "pr", "list", "--repo", upstream, "--state", "all", "--head", branch,
        "--json", "url,headRepositoryOwner",
    ], cwd=repo))
    matches = [pull for pull in pulls if pull.get("headRepositoryOwner", {}).get("login", "").casefold() == fork.split("/")[0].casefold()]
    if len(matches) > 1:
        raise PublicationError("Multiple pull requests already exist for this publication branch")
    return matches[0]["url"] if matches else None


def _guardian(worktree: Path, repo: Path, config_path: Path, *, upstream: str, head: str, url: str) -> None:
    if os.environ.get("GUARDIAN_QUALITY_REPORT_ENABLED", "false").lower() != "true":
        return
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    input_folder = Path(config["input_folder"])
    if not input_folder.is_absolute():
        input_folder = repo / input_folder
    relative = input_folder.resolve().relative_to(repo)
    _run([
        sys.executable, "-m", "localize.guardian.quality_report_publication",
        "--repository", upstream, "--pull-number", url.rsplit("/", 1)[1],
        "--expected-head", head, "--repo-root", str(worktree),
        "--config", str(config_path), "--input-folder", str(worktree / relative),
    ], cwd=Path(__file__).resolve().parents[1])


def publish(
    repo: Path, base: str, files: list[str], report_dir: Path, config_path: Path, *,
    upstream: str, fork: str, branch: str, max_files_per_pr: int = DEFAULT_MAX_FILES_PER_PR,
    target_branch: str | None = None,
) -> list[str]:
    """Publish staged, reviewed files; retain candidate and receipts on failure.

    ``base`` is the exact reviewed commit, not a moving branch. ``target_branch``
    is the GitHub base branch; TARGET_BRANCH_FOR_PR is a compatibility fallback.
    No commits, pushes or GitHub writes occur until the full candidate is valid.
    """
    repo, report_dir, config_path = (Path(p).resolve() for p in (repo, report_dir, config_path))
    target_branch = target_branch or os.environ.get("TARGET_BRANCH_FOR_PR")
    if not target_branch:
        raise PublicationError("An explicit PR target_branch is required")
    if not isinstance(max_files_per_pr, int) or isinstance(max_files_per_pr, bool) or not 1 <= max_files_per_pr < 100:
        raise PublicationError("max_files_per_pr must be between 1 and 99 to keep batches reviewable")
    if len(files) != len(set(files)):
        raise PublicationError("Duplicate paths in reviewed file list")
    for path in files:
        parsed = PurePosixPath(path)
        if not path or parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path or "\0" in path:
            raise PublicationError("Reviewed paths must be normalized repository-relative paths")
    if not files:
        return []
    _git(repo, "check-ref-format", "--branch", branch)
    _git(repo, "check-ref-format", "--branch", target_branch)
    resolved_base = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    if base != resolved_base:
        raise PublicationError("Publication requires the full reviewed base commit SHA")
    quality_path = report_dir / "translation_quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if quality.get("blocking") is not False or quality.get("status_state") != "success":
        raise PublicationError("Publication requires a passing whole-run quality report")
    candidate_patch = _check_candidate(repo, base, files)
    preflight(repo, upstream=upstream, fork=fork)
    if _remote_head(repo, "upstream", target_branch) != base:
        raise PublicationError("Upstream target moved from the reviewed base; review before publication")
    batches = [files[start:start + max_files_per_pr] for start in range(0, len(files), max_files_per_pr)]
    branches = [branch if len(batches) == 1 else f"{branch}-{index}" for index in range(1, len(batches) + 1)]
    publication_dir = report_dir / "publication"
    plan = {
        "reviewed_base": base, "upstream": upstream, "fork": fork,
        "target_branch": target_branch, "candidate_patch_sha256": _sha(candidate_patch),
        "quality_report_sha256": hashlib.sha256(quality_path.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "batches": [{"branch": name, "files": batch} for name, batch in zip(branches, batches)],
    }
    if publication_dir.exists():
        saved_plan = json.loads((publication_dir / "plan.json").read_text(encoding="utf-8"))
        if saved_plan != plan:
            raise PublicationError("Saved publication plan differs from this reviewed candidate")
    else:
        # Reject unrelated collisions before the first publication. Only a
        # matching saved plan permits resuming a prior batch/PR.
        existing = set(_git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/").splitlines())
        for name in branches:
            if f"refs/heads/{name}" in existing or _remote_head(repo, "origin", name):
                raise PublicationError(f"Publication branch already exists: {name}")
        publication_dir.mkdir()
        _write_json(publication_dir / "plan.json", plan)
    urls: list[str] = []
    for number, (name, batch) in enumerate(zip(branches, batches), 1):
        directory = publication_dir / f"batch-{number}"
        directory.mkdir(exist_ok=True)
        worktree = directory / "worktree"
        receipt = {"branch": name, "reviewed_base": base, "files": batch, "state": "prepared"}
        receipt_path = directory / "receipt.json"
        try:
            if _sha(_check_candidate(repo, base, files)) != plan["candidate_patch_sha256"]:
                raise PublicationError("Reviewed candidate changed during publication")
            patch = _patch(repo, base, batch)
            receipt["patch_sha256"] = _sha(patch)
            if receipt_path.exists():
                saved_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if any(saved_receipt.get(key) != receipt[key] for key in ("branch", "reviewed_base", "files", "patch_sha256")):
                    raise PublicationError("Saved batch receipt differs from its reviewed batch")
                receipt = saved_receipt
                if (directory / "candidate.patch").read_bytes().decode("utf-8") != patch:
                    raise PublicationError("Saved batch patch differs from its reviewed batch")
            else:
                (directory / "candidate.patch").write_text(patch, encoding="utf-8")
                _write_json(receipt_path, receipt)
            if not worktree.exists():
                _git(repo, "worktree", "add", "-b", name, str(worktree), base)
            if _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != name:
                raise PublicationError("Publication worktree is on an unexpected branch")
            if _git(worktree, "rev-parse", "HEAD") == base:
                if not _git(worktree, "status", "--porcelain"):
                    _run(["git", "apply", "--index", "--binary", "-"], cwd=worktree, input_text=patch)
                if _check_candidate(worktree, base, batch) != patch:
                    raise PublicationError("Publication worktree differs from its reviewed batch")
                _git(worktree, "commit", "-S", "-m", "Update translations" + (f" (batch {number}/{len(batches)})" if len(batches) > 1 else ""))
            head = _git(worktree, "rev-parse", "HEAD")
            if receipt.get("head") and receipt["head"] != head:
                raise PublicationError("Saved batch head changed since the prior publication attempt")
            _git(worktree, "verify-commit", head)
            if _git(worktree, "rev-parse", "HEAD^") != base or _git(worktree, "status", "--porcelain"):
                raise PublicationError("Signed batch has unexpected ancestry or uncommitted changes")
            committed_patch = _run([
                "git", "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv",
                "--no-renames", base, head, "--",
            ], cwd=worktree)
            if committed_patch != patch:
                raise PublicationError("Signed commit differs from the reviewed batch")
            completed = receipt.get("state") == "complete"
            receipt.update(head=head, state="signed")
            _write_json(receipt_path, receipt)
            if _sha(_check_candidate(repo, base, files)) != plan["candidate_patch_sha256"]:
                raise PublicationError("Reviewed candidate changed before push")
            _check_remotes(repo, upstream=upstream, fork=fork)
            if _remote_head(repo, "upstream", target_branch) != base:
                raise PublicationError("Upstream target moved during publication")
            remote_head = _remote_head(repo, "origin", name)
            if remote_head and remote_head != head:
                raise PublicationError("Publication branch changed on the fork; refusing to overwrite it")
            if not remote_head:
                _git(worktree, "push", "origin", f"HEAD:refs/heads/{name}")
            receipt["state"] = "pushed"
            _write_json(receipt_path, receipt)
            if _remote_head(repo, "origin", name) != head:
                raise PublicationError("Pushed batch head could not be verified")
            body_path = directory / "pr-body.md"
            body_path.write_text(_body(report_dir, base=base, files=batch, batch=number, total=len(batches)), encoding="utf-8")
            url = receipt.get("url") or _find_pr(worktree, upstream=upstream, fork=fork, branch=name)
            if not url:
                url = _run([
                    "gh", "pr", "create", "--repo", upstream, "--base", target_branch,
                    "--head", f"{fork.split('/')[0]}:{name}", "--title", "Update translations" +
                    (f" (batch {number}/{len(batches)})" if len(batches) > 1 else ""),
                    "--body-file", str(body_path),
                ], cwd=worktree).strip()
            if not re.fullmatch(rf"https://github\.com/{re.escape(upstream)}/pull/[0-9]+", url, re.IGNORECASE):
                raise PublicationError("GitHub returned an unexpected pull request URL")
            urls.append(url)
            receipt.update(url=url, state="pull_request_created")
            _write_json(receipt_path, receipt)
            pull = json.loads(_run([
                "gh", "pr", "view", url, "--repo", upstream,
                "--json", "url,headRefOid,baseRefOid,baseRefName",
            ], cwd=worktree))
            if pull != {"url": url, "headRefOid": head, "baseRefOid": base, "baseRefName": target_branch}:
                raise PublicationError("Created pull request does not match the reviewed head/base")
            if not completed:
                _publish_status(worktree, fork=fork, head=head, url=url, description=quality.get("status_description", "Translation quality gate passed."))
                receipt["quality_status_verified"] = True
                _write_json(receipt_path, receipt)
                _guardian(worktree, repo, config_path, upstream=upstream, head=head, url=url)
            else:
                _verify_status(worktree, fork=fork, head=head, url=url)
            receipt["state"] = "complete"
            receipt.pop("error", None)
            _write_json(receipt_path, receipt)
        except Exception as exc:
            receipt["state"] = "failed"
            receipt["error"] = str(exc) if isinstance(exc, PublicationError) else type(exc).__name__
            _write_json(receipt_path, receipt)
            raise PublicationError(
                f"Publication batch {number} failed; candidate and receipt retained at {receipt_path}",
                published_urls=urls,
            ) from exc
    return urls
