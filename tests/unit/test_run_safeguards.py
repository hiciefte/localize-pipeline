import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from localize.run_safeguards import (
    RequestBudgetExceeded,
    RunSafeguards,
    load_safeguards,
)


def guard(tmp_path, limit=7):
    return RunSafeguards(tmp_path / "safeguards.sqlite3", limit, 3, 5, 60)


def test_budget_is_atomic_and_survives_restarts(tmp_path):
    def request(_):
        try:
            guard(tmp_path).reserve()
            return True
        except RequestBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(request, range(30))) == 7
    with pytest.raises(RequestBudgetExceeded):
        guard(tmp_path).reserve()


def test_budget_expires_only_after_rolling_24_hours(tmp_path, monkeypatch):
    monkeypatch.setattr("localize.run_safeguards.time.time", lambda: 100000)
    guard(tmp_path, 1).reserve()
    monkeypatch.setattr("localize.run_safeguards.time.time", lambda: 100001)
    with pytest.raises(RequestBudgetExceeded):
        guard(tmp_path, 1).reserve()
    monkeypatch.setattr("localize.run_safeguards.time.time", lambda: 186401)
    guard(tmp_path, 1).reserve()


def test_sample_has_its_own_small_cap(tmp_path):
    safeguards = guard(tmp_path, 100)
    safeguards.sample_max_requests = 2
    with safeguards.sample_budget():
        safeguards.reserve()
        safeguards.reserve()
        with pytest.raises(RequestBudgetExceeded):
            safeguards.reserve()
    # The sample cap does not replace or reset the shared rolling budget.
    assert safeguards.request_count() == 2


def test_cache_is_exact_and_persistent(tmp_path):
    safeguards = guard(tmp_path)
    key = safeguards.fingerprint({"source": "Send {0}", "model": "first"})
    safeguards.accept(key, {"content": "Senden {0}"})
    assert guard(tmp_path).lookup(key) == {"content": "Senden {0}"}
    assert safeguards.lookup(safeguards.fingerprint(
        {"source": "Send {1}", "model": "first"}
    )) is None
    assert safeguards.lookup(safeguards.fingerprint(
        {"source": "Send {0}", "model": "second"}
    )) is None
    assert safeguards.request_count() == 0


def test_provisional_candidates_are_separate_from_acceptance_and_budget(tmp_path):
    safeguards = guard(tmp_path)
    safeguards.reserve("already charged")
    key = safeguards.fingerprint({"source": "Pay {0}", "target": "Alt", "policy": 1})
    safeguards.save_candidates(key, {"good": "Zahlen {0}"})
    restarted = guard(tmp_path)
    assert restarted.lookup_candidates(key) == {"good": "Zahlen {0}"}
    assert restarted.lookup(key) is None
    assert restarted.request_count() == 1
    assert restarted.lookup_candidates(restarted.fingerprint(
        {"source": "Pay {1}", "target": "Alt", "policy": 1},
    )) == {}
    # Revalidation can revoke an earlier candidate; do not merge stale values.
    restarted.save_candidates(key, {})
    assert safeguards.lookup_candidates(key) == {}


@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5])
def test_invalid_caps_fail_closed(tmp_path, value):
    with pytest.raises(ValueError):
        load_safeguards(
            {"run_safeguards": {"max_requests_per_24h": value}},
            str(tmp_path / "config.yaml"),
        )


@pytest.mark.asyncio
async def test_parameter_fallback_consumes_two_requests(tmp_path):
    from localize.openai_compat import create_chat_completion_with_fallback

    safeguards = guard(tmp_path, 1)
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        raise TypeError("unsupported parameter max_tokens")

    with pytest.raises(RequestBudgetExceeded):
        await create_chat_completion_with_fallback(
            create, model="custom", messages=[], completion_token_limit=10,
            before_request=safeguards.reserve,
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_requests_are_not_refunded(tmp_path):
    from localize.openai_compat import create_chat_completion_with_fallback

    safeguards = guard(tmp_path, 1)

    async def create(**kwargs):
        raise TimeoutError("unknown provider outcome")

    with pytest.raises(TimeoutError):
        await create_chat_completion_with_fallback(
            create, model="custom", messages=[], before_request=safeguards.reserve,
        )
    with pytest.raises(RequestBudgetExceeded):
        safeguards.reserve()


def test_sdk_retries_disabled_for_both_openai_routes(tmp_path):
    from localize.model_provider import create_model_provider
    safeguards = guard(tmp_path)
    with patch("localize.model_provider.AsyncOpenAI") as client:
        create_model_provider(
            provider_name="openai_compatible", api_key="sk-test", api_base_url=None,
            logger=logging.getLogger(__name__), safeguards=safeguards,
        )
        assert client.call_args.kwargs["max_retries"] == 0
    with patch("localize.model_provider.create_aisuite_provider") as create:
        create.return_value = SimpleNamespace()
        provider = create_model_provider(
            provider_name="aisuite", api_key="sk-test", api_base_url=None,
            logger=logging.getLogger(__name__), safeguards=safeguards,
            model_names=("gpt-4o-mini",),
            aisuite_provider_configs={"openai": {"max_retries": 99}},
        )
        assert create.call_args.kwargs["provider_configs"]["openai"]["max_retries"] == 0
        assert provider.safeguards is safeguards


@pytest.mark.asyncio
async def test_semantic_clean_result_reused_but_flagged_result_not_cached(tmp_path):
    from unittest.mock import AsyncMock
    from localize.model_provider import OpenAICompatibleProvider
    from localize.semantic_quality import TranslationChange
    from localize.translation_semantic_reviewer import _review_translation_change_batch

    create = AsyncMock(return_value=SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content='{"findings": []}'), finish_reason="stop"),
    ]))
    provider = OpenAICompatibleProvider(client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    provider.safeguards = guard(tmp_path)
    args = dict(
        provider=provider, model="gpt-4o-mini", target_language="German",
        changes=[TranslationChange("app_de.properties", "de", "key", "Hello", "", "Hallo")],
        style_rules=[], brand_glossary=[], reasoning_effort=None,
    )
    assert await _review_translation_change_batch(**args) == []
    assert await _review_translation_change_batch(**args) == []
    assert create.await_count == 1
    args["style_rules"] = ["Use a different register."]
    create.return_value.choices[0].message.content = (
        '{"findings":[{"file":"app_de.properties","key":"key",'
        '"severity":"error","reason":"wrong meaning"}]}'
    )
    assert await _review_translation_change_batch(**args)
    assert await _review_translation_change_batch(**args)
    assert create.await_count == 3
