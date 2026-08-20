"""The single model call, with validated retry (PLAN.md §7).

Provider is selectable because this project has a built-in benchmark: `coverage_pct`
across the fixture corpus. Swapping models is only meaningful if the two can be measured
against each other, so both paths stay live and `VERSO_PROVIDER` picks one.

Schema-constrained decoding is deliberately not used on either provider:

* `claude-sonnet-4-6` is not among the models supporting `output_config.format`, and
  structured outputs reject recursive schemas anyway -- `Role.sub_roles: list[Role]` is
  recursive, and it is what makes C6 promotion chains representable.
* DeepSeek's `json_object` mode guarantees syntactic JSON, not schema conformance.

So both providers get the PLAN.md §7 flow: ask for JSON, validate with Pydantic, and on
failure re-send with the error appended. Invalid model output never propagates
(CLAUDE.md hard rule #3).
"""

import json
import os
import re
from functools import lru_cache

from pydantic import ValidationError

from app.models import Line, ParsedResumeLLM
from app.structure.prompt import system_prompt, user_message

PROVIDER = os.getenv("VERSO_PROVIDER", "deepseek").strip().lower()

# DeepSeek-V4-Flash: 284B MoE, 1M context, 384K max output, OpenAI-compatible.
DEEPSEEK_MODEL = os.getenv("VERSO_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
ANTHROPIC_MODEL = os.getenv("VERSO_MODEL", "claude-sonnet-4-6")

# Cerebras: OpenAI-compatible, with two differences from DeepSeek worth noting. The token
# ceiling is `max_completion_tokens`, and structured output is offered as `json_schema`.
# Our schema is recursive (`Role.sub_roles`, which C6 promotion chains need) and schema
# enforcement rejects recursion, so this uses `json_object` like the DeepSeek path.
CEREBRAS_MODEL = os.getenv("VERSO_MODEL", "gpt-oss-120b")
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"

# Gemini via the OpenAI-compatible endpoint. NOTE ON DATA: Google's pricing table states
# the FREE tier content is "used to improve our products"; the paid tier is not. Resumes
# carry names, phone numbers and addresses, so use the free tier for `fixtures/synthetic/`
# (invented filler) and pay the tier before sending anyone's real resume.
GEMINI_MODEL = os.getenv("VERSO_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# Cerebras: OpenAI-compatible, but note two differences from DeepSeek -- the token ceiling
# is `max_completion_tokens`, and structured outputs are offered as `json_schema`. The
# schema here is recursive (`Role.sub_roles`, which C6 promotion chains need), and schema
# enforcement rejects recursion, so this uses `json_object` like the DeepSeek path.
CEREBRAS_MODEL = os.getenv("VERSO_MODEL", "gpt-oss-120b")
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"

# Gemini via the OpenAI-compatible endpoint. NOTE ON DATA: Google's pricing table states
# the FREE tier content is "used to improve our products"; the paid tier is not. Resumes
# carry names, phone numbers and addresses, so use the free tier for `fixtures/synthetic/`
# (invented filler) and pay the tier before sending anyone's real resume.
GEMINI_MODEL = os.getenv("VERSO_MODEL", "gemini-2.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# V4-Flash supports 384K max output, and reasoning tokens draw from this same budget.
# 16000 was an Anthropic-era figure: here deliberation consumes most of it before the JSON
# finishes. Measured on this corpus, one resume used 21,400 completion tokens at default
# effort -- 19,034 of them reasoning. We only pay for tokens generated, so a high ceiling
# costs nothing.
MAX_TOKENS = int(os.getenv("VERSO_MAX_TOKENS", "64000"))

# Classifying pre-extracted lines into a schema is mechanical, not a reasoning task.
# Measured: "low" and "high" produce the same JSON (2,535 vs 2,366 tokens of content) while
# "high" spends 3x the reasoning and 2.5x the wall clock. Raise it only if coverage_pct says
# parse quality needs it -- measure, don't assume.
REASONING_EFFORT = os.getenv("VERSO_REASONING_EFFORT", "low")
# PLAN.md pins temperature 0 so `coverage_pct` is reproducible run to run, which a
# regression metric requires. Note DeepSeek recommend 1.0 for V4-Flash; if greedy decoding
# degrades the parse, raising this trades reproducibility for quality. Measure, don't guess.
TEMPERATURE = float(os.getenv("VERSO_TEMPERATURE", "0"))
MAX_RETRIES = 2  # PLAN.md §7
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class StructuringError(RuntimeError):
    """Structuring could not complete. `code` lets the UI say something specific."""

    def __init__(self, message: str, code: str = "structuring_failed"):
        super().__init__(message)
        self.code = code


def _strip_fence(text: str) -> str:
    """The prompt forbids fences; strip one anyway rather than fail on formatting."""
    return FENCE.sub("", text.strip())


# ------------------------------------------------------------------------- providers


def _deepseek_client():
    """OpenAI-compatible client pointed at DeepSeek."""
    from openai import OpenAI

    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise StructuringError(
            "No DeepSeek API key is configured. Put DEEPSEEK_API_KEY in a .env file in "
            "the project root, then try again.",
            code="missing_api_key",
        )
    return OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)


def _cerebras_client():
    from openai import OpenAI

    key = os.getenv("CEREBRAS_API_KEY")
    if not key:
        raise StructuringError(
            "No Cerebras API key is configured. Put CEREBRAS_API_KEY in a .env file in "
            "the project root, then try again.",
            code="missing_api_key",
        )
    return OpenAI(api_key=key, base_url=CEREBRAS_BASE_URL)


def _cerebras_client():
    from openai import OpenAI

    key = os.getenv("CEREBRAS_API_KEY")
    if not key:
        raise StructuringError(
            "No Cerebras API key is configured. Put CEREBRAS_API_KEY in a .env file in "
            "the project root, then try again.",
            code="missing_api_key",
        )
    return OpenAI(api_key=key, base_url=CEREBRAS_BASE_URL)


def _gemini_client():
    from openai import OpenAI

    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise StructuringError(
            "No Gemini API key is configured. Put GEMINI_API_KEY in a .env file in the "
            "project root, then try again.",
            code="missing_api_key",
        )
    # The free tier caps requests per minute, and a batch run bursts past it. The SDK
    # retries 429 with exponential backoff, so raising the retry count absorbs the burst
    # instead of dropping files from the corpus.
    return OpenAI(api_key=key, base_url=GEMINI_BASE_URL, max_retries=8)


def _anthropic_client():
    """The SDK constructs happily with no credential and only raises at request time."""
    import anthropic

    client = anthropic.Anthropic()
    if not (client.api_key or client.auth_token):
        raise StructuringError(
            "No Anthropic API key is configured. Put ANTHROPIC_API_KEY in a .env file in "
            "the project root, then try again.",
            code="missing_api_key",
        )
    return client


def _budget_note(usage, limit: int) -> str:
    """Where the output budget went. Returned, not stored, so it stays thread-safe."""
    if usage is None:
        return ""
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None)
    total = getattr(usage, "completion_tokens", None)
    if total is None:
        return ""
    spent = f" Used {total} completion tokens"
    if reasoning is not None:
        spent += f" ({reasoning} on reasoning, {total - reasoning} on content)"
    return spent + f" against a {limit} limit."


def _call_deepseek(client, system: str, messages: list[dict]) -> tuple[str, bool, str]:
    """Returns (text, truncated, budget_note). Prefix caching is automatic."""
    import openai

    try:
        response = client.chat.completions.create(
            model=current_model("deepseek"),
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            reasoning_effort=REASONING_EFFORT,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, *messages],
        )
    except openai.AuthenticationError as exc:
        raise StructuringError(
            "The DeepSeek API key was rejected. Check DEEPSEEK_API_KEY in .env.",
            code="invalid_api_key",
        ) from exc
    except openai.RateLimitError as exc:
        raise StructuringError(
            "Rate limited by the DeepSeek API. Wait a moment and try again.",
            code="rate_limited",
        ) from exc
    except openai.APIConnectionError as exc:
        raise StructuringError(
            "Could not reach the DeepSeek API. Check your network connection.",
            code="connection_failed",
        ) from exc
    except openai.APIStatusError as exc:
        raise StructuringError(
            f"The DeepSeek API returned an error ({exc.status_code}).", code="api_error"
        ) from exc

    choice = response.choices[0]
    truncated = choice.finish_reason == "length"
    note = _budget_note(getattr(response, "usage", None), MAX_TOKENS) if truncated else ""
    return (choice.message.content or ""), truncated, note


