"""The complete, shared local/server workflow. Publishing is an optional last step.

Run this module in a fresh process: the translation engine loads one configuration
at import time. Reports retain candidates on failure; accepted state is promoted
only after the final gate. The request database is deliberately never rolled back.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml
from dotenv import load_dotenv

from localize.localization_profiles import load_localization_profiles
from localize.localization_adapters import get_localization_adapter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_FILES_PER_PR = 90


def command(*args, cwd=None):
    result = subprocess.run(list(map(str, args)), cwd=cwd, text=True, capture_output=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1] if len(args) > 1 else ''} failed (exit {result.returncode}).")
    return result.stdout.strip()


def git(repo, *args):
    return command("git", "-C", repo, *args)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def event(name, **fields):
    print("PIPELINE_EVENT " + json.dumps({"event": name, **fields}), flush=True)


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another localization run holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def resolve_config(args):
    path = (ROOT / "profiles" / args.profile if args.profile else
            Path(args.config or os.environ.get("TRANSLATOR_CONFIG_FILE", ROOT / "config.yaml")))
    path = path.expanduser().resolve()
    if path.is_dir():
        path /= "config.yaml"
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping.")
    return path, config


def resolved_path(value, parent):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else parent / path).resolve()


def boolean(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes", "on"}:
        return True
    if value is None or str(value).lower() in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"Expected a boolean, got {value!r}.")


def persistent_path(raw, default, config_dir):
    # Bundled profiles contain container/default paths. Explicit custom paths
    # retain their meaning, including existing server state in /app/logs.
    if not raw:
        return default
    text = str(raw)
    defaults = {"logs/translation_key_ledger.json", "logs/translation_memory.json",
                "logs/run_safeguards.sqlite3"}
    if text in defaults or (text.startswith("/app/") and ROOT != Path("/app")):
        return default
    return resolved_path(text, config_dir)


def prepare_config(args, path, config, state, report):
    config = dict(config)
    workflow = config.get("workflow", {}) or {}
    original_root = str(config.get("target_project_root", "/target_repo"))
    requested = args.repo or os.environ.get("TARGET_REPO_DIR")
    if requested:
        repo = Path(requested).expanduser().resolve()
    elif original_root == "/target_repo" and ROOT != Path("/app"):
        repo = state / "repo"
    else:
        repo = resolved_path(original_root, path.parent)
    raw_input = str(config.get("input_folder", ""))
    if not raw_input:
        raise ValueError("input_folder must be configured.")
    if Path(raw_input).is_absolute() and raw_input.startswith(original_root.rstrip("/") + "/"):
        raw_input = raw_input[len(original_root.rstrip("/")) + 1:]
    input_folder = resolved_path(raw_input, repo)
    if not input_folder.is_relative_to(repo):
        raise ValueError("input_folder must be inside the target repository.")
    codes = [row if isinstance(row, str) else row["code"] for row in config.get("supported_locales", [])]
    if not codes or set(args.locales) - set(codes):
        raise ValueError("Configure supported_locales and select only known locale codes.")
    if args.locales:
        config["supported_locales"] = [row for row in config["supported_locales"]
                                      if (row if isinstance(row, str) else row["code"]) in args.locales]
    raw_glossary = str(config.get("glossary_file_path", "glossary.json"))
    if raw_glossary == "/app/glossary.json" and ROOT != Path("/app"):
        raw_glossary = "glossary.json"
    glossary = resolved_path(raw_glossary, path.parent)
    if not glossary.is_file():
        raise ValueError(f"Glossary not found: {glossary}")
    safeguards = config.get("run_safeguards")
    if safeguards is not None and not isinstance(safeguards, dict):
        raise ValueError("run_safeguards must be a mapping; full runs require safeguards.")
    safeguards = dict(safeguards or {})
    safeguards.setdefault("max_requests_per_24h", 1000)
    for key, value in {"sample_files": 3, "sample_keys": 5, "sample_max_requests": 60}.items():
        safeguards.setdefault(key, value)
    memory_default = ROOT / "logs/translation_memory.json" if ROOT == Path("/app") else state / "memory.json"
    accepted = {
        "ledger.json": persistent_path(config.get("translation_key_ledger_file_path"), state / "ledger.json", path.parent),
        "memory.json": persistent_path(config.get("translation_memory_file_path"), memory_default, path.parent),
    }
    safeguards["state_path"] = str(persistent_path(safeguards.get("state_path"), state / "run_safeguards.sqlite3", path.parent))
    persistent = [*accepted.values(), Path(safeguards["state_path"])]
    if len(set(persistent)) != len(persistent) or any(p.is_relative_to(report) or p in {repo, path, glossary} or p.is_relative_to(input_folder) for p in persistent):
        raise ValueError("Ledger, memory and request database must have distinct paths outside inputs and run reports.")
    config.update({
        "target_project_root": str(repo), "input_folder": str(input_folder),
        "glossary_file_path": str(glossary), "run_safeguards": safeguards,
        "translation_queue_folder": str(report / "queue"),
        "translated_queue_folder": str(report / "translated"),
        "translation_key_ledger_file_path": str(report / "ledger.json"),
        "translation_memory_file_path": str(report / "memory.json"),
        "process_all_files": True,
        "logging": {**(config.get("logging") or {}), "log_file_path": str(report / "logs/translation.log")},
    })
    # Workflow metadata is operational, not part of model acceptance fingerprints.
    config.pop("workflow", None)
    config.setdefault("semantic_review", {"enabled": True})
    if not isinstance(config["semantic_review"], dict) or not boolean(config["semantic_review"].get("enabled", False)):
        raise ValueError("The full workflow requires semantic_review.enabled: true.")
    config["semantic_review"]["enabled"] = True
    if "pull_source_files_from_transifex" in config:
        config["pull_source_files_from_transifex"] = boolean(config["pull_source_files_from_transifex"])
    source = str(config.get("translation_source", "transifex")).lower()
    if source not in {"git", "transifex"}:
        raise ValueError("translation_source must be git or transifex.")
    config["translation_source"] = source
    return config, repo, input_folder, accepted, workflow


def fork_url(fork):
    return (f"git@github.com:{fork}.git" if ROOT == Path("/app") or os.environ.get("DEPLOY_KEY_PATH")
            else f"https://github.com/{fork}.git")


def prepare_checkout(repo, workflow, *, no_fetch=False, upstream=None, target_branch=None, fork=None):
    upstream = upstream or os.environ.get("UPSTREAM_REPO_NAME") or workflow.get("upstream_repository")
    branch = target_branch or os.environ.get("TARGET_BRANCH_FOR_PR") or workflow.get("default_branch")
    clone_url = os.environ.get("FORK_REPO_URL") or (fork_url(fork) if fork else workflow.get("repository_url"))
    if not repo.exists() or not any(repo.iterdir()):
        if no_fetch:
            raise ValueError("--no-fetch requires an existing repository.")
        if not clone_url:
            raise ValueError("Provide --repo with an existing checkout, or workflow.repository_url.")
        command("git", "clone", "--no-tags", "--", clone_url, repo)
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("Target must be the Git checkout root.")
    if git(repo, "status", "--porcelain"):
        raise RuntimeError("Workspace has pending changes. Review them or use --publish-run; nothing was discarded.")
    if no_fetch:
        return git(repo, "rev-parse", "HEAD"), branch
    remotes = git(repo, "remote").splitlines()
    if upstream and "upstream" not in remotes:
        origin = git(repo, "remote", "get-url", "origin")
        if not origin.rstrip("/").removesuffix(".git").endswith("/" + upstream) and not origin.endswith(":" + upstream + ".git"):
            git(repo, "remote", "add", "upstream", f"https://github.com/{upstream}.git")
            remotes.append("upstream")
    remote = "upstream" if "upstream" in remotes else "origin"
    if remote not in remotes:
        raise ValueError("No source remote; use --no-fetch for an offline existing checkout.")
    if upstream:
        from localize.workflow_publication import _remote_repository
        if _remote_repository(git(repo, "remote", "get-url", remote)) != upstream.casefold():
            raise ValueError("Source remote does not match the selected upstream repository.")
    if not branch:
        branch = git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD").split("/", 1)[1]
    git(repo, "check-ref-format", f"refs/heads/{branch}")
    git(repo, "fetch", "--no-tags", remote, branch)
    head = git(repo, "rev-parse", "HEAD")
    upstream_head = git(repo, "rev-parse", "FETCH_HEAD")
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, upstream_head], check=False).returncode:
        raise RuntimeError("Local commits are not yet in upstream; review/merge them before another run.")
    git(repo, "merge", "--ff-only", upstream_head)
    return git(repo, "rev-parse", "HEAD"), branch


def pending_pr_guard(upstream, *, required=False):
    if not upstream or not shutil.which("gh"):
        return False
    # Local previews need no GitHub credentials. Configured server credentials
    # retain the existing guard, and query errors cannot silently bypass it.
    if not required and not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        return False
    rows = json.loads(command("gh", "pr", "list", "--repo", upstream, "--state", "open",
                              "--author", "@me", "--json", "number,title,headRefName"))
    for row in rows:
        if "[PIPELINE-BLOCK]" in row["title"] or row["headRefName"].startswith(("translation-updates", "codex/bisq1-translations")):
            event("pending_pr_guard", number=row["number"])
            print("BLOCKING CONDITION DETECTED: existing translation PR; no model calls.", flush=True)
            return True
    return False


def pull_source(config, repo):
    if config["translation_source"] == "git":
        return
    if not shutil.which("tx"):
        raise RuntimeError("Transifex source requires the tx CLI.")
    args = ["tx", "pull", "-t", "-f", "--use-git-timestamps"]
    if config.get("pull_source_files_from_transifex", False):
        args.insert(2, "-s")
    # Only configured/selected locales belong to this run.
    codes = [row if isinstance(row, str) else row["code"] for row in config["supported_locales"]]
    args += ["-l", ",".join(codes)]
    timeout = int(os.environ.get("TX_PULL_TIMEOUT_SECONDS", os.environ.get("TX_PULL_TIMEOUT", "3600")))
    if timeout <= 0:
        raise ValueError("TX_PULL_TIMEOUT_SECONDS must be positive.")
    subprocess.run(args, cwd=repo, check=True, timeout=timeout)


def discover(config, repo, input_folder):
    profiles = load_localization_profiles(config)
    codes = [row if isinstance(row, str) else row["code"] for row in config["supported_locales"]]
    selected, sources = {}, set()
    for path in sorted(input_folder.rglob("*")):
        if not path.is_file() or "archive" in path.relative_to(input_folder).parts:
            continue
        relative = path.relative_to(input_folder).as_posix()
        for profile in profiles:
            layout, fmt = profile.localization_layout, profile.localization_format
            if not layout.is_target_file(relative, codes, fmt):
                continue
            pattern = os.environ.get("TRANSLATION_FILTER_GLOB") or config.get("translation_file_filter_glob")
            if pattern and not fnmatch.fnmatch(relative, pattern):
                continue
            source = input_folder / layout.source_path_for_target(relative, codes, fmt)
            if not source.is_file():
                raise ValueError(f"Source missing for {relative}: {source}")
            if path.is_symlink() or source.is_symlink() or not path.resolve().is_relative_to(repo) or not source.resolve().is_relative_to(repo):
                raise ValueError(f"Localization files must remain inside the checkout: {relative}")
            selected[relative] = profile
            sources.add(source)
            break
    if not selected:
        raise ValueError("No configured target files found; check profile, locales and input_folder.")
    return selected, sources


def seed_state(config_path, report, accepted, selected, repo, input_folder, base_ref=None, resume_memory=None):
    from localize.translation_memory import load_translation_memory_strict, write_translation_memory
    from localize import translate_localization_files as runtime
    # Strict parsing prevents a corrupt accepted file from becoming an empty ledger.
    payload = json.loads(accepted["ledger.json"].read_text()) if accepted["ledger.json"].exists() else {}
    if not isinstance(payload, dict) or (payload and not isinstance(payload.get("files"), dict)):
        raise ValueError("Accepted ledger is not a mapping.")
    ledger = runtime.load_translation_key_ledger(str(accepted["ledger.json"]))
    memory = load_translation_memory_strict(accepted["memory.json"])
    if resume_memory:
        memory.merge_from(load_translation_memory_strict(resume_memory, require_exists=True))
    if base_ref:
        base = git(repo, "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}")
        subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, "HEAD"], check=True)
        for filename, profile in selected.items():
            if filename in ledger:
                continue
            source = runtime.get_source_filename(filename, list(runtime.LANGUAGE_CODES))
            rel = (input_folder / source).relative_to(repo).as_posix()
            old = report / "baseline" / source
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_bytes(subprocess.check_output(["git", "-C", str(repo), "show", f"{base}:{rel}"]))
            adapter = get_localization_adapter(profile.localization_format)
            ledger[filename] = runtime.build_file_key_ledger(adapter.parse_file(str(old))[1],
                                                          adapter.parse_file(str(input_folder / filename))[1])
    runtime.save_translation_key_ledger(str(report / "ledger.json"), ledger)
    if memory.entries or accepted["memory.json"].exists():
        write_translation_memory(report / "memory.json", memory)


def run_engine(config_path, report_dir, selected_files):
    from localize import translate_localization_files as runtime
    runtime.PROJECT_ROOT_DIR = str(report_dir)
    original_discover, original_archive = runtime.get_changed_translation_files, runtime.archive_original_files
    runtime.get_changed_translation_files = lambda *args, **kwargs: list(selected_files)
    runtime.archive_original_files = lambda files, folder, archive: original_archive(files, folder, str(report_dir / "originals"))
    try:
        return asyncio.run(runtime.main())
    finally:
        runtime.get_changed_translation_files, runtime.archive_original_files = original_discover, original_archive


def review_arguments(config_path, repo, input_folder, changed, report_dir):
    return ["--repo-root", str(repo), "--input-folder", str(input_folder), "--config", str(config_path),
            "--validation-summary", str(report_dir / "logs/translation_validation_summary.json"),
            "--changed-files", *changed]


def run_semantic(config_path, repo, input_folder, changed, report_dir):
    from localize.translation_semantic_reviewer import main
    return main([*review_arguments(config_path, repo, input_folder, changed, report_dir),
                 "--token-usage-summary", str(report_dir / "logs/token_usage_summary.json")])


def run_quality(config_path, repo, input_folder, changed, report_dir):
    from localize.translation_quality_gate import main
    args = [*review_arguments(config_path, repo, input_folder, changed, report_dir),
            "--output-json", str(report_dir / "translation_quality_report.json"),
            "--output-markdown", str(report_dir / "translation_quality_report.md")]
    if os.environ.get("TRANSLATION_DIFF_BASE"):
        args += ["--source-diff-base", os.environ["TRANSLATION_DIFF_BASE"]]
    if os.environ.get("TRANSLATION_QUALITY_AUDIT_SCOPE"):
        args += ["--audit-scope", os.environ["TRANSLATION_QUALITY_AUDIT_SCOPE"]]
    return main(args)


def reconcile_state(report, selected, input_folder):
    from localize import translate_localization_files as runtime
    from localize.translation_memory import load_translation_memory_strict, memory_source_hash, write_translation_memory
    ledger = runtime.load_translation_key_ledger(str(report / "ledger.json"))
    memory = load_translation_memory_strict(report / "memory.json")
    for filename, profile in selected.items():
        adapter = get_localization_adapter(profile.localization_format)
        source = runtime.get_source_filename(filename, list(runtime.LANGUAGE_CODES))
        sources = adapter.parse_file(str(input_folder / source))[1]
        targets = adapter.parse_file(str(input_folder / filename))[1]
        previous = ledger.get(filename, {})
        locale = profile.localization_layout.extract_locale(filename, list(runtime.LANGUAGE_CODES), profile.localization_format)
        for key, value in targets.items():
            if key not in sources:
                continue
            entry = previous.get(key, {})
            if entry.get("source_hash") != runtime.compute_ledger_hash(sources[key]):
                raise RuntimeError(f"Final value has no validated source ledger: {filename}:{key}")
            identity = f"{profile.localization_format.id}:{locale}:{memory_source_hash(sources[key])}"
            cached = memory.entries.get(identity)
            if cached and cached.get("status") != "conflict" and cached.get("target") != value:
                memory.entries.pop(identity, None)
        ledger[filename] = runtime.build_file_key_ledger(sources, targets)
    runtime.save_translation_key_ledger(str(report / "ledger.json"), ledger)
    if (report / "memory.json").exists():
        write_translation_memory(report / "memory.json", memory)


def changed_files(repo):
    return [p for p in subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--name-only", "-z"]).decode().split("\0") if p]


def verify_scope(repo, allowed, source_hashes):
    changed = changed_files(repo)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    unexpected = (set(changed) | set(filter(None, untracked))) - set(allowed)
    if unexpected:
        raise RuntimeError(f"Unexpected changed files retained for inspection: {sorted(unexpected)}")
    if any(not path.is_file() or digest(path) != value for path, value in source_hashes.items()):
        raise RuntimeError("Source changed during translation or review; candidate retained.")
    return sorted(set(changed) | set(filter(None, untracked)))


def promote_state(report, accepted):
    for name, target in accepted.items():
        source = report / name
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(target)


def heartbeat():
    url = os.environ.get("HEALTHCHECK_URL")
    if url:
        # Never include the private URL in logs or error messages.
        import urllib.request
        print("Sending heartbeat to health check URL", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                response.read()
        except Exception:
            print("Warning: Health check ping failed.", file=sys.stderr)


def prepare_publication(repo, upstream, fork):
    from localize.workflow_publication import preflight, _remote_repository
    if not upstream or not fork:
        raise ValueError("Publishing requires an explicit upstream and fork repository.")
    remotes = git(repo, "remote").splitlines()
    origin = _remote_repository(git(repo, "remote", "get-url", "origin"))
    if origin not in {upstream.casefold(), fork.casefold()}:
        raise ValueError("Existing origin is neither the selected upstream nor fork; preserve its configuration.")
    if "upstream" not in remotes:
        git(repo, "remote", "add", "upstream", f"https://github.com/{upstream}.git")
    if origin == upstream.casefold() and upstream.casefold() != fork.casefold():
        git(repo, "remote", "set-url", "origin", fork_url(fork))
    if git(repo, "remote", "get-url", "origin").startswith("https://github.com/"):
        # gh login/token authenticates its API, not Git automatically. Scope the
        # helper to this checkout and host; never put credentials in Git URLs.
        git(repo, "config", "--local", "credential.https://github.com.helper", "!gh auth git-credential")
    preflight(repo, upstream=upstream, fork=fork)


def publish_receipt(report, args):
    from localize.workflow_publication import publish, PublicationError
    receipt = json.loads((report / "receipt.json").read_text())
    if receipt.get("status") not in {"accepted", "published"}:
        raise RuntimeError("Only a successfully reviewed run can be published.")
    repo = Path(receipt["repo"])
    if git(repo, "rev-parse", "HEAD") != receipt["base"]:
        raise RuntimeError("Checkout HEAD changed since review; refusing to publish.")
    if digest(report / "config.yaml") != receipt["config_sha256"] or digest(report / "translation_quality_report.json") != receipt["quality_sha256"]:
        raise RuntimeError("Reviewed configuration or quality report changed.")
    patch = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
    if hashlib.sha256(patch).hexdigest() != receipt["patch_sha256"] or git(repo, "diff", "--name-only"):
        raise RuntimeError("Candidate differs from the reviewed patch.")
    upstream = args.upstream or receipt.get("upstream")
    fork = args.fork or os.environ.get("FORK_REPO_NAME") or receipt.get("fork")
    target_branch = args.target_branch or receipt.get("target_branch")
    if not upstream or not fork or not target_branch:
        raise ValueError("Publishing requires --upstream, --fork and --target-branch (or workflow/environment settings).")
    prepare_publication(repo, upstream, fork)
    try:
        urls = publish(repo, receipt["base"], receipt["changed_files"], report, report / "config.yaml",
                       upstream=upstream, fork=fork, branch=receipt["publication_branch"], target_branch=target_branch,
                       max_files_per_pr=args.max_files_per_pr)
    except PublicationError as exc:
        receipt["partial_pull_requests"] = exc.published_urls
        write_json(report / "receipt.json", receipt)
        for url in exc.published_urls:
            event("pull_request_created", url=url, publication_incomplete=True)
        raise
    receipt.update(status="published", pull_requests=urls, upstream=upstream, fork=fork, target_branch=target_branch)
    write_json(report / "receipt.json", receipt)
    write_json(Path(receipt["state_dir"]) / "published-run.json", {"report_dir": str(report)})
    for url in urls:
        event("pull_request_created", url=url)
    return urls


def run(args):
    if args.publish_run:
        report = Path(args.publish_run).expanduser().resolve()
        receipt = json.loads((report / "receipt.json").read_text())
        state = Path(receipt["state_dir"])
        repo = Path(receipt["repo"])
        load_dotenv(state / ".env", override=False)
        load_dotenv(Path(receipt["profile"]).parent / ".env", override=False)
        common = resolved_path(git(repo, "rev-parse", "--git-common-dir"), repo)
        with lock(state / "job.lock"), lock(common / "localize-workflow.lock"):
            return publish_receipt(report, args)
    path, raw = resolve_config(args)
    workflow = raw.get("workflow", {}) or {}
    if not isinstance(workflow, dict):
        raise ValueError("workflow must be a mapping.")
    identity = workflow.get("id") or path.parent.name + "-" + hashlib.sha256(str(path).encode()).hexdigest()[:10]
    if Path(identity).name != identity or identity in {".", ".."}:
        raise ValueError("workflow.id must be a simple directory name.")
    state = Path(args.state_dir or os.environ.get("LOCALIZE_STATE_DIR", ROOT / ".localize" / identity)).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True)
    for env_path in (state / ".env", path.parent / ".env", ROOT / ".env"):
        load_dotenv(env_path, override=False)
    from localize.plugins import load_plugins
    load_plugins(args.plugin)
    with ExitStack() as stack:
        stack.enter_context(lock(state / "job.lock"))
        report = state / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        report.mkdir(parents=True)
        (state / "latest-run.txt").write_text(str(report) + "\n")
        config, repo, input_folder, accepted, workflow = prepare_config(args, path, raw, state, report)
        identity_path = state / "workspace.json"
        workspace_identity = {"repo": str(repo), "input_folder": str(input_folder)}
        if identity_path.exists() and json.loads(identity_path.read_text()) != workspace_identity:
            raise ValueError("This state directory belongs to another checkout/input folder; choose its existing workspace or a distinct state directory.")
        check = args.check or boolean(config.get("dry_run")) or boolean(os.environ.get("LOCALIZE_DRY_RUN", ""))
        config["dry_run"] = check
        upstream = args.upstream or os.environ.get("UPSTREAM_REPO_NAME") or workflow.get("upstream_repository")
        fork = args.fork or os.environ.get("FORK_REPO_NAME")
        if not check:
            print(datetime.now().astimezone().strftime("[%Y-%m-%d %H:%M:%S] [INFO] Starting Git and Transifex validation"), flush=True)
        if not check and pending_pr_guard(upstream, required=args.publish):
            heartbeat()
            return []
        locked_repo = (repo / ".git").exists()
        if locked_repo:
            common = Path(git(repo, "rev-parse", "--git-common-dir"))
            stack.enter_context(lock(resolved_path(common, repo) / "localize-workflow.lock"))
            if not args.no_fetch and not check:
                from localize.workflow_recovery import reconcile_published
                reconcile_published(repo, state, upstream=upstream,
                                    target_branch=args.target_branch or os.environ.get("TARGET_BRANCH_FOR_PR") or workflow.get("default_branch"))
        base, target_branch = prepare_checkout(repo, workflow, no_fetch=args.no_fetch, upstream=upstream,
                                              target_branch=args.target_branch, fork=fork if args.publish else None)
        write_json(identity_path, workspace_identity)
        if not locked_repo:
            common = resolved_path(git(repo, "rev-parse", "--git-common-dir"), repo)
            stack.enter_context(lock(common / "localize-workflow.lock"))
        for target in sorted(set(accepted.values()) | {Path(config["run_safeguards"]["state_path"])}):
            stack.enter_context(lock(target.with_suffix(target.suffix + ".workflow.lock")))
        config_path = report / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
        os.environ["TRANSLATOR_CONFIG_FILE"] = str(config_path)
        os.environ["LOCALIZE_DRY_RUN"] = "true" if check else "false"
        selected, sources = discover(config, repo, input_folder)
        write_json(report / "plan.json", {"base": base, "files": list(selected), "model_calls": 0})
        from localize.app_config import validate_config
        issues = validate_config(config, config_base_dir=str(report))
        errors = [issue.message for issue in issues if issue.level == "error"]
        if errors:
            raise ValueError("; ".join(errors))
        from localize.run_safeguards import load_safeguards
        load_safeguards(config, str(config_path))
        if check:
            print(f"Preflight passed; no source pull or model calls. {report / 'plan.json'}")
            return []
        if args.publish:
            if not target_branch:
                raise ValueError("Publishing requires --target-branch or workflow.default_branch.")
            prepare_publication(repo, upstream, fork)
        baseline_path = state / "source-baseline.txt"
        if ROOT == Path("/app") and not baseline_path.exists() and upstream and target_branch:
            import re
            def component(value):
                return re.sub(r"[^A-Za-z0-9_.-]", "_", value)
            old_input = str(raw.get("input_folder", "default"))
            legacy = ROOT / "logs/state" / f"git-source-baseline-{component(upstream)}-{component(target_branch)}-{component(old_input)}.sha"
            if legacy.exists():
                baseline_path = legacy
        if not os.environ.get("TRANSLATION_DIFF_BASE") and baseline_path.exists():
            os.environ["TRANSLATION_DIFF_BASE"] = baseline_path.read_text().strip()
        base_ref = args.base_ref or workflow.get("source_baseline") or os.environ.get("TRANSLATION_DIFF_BASE")
        if base_ref:
            os.environ.setdefault("TRANSLATION_DIFF_BASE", base_ref)
        allowed = [(input_folder / filename).relative_to(repo).as_posix() for filename in selected]
        if config.get("pull_source_files_from_transifex"):
            allowed.extend(p.relative_to(repo).as_posix() for p in sources)
        receipt = {"status": "running", "repo": str(repo), "base": base, "profile": str(path), "state_dir": str(state),
                   "upstream": upstream, "fork": args.fork or os.environ.get("FORK_REPO_NAME"),
                   "target_branch": args.target_branch or target_branch,
                   "publication_branch": "translation-updates-" + report.name,
                   "accepted_state": {k: str(v) for k, v in accepted.items()}}
        try:
            pull_source(config, repo)
            selected, sources = discover(config, repo, input_folder)
            write_json(report / "plan.json", {"base": base, "files": list(selected), "source_prepared": True})
            allowed = [(input_folder / filename).relative_to(repo).as_posix() for filename in selected]
            if config.get("pull_source_files_from_transifex"):
                allowed.extend(p.relative_to(repo).as_posix() for p in sources)
            source_hashes = {p: digest(p) for p in sources}
            verify_scope(repo, allowed, source_hashes)
            seed_state(config_path, report, accepted, selected, repo, input_folder, base_ref, args.resume_memory)
            result = run_engine(config_path, report, list(selected))
            if result is False or getattr(result, "skipped_files", None):
                raise RuntimeError("Translation failed or skipped files; candidate and reports retained.")
            event("files_processed", count=getattr(result, "processed_files_count", 0))
            changed = verify_scope(repo, allowed, source_hashes)
            if changed:
                git(repo, "add", "--", *[f":(literal){p}" for p in changed])
            review_files = changed or allowed
            semantic_status = run_semantic(config_path, repo, input_folder, review_files, report)
            # Review can restore a value to HEAD, so restage the full owned
            # scope; a diff-vs-HEAD file list would miss that reversal.
            git(repo, "add", "--", *[f":(literal){p}" for p in allowed])
            changed = verify_scope(repo, allowed, source_hashes)
            patch_before_gate = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
            quality_status = run_quality(config_path, repo, input_folder, changed or allowed, report)
            patch = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary"])
            (report / "translations.patch").write_bytes(patch)
            quality = json.loads((report / "translation_quality_report.json").read_text())
            if semantic_status or quality_status or quality.get("blocking") is not False or quality.get("status_state") != "success":
                raise RuntimeError(f"Final quality checks failed; inspect {report}. Accepted state and publication withheld.")
            verify_scope(repo, allowed, source_hashes)
            if patch != patch_before_gate or git(repo, "rev-parse", "HEAD") != base or git(repo, "diff", "--name-only"):
                raise RuntimeError("Candidate changed during final validation; refusing acceptance.")
            reconcile_state(report, selected, input_folder)
            promote_state(report, accepted)
            receipt.update(status="accepted", changed_files=changed, config_sha256=digest(config_path),
                           quality_sha256=digest(report / "translation_quality_report.json"),
                           patch_sha256=digest(report / "translations.patch"))
            write_json(report / "receipt.json", receipt)
            if not changed:
                baseline_path.write_text(base + "\n")
            print(f"All quality checks passed. Reviewed patch: {report / 'translations.patch'}")
            if args.publish and changed:
                urls = publish_receipt(report, args)
            else:
                urls = []
                print(f"Publishing disabled or no changes. Publish later with --publish-run {report}")
            heartbeat()
            print("Translation update script finished successfully.", flush=True)
            return urls
        except Exception as exc:
            if receipt["status"] == "running":
                receipt.update(status="failed", error=str(exc))
                write_json(report / "receipt.json", receipt)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--config")
    selection.add_argument("--profile")
    parser.add_argument("--plugin", action="append", default=[], help="Load an external format adapter module.")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--locales", nargs="+", default=[])
    parser.add_argument("--base-ref")
    parser.add_argument("--resume-memory", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-fetch", action="store_true", help="Use an existing checkout without fetching; useful offline.")
    publication = parser.add_mutually_exclusive_group()
    publication.add_argument("--publish", action="store_true")
    publication.add_argument("--no-publish", action="store_false", dest="publish")
    parser.set_defaults(publish=False)
    parser.add_argument("--publish-run", type=Path, help="Publish a previously accepted run without model calls.")
    parser.add_argument("--upstream")
    parser.add_argument("--fork")
    parser.add_argument("--target-branch")
    parser.add_argument("--max-files-per-pr", type=int, default=os.environ.get("MAX_FILES_PER_PR", str(DEFAULT_MAX_FILES_PER_PR)))
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.max_files_per_pr <= 99:
            raise ValueError("--max-files-per-pr must be between 1 and 99.")
        if args.check and (args.publish or args.publish_run):
            raise ValueError("--check cannot publish.")
        run(args)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
