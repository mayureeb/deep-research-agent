"""Thin Anthropic SDK wrapper. Three helpers: chat_text, chat_json, chat_with_tools."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from anthropic import Anthropic
from pydantic import BaseModel, ValidationError


_client: Anthropic | None = None


def client(api_key: str) -> Anthropic:
    """Get the cached Anthropic client.

    The api_key is honored only on the FIRST call (singleton). Reset _client
    to None to swap keys mid-process.
    """
    global _client
    if _client is None:
        _client = Anthropic(api_key=api_key, max_retries=6)
    return _client


# USD per 1M tokens, (input, output). Unknown models bill at 0.0 with a stderr warning.
PRICE_USD_PER_M_TOKENS: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":   (1.0,  5.0),
    "claude-sonnet-4-6":  (3.0, 15.0),
    "claude-opus-4-7":    (15.0, 75.0),
}


@dataclass
class LLMCall:
    """One recorded LLM call."""
    stage: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


_call_log: list[LLMCall] = []
_call_log_lock = threading.Lock()
_current_stage: str = "unknown"
_warned_unknown_models: set[str] = set()


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    """Estimate USD cost for one call. 0.0 if model not in PRICE table."""
    if model not in PRICE_USD_PER_M_TOKENS:
        if model not in _warned_unknown_models:
            _warned_unknown_models.add(model)
            import sys
            print(
                f"[llm.py cost] unknown model {model!r} — cost will be 0.0; "
                f"add to PRICE_USD_PER_M_TOKENS to fix",
                file=sys.stderr,
            )
        return 0.0
    in_price, out_price = PRICE_USD_PER_M_TOKENS[model]
    return (in_tokens * in_price + out_tokens * out_price) / 1_000_000.0


def _record_call(model: str, response: Any) -> None:
    """Pull usage out of an Anthropic response and append an LLMCall."""
    in_tokens = 0
    out_tokens = 0
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    except Exception:
        pass
    cost = _estimate_cost(model, in_tokens, out_tokens)
    call = LLMCall(
        stage=_current_stage, model=model,
        input_tokens=in_tokens, output_tokens=out_tokens, cost_usd=cost,
    )
    with _call_log_lock:
        _call_log.append(call)


@contextmanager
def track_stage(stage: str) -> Iterator[None]:
    """Tag every LLM call inside the block with `stage`. Reentrant — nested
    blocks restore the outer stage on exit."""
    global _current_stage
    prev = _current_stage
    _current_stage = stage
    try:
        yield
    finally:
        _current_stage = prev


def reset_call_log() -> None:
    """Clear the call log."""
    with _call_log_lock:
        _call_log.clear()


def get_call_log() -> list[LLMCall]:
    """Return a shallow copy of the call log."""
    with _call_log_lock:
        return list(_call_log)


@dataclass
class StageRollup:
    """One stage's aggregate across the call log."""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def aggregate_calls_by_stage() -> dict[str, StageRollup]:
    """Roll up the call log into per-stage totals."""
    out: dict[str, StageRollup] = {}
    for call in get_call_log():
        rollup = out.setdefault(call.stage, StageRollup())
        rollup.calls += 1
        rollup.input_tokens += call.input_tokens
        rollup.output_tokens += call.output_tokens
        rollup.cost_usd += call.cost_usd
    return out


def _chat_raw(
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
) -> tuple[str, str]:
    """One LLM call returning (first-text-block, stop_reason)."""
    resp = client(api_key).messages.create(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )
    _record_call(model, resp)
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    stop_reason = getattr(resp, "stop_reason", "") or ""
    return text, stop_reason


def chat_text(
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int = 16384,
) -> str:
    """One LLM call returning the first text block as a string."""
    text, _ = _chat_raw(api_key, model, system, messages, max_tokens)
    return text


def chat_json(
    api_key: str,
    model: str,
    system: str,
    user: str,
    schema: type[BaseModel],
    max_retries: int = 2,
    max_tokens: int = 16384,
) -> BaseModel:
    """Get structured output, validating against `schema`. Reprompts on parse failure
    with the actual ValidationError so the model can self-correct.

    Raises:
        RuntimeError: After exhausting retries, with the final ValidationError.
    """
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    base_system = (
        f"{system}\n\n"
        "Respond with a single JSON object that matches this schema exactly. "
        "Output ONLY the JSON object — no prose, no markdown fences.\n\n"
        f"Schema:\n{schema_json}"
    )
    messages: list[dict] = [{"role": "user", "content": user}]
    last_err = ""

    for attempt in range(max_retries + 1):
        text, stop_reason = _chat_raw(
            api_key, model, base_system, messages, max_tokens
        )
        # Truncation is mechanically incomplete JSON; further retries at the
        # same cap will fail identically. Surface a diagnostic instead.
        if stop_reason == "max_tokens":
            raise RuntimeError(
                f"chat_json: response truncated by max_tokens={max_tokens} "
                f"on attempt {attempt + 1}; output JSON is incomplete and "
                f"cannot be parsed. Increase max_tokens for this call."
            )
        text = _strip_fences(text)
        try:
            return schema.model_validate_json(text)
        except (ValidationError, json.JSONDecodeError) as e:
            last_err = str(e)
            if attempt == max_retries:
                break
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        f"That output failed validation:\n{last_err}\n\n"
                        "Re-emit the JSON, fixing the validation errors. JSON only."
                    ),
                },
            ]
    raise RuntimeError(f"chat_json failed after {max_retries + 1} attempts: {last_err}")


def chat_with_tools(
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int = 16384,
    temperature: float | None = None,
) -> Any:
    """One turn of a tool-using loop. Returns the raw response object.

    The caller drives the loop: appending response.content to messages,
    dispatching tool_use blocks, building tool_result blocks, and re-calling
    until stop_reason == "end_turn".
    """
    kwargs: dict[str, Any] = dict(
        model=model, system=system, messages=messages, tools=tools,
        max_tokens=max_tokens,
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client(api_key).messages.create(**kwargs)
    _record_call(model, resp)
    return resp


def chat_with_tools_parallel(
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    k: int,
    temperature: float,
    max_tokens: int = 16384,
) -> list[Any]:
    """Run k concurrent tool-using turns at `temperature`. Returns k raw
    anthropic.types.Message objects in submission order."""
    from concurrent.futures import ThreadPoolExecutor

    if k <= 0:
        return []

    def _one_call() -> Any:
        return chat_with_tools(
            api_key, model, system, messages, tools,
            max_tokens=max_tokens, temperature=temperature,
        )

    with ThreadPoolExecutor(max_workers=k) as pool:
        futures = [pool.submit(_one_call) for _ in range(k)]
        return [f.result() for f in futures]


def _strip_fences(s: str) -> str:
    """Remove ```json ... ``` markdown fences if the model added them."""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()
