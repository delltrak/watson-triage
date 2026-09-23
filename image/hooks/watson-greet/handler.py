"""Watson greeting short-circuit (Hermes gateway hook, loaded on gateway:startup).

Wraps GatewayTurnMixin._run_agent: when the owner's Plow DM is a plain-text
greeting ("oi", "olá", "hey", "bom dia", ...), the turn returns
watson.greeting.greeting_reply() — the exact watson_status speak_this — with
no LLM call, so connection status cannot be paraphrased or invented. The Plow
restart wake ("you just came online") gets its silence sentinel the same way:
the persona stays silent there, and no LLM turn can volunteer a stale status.

It runs after Hermes admission/auth, slash commands and session resolution;
the gateway then shapes, persists and delivers the synthetic result like any
other turn (the greeting + reply land in the transcript). Every failure falls
back to the normal LLM path.

It also answers /help on Plow with Watson's own help (command:help hook
decision), instead of the stock Hermes command list.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("watson.greet")

_PLATFORM = "plow_chat"
# Synthetic Plow turns (restart wake, system notices, goal checks) never bypass.
_SYNTHETIC_USERS = frozenset({"plow_setup", "plow_system", "plow_goal"})
_RUN_AGENT_PARAMS = ["self", "message", "context_prompt", "history", "source", "session_id"]
_TIMEOUT_SEC = 40.0
# plow_chat prepends blocks to event.text (quote-reply, /goal line, roster,
# referrer). Only the referrer note is context-free; any other block means the
# words answer something (a quoted message, a standing goal) — LLM path.
_BENIGN_PREFIX = "[Untrusted account data"
# plow_chat WAKEUP_TURN: "Plow, not your owner: you just came online ...
# If you have nothing to say, reply with exactly NO_REPLY."
_WAKE_MARK = "Plow, not your owner: you just came online"
_WAKE_SENTINEL = re.compile(r"reply with exactly (\w+)\.?\s*$")

# The speaker's own words for this inbound turn (Plow puts them in recall_text;
# event.text may carry prepended roster/quote/goal blocks).
_inbound_text = contextvars.ContextVar("watson_greet_inbound_text", default=None)


def _watson_home() -> Path:
    """Same home the watson MCP server uses (runtime/mcp-watson.yaml --home)."""
    return Path(os.environ.get("HERMES_HOME", "/var/lib/hermes")) / "watson"


def _watson_config() -> dict:
    """Watson's config.json (line default language), or {} when unreadable."""
    try:
        import json

        return json.loads((_watson_home() / "config.json").read_text())
    except Exception:
        return {}


def _remember_owner_language(text) -> None:
    """Owner's language for turns with no text to detect (/help, proactive pings)."""
    try:
        from watson.language import detect, remember_language

        language = detect(text)
        if language:
            remember_language(_watson_home(), language)
    except Exception:
        logger.debug("watson-greet: could not remember owner language", exc_info=True)


def _owner_dm(source) -> bool:
    platform = getattr(getattr(source, "platform", None), "value", None)
    return (
        platform == _PLATFORM
        and getattr(source, "chat_type", None) == "dm"
        # plow_chat sets role_authorized only for the owner.
        and getattr(source, "role_authorized", False) is True
        and not getattr(source, "is_bot", False)
        and str(getattr(source, "user_id", "") or "") not in _SYNTHETIC_USERS
    )


def _spoken_words(event):
    """Owner's own words for a plain-text turn; None for media, quotes, goals."""
    if getattr(event, "media_urls", None) or getattr(event, "media_types", None):
        return None
    kind = getattr(event, "message_type", None)
    if getattr(kind, "value", kind) not in (None, "text"):
        return None
    text = getattr(event, "text", None)
    spoken = getattr(event, "recall_text", None)
    if not isinstance(spoken, str):
        spoken = text
    if not isinstance(spoken, str):
        return None
    if isinstance(text, str):
        prefix = text[:max(len(text) - len(spoken), 0)]
        blocks = [block for block in prefix.split("\n\n") if block.strip()]
        if any(not block.lstrip().startswith(_BENIGN_PREFIX) for block in blocks):
            return None
    return spoken


def _wake_sentinel(source, text):
    """Silence token for the Plow restart wake turn, else None."""
    platform = getattr(getattr(source, "platform", None), "value", None)
    if platform != _PLATFORM or getattr(source, "user_id", None) != "plow_setup":
        return None
    if not isinstance(text, str) or not text.strip().startswith(_WAKE_MARK):
        return None
    match = _WAKE_SENTINEL.search(text)
    return match.group(1) if match else None


