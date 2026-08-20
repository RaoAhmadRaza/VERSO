"""The validated retry loop and its failure modes, on both providers (PLAN.md §7).

Fake clients stand in for the APIs, so the retry contract is testable without a key and
without spending anything. Not in the PLAN.md §10 list, but §7 describes real logic --
retry budget, truncation refusal, error surfacing -- that would otherwise ship untested.
"""

import json
from types import SimpleNamespace

import pytest

from app.models import Line
from app.structure import client as structure_client
from app.structure.client import StructuringError, structure
from app.structure.prompt import number_lines, system_prompt, user_message

LINES = [
    Line(no=1, text="JANE MORGAN", page=1, bbox=(0, 0, 0, 1)),
    Line(no=2, text="jane@example.com", page=1, bbox=(0, 1, 0, 2)),
]

VALID = {
    "language": "en",
    "contact": {
        "name": {"text": "JANE MORGAN", "source_lines": [1]},
        "emails": [{"text": "jane@example.com", "source_lines": [2]}],
        "source_lines": [1, 2],
    },
}


class FakeDeepSeek:
    """OpenAI-compatible shape: chat.completions.create -> choices[0].message.content."""

    def __init__(self, *responses: tuple[str, str]):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        text, finish_reason = self._responses.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text), finish_reason=finish_reason
                )
            ],
            usage=SimpleNamespace(
                completion_tokens=64000,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=61000),
            ),
        )


class FakeAnthropic:
    """Messages shape: messages.create -> content[].text, stop_reason."""

    def __init__(self, *responses: tuple[str, str]):
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        text, stop_reason = self._responses.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason,
            usage=SimpleNamespace(completion_tokens=64000, completion_tokens_details=None),
        )


def _run(fake, provider="deepseek"):
    return structure(LINES, client=fake, provider=provider)


# --------------------------------------------------------------- the retry contract


def test_valid_first_response_is_returned():
    fake = FakeDeepSeek((json.dumps(VALID), "stop"))
    assert _run(fake).contact.name.text == "JANE MORGAN"
    assert len(fake.requests) == 1


def test_markdown_fence_is_tolerated():
    fake = FakeDeepSeek((f"```json\n{json.dumps(VALID)}\n```", "stop"))
    assert _run(fake).contact.name.text == "JANE MORGAN"


def test_invalid_json_is_retried_with_the_error_appended():
    fake = FakeDeepSeek(("not json at all", "stop"), (json.dumps(VALID), "stop"))
    assert _run(fake).contact.name.text == "JANE MORGAN"
    assert len(fake.requests) == 2
    assert "failed validation" in fake.requests[1]["messages"][-1]["content"]


def test_schema_violation_is_retried():
    bad = json.dumps({"contact": {"emails": "not-a-list"}})
    fake = FakeDeepSeek((bad, "stop"), (json.dumps(VALID), "stop"))
    assert _run(fake).contact.name.text == "JANE MORGAN"


def test_empty_response_is_retried_not_accepted():
    """DeepSeek's json_object mode occasionally returns empty content."""
    fake = FakeDeepSeek(("", "stop"), (json.dumps(VALID), "stop"))
    assert _run(fake).contact.name.text == "JANE MORGAN"
    assert "empty response" in fake.requests[1]["messages"][-1]["content"]


def test_retry_budget_is_three_attempts_then_it_gives_up():
    fake = FakeDeepSeek(*[("nonsense", "stop")] * 3)
    with pytest.raises(StructuringError) as caught:
        _run(fake)
    assert len(fake.requests) == 3  # initial + MAX_RETRIES
    assert caught.value.code == "validation_failed"


def test_truncated_response_is_refused_not_accepted():
    """Silently losing the tail of a resume is the failure this project exists to prevent."""
    fake = FakeDeepSeek((json.dumps(VALID)[:40], "length"))
    with pytest.raises(StructuringError) as caught:
        _run(fake)
    assert caught.value.code == "response_truncated"


def test_truncation_error_says_where_the_budget_went():
    """Reasoning vs content decides whether to raise the ceiling or lower the effort."""
    fake = FakeDeepSeek((json.dumps(VALID)[:40], "length"))
    with pytest.raises(StructuringError) as caught:
        _run(fake)
    message = str(caught.value)
    assert "61000 on reasoning" in message and "3000 on content" in message


# ------------------------------------------------------------------ request shaping


