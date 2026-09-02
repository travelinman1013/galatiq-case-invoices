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
DEFAULT_LOCAL_BASE_URL = "http://localhost:1235/v1"
DEFAULT_LOCAL_MODEL = "qwen3.8-27b-mlx"

# Token accounting for the run header/footer. Reset per CLI run.
USAGE: dict[str, int] = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def provider() -> str:
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    return "xai" if os.getenv("XAI_API_KEY") else "none"


def describe() -> str:
    p = provider()
    if p == "xai":
        return f"xai · {os.getenv('XAI_MODEL', DEFAULT_XAI_MODEL)}"
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

    common = {"temperature": 0, "timeout": 90, "max_retries": 2}
    if p == "xai":
        key = os.getenv("XAI_API_KEY")
        if not key:
            raise RuntimeError("LLM_PROVIDER=xai but XAI_API_KEY is not set")
        return ChatOpenAI(base_url=XAI_BASE_URL, api_key=key, model=os.getenv("XAI_MODEL", DEFAULT_XAI_MODEL), **common)
    if p == "local":
        return ChatOpenAI(
            base_url=os.getenv("LOCAL_BASE_URL", DEFAULT_LOCAL_BASE_URL),
            api_key=os.getenv("LOCAL_API_KEY", "not-needed"),
            model=os.getenv("LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
            **common,
        )
    raise RuntimeError(f"Unknown LLM_PROVIDER={p!r} (use xai, local, or none)")


def reset_usage() -> None:
    USAGE.update(calls=0, input_tokens=0, output_tokens=0)


def record_usage(message: Any) -> None:
    USAGE["calls"] += 1
    meta = getattr(message, "usage_metadata", None) or {}
    USAGE["input_tokens"] += int(meta.get("input_tokens", 0) or 0)
    USAGE["output_tokens"] += int(meta.get("output_tokens", 0) or 0)


def structured[T: BaseModel](llm: Any, schema: type[T], messages: list) -> T:
    """Call the model and get a validated `schema` back.

    Tries the provider's native JSON-schema mode first; falls back to tool-calling-based
    structured output for servers that reject strict schemas.
    """
    last_error: Exception | None = None
    for method in ("json_schema", "function_calling"):
        try:
            runnable = llm.with_structured_output(schema, method=method, include_raw=True)
            out = runnable.invoke(messages)
            if out.get("raw") is not None:
                record_usage(out["raw"])
            if out.get("parsing_error") or out.get("parsed") is None:
                raise ValueError(f"structured output could not be parsed: {out.get('parsing_error')}")
            return out["parsed"]
        except Exception as exc:  # noqa: BLE001 — fall through to the next method, then re-raise
            last_error = exc
    raise RuntimeError(f"model failed to return {schema.__name__}: {last_error}") from last_error
