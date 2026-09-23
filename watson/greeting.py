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
    'pt': ('ola', 'bom dia', 'boa tarde', 'boa noite'),
    'en': ('hey', 'hi', 'hello', 'good morning', 'good afternoon', 'good evening'),
}
# Asking what Watson does / how to use it → the help text (no need to type /help).
# Only unambiguous capability asks: "como funciona?", "me ajuda" or "preciso de
# ajuda" right after Watson says something are follow-ups that need context → LLM.
_HELP = {
    'pt': ('ajuda', 'o que voce faz', 'o que vc faz', 'o que voce sabe fazer', 'o que da pra fazer',
           'como voce funciona', 'como te uso', 'como eu te uso', 'quais comandos', 'quais sao os comandos',
           'comandos', 'quem e voce', 'quem e vc', 'menu'),
    'en': ('help', 'what can you do', 'what do you do', 'how do you work', 'how do i use you', 'commands',
           'what are the commands', 'who are you'),
}
# One word that reads the same in both languages: answer in the owner's language.
_EITHER = frozenset({'help', 'menu'})
_POLITE = re.compile(r'(?: (?:por favor|pfv|pls|please|watson))+$')
_EXTRAS = {
    'pt': ('tudo bem', 'td bem', 'tudo bom', 'tudo certo', 'tudo joia', 'tudo tranquilo', 'com voce', 'com vc',
           'como vai', 'como vai voce', 'como vc ta', 'como voce esta', 'beleza', 'blz', 'joia',
           'e ai', 'eai', 'eae', 'opa', 'fala', 'salve'),
    'en': ('there', 'how are you', 'how are you doing', 'how r u', 'whats up', 'sup', 'how is it going',
           'hows it going'),
}
_NAMES = frozenset({'watson'})
# Openers that are a hello when they call Watson by name ("e aí, Watson", "fala watson");
# alone they are mid-conversation filler and go to the LLM.
_OPENERS = frozenset({('e', 'ai'), ('eai',), ('eae',), ('opa',), ('fala',), ('salve',)})

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
    # "Bom diaaa" → "bom dia": only whitelisted phrases can match, so this is safe.
    words = [re.sub(r'(\w)\1{2,}', r'\1', w) for w in _normalize(raw).split()]
    if not words or len(words) > _MAX_WORDS:
        return None
    langs, hello, named, opener, i = [], False, False, False, 0
    while i < len(words):
        if words[i] in _NAMES:
            named, i = True, i + 1
            continue
        if _OI.match(words[i]):
            langs.append('pt')
            hello, i = True, i + 1
            continue
        for phrase, lang, is_hello in _PHRASES:
            if tuple(words[i:i + len(phrase)]) == phrase:
                langs.append(lang)
                hello = hello or is_hello
                opener = opener or phrase in _OPENERS
                i += len(phrase)
                break
        else:
            return None
    if not (hello or (named and opener)):
        return None
    return 'en' if 'pt' not in langs and 'en' in langs else 'pt'


def classify_help(text, fallback=None):
    """'pt' / 'en' when the text only asks what Watson does or how to use it, else None.

    A one-word ask that reads the same in both languages ("help", "menu") answers in
    fallback (the owner's language) when given.
    """
    raw = str(text or '').strip()
    if not raw or len(raw) > _MAX_CHARS or '\n' in raw:
        return None
    norm = _POLITE.sub('', _normalize(raw))
    if norm.startswith('watson '):
        norm = norm[len('watson '):]
    for lang, phrases in _HELP.items():
        if norm in phrases:
            return fallback if fallback in {'pt', 'en'} and norm in _EITHER else lang
    return None


def greeting_reply(text, run=subprocess.run, home=None, owner_uid=None, remember=False):
    """Greeting copy for a pure greeting, else None.

    The full checklist (exact watson_status speak_this) on first contact or when the
    connection picture changed; one short line otherwise (watson/onboarding.py).
    Only the owner's hook turn (remember=True) records what was shown.
    """
    language = classify(text)
    if language is None:
        return None
    squad = None
    if home is not None:
        from .roster import load
        squad = load(home)
    report = capabilities_report(language=language, run=run, squad=squad)
    if home is not None and owner_uid is not None:
        from . import onboarding
        from .capabilities import short_greeting
        if onboarding.decide(onboarding.load(home), report, owner_uid) == 'short':
            return short_greeting(report, language)
        if remember:
            onboarding.mark_shown(home, report, owner_uid)
    speak = report.get('speak_this')
    if not isinstance(speak, str) or not speak.strip():
        raise RuntimeError('watson speak_this is empty')
    return speak


def owner_shortcut(text, home=None, run=subprocess.run, owner_uid=None, remember=False):
    """Deterministic owner-DM answers (no LLM): squad, help, greetings. None = LLM."""
    if home is not None:
        from .roster import connected_teams, parse_request, reply
        try:
            request = parse_request(text, home)
        except Exception:  # noqa: BLE001 — a squad parser problem must never cost the greeting
            request = None
        if request is not None:
            return reply(home, text, connected_teams(run=run))
    fallback = None
    if home is not None:
        from .language import preferred_language
        try:
            fallback = preferred_language(home)
        except Exception:  # noqa: BLE001 — the phrase's own language is fine
            fallback = None
    language = classify_help(text, fallback)
    if language is not None:
        from .chat_help import help_text
        return help_text(language)
    return greeting_reply(text, run=run, home=home, owner_uid=owner_uid, remember=remember)
