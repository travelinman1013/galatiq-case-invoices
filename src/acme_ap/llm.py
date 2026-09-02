"""One model factory. The provider is an env var; everything speaks the OpenAI-compatible API.

LLM_PROVIDER=xai    Grok at https://api.x.ai/v1            (XAI_API_KEY, XAI_MODEL)
LLM_PROVIDER=local  any OpenAI-compatible server, e.g. LM Studio (LOCAL_BASE_URL, LOCAL_MODEL)
LLM_PROVIDER=none   no model: structured files still flow, unstructured ones go to a human
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_XAI_MODEL = "grok-4.5"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_LOCAL_BASE_URL = "http://localhost:1235/v1"
DEFAULT_LOCAL_MODEL = "qwen3.8-27b-mlx"

# Token accounting for the run header/footer. Reset per CLI run.
USAGE: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def provider() -> str:
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("XAI_API_KEY"):
        return "xai"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "none"


def describe() -> str:
    p = provider()
    if p == "xai":
        return f"xai · {os.getenv('XAI_MODEL', DEFAULT_XAI_MODEL)}"
    if p == "openai":
        return f"openai · {os.getenv('OPENAI_MODEL', DEFAULT_OPENAI_MODEL)}"
    if p == "local":
        return (
            f"local · {os.getenv('LOCAL_MODEL', DEFAULT_LOCAL_MODEL)} "
            f"@ {os.getenv('LOCAL_BASE_URL', DEFAULT_LOCAL_BASE_URL)}"
        )
    return "none · deterministic only (unstructured invoices go to the human queue)"


def get_llm() -> Any | None:
    """A configured chat model, or None when running without one."""
    p = provider()
    if p == "none":
        return None
    from langchain_openai import ChatOpenAI

    common: dict[str, Any] = {"timeout": 90, "max_retries": 2}
    if p == "xai":
        key = os.getenv("XAI_API_KEY")
        if not key:
            raise RuntimeError("LLM_PROVIDER=xai but XAI_API_KEY is not set")
        model = os.getenv("XAI_MODEL", DEFAULT_XAI_MODEL)
        return ChatOpenAI(base_url=XAI_BASE_URL, api_key=key, model=model, temperature=0, **common)
    if p == "openai":
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        # The gpt-5 family rejects a temperature parameter and takes a reasoning effort instead.
        if model.startswith(("gpt-5", "o")):
            extra: dict[str, Any] = {"reasoning_effort": os.getenv("OPENAI_REASONING_EFFORT", "low")}
        else:
            extra = {"temperature": 0}
        return ChatOpenAI(api_key=key, model=model, **extra, **common)
    if p == "local":
        return ChatOpenAI(
            base_url=os.getenv("LOCAL_BASE_URL", DEFAULT_LOCAL_BASE_URL),
            api_key=os.getenv("LOCAL_API_KEY", "not-needed"),
            model=os.getenv("LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
            temperature=0,
            **common,
        )
    raise RuntimeError(f"Unknown LLM_PROVIDER={p!r} (use xai, openai, local, or none)")


def reset_usage() -> None:
    USAGE.update(calls=0, input_tokens=0, output_tokens=0)


def record_usage(message: Any) -> None:
    USAGE["calls"] += 1
    meta = getattr(message, "usage_metadata", None) or {}
    USAGE["input_tokens"] += int(meta.get("input_tokens", 0) or 0)
    USAGE["output_tokens"] += int(meta.get("output_tokens", 0) or 0)


def structured[T: BaseModel](llm: Any, schema: type[T], messages: list) -> T:
    """Call the model and get a validated `schema` back.

    First the provider's native JSON-schema mode (xAI, OpenAI). If that yields nothing usable —
    some local servers route constrained JSON into a "reasoning" field, or reject strict schemas —
    fall back to a forced tool call, which every OpenAI-compatible server handles the same way.
    """
    errors: list[str] = []
    try:
        out = llm.with_structured_output(schema, method="json_schema", include_raw=True).invoke(messages)
        if out.get("raw") is not None:
            record_usage(out["raw"])
        if out.get("parsed") is not None and not out.get("parsing_error"):
            return out["parsed"]
        errors.append(f"json_schema: {out.get('parsing_error') or 'empty response'}")
    except Exception as exc:  # noqa: BLE001 — try the next strategy
        errors.append(f"json_schema: {exc}")

    try:
        message = llm.bind_tools([schema], tool_choice="required").invoke(messages)
        record_usage(message)
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") == schema.__name__:
                return schema.model_validate(call.get("args") or {})
        errors.append("forced_tool: model returned no matching tool call")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"forced_tool: {exc}")
    raise RuntimeError(f"model failed to return {schema.__name__}: " + " | ".join(errors))