def _call_cerebras(client, system: str, messages: list[dict]) -> tuple[str, bool, str]:
    """Returns (text, truncated, budget_note)."""
    import openai

    try:
        response = client.chat.completions.create(
            model=current_model("cerebras"),
            max_completion_tokens=MAX_TOKENS,  # not `max_tokens` on this provider
            temperature=TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, *messages],
        )
    except openai.AuthenticationError as exc:
        raise StructuringError(
            "The Cerebras API key was rejected. Check CEREBRAS_API_KEY in .env.",
            code="invalid_api_key",
        ) from exc
    except openai.RateLimitError as exc:
        raise StructuringError(
            "Rate limited by the Cerebras API. The free tier caps tokens per day.",
            code="rate_limited",
        ) from exc
    except openai.APIConnectionError as exc:
        raise StructuringError(
            "Could not reach the Cerebras API. Check your network connection.",
            code="connection_failed",
        ) from exc
    except openai.APIStatusError as exc:
        raise StructuringError(
            f"The Cerebras API returned an error ({exc.status_code}).", code="api_error"
        ) from exc

    choice = response.choices[0]
    truncated = choice.finish_reason == "length"
    note = _budget_note(getattr(response, "usage", None), MAX_TOKENS) if truncated else ""
    return (choice.message.content or ""), truncated, note


