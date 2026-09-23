"""Watson's /help for chat. The gateway hook answers /help with this instead of
the stock Hermes command list. iMessage renders **bold** but not `code`, so the
copy uses no backticks.
"""
from __future__ import annotations

_HELP = {
    'pt': (
        '🔧 **Watson — como eu te ajudo**\n'
        '\n'
        '1. **Investigar uma issue** — manda o número (42 ou #42) ou o link da issue '
        'no GitHub. Eu leio a issue, o código e o CI e te explico o que está rolando, '
        'com evidência.\n'
        '\n'
        '2. **Ver o que está conectado** — manda "oi" ou pergunta "o que falta?".\n'
        '\n'
        '3. **Conectar Codex ou Claude** — manda "conecta o Codex" ou "conecta o Claude" '
        'que eu te mando o link de login aqui no chat e aviso quando terminar.\n'
        '\n'
        '4. **Correções** — saem sempre como **draft PR** pra revisão humana. '
        'Nunca faço merge.\n'
        '\n'
        '/new — começa uma conversa do zero.\n'
        '\n'
        'Pode falar comigo em português ou inglês. (English: /help en)'
    ),
    'en': (
        '🔧 **Watson — how I can help**\n'
        '\n'
        '1. **Investigate an issue** — send the number (42 or #42) or the GitHub '
        'issue link. I read the issue, the code and CI, and explain what is going on, '
        'with evidence.\n'
        '\n'
        '2. **See what is connected** — say "hi" or ask "what\'s missing?".\n'
        '\n'
        '3. **Connect Codex or Claude** — say "connect Codex" or "connect Claude" and '
        'I will send you the login link here in chat and let you know when it is done.\n'
        '\n'
        '4. **Fixes** — always go out as a **draft PR** for human review. '
        'I never merge.\n'
        '\n'
        '/new — start a fresh conversation.\n'
        '\n'
        'You can talk to me in Portuguese or English. (Português: /help pt)'
    ),
}

_ARG_LANGUAGES = {
    '': 'pt', 'pt': 'pt', 'pt-br': 'pt', 'portugues': 'pt', 'português': 'pt',
    'en': 'en', 'english': 'en', 'ingles': 'en', 'inglês': 'en',
}


def help_language(args):
    """'pt' / 'en' for the /help arguments; None for anything else (e.g. /help skills)."""
    return _ARG_LANGUAGES.get(str(args or '').strip().lower())


def help_text(language='pt'):
    return _HELP['en' if language == 'en' else 'pt']
