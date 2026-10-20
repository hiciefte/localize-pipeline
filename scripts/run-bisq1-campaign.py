#!/usr/bin/env python3
"""Add independently gated locales to the approved Bisq 1 German patch."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from localize.properties_parser import parse_properties_file
from localize.translation_memory import (
    TranslationMemory, load_translation_memory_strict, write_translation_memory, normalize_memory_source,
)

RESOURCE_PATH = "core/src/main/resources/i18n"
UPSTREAM = "https://github.com/bisq-network/bisq.git"
BRANCH = "codex/bisq1-translations"
FROZEN_HEAD = "2dab5f8f81d6937f8bc7f6308908894339fa0734"
GERMAN_SHA = "c136811bf6e49ec4e5cbfcf03c869d5f489552d965272bc069039966d4cce1f3"
ADDITIONAL_REQUESTS = 3000
STATE_FILES = ("ledger.json", "memory.json")


def git(repo, *args, env=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, env=env).strip()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def atomic_write(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    if path.exists():
        temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def profile_locales():
    return [row["code"] for row in yaml.safe_load((ROOT / "profiles/bisq1/config.yaml").read_text())["supported_locales"]]


def filename(locale):
    return f"displayStrings_{locale}.properties"


def load_job():
    spec = importlib.util.spec_from_file_location("bisq1_local", ROOT / "scripts/run-bisq1-local.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context_hash(state=None):
    digest = hashlib.sha256()
    files = sorted((ROOT / "localize").rglob("*.py")) + [
        ROOT / "profiles/bisq1/config.yaml", ROOT / "profiles/bisq1/glossary.json",
        ROOT / "scripts/run-bisq1-local.py", Path(__file__).resolve(),
    ]
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    if state is not None:
        from dotenv import dotenv_values
        inherited = {**dotenv_values(ROOT / ".env"), **dotenv_values(state / ".env"), **os.environ}
        for name in ("OPENAI_BASE_URL", "REVIEW_MODEL_NAME", "REVIEW_REASONING_EFFORT",
                     "MODEL_REASONING_EFFORT", "HOLISTIC_REVIEW_CHUNK_SIZE", "TRANSLATION_FILTER_GLOB"):
            digest.update(json.dumps([name, str(inherited.get(name) or "").strip()]).encode())
    return digest.hexdigest()


def request_count(state):
    database = Path(state) / "run_safeguards.sqlite3"
    if not database.is_file():
        raise RuntimeError("Existing shared request database is required; never create a fresh budget.")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]


def state_hashes(state):
    return {name: sha(state / name) for name in STATE_FILES}


def load_manifest(directory):
    return json.loads((directory / "manifest.json").read_text())


def save_manifest(directory, manifest):
    write_json(directory / "manifest.json", manifest)


def create_campaign(state, directory, locales):
    state, directory = Path(state).resolve(), Path(directory).resolve()
    if directory.exists():
        raise RuntimeError("Campaign already exists; use --resume.")
    if not directory.is_relative_to(state / "campaigns"):
        raise RuntimeError("Campaign must be inside the canonical state's campaigns directory.")
    if not locales or len(set(locales)) != len(locales) or set(locales) - set(profile_locales()) or "de" in locales:
        raise ValueError("Choose distinct configured locales other than the already approved German locale.")
    repo = state / "bisq"
    if git(repo, "remote", "get-url", "origin") != UPSTREAM or git(repo, "branch", "--show-current") != BRANCH:
        raise RuntimeError("Unexpected Bisq 1 origin or branch.")
    if git(repo, "rev-parse", "HEAD") != FROZEN_HEAD:
        raise RuntimeError("Campaign must start at the approved frozen upstream HEAD.")
    german = repo / RESOURCE_PATH / filename("de")
    if sha(german) != GERMAN_SHA:
        raise RuntimeError("German file does not match the explicitly approved content.")
    if git(repo, "diff", "--name-only") or git(repo, "ls-files", "--others", "--exclude-standard"):
        raise RuntimeError("Unstaged or untracked work must be preserved and reviewed first.")
    if git(repo, "diff", "--cached", "--name-only").splitlines() != [f"{RESOURCE_PATH}/{filename('de')}"]:
        raise RuntimeError("Initial staged patch must contain only the approved German target.")
    source = parse_properties_file(str(repo / RESOURCE_PATH / "displayStrings.properties"))[1]
    target = parse_properties_file(str(german))[1]
    if read_ledger(state / "ledger.json")["files"].get(filename("de")) != expected_ledger(source, target):
        raise RuntimeError("Approved German ledger does not match final source/target content.")
    count = request_count(state)
    manifest = {
        "version": 1, "state": str(state), "created": stamp(), "source_head": FROZEN_HEAD,
        "source_hash": sha(repo / RESOURCE_PATH / "displayStrings.properties"),
        "context_hash": context_hash(state), "locales": locales,
        "targets": {locale: sha(repo / RESOURCE_PATH / filename(locale)) for locale in profile_locales()},
        "index_tree": git(repo, "write-tree"), "state_hashes": state_hashes(state),
        "initial_request_count": count, "request_ceiling": count + ADDITIONAL_REQUESTS,
        "accepted": {"de": {"status": "independent_reviewed", "target_hash": GERMAN_SHA}},
        "attempts": [],
    }
    if any(value is None for value in manifest["targets"].values()) or manifest["state_hashes"]["ledger.json"] is None:
        raise RuntimeError("All baseline target files and accepted ledger must exist.")
    load_translation_memory_strict(state / "memory.json")
    directory.mkdir(parents=True)
    save_manifest(directory, manifest)
    write_json(directory / "origin.json", {key: manifest[key] for key in (
        "state", "created", "source_head", "source_hash", "context_hash", "locales",
        "initial_request_count", "request_ceiling",
    )})
    return manifest


def verify_parent(state, manifest):
    repo = state / "bisq"
    if str(state.resolve()) != manifest["state"] or context_hash(state) != manifest["context_hash"]:
        raise RuntimeError("Campaign state or code/profile context changed; review before continuing.")
    if git(repo, "remote", "get-url", "origin") != UPSTREAM or git(repo, "branch", "--show-current") != BRANCH:
        raise RuntimeError("Campaign origin or approved branch changed.")
    if git(repo, "rev-parse", "HEAD") != manifest["source_head"] or sha(repo / RESOURCE_PATH / "displayStrings.properties") != manifest["source_hash"]:
        raise RuntimeError("Frozen English/source HEAD changed.")
    if git(repo, "write-tree") != manifest["index_tree"] or git(repo, "diff", "--name-only") or git(repo, "ls-files", "--others", "--exclude-standard"):
        raise RuntimeError("Approved index or worktree changed outside campaign promotion.")
    for locale, expected in manifest["targets"].items():
        if sha(repo / RESOURCE_PATH / filename(locale)) != expected:
            raise RuntimeError(f"Unexpected target change: {locale}")
    if state_hashes(state) != manifest["state_hashes"]:
        raise RuntimeError("Canonical ledger/memory changed outside campaign promotion.")
    if request_count(state) < manifest["initial_request_count"]:
        raise RuntimeError("Shared request history was reset or replaced.")


def write_child_config(job, repo, attempt, locales, report, state, manifest):
    path = job.write_config(repo, attempt, locales, report)
    config = yaml.safe_load(path.read_text())
    config["run_safeguards"].update({
        "state_path": str((state / "run_safeguards.sqlite3").resolve()),
        "max_requests_per_24h": manifest["request_ceiling"],
        "max_requests_total": manifest["request_ceiling"],
    })
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    return path


def run_child(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    state, attempt = Path(spec["state"]), Path(spec["attempt"])
    manifest = load_manifest(Path(spec["campaign"]))
    verify_parent(state, manifest)
    from dotenv import load_dotenv
    load_dotenv(state / ".env", override=False)
    job = load_job()
    original_write = job.write_config

    def configure(repo, child_state, locales, report=None):
        # Restore only while invoking the original helper; each child owns its process.
        job.write_config = original_write
        try:
            path = write_child_config(job, repo, child_state, locales, report, state, manifest)
        finally:
            job.write_config = configure
        # Verify the effective loader, not just the YAML spelling, before any call.
        from localize.run_safeguards import load_safeguards
        safeguards = load_safeguards(yaml.safe_load(path.read_text()), str(path))
        if safeguards.path.resolve() != (state / "run_safeguards.sqlite3").resolve() or safeguards.max_requests_total != manifest["request_ceiling"]:
            raise RuntimeError("Child did not bind the shared lifetime request limit.")
        return path

    def pinned_checkout(repo):
        if repo.resolve() != (attempt / "bisq").resolve() or git(repo, "rev-parse", "HEAD") != manifest["source_head"]:
            raise RuntimeError("Private candidate is not at the manifest-pinned source HEAD.")
        if git(repo, "status", "--porcelain") or sha(repo / RESOURCE_PATH / "displayStrings.properties") != manifest["source_hash"]:
            raise RuntimeError("Private candidate is not clean or has changed English.")

    job.write_config = configure
    job.prepare_checkout = pinned_checkout
    args = SimpleNamespace(locales=[spec["locale"]], check=False, base_ref=spec["base_ref"], resume_memory=None)
    job.run(args, attempt)


def prepare_attempt(state, directory, manifest, locale):
    verify_parent(state, manifest)
    attempt = directory / "runs" / f"{locale}-{stamp()}"
    repo = attempt / "bisq"
    attempt.mkdir(parents=True)
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(state / "bisq"), str(repo)], check=True)
    git(repo, "sparse-checkout", "init", "--cone")
    git(repo, "sparse-checkout", "set", RESOURCE_PATH)
    git(repo, "checkout", "--quiet", "--detach", manifest["source_head"])
    git(repo, "remote", "set-url", "origin", UPSTREAM)
    with (repo / ".git/info/exclude").open("a") as stream:
        stream.write(f"\n/{RESOURCE_PATH}/archive/\n")
    # Copy only this locale's starting bytes. Previous accepted locales stay in parent.
    target = repo / RESOURCE_PATH / filename(locale)
    shutil.copyfile(state / "bisq" / RESOURCE_PATH / filename(locale), target)
    if git(repo, "status", "--porcelain"):
        raise RuntimeError("Unaccepted locale has changed from frozen upstream; review it first.")
    for name in STATE_FILES:
        if (state / name).exists(): shutil.copyfile(state / name, attempt / name)
    spec = {"state": str(state), "campaign": str(directory), "attempt": str(attempt), "locale": locale,
            "base_ref": load_job().INITIAL_BASE}
    write_json(attempt / "child.json", spec)
    return attempt


def read_ledger(path):
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("files"), dict):
        raise RuntimeError("Invalid accepted ledger schema.")
    return payload


def expected_ledger(source, target):
    # Match the runtime's normalized ledger hashes without initializing a provider.
    def digest(value):
        value = re.sub(r"\s+", " ", normalize_memory_source(value).strip())
        return hashlib.sha256(value.encode()).hexdigest()
    return {key: {"source_hash": digest(source.get(key, "")), "target_hash": digest(value)}
            for key, value in target.items()}


def verify_candidate(state, manifest, locale, attempt, report):
    from localize.semantic_quality import iter_translation_changes_from_diff
    from localize.translation_quality_gate import get_staged_diff
    verify_parent(state, manifest)
    repo = attempt / "bisq"
    path = f"{RESOURCE_PATH}/{filename(locale)}"
    if git(repo, "rev-parse", "HEAD") != manifest["source_head"] or sha(repo / RESOURCE_PATH / "displayStrings.properties") != manifest["source_hash"]:
        raise RuntimeError("Candidate English/head changed.")
    if git(repo, "diff", "--name-only") or set(git(repo, "diff", "--cached", "--name-only").splitlines()) - {path}:
        raise RuntimeError("Candidate contains unexpected changed paths.")
    source = parse_properties_file(str(repo / RESOURCE_PATH / "displayStrings.properties"))[1]
    target = parse_properties_file(str(repo / path))[1]
    if set(target) != set(source):
        raise RuntimeError("Candidate key coverage does not match English.")
    from localize.translation_validator import check_placeholder_parity, find_disallowed_control_characters
    before = parse_properties_file(str(state / "bisq" / path))[1]
    expected = {key for key, value in target.items() if key in source and before.get(key) != value}
    if any(not check_placeholder_parity(source[key], target[key]) or find_disallowed_control_characters(target[key]) for key in expected):
        raise RuntimeError("Candidate changed value failed structural validation.")
    changes = list(iter_translation_changes_from_diff(get_staged_diff(str(repo), [path]), str(repo), RESOURCE_PATH, [locale], require_source=True))
    if {change.key for change in changes} != expected or any(change.source_value != source[change.key] for change in changes):
        raise RuntimeError("Candidate source-hydrated coverage does not match actual changed keys.")
    quality = json.loads((report / "quality.json").read_text())
    if quality.get("blocking") is not False:
        raise RuntimeError("Candidate quality gate did not pass.")
    stats = quality.get("source_identical", {})
    if stats.get("changed_entries_count") != len(expected) or stats.get("checked_entries_count", 0) + stats.get("expected_source_identical_count", 0) != len(expected):
        raise RuntimeError("Quality coverage does not match changed values and expected exemptions.")
    summary_path = report / "logs/translation_validation_summary.json"
    if not summary_path.exists(): summary_path = report / "validation.json"
    summary = json.loads(summary_path.read_text())
    semantic = summary.get("semantic_review", {})
    if expected and (not semantic.get("attempted") or semantic.get("failed_batches") != 0 or semantic.get("failed_locales") != [] or semantic.get("failed_run")):
        raise RuntimeError("Candidate semantic review is missing or incomplete.")
    ledger = read_ledger(report / "ledger.json")["files"]
    if ledger.get(filename(locale)) != expected_ledger(source, target):
        raise RuntimeError("Candidate ledger does not match the final serialized target.")
    return {"coverage": len(expected), "changed_keys": sorted(expected), "target_hash": sha(repo / path),
            "source_hash": manifest["source_hash"], "context_hash": manifest["context_hash"]}


def index_path(repo):
    path = Path(git(repo, "rev-parse", "--git-path", "index"))
    return path if path.is_absolute() else repo / path


def prepare_promotion(state, directory, manifest, locale, attempt, report, proof):
    verify_parent(state, manifest)
    journal = directory / "promotions" / f"{locale}-{stamp()}"
    journal.mkdir(parents=True)
    repo = state / "bisq"
    target = repo / RESOURCE_PATH / filename(locale)
    shutil.copyfile(target, journal / "target.before")
    shutil.copyfile(attempt / "bisq" / RESOURCE_PATH / filename(locale), journal / "target.after")
    shutil.copyfile(index_path(repo), journal / "index.before")
    shutil.copyfile(index_path(repo), journal / "index.after")
    environment = {**os.environ, "GIT_INDEX_FILE": str(journal / "index.after")}
    blob = git(repo, "hash-object", "-w", str(journal / "target.after"))
    mode = git(repo, "ls-files", "-s", "--", f"{RESOURCE_PATH}/{filename(locale)}").split()[0]
    git(repo, "update-index", "--cacheinfo", f"{mode},{blob},{RESOURCE_PATH}/{filename(locale)}", env=environment)
    after = deepcopy(manifest)
    after["targets"][locale] = proof["target_hash"]
    after["index_tree"] = git(repo, "write-tree", env=environment)
    after["accepted"][locale] = {**proof, "status": "pipeline_validated", "report": str(report), "accepted": stamp(), "requests_after": request_count(state)}
    for name in STATE_FILES:
        if (state / name).exists(): shutil.copyfile(state / name, journal / (name + ".before"))
    ledger = read_ledger(state / "ledger.json")
    ledger["files"][filename(locale)] = read_ledger(report / "ledger.json")["files"][filename(locale)]
    write_json(journal / "ledger.json.after", ledger)
    memory = load_translation_memory_strict(state / "memory.json")
    candidate = load_translation_memory_strict(report / "memory.json")
    local = TranslationMemory(entries={key: value for key, value in candidate.entries.items() if value.get("locale") == locale})
    memory.merge_from(local)
    write_translation_memory(journal / "memory.json.after", memory)
    after["state_hashes"] = {name: sha(journal / (name + ".after")) for name in STATE_FILES}
    write_json(journal / "manifest.before", manifest)
    write_json(journal / "manifest.after", after)
    write_json(journal / "journal.json", {"locale": locale, "phase": "prepared"})
    write_json(directory / "promotion.json", {"journal": str(journal)})
    return journal


def recover_promotion(state, directory):
    pointer = directory / "promotion.json"
    if not pointer.exists(): return
    journal = Path(json.loads(pointer.read_text())["journal"])
    if not journal.resolve().is_relative_to((directory / "promotions").resolve()):
        raise RuntimeError("Promotion journal is outside this campaign.")
    info = json.loads((journal / "journal.json").read_text())
    before = json.loads((journal / "manifest.before").read_text())
    after = json.loads((journal / "manifest.after").read_text())
    repo = state / "bisq"
    # Only our exact before/after bytes may be restored; preserve human changes.
    locale = info["locale"]
    if context_hash(state) != before["context_hash"] or git(repo, "rev-parse", "HEAD") != before["source_head"] or sha(repo / RESOURCE_PATH / "displayStrings.properties") != before["source_hash"]:
        raise RuntimeError("Source/context drift during interrupted promotion; manual recovery required.")
    for code, value in before["targets"].items():
        permitted = {value, after["targets"][code]} if code == locale else {value}
        if sha(repo / RESOURCE_PATH / filename(code)) not in permitted:
            raise RuntimeError("Target edited during interrupted promotion; preserve manual changes.")
    if sha(index_path(repo)) not in {sha(journal / "index.before"), sha(journal / "index.after")}:
        raise RuntimeError("Index bytes changed during interrupted promotion; preserve manual metadata.")
    if git(repo, "write-tree") not in {before["index_tree"], after["index_tree"]} or state_hashes(state) not in [before["state_hashes"], after["state_hashes"]]:
        # Mixed state writes are also valid; check each file independently below.
        for name in STATE_FILES:
            if sha(state / name) not in {before["state_hashes"][name], after["state_hashes"][name]}:
                raise RuntimeError("State edited during interrupted promotion; manual recovery required.")
        if git(repo, "write-tree") not in {before["index_tree"], after["index_tree"]}:
            raise RuntimeError("Index edited during interrupted promotion; manual recovery required.")
    if sha(directory / "manifest.json") not in {sha(journal / "manifest.before"), sha(journal / "manifest.after")}:
        raise RuntimeError("Manifest edited during interrupted promotion.")
    if info["phase"] != "committed":
        atomic_write(repo / RESOURCE_PATH / filename(locale), (journal / "target.before").read_bytes())
        atomic_write(index_path(repo), (journal / "index.before").read_bytes())
        for name in STATE_FILES:
            backup = journal / (name + ".before")
            if backup.exists(): atomic_write(state / name, backup.read_bytes())
            else: (state / name).unlink(missing_ok=True)
        atomic_write(directory / "manifest.json", (journal / "manifest.before").read_bytes())
        write_json(journal / "journal.json", {**info, "phase": "rolled_back"})
    else:
        verify_parent(state, after)
    pointer.unlink()


def promote(state, directory, manifest, locale, attempt, report, proof):
    # Recheck evidence immediately before accepting any canonical state.
    if verify_candidate(state, manifest, locale, attempt, report) != proof:
        raise RuntimeError("Candidate changed after gate verification.")
    journal = prepare_promotion(state, directory, manifest, locale, attempt, report, proof)
    try:
        repo = state / "bisq"
        atomic_write(repo / RESOURCE_PATH / filename(locale), (journal / "target.after").read_bytes())
        atomic_write(index_path(repo), (journal / "index.after").read_bytes())
        for name in STATE_FILES: atomic_write(state / name, (journal / (name + ".after")).read_bytes())
        atomic_write(directory / "manifest.json", (journal / "manifest.after").read_bytes())
        verify_parent(state, load_manifest(directory))
        write_json(journal / "journal.json", {"locale": locale, "phase": "committed"})
        (directory / "promotion.json").unlink()
    except BaseException:
        recover_promotion(state, directory)
        raise


def verify_origin(directory, manifest):
    origin = json.loads((directory / "origin.json").read_text())
    if any(manifest.get(key) != value for key, value in origin.items()):
        raise RuntimeError("Immutable campaign source/context/allowance changed.")


def reusable_report(state, directory, manifest, locale):
    # Discover even a child completed before its parent's attempt receipt was saved.
    for attempt in sorted((directory / "runs").glob(locale + "-*"), reverse=True):
        latest = attempt / "latest-run.txt"
        if not latest.exists(): continue
        report = Path(latest.read_text().strip()).resolve()
        if not report.is_relative_to(attempt.resolve()):
            raise RuntimeError("Prior candidate report is outside its attempt workspace.")
        try:
            proof = verify_candidate(state, manifest, locale, attempt, report)
        except (OSError, ValueError, RuntimeError, KeyError):
            continue
        return attempt, report, proof
    return None


def execute(state, directory, worker=None, lock_fd=None):
    recover_promotion(state, directory)
    manifest = load_manifest(directory)
    verify_origin(directory, manifest)
    verify_parent(state, manifest)
    failed = []
    for locale in manifest["locales"]:
        manifest = load_manifest(directory)
        verify_origin(directory, manifest)
        if locale in manifest["accepted"]: continue
        previous = reusable_report(state, directory, manifest, locale)
        if previous:
            attempt, report, proof = previous
            promote(state, directory, manifest, locale, attempt, report, proof)
            print(f"{locale}: reused an already gated candidate; no new calls.", flush=True)
            continue
        if request_count(state) >= manifest["request_ceiling"]:
            raise RuntimeError("Campaign lifetime request allowance is exhausted; accepted locales remain preserved.")
        attempt = prepare_attempt(state, directory, manifest, locale)
        manifest["attempts"].append({"locale": locale, "path": str(attempt), "status": "running", "requests_before": request_count(state)})
        save_manifest(directory, manifest)
        print(f"Starting {locale}; report workspace {attempt}", flush=True)
        try:
            if worker is None:
                if lock_fd is None: raise RuntimeError("Paid child requires the inherited canonical job lock.")
                with (attempt / "execution.log").open("w") as log:
                    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(attempt / "child.json")], stdout=log, stderr=subprocess.STDOUT, check=True, pass_fds=(lock_fd,))
                report = Path((attempt / "latest-run.txt").read_text().strip())
            else:
                report = worker(state, directory, manifest, locale, attempt)
            proof = verify_candidate(state, manifest, locale, attempt, report)
            promote(state, directory, manifest, locale, attempt, report, proof)
            print(f"{locale}: pipeline validated; independent meaning review remains separate.", flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            failed.append(locale)
            print(f"{locale}: withheld: {exc}; inspect {attempt}", flush=True)
        manifest = load_manifest(directory)
        receipt = next(row for row in reversed(manifest["attempts"]) if row["path"] == str(attempt))
        receipt.update({"status": "failed" if locale in failed else "pipeline_validated", "requests_after": request_count(state)})
        save_manifest(directory, manifest)
        verify_parent(state, manifest)
    return failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / ".agent-artifacts/bisq1-local")
    parser.add_argument("--locales", nargs="+")
    parser.add_argument("--resume", type=Path, help="Existing campaign directory; its fixed allowance is retained.")
    parser.add_argument("--prepare-only", action="store_true", help="Freeze manifest and preflight without model calls.")
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        run_child(args.child)
        return 0
    state = args.work_dir.expanduser().resolve()
    with (state / "job.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.resume:
                directory = args.resume.expanduser().resolve()
                if not directory.is_relative_to(state / "campaigns"):
                    raise RuntimeError("Resume directory is outside canonical campaign state.")
                if args.locales: raise ValueError("Resume uses the campaign's original locale list.")
                recover_promotion(state, directory)
                verify_origin(directory, load_manifest(directory))
                verify_parent(state, load_manifest(directory))
            else:
                directory = state / "campaigns" / stamp()
                create_campaign(state, directory, args.locales)
            print(f"Campaign: {directory}", flush=True)
            return int(bool(execute(state, directory, lock_fd=lock.fileno()))) if not args.prepare_only else 0
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