def _call_cerebras(client, system: str, messages: list[dict]) -> tuple[str, bool, str]:
    """Returns (text, truncated, budget_note)."""
    import openai

    try:
        response = client.chat.completions.create(
            model=current_model("cerebras"),
            max_completion_tokens=MAX_TOKENS,  # not `max_tokens` on this provider
            temperature=TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, *messages],
        )
    except openai.AuthenticationError as exc:
        raise StructuringError(
            "The Cerebras API key was rejected. Check CEREBRAS_API_KEY in .env.",
            code="invalid_api_key",
        ) from exc
    except openai.RateLimitError as exc:
        raise StructuringError(
            "Rate limited by the Cerebras API. The free tier caps tokens per day.",
            code="rate_limited",
        ) from exc
    except openai.APIConnectionError as exc:
        raise StructuringError(
            "Could not reach the Cerebras API. Check your network connection.",
            code="connection_failed",
        ) from exc
    except openai.APIStatusError as exc:
        raise StructuringError(
            f"The Cerebras API returned an error ({exc.status_code}): {exc.message}",
            code="api_error",
        ) from exc

    choice = response.choices[0]
    truncated = choice.finish_reason == "length"
    note = _budget_note(getattr(response, "usage", None), MAX_TOKENS) if truncated else ""
    return (choice.message.content or ""), truncated, note


def _call_gemini(client, system: str, messages: list[dict]) -> tuple[str, bool, str]:
    """Returns (text, truncated, budget_note)."""
    import openai

    try:
        response = client.chat.completions.create(
            model=current_model("gemini"),
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, *messages],
        )
    except openai.AuthenticationError as exc:
        raise StructuringError(
            "The Gemini API key was rejected. Check GEMINI_API_KEY in .env.",
            code="invalid_api_key",
        ) from exc
    except openai.RateLimitError as exc:
        raise StructuringError(
            "Rate limited by the Gemini API. The free tier caps requests per minute "
            "and per day.",
            code="rate_limited",
        ) from exc
    except openai.APIConnectionError as exc:
        raise StructuringError(
            "Could not reach the Gemini API. Check your network connection.",
            code="connection_failed",
        ) from exc
    except openai.APIStatusError as exc:
        raise StructuringError(
            f"The Gemini API returned an error ({exc.status_code}): {exc.message}",
            code="api_error",
        ) from exc

    choice = response.choices[0]
    truncated = choice.finish_reason == "length"
    note = _budget_note(getattr(response, "usage", None), MAX_TOKENS) if truncated else ""
    return (choice.message.content or ""), truncated, note


def _call_anthropic(client, system: str, messages: list[dict]) -> tuple[str, bool, str]:
    import anthropic

    try:
        response = client.messages.create(
            model=current_model("anthropic"),
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=messages,
        )
    except anthropic.AuthenticationError as exc:
        raise StructuringError(
            "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.",
            code="invalid_api_key",
        ) from exc
    except anthropic.RateLimitError as exc:
        raise StructuringError(
            "Rate limited by the Anthropic API. Wait a moment and try again.",
            code="rate_limited",
        ) from exc
    except anthropic.APIConnectionError as exc:
        raise StructuringError(
            "Could not reach the Anthropic API. Check your network connection.",
            code="connection_failed",
        ) from exc
    except anthropic.APIStatusError as exc:
        raise StructuringError(
            f"The Anthropic API returned an error ({exc.status_code}).", code="api_error"
        ) from exc

    text = "".join(block.text for block in response.content if block.type == "text")
    truncated = response.stop_reason == "max_tokens"
    note = _budget_note(getattr(response, "usage", None), MAX_TOKENS) if truncated else ""
    return text, truncated, note