def test_deepseek_request_uses_json_mode_and_greedy_decoding():
    fake = FakeDeepSeek((json.dumps(VALID), "stop"))
    _run(fake)
    request = fake.requests[0]
    assert request["model"] == "deepseek-v4-flash"
    assert request["temperature"] == 0
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["role"] == "system"
    # Reasoning tokens draw from max_tokens on this model; low effort keeps the JSON
    # inside the ceiling and cuts wall clock without changing the output.
    assert request["reasoning_effort"] == "low"
    assert request["max_tokens"] == 64000


def test_deepseek_is_the_default_provider(monkeypatch):
    """The provider with nothing configured.

    Asserted against `current_provider()`, not the module constant. `PROVIDER` is read once
    at import, so its value depends on whether something called `load_dotenv()` first -- it
    reads "gemini" from .env if `app.main` was imported earlier in the session and
    "deepseek" otherwise. That makes it a statement about test ordering, not about the
    default.
    """
    monkeypatch.delenv("VERSO_PROVIDER", raising=False)
    assert structure_client.current_provider() == "deepseek"


def test_unknown_provider_is_rejected_with_a_sentence():
    with pytest.raises(StructuringError) as caught:
        structure(LINES, client=object(), provider="gpt5")
    assert caught.value.code == "unknown_provider"


def test_missing_deepseek_key_gives_a_sentence(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(StructuringError) as caught:
        structure_client._deepseek_client()
    assert caught.value.code == "missing_api_key"
    assert "DEEPSEEK_API_KEY" in str(caught.value)


# ------------------------------------------------- the anthropic path stays testable


def test_anthropic_path_still_works_for_coverage_comparison():
    fake = FakeAnthropic((json.dumps(VALID), "end_turn"))
    assert _run(fake, provider="anthropic").contact.name.text == "JANE MORGAN"
    request = fake.requests[0]
    assert request["model"] == "claude-sonnet-4-6"
    assert request["temperature"] == 0
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_truncation_is_also_refused():
    fake = FakeAnthropic((json.dumps(VALID)[:40], "max_tokens"))
    with pytest.raises(StructuringError) as caught:
        _run(fake, provider="anthropic")
    assert caught.value.code == "response_truncated"


def test_missing_anthropic_key_gives_a_sentence(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(StructuringError) as caught:
        structure_client._anthropic_client()
    assert caught.value.code == "missing_api_key"


# ----------------------------------------------------------------------- the prompt


def test_system_prompt_is_byte_stable_so_prefix_caching_hits():
    assert system_prompt() == system_prompt()


def test_prompt_satisfies_deepseek_json_mode_requirements():
    """json_object mode wants the word 'json' plus an example of the desired shape."""
    system = system_prompt()
    assert "JSON" in system
    assert "EXAMPLE OF THE JSON SHAPE" in system
    json.loads(system.split("EXAMPLE OF THE JSON SHAPE (structure only -- never copy this content):\n")[1]
               .split("\n\nJSON SCHEMA")[0])  # the example must itself be valid JSON


def test_resume_text_is_fenced_as_untrusted_data():
    """Hard rule #5 / C18 -- resumes can contain injected instructions."""
    message = user_message(LINES)
    assert message.startswith("<resume_lines>")
    assert "</resume_lines>" in message
    assert "UNTRUSTED DATA" in system_prompt()


def test_prompt_carries_the_closed_skill_taxonomy():
    system = system_prompt()
    for category in ("cloud_platform", "domain_knowledge", "certification_skill", "other"):
        assert category in system


def test_numbered_lines_keep_blanks_so_numbering_matches_the_document():
    lines = LINES + [Line(no=3, text="", page=1, bbox=(0, 2, 0, 3))]
    assert number_lines(lines).splitlines()[2] == "3: "


def test_verso_model_overrides_only_the_selected_provider(monkeypatch):
    """Pinning a model for one provider must not leak into the others."""
    from app.structure.client import current_model

    monkeypatch.setenv("VERSO_PROVIDER", "gemini")
    monkeypatch.setenv("VERSO_MODEL", "gemini-3.5-flash-lite")
    assert current_model("gemini") == "gemini-3.5-flash-lite"
    assert current_model("deepseek") == "deepseek-v4-flash"
    assert current_model("anthropic") == "claude-sonnet-4-6"


def test_provider_is_read_at_call_time_not_at_import(monkeypatch):
    """A module-level constant is only correct if .env loaded before the import."""
    from app.structure.client import active_model, current_provider

    monkeypatch.setenv("VERSO_PROVIDER", "cerebras")
    monkeypatch.delenv("VERSO_MODEL", raising=False)
    assert current_provider() == "cerebras"
    assert active_model() == "cerebras/gpt-oss-120b"
