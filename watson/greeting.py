"""Deterministic replies for pure greetings.

The Hermes gateway hook (image/hooks/watson-greet) answers these with
capabilities_report()['speak_this'] verbatim, without calling the LLM, so
connection status can never be paraphrased or invented. Anything that is not
only a greeting ("oi, investiga a 42") returns None and goes to the LLM.
"""
from __future__ import annotations

import re
import subprocess
import unicodedata

from .capabilities import capabilities_report

# Accent-free, punctuation-free phrases (see _normalize).
# A message bypasses the LLM only if it has a real hello (or "who are you");
# ack/filler words ride along ("oi, tudo bem?") but never trigger on their own:
# "tudo bem", "opa", "e aí?", "status?", "tá pronto?" mid-conversation are
# answers or progress asks that need context, so they go to the LLM.
_HELLOS = {
    'pt': ('ola', 'bom dia', 'boa tarde', 'boa noite', 'quem e voce', 'quem e vc'),
    'en': ('hey', 'hi', 'hello', 'good morning', 'good afternoon', 'good evening',
           'who are you'),
}
_EXTRAS = {
    'pt': ('tudo bem', 'td bem', 'tudo bom', 'tudo certo', 'e ai', 'eai', 'eae',
           'opa', 'fala', 'salve'),
    'en': ('there',),
}
_NAMES = frozenset({'watson'})

_PHRASES = sorted(
    [(tuple(p.split()), lang, True) for lang, ps in _HELLOS.items() for p in ps]
    + [(tuple(p.split()), lang, False) for lang, ps in _EXTRAS.items() for p in ps],
    key=lambda item: -len(item[0]),
)
_OI = re.compile(r'^oi+e?$')  # oi, oii, oiii, oie
_MAX_CHARS = 60
_MAX_WORDS = 8


def _normalize(text):
    """Lowercase, strip accents and punctuation ('Olá!' -> 'ola', 'Ol;a' -> 'ola')."""
    text = unicodedata.normalize('NFKD', str(text or '').lower())
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("'", '').replace('’', '')
    text = re.sub(r'[^\w\s]', '', text)
    return ' '.join(text.split())


def classify(text):
    """Return 'pt' / 'en' when text is only a greeting, else None."""
    raw = str(text or '').strip()
    if not raw or len(raw) > _MAX_CHARS or '\n' in raw:
        return None
    words = _normalize(raw).split()
    if not words or len(words) > _MAX_WORDS:
        return None
    langs, hello, i = [], False, 0
    while i < len(words):
        if words[i] in _NAMES:
            i += 1
            continue
        if _OI.match(words[i]):
            langs.append('pt')
            hello, i = True, i + 1
            continue
        for phrase, lang, is_hello in _PHRASES:
            if tuple(words[i:i + len(phrase)]) == phrase:
                langs.append(lang)
                hello = hello or is_hello
                i += len(phrase)
                break
        else:
            return None
    if not hello:
        return None
    return 'en' if 'pt' not in langs and 'en' in langs else 'pt'


def greeting_reply(text, run=subprocess.run):
    """speak_this for a pure greeting (exact watson_status copy), else None."""
    language = classify(text)
    if language is None:
        return None
    speak = capabilities_report(language=language, run=run).get('speak_this')
    if not isinstance(speak, str) or not speak.strip():
        raise RuntimeError('watson speak_this is empty')
    return speak
