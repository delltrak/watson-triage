"""Owner language for chat copy: explicit per-call > last confident detection > line default.

The gateway hook remembers the owner's language from each plain-text DM so that
turns without text to detect (/help, proactive pings) still answer in it.
Line default: config.json "language" or WATSON_LANGUAGE (e.g. "en" for a US
line), else Portuguese.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .core import now, private_json

LANGUAGES = ('pt', 'en')
_FILE = 'language.json'

# Small, conservative word lists: a message is classified only when it clearly
# leans one way; otherwise the remembered language is kept.
# Portuguese function words that are not also English words ('a', 'as', 'do', 'no', 'se',
# 'pro' are left out on purpose). English list drops loanwords Brazilians use in PT
# sentences ('check', 'show', 'open', 'close', 'send', 'look', 'squad', 'need').
_PT_WORDS = frozenset('''
    não nao você voce vocês vc que uma para pra com isso essa esse esta está tá ta
    o os e é de da das dos na nas em tb pq aí mas já ja tem faz foi vou dá pros pras
    investiga investigar analisa analisar olha olhar veja ver abre abrir fecha fechar
    conecta conectar obrigado obrigada valeu por favor qual quais como quando onde porque
    sim também tambem minha meu tropa preciso pode consegue manda mandar
'''.split())
_EN_WORDS = frozenset('''
    the is are can could would you your please investigate connect what why how when
    where this that with for my thanks thank yes also tell
'''.split())
_MAX_DETECT = 500  # enough to tell pt from en; keeps detection cheap on the gateway loop
_QUOTED = re.compile(r"(?<!\w)['\"“‘][^'\"”’\n]{1,200}['\"”’](?!\w)")
_LINKISH = re.compile(r'://|\.(?:com|br|org|io|dev|co)\b')
_PT_CHARS = re.compile(r'[ãõçáéíóúâêôà]')


def normalize(value):
    text = str(value or '').strip().lower().replace('_', '-')
    if text in {'pt', 'pt-br', 'pt-pt', 'portuguese', 'português', 'portugues'}:
        return 'pt'
    if text in {'en', 'en-us', 'en-gb', 'english', 'inglês', 'ingles'}:
        return 'en'
    return None


def detect(text):
    """'pt' / 'en' when the text clearly leans one way, else None (keep what we know)."""
    from .greeting import classify

    greeting = classify(text)
    if greeting:
        return greeting
    lower = str(text or '')[:_MAX_DETECT].lower()
    # Links carry no language ('.com' would read as the Portuguese 'com'); quoted titles
    # belong to someone else ("status of 'Não abre o app'?" is English).
    lower = ' '.join(tok for tok in lower.split() if not _LINKISH.search(tok))
    lower = _QUOTED.sub(' ', lower)
    words = re.findall(r"[a-zà-ú']+", lower)
    pt = sum(w in _PT_WORDS for w in words) + (2 if _PT_CHARS.search(lower) else 0)
    en = sum(w in _EN_WORDS for w in words)
    if pt > en and pt >= 1 and en == 0 or pt >= en + 2:
        return 'pt'
    if en > pt and en >= 1 and pt == 0 or en >= pt + 2:
        return 'en'
    return None


def default_language(config=None):
    return (normalize((config or {}).get('language')) or normalize(os.environ.get('WATSON_LANGUAGE'))
            or 'pt')


def preferred_language(home, config=None):
    try:
        data = json.loads((Path(home) / _FILE).read_text())
        lang = normalize(data.get('language'))
        if lang:
            return lang
    except (OSError, ValueError, AttributeError):
        pass
    return default_language(config)


def remember_language(home, language):
    lang = normalize(language)
    if not lang:
        return None
    path = Path(home) / _FILE
    try:
        if json.loads(path.read_text()).get('language') == lang:
            return lang
    except (OSError, ValueError, AttributeError):
        pass
    private_json(path, {'language': lang, 'at': now()})
    return lang