PROVIDERS = {
    "deepseek": (_deepseek_client, _call_deepseek),
    "cerebras": (_cerebras_client, _call_cerebras),
    "gemini": (_gemini_client, _call_gemini),
    "anthropic": (_anthropic_client, _call_anthropic),
}


def current_provider() -> str:
    """Read at call time. A module-level constant is only correct if .env loaded first."""
    return os.getenv("VERSO_PROVIDER", "deepseek").strip().lower()


def _resolve(provider: str | None):
    name = (provider or current_provider()).strip().lower()
    if name not in PROVIDERS:
        raise StructuringError(
            f"Unknown VERSO_PROVIDER '{name}'. Use one of: {', '.join(PROVIDERS)}.",
            code="unknown_provider",
        )
    return name, PROVIDERS[name]


@lru_cache(maxsize=8)
def _shared_client(provider: str):
    """One client per provider, reused across worker threads.

    Both SDKs are thread-safe; building a client per call would spin up a fresh connection
    pool each time. `lru_cache` does not cache exceptions, so the missing-key path still
    raises on every call rather than being memoised into silence.
    """
    return PROVIDERS[provider][0]()


# ----------------------------------------------------------------------------- driver


def structure(
    lines: list[Line], client=None, provider: str | None = None
) -> ParsedResumeLLM:
    """Numbered lines -> a validated `ParsedResumeLLM`.

    Raises `StructuringError` if the model cannot produce valid output. PLAN.md says to
    return None and let the caller decide; raising carries the reason, which the caller
    needs in order to say anything useful to the user.
    """
    name, (_, call) = _resolve(provider)
    client = client if client is not None else _shared_client(name)
    system = system_prompt()
    messages: list[dict] = [{"role": "user", "content": user_message(lines)}]
    last_error = ""

    for attempt in range(MAX_RETRIES + 1):
        raw, truncated, budget = call(client, system, messages)

        if truncated:
            # Never accept a truncated parse. Silently losing the tail of a resume is
            # precisely the failure mode this project exists to prevent. The budget note
            # says whether reasoning or content ate the ceiling, which decides whether to
            # raise VERSO_MAX_TOKENS or lower VERSO_REASONING_EFFORT.
            raise StructuringError(
                "The model's response was cut off by the token limit, so the parse would "
                "be incomplete." + budget,
                code="response_truncated",
            )

        try:
            return ParsedResumeLLM.model_validate(json.loads(_strip_fence(raw)))
        except (json.JSONDecodeError, ValidationError) as exc:
            # DeepSeek's json_object mode occasionally returns empty content; that lands
            # here as a JSONDecodeError and is retried like any other invalid response.
            last_error = "The model returned an empty response." if not raw.strip() else str(exc)
            if attempt == MAX_RETRIES:
                break
            messages += [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "That response failed validation with the following error. "
                        "Return corrected JSON only, matching the schema exactly.\n\n"
                        f"{last_error}"
                    ),
                },
            ]

    raise StructuringError(
        f"The model could not produce a valid parse after {MAX_RETRIES + 1} attempts. "
        f"Last error: {last_error[:400]}",
        code="validation_failed",
    )


DEFAULT_MODELS = {
    "deepseek": "deepseek-v4-flash",
    "cerebras": "gpt-oss-120b",
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-6",
}


def current_model(provider: str | None = None) -> str:
    """The model id in force, read at call time like `current_provider`.

    `VERSO_MODEL` overrides only the provider actually selected. Applying it to every
    provider would send a Gemini model id to DeepSeek the moment someone pins one, which
    is exactly what happens when several providers are configured at once.
    """
    name = provider or current_provider()
    override = os.getenv("VERSO_MODEL")
    if override and name == current_provider():
        return override
    return DEFAULT_MODELS.get(name, DEFAULT_MODELS["deepseek"])


def active_model() -> str:
    """`provider/model` for the running process, for display in the UI and CLI."""
    return f"{current_provider()}/{current_model()}"
