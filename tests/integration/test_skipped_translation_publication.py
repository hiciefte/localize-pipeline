"""Imported files that fail validation withhold the entire shared workflow."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.mark.parametrize("check,valid_file,new_file", [
    (False, True, False), (False, False, False),
    (True, True, False), (False, True, True),
])
def test_skipped_import_never_reaches_publication(tmp_path, check, valid_file, new_file):
    """Retain failed import evidence and publish no successful subset."""
    repo = tmp_path / "target"
    folder = repo / "l10n"
    folder.mkdir(parents=True)
    source = "label=Options\nhelp=Choose options\nunrelated=Value\n"
    (folder / "settings.properties").write_text(source)
    (folder / "new.properties").write_text(source)
    skipped = folder / "settings_pt_PT.properties"
    valid = folder / "settings_ta.properties"
    original = "label=Opções\nhelp=Escolha as opções\nunrelated=Valor\n"
    imported = "label=Options\nhelp=Choose options\nunrelated=Valor\x7f\n"
    valid_original = "label=முன்பு\nhelp=முந்தைய தேர்வுகள்\nunrelated=மதிப்பு\n"
    skipped.write_text(original)
    valid.write_text(valid_original)
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Import test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "Original")
    base = git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    state.mkdir()
    baseline = state / "source-baseline.txt"
    baseline.write_text(base + "\n")
    if new_file:
        skipped = folder / "new_pt_PT.properties"
    config = tmp_path / "config.yaml"
    (tmp_path / "glossary.json").write_text("{}")
    config.write_text(yaml.safe_dump({
        "target_project_root": str(repo), "input_folder": "l10n",
        "workflow": {"default_branch": "main"},
        "translation_source": "transifex", "process_all_files": True,
        "glossary_file_path": str(tmp_path / "glossary.json"),
        "model_name": "gpt-4o-mini", "review_model_name": "gpt-4o-mini",
        "semantic_review": {"enabled": True, "model": "gpt-4o-mini"},
        "supported_locales": [
            {"code": "pt_PT", "name": "Portuguese"}, {"code": "ta", "name": "Tamil"},
        ],
    }))
    fixture = tmp_path / "import.json"
    fixture.write_text(json.dumps({
        "skipped": str(skipped), "imported": imported,
        "valid": str(valid), "valid_file": valid_file,
    }))
    harness = r'''
import json
from pathlib import Path
import sys
from localize import workflow

data = json.loads(Path(sys.argv[1]).read_text())
marker = Path(sys.argv[2])
def import_translations(config, repo):
    Path(data["skipped"]).write_text(data["imported"])
    if data["valid_file"]:
        Path(data["valid"]).write_text("label=விருப்பங்கள்\nhelp=விருப்பங்களைத் தேர்வுசெய்க\nunrelated=மதிப்பு\n")
def forbidden_publication(*args):
    marker.write_text("attempted")
    raise AssertionError("A skipped file must withhold the entire publication")
workflow.pull_source = import_translations
workflow.prepare_publication = lambda *args: None
workflow.publish_receipt = forbidden_publication
workflow.heartbeat = lambda: marker.write_text("heartbeat")
raise SystemExit(workflow.main(sys.argv[3:]))
'''
    marker = tmp_path / "published-or-heartbeat"
    env = dict(os.environ, OPENAI_API_KEY="test-only", OPENAI_BASE_URL="http://127.0.0.1:1")
    for name in ("TRANSLATION_DIFF_BASE", "LOCALIZE_DRY_RUN", "TRANSLATION_FILTER_GLOB"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", harness, str(fixture), str(marker),
         "--config", str(config), "--repo", str(repo), "--state-dir", str(state),
         "--no-fetch", *(["--check", "--no-publish"] if check else ["--publish"])],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == (0 if check else 1), result.stdout + result.stderr
    assert not marker.exists()
    assert baseline.read_text().strip() == base
    assert git(repo, "rev-parse", "HEAD") == base
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert not (state / "ledger.json").exists()
    assert not (state / "memory.json").exists()
    if check:
        assert skipped.read_text() == original
        assert valid.read_text() == valid_original
        return
    report = Path((state / "latest-run.txt").read_text().strip())
    assert (report / "originals" / skipped.name).read_text() == imported
    assert skipped.read_text() == imported
    receipt = json.loads((report / "receipt.json").read_text())
    assert receipt["status"] == "failed"
    assert skipped.name in receipt["error"]
    assert "U+007F" in receipt["error"]
    usage = json.loads((report / "logs/token_usage_summary.json").read_text())
    assert usage["totals"]["calls"] == 0
