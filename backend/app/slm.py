"""Local small-language-model (SLM) fallback, used only when no
ANTHROPIC_API_KEY is configured -- e.g. a free-tier deploy with no budget for
API calls, or the Anthropic API being unreachable. Keeps the app answering
(in a clearly-labelled, degraded form) instead of going fully down.

Design (see docs/ARCHITECTURE.md "Local fallback mode" for the full writeup):

- The model's ONLY job is PHRASING: turning facts already produced by the
  existing deterministic engines/retrieval (app/rules.py, app/retrieval.py)
  into a short natural-language reply. It never chooses tools, calls
  anything, or reasons about policy itself -- a ~135M-parameter model is not
  reliable at multi-step tool-use, and the trust requirements this system is
  built around matter more than a flashier degraded mode. Tool selection is
  done by plain Python in app/fallback_agent.py.
- Lazy singleton: the GGUF model loads once (on first use) and stays resident
  for the life of the process/instance. On a persistent host (Render, Docker)
  the file is baked into the image at build time. On Vercel's read-only
  serverless bundle, `_ensure_model_file` downloads it into /tmp on first use
  instead -- a cold instance pays that cost once, then reuses it for every
  warm invocation, same lazy-singleton pattern either way.
- Bounded latency, not bounded quality: measured on a Render-free-equivalent
  0.1 vCPU container, this model generates at roughly 0.5-0.6 tokens/sec --
  slow enough that an unbounded generation is a real availability risk (a
  bigger model was 3-4x slower still, which is why this one was chosen over
  it). `phrase_answer` enforces a hard wall-clock deadline via llama.cpp's
  `stopping_criteria` hook, so a slow host gets a shorter (or, worst case,
  template) answer within a bounded time instead of an open-ended hang.
- Fails closed to a deterministic template, never to a crash: if
  llama-cpp-python isn't installed, the model file is missing/can't be
  downloaded, generation raises, or the model is busy past its wait budget,
  `phrase_answer` degrades to listing the facts as plain bullet points. The
  fallback must never become a new way to break the app.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.request

from . import config

logger = logging.getLogger(__name__)

_load_lock = threading.Lock()
_generate_lock = threading.Lock()  # llama.cpp contexts aren't safe for concurrent calls
_model = None
_load_attempted = False


def _ensure_model_file() -> bool:
    """Make sure the GGUF file exists at config.SLM_MODEL_PATH, downloading it
    on first use if not (e.g. Vercel's read-only bundle: cached into /tmp,
    which persists across warm invocations of the same instance). On
    Docker/Render the file is already baked in at build time, so this is a
    no-op there. Returns False (never raises) if neither is possible, so the
    caller degrades to the template path instead of failing."""
    if config.SLM_MODEL_PATH.exists():
        return True
    try:
        config.SLM_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = config.SLM_MODEL_PATH.with_suffix(".part")
        logger.info("Downloading local SLM model to %s ...", config.SLM_MODEL_PATH)
        urllib.request.urlretrieve(config.SLM_MODEL_URL, tmp)
        tmp.rename(config.SLM_MODEL_PATH)
        return True
    except Exception:
        logger.exception(
            "Could not fetch the SLM model (%s); SLM fallback will use plain templates.",
            config.SLM_MODEL_URL,
        )
        return False

SYSTEM_INSTRUCTION = (
    "You are ParcelPilot support. Use ONLY the FACTS below, add nothing. "
    "Answer in 1-3 short sentences, keeping numbers/dates exact."
)

_STOP_SEQUENCES = ["<|im_end|>", "<|im_start|>"]


def _prompt(question: str, facts: list[str]) -> str:
    fact_block = "\n".join(f"- {f}" for f in facts) if facts else "(no matching facts found)"
    return (
        f"<|im_start|>system\n{SYSTEM_INSTRUCTION}<|im_end|>\n"
        f"<|im_start|>user\nFACTS:\n{fact_block}\n\nQUESTION: {question.strip()}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )


def _load() -> "object | None":
    """Load the GGUF model once. Returns the Llama instance, or None if unavailable."""
    global _model, _load_attempted
    if _model is not None or _load_attempted:
        return _model
    with _load_lock:
        if _model is not None or _load_attempted:
            return _model
        _load_attempted = True
        try:
            from llama_cpp import Llama  # optional dependency, imported lazily
        except ImportError:
            logger.warning("llama-cpp-python is not installed; SLM fallback will use plain templates.")
            return None
        if not _ensure_model_file():
            return None
        try:
            _model = Llama(
                model_path=str(config.SLM_MODEL_PATH),
                n_ctx=config.SLM_CONTEXT_TOKENS,
                n_threads=config.SLM_THREADS,
                verbose=False,
            )
            logger.info("Loaded local SLM from %s", config.SLM_MODEL_PATH)
        except Exception:
            logger.exception("Failed to load local SLM; SLM fallback will use plain templates.")
            _model = None
        return _model


def available() -> bool:
    """Whether real text generation is available (vs. the plain-template degrade)."""
    return _load() is not None


def _template_answer(question: str, facts: list[str]) -> str:
    """Zero-model-weight degrade: list the facts as-is. Always works, no dependencies."""
    if not facts:
        return (
            "I couldn't confidently match this to a specific policy, order, or ticket in the "
            "ParcelPilot sources. I'd recommend escalating this to the human support team so a "
            "person can look into it."
        )
    bullets = "\n".join(f"- {f}" for f in facts)
    return (
        f"Running in lightweight offline mode (no AI phrasing available), here is what I found "
        f"for \u201c{question.strip()}\u201d:\n\n{bullets}"
    )


def _tidy_truncation(text: str) -> str:
    """If a deadline cut generation off mid-sentence, trim back to the last
    complete sentence rather than showing a dangling half-clause."""
    text = text.strip()
    last_stop = max(text.rfind("."), text.rfind("!"), text.rfind("?"))
    if last_stop >= 0 and last_stop < len(text) - 1:
        return text[: last_stop + 1]
    return text


def phrase_answer(question: str, facts: list[str]) -> str:
    """Turn a list of already-verified facts into a short natural-language answer.

    `facts` are plain-English statements already produced by the deterministic
    engines/retrieval (clause citations included as text) -- the model is
    never handed raw policy text to reinterpret, only conclusions to phrase,
    so a wrong/hallucinated word choice can't invent a new policy outcome.

    Bounded by config.SLM_TIMEOUT_SECONDS end-to-end (lock wait + generation),
    so a slow/loaded host degrades to the instant template rather than hanging.
    """
    model = _load()
    if model is None or not facts:
        return _template_answer(question, facts)
    if not _generate_lock.acquire(timeout=config.SLM_TIMEOUT_SECONDS):
        logger.warning("SLM busy past the wait budget; using plain template.")
        return _template_answer(question, facts)
    try:
        from llama_cpp import StoppingCriteriaList

        deadline = time.monotonic() + config.SLM_TIMEOUT_SECONDS
        result = model(
            _prompt(question, facts),
            max_tokens=config.SLM_MAX_NEW_TOKENS,
            temperature=0.2,
            stop=_STOP_SEQUENCES,
            stopping_criteria=StoppingCriteriaList([lambda ids, logits: time.monotonic() > deadline]),
        )
        text = _tidy_truncation(result["choices"][0]["text"])
        return text or _template_answer(question, facts)
    except Exception:
        logger.exception("SLM generation failed; falling back to plain template.")
        return _template_answer(question, facts)
    finally:
        _generate_lock.release()
