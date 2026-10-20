"""Batch isolation regression for the shared Python publisher."""

import subprocess

import pytest

from localize.workflow_publication import PublicationError
from tests.integration.test_workflow_publication import (
    assert_candidate_preserved, case as publication_case, git, publish, receipt,
)

case = publication_case


def _run(command, *, cwd, env=None, check=True):
    # Retained for the skipped-translation integration harness.
    return subprocess.run(
        command, cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
    )


def test_failed_batch_cannot_leak_into_renamed_translation_batch(case):
    """Resume a failed batch without adding its paths to a later rename PR."""
    repo = case["repo"]
    git(repo, "rm", "-f", "--", "l10n/new space.properties")
    renamed = repo / "l10n/second-renamed.properties"
    renamed.write_text("key=old\n", encoding="utf-8")
    git(repo, "add", "--", "l10n/second-renamed.properties")
    case["files"] = [
        "l10n/first.properties", "l10n/third.properties",
        "l10n/second.properties", "l10n/second-renamed.properties",
    ]
    case["patch"] = git(repo, "diff", "--cached", "--binary", "--full-index", "--no-renames")
    case["faults"]["pr"] = 1
    with pytest.raises(PublicationError):
        publish(case, max_files_per_pr=2)
    first_head = receipt(case, 1)["head"]
    assert not (case["report_dir"] / "publication/batch-2").exists()
    assert_candidate_preserved(case)

    case["faults"].clear()
    assert len(publish(case, max_files_per_pr=2)) == 2
    assert receipt(case, 1)["head"] == first_head
    second = receipt(case, 2)
    assert git(repo, "rev-parse", second["head"] + "^") == case["base"]
    assert set(git(repo, "diff", "--name-only", "--no-renames", case["base"], second["head"]).splitlines()) == set(case["files"][2:])
    assert git(repo, "diff", "--name-status", "--find-renames", case["base"], second["head"]) == "R100\tl10n/second.properties\tl10n/second-renamed.properties"
    assert_candidate_preserved(case)