def _turn_text(message, turn_kwargs):
    """Owner's words for this turn, or None when they don't match the turn being run."""
    spoken = _inbound_text.get()
    persisted = turn_kwargs.get("persist_user_message")
    turn = persisted if isinstance(persisted, str) else message
    if not isinstance(spoken, str) or not isinstance(turn, str):
        return None
    spoken = spoken.strip()
    # Guard against a stale context: the running turn must end with these words.
    if not spoken or not turn.strip().endswith(spoken):
        return None
    return spoken


def _synthetic_result(speak: str, session_id: str) -> dict:
    return {
        "final_response": speak,
        "messages": [],
        "api_calls": 0,
        "completed": True,
        "failed": False,
        "partial": False,
        "interrupted": False,
        # Gateway writes the user + assistant rows itself.
        "agent_persisted": False,
        "tools": [],
        "session_id": session_id,
    }


def _install() -> None:
    from gateway.run_turn import GatewayTurnMixin as mixin

    if mixin.__dict__.get("_watson_greet_patched"):
        return
    run_agent = mixin.__dict__.get("_run_agent")
    handle = mixin.__dict__.get("_handle_message_with_agent")
    if not (inspect.iscoroutinefunction(run_agent) and inspect.iscoroutinefunction(handle)):
        print("[watson-greet] gateway methods missing; bypass OFF", flush=True)
        return
    params = list(inspect.signature(run_agent).parameters)
    if params[:len(_RUN_AGENT_PARAMS)] != _RUN_AGENT_PARAMS:
        print(f"[watson-greet] unexpected _run_agent signature {params}; bypass OFF", flush=True)
        return
    if list(inspect.signature(handle).parameters)[:2] != ["self", "event"]:
        print("[watson-greet] unexpected _handle_message_with_agent signature; bypass OFF", flush=True)
        return

    from watson.greeting import greeting_reply

    async def _handle_message_with_agent(self, event, *args, **kwargs):
        token = _inbound_text.set(_spoken_words(event))
        try:
            return await handle(self, event, *args, **kwargs)
        finally:
            _inbound_text.reset(token)

    async def _run_agent(self, message, context_prompt, history, source, session_id, **turn_kwargs):
        speak, text = None, None
        try:
            if int(turn_kwargs.get("_interrupt_depth") or 0) == 0:
                text = _turn_text(message, turn_kwargs)
                silence = _wake_sentinel(source, text)
                if silence is not None:
                    speak, text = silence, "(plow restart wake)"
                elif text is not None and _owner_dm(source):
                    _remember_owner_language(text)
                    speak = await asyncio.wait_for(
                        asyncio.to_thread(greeting_reply, text), _TIMEOUT_SEC)
        except Exception:
            logger.exception("watson-greet: speak_this failed; falling back to the LLM")
            speak = None
        if speak is None:
            return await run_agent(self, message, context_prompt, history, source, session_id, **turn_kwargs)
        # A real turn consumes the one-shot notes staged for it; so does this one.
        with contextlib.suppress(Exception):
            self._consume_pending_turn_sidecar_notes(turn_kwargs.get("session_key"))
        logger.info("watson-greet: LLM bypassed for %r -> %d chars", text[:40], len(speak))
        return _synthetic_result(speak, session_id)

    _handle_message_with_agent.__wrapped__ = handle
    _run_agent.__wrapped__ = run_agent
    mixin._handle_message_with_agent = _handle_message_with_agent
    mixin._run_agent = _run_agent
    mixin._watson_greet_patched = True
    print("[watson-greet] patched GatewayTurnMixin._run_agent (owner DM greetings -> speak_this)", flush=True)


def _help_decision(context):
    """Watson /help on Plow; None keeps Hermes' own /help (other platforms, /help skills).

    /help alone answers in the owner's remembered language (or the line default);
    /help en | /help pt picks one explicitly."""
    if (context or {}).get("platform") != _PLATFORM:
        return None
    from watson.chat_help import help_language, help_text
    from watson.language import preferred_language

    args = str((context or {}).get("args") or "").strip()
    language = help_language(args) if args else preferred_language(_watson_home(), _watson_config())
    if language is None:
        return None
    return {"decision": "handled", "message": help_text(language)}


def handle(event_type, context):
    if event_type == "command:help":
        try:
            return _help_decision(context)
        except Exception as exc:  # fall back to the Hermes list
            print(f"[watson-greet] /help failed; Hermes help used: {exc}", flush=True)
            return None
    if event_type != "gateway:startup":
        return None
    try:
        _install()
    except Exception as exc:  # never block gateway boot
        print(f"[watson-greet] install failed; LLM path unchanged: {exc}", flush=True)
    return None
