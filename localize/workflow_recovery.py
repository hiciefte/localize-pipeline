"""Advance a published candidate only after upstream contains its exact results.

The caller holds the workflow's state and repository locks. Unrecognized dirty
work is never reset or cleaned; the reviewed patch remains the recovery record.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

from localize.workflow_publication import _remote_repository


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(repo), *args],
        input=input_bytes, capture_output=True, timeout=180, check=False,
    )
    if result.returncode:
        # Authentication helpers may include credentials in stderr.
        raise RuntimeError(f"Published candidate reconciliation failed: git {args[0]}.")
    return result.stdout


def _text(repo: Path, *args: str) -> str:
    return _git(repo, *args).decode("utf-8").strip()


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Published candidate metadata cannot be read; candidate retained.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Published candidate metadata must be an object.")
    return value


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _fetch_upstream(repo: Path, upstream: str, target_branch: str) -> str:
    """Fetch a verified source remote; tests replace this network boundary."""
    _git(repo, "check-ref-format", "--branch", target_branch)
    remotes = _text(repo, "remote").splitlines()
    remote = "upstream" if "upstream" in remotes else "origin"
    urls = _text(repo, "remote", "get-url", "--all", remote).splitlines()
    if len(urls) != 1 or _remote_repository(urls[0]) != upstream.casefold():
        raise RuntimeError("Source remote does not match the published upstream; candidate retained.")
    _git(repo, "fetch", "--no-tags", remote, target_branch)
    return _text(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")


def _candidate(repo: Path, receipt: dict, patch: bytes) -> list[str]:
    if _text(repo, "rev-parse", "HEAD") != receipt["base"]:
        raise RuntimeError("Published candidate HEAD changed; candidate retained.")
    if _git(repo, "diff", "--name-only", "-z") or _git(repo, "ls-files", "--others", "--exclude-standard", "-z"):
        raise RuntimeError("Published workspace contains other local changes; candidate retained.")
    actual = _git(repo, "diff", "--cached", "--binary")
    if actual != patch or hashlib.sha256(actual).hexdigest() != receipt["patch_sha256"]:
        raise RuntimeError("Staged candidate differs from its published patch; candidate retained.")
    files = receipt.get("changed_files")
    if not isinstance(files, list) or not files or any(not isinstance(path, str) for path in files):
        raise RuntimeError("Published receipt has no valid file list.")
    if len(files) != len(set(files)):
        raise RuntimeError("Published receipt contains duplicate paths.")
    for path in files:
        parsed = PurePosixPath(path)
        if not path or parsed.is_absolute() or ".." in parsed.parts or str(parsed) != path or "\0" in path:
            raise RuntimeError("Published receipt contains an unsafe path.")
    staged = set(_git(repo, "diff", "--cached", "--no-renames", "--name-only", "-z").decode("utf-8").split("\0")) - {""}
    if staged != set(files):
        raise RuntimeError("Published receipt does not describe the complete staged candidate.")
    return files


def _require_merged_files(repo: Path, head: str, files: list[str]) -> None:
    for path in files:
        index_rows = _git(repo, "ls-files", "--stage", "-z", "--", path).split(b"\0")
        index_rows = [row for row in index_rows if row]
        upstream_rows = _git(repo, "ls-tree", "-z", head, "--", path).split(b"\0")
        upstream_rows = [row for row in upstream_rows if row]
        if not index_rows:
            if upstream_rows:
                raise RuntimeError(f"Upstream has not accepted the published deletion: {path}")
            continue
        if len(index_rows) != 1 or len(upstream_rows) != 1:
            raise RuntimeError(f"Upstream has not accepted the published file: {path}")
        index_meta, index_path = index_rows[0].split(b"\t", 1)
        mode, blob, stage = index_meta.split()
        upstream_meta, upstream_path = upstream_rows[0].split(b"\t", 1)
        upstream_mode, kind, upstream_blob = upstream_meta.split()
        if (stage != b"0" or mode not in {b"100644", b"100755"}
                or kind != b"blob" or (mode, blob, index_path) != (upstream_mode, upstream_blob, upstream_path)):
            raise RuntimeError(f"Upstream differs from the reviewed translation: {path}")


def reconcile_published(repo: Path, state: Path, *, upstream: str, target_branch: str) -> bool:
    """Return true after advancing an exact published candidate to upstream.

    Squash merges are supported because file modes and Git blob IDs, rather than
    the generated commit IDs, prove that upstream contains the reviewed output.
    """
    repo, state = Path(repo).resolve(), Path(state).resolve()
    pointer_path = state / "published-run.json"
    if not pointer_path.exists() or not _git(repo, "status", "--porcelain"):
        return False
    pointer = _json(pointer_path)
    if not isinstance(pointer.get("report_dir"), str) or not pointer["report_dir"]:
        raise RuntimeError("Published candidate pointer has no report directory.")
    report = Path(pointer["report_dir"]).resolve()
    receipt_path = report / "receipt.json"
    receipt = _json(receipt_path)
    required = ("repo", "base", "upstream", "target_branch", "patch_sha256")
    if any(not isinstance(receipt.get(key), str) or not receipt[key] for key in required):
        raise RuntimeError("Published candidate receipt is incomplete; candidate retained.")
    if (receipt.get("status") != "published" or Path(receipt.get("repo", "")).resolve() != repo
            or receipt.get("upstream", "").casefold() != upstream.casefold()
            or receipt.get("target_branch") != target_branch):
        raise RuntimeError("Dirty workspace is not the recorded published candidate; nothing was discarded.")
    patch = (report / "translations.patch").read_bytes()
    files = _candidate(repo, receipt, patch)
    head = _fetch_upstream(repo, upstream, target_branch)
    _git(repo, "merge-base", "--is-ancestor", receipt["base"], head)
    _require_merged_files(repo, head, files)
    _candidate(repo, receipt, patch)
    _git(repo, "apply", "--reverse", "--check", "--index", "--binary", "-", input_bytes=patch)
    _git(repo, "apply", "--reverse", "--index", "--binary", "-", input_bytes=patch)
    try:
        _git(repo, "merge", "--ff-only", head)
    except (RuntimeError, subprocess.SubprocessError):
        # Restore only if Git still has the exact clean base we just exposed.
        if (_text(repo, "rev-parse", "HEAD") == receipt["base"]
                and not _git(repo, "status", "--porcelain")):
            _git(repo, "apply", "--index", "--binary", "-", input_bytes=patch)
        raise RuntimeError("Could not advance the published candidate; the recorded patch is retained.") from None
    if _text(repo, "rev-parse", "HEAD") != head or _git(repo, "status", "--porcelain"):
        raise RuntimeError("Checkout changed during reconciliation; inspect the retained published patch.")
    receipt["reconciled_head"] = head
    pointer["reconciled_head"] = head
    _write_json(receipt_path, receipt)
    _write_json(pointer_path, pointer)
    return True
