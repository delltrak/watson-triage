"""Runtime capability checks for GitHub (and Codex when relevant).

Chat-facing setup copy is bilingual (en/pt). Callers pick a language or keep both.
Never claim GitHub is connected when auth is missing.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from .core import WatsonError


# Junior-friendly owner steps. No Docker / PAT / branch jargon.
_GITHUB_MISSING = {
    'en': (
        'GitHub is not connected yet, so I cannot investigate issues.\n'
        '\n'
        'Please connect GitHub once on the computer that runs Watson:\n'
        '\n'
        '1) Create a GitHub token with read access to your repositories '
        '(GitHub → Settings → Developer settings → Personal access tokens).\n'
        '\n'
        '2) In the watson-triage folder, create a private file named '
        '`github-credentials` with one line: GH_TOKEN=your_token '
        '(never share or commit this file).\n'
        '\n'
        '3) Restart Watson the same way you usually start this pilot, then ask me again.'
    ),
    'pt': (
        'O GitHub ainda não está conectado, então não consigo investigar issues.\n'
        '\n'
        'Conecte o GitHub uma vez no computador onde o Watson roda:\n'
        '\n'
        '1) Crie um token do GitHub com leitura dos seus repositórios '
        '(GitHub → Settings → Developer settings → Personal access tokens).\n'
        '\n'
        '2) Na pasta watson-triage, crie o arquivo privado `github-credentials` '
        'com uma linha: GH_TOKEN=seu_token (nunca compartilhe nem versione este arquivo).\n'
        '\n'
        '3) Reinicie o Watson como você costuma iniciar este piloto e peça de novo.'
    ),
}

_GITHUB_CLI_MISSING = {
    'en': (
        'GitHub tools are not available in this Watson install yet, so I cannot '
        'investigate issues. Ask the owner to update/restart Watson with the '
        'latest piloto setup that includes GitHub access, then try again.'
    ),
    'pt': (
        'As ferramentas do GitHub ainda não estão disponíveis nesta instalação '
        'do Watson, então não consigo investigar issues. Peça ao dono para '
        'atualizar/reiniciar o Watson com a configuração mais recente do piloto '
        'que inclui acesso ao GitHub e tente de novo.'
    ),
}

_CODEX_MISSING = {
    'en': 'Codex is not available in this environment (local login/CLI missing).',
    'pt': 'O Codex não está disponível neste ambiente (login/CLI local ausente).',
}

_CODEX_LOGIN = {
    'en': (
        'Codex CLI is installed but not logged in yet. The owner needs to connect '
        'Codex once on this line (local login), then ask me again.'
    ),
    'pt': (
        'O Codex CLI está instalado, mas ainda sem login. O dono precisa conectar '
        'o Codex uma vez nesta linha (login local) e me pedir de novo.'
    ),
}

_CLAUDE_MISSING = {
    'en': 'Claude Code is not available in this environment (local login/CLI missing).',
    'pt': 'O Claude Code não está disponível neste ambiente (login/CLI local ausente).',
}

_CLAUDE_LOGIN = {
    'en': (
        'Claude Code CLI is installed but not logged in yet. The owner needs to '
        'connect Claude once on this line (local login), then ask me again.'
    ),
    'pt': (
        'O Claude Code CLI está instalado, mas ainda sem login. O dono precisa '
        'conectar o Claude uma vez nesta linha (login local) e me pedir de novo.'
    ),
}


def normalize_language(value):
    if value is None or value == '' or value == 'auto':
        return None
    text = str(value).strip().lower().replace('_', '-')
    if text in {'pt', 'pt-br', 'pt-pt', 'portuguese', 'português', 'portugues'}:
        return 'pt'
    if text in {'en', 'en-us', 'en-gb', 'english', 'inglês', 'ingles'}:
        return 'en'
    raise WatsonError('language must be "en", "pt", or "auto".')


def detect_language(text):
    """Lightweight hint from free text (user message). Defaults to English."""
    if not text or not str(text).strip():
        return 'en'
    lower = str(text).lower().strip()
    # Short PT openers that otherwise look language-neutral ("oi", "olá").
    pt_greetings = {
        'oi', 'olá', 'ola', 'oie', 'eai', 'e aí', 'e ai', 'bom dia',
        'boa tarde', 'boa noite', 'fala', 'opa', 'eae', 'salve',
    }
    if lower in pt_greetings or lower.rstrip('!?.') in pt_greetings:
        return 'pt'
    pt_markers = (
        'ção', 'ções', 'ã', 'õ', 'você', 'voce', 'não', 'nao', 'obrigado',
        'preciso', 'investigar', 'conecte', 'por favor', 'está', 'esta',
        'também', 'tambem', 'me diga', 'consegue', 'tudo bem', 'td bem',
    )
    # Prefer Portuguese when clear PT orthography / common words appear.
    if any(m in lower for m in ('ção', 'ções', 'ã', 'õ', 'você', 'não ', 'nao ', 'olá')):
        return 'pt'
    if sum(1 for m in pt_markers if m in lower) >= 2:
        return 'pt'
    return 'en'


def _bilingual(mapping):
    return f"{mapping['en']}\n\n---\n\n{mapping['pt']}"


def message_for(mapping, language=None):
    lang = normalize_language(language) if language not in (None, '') else None
    if lang in mapping:
        return mapping[lang]
    return _bilingual(mapping)


def _run(cmd, run, timeout=20, env=None):
    try:
        return run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, '', 'not found')
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, '', 'timeout')


def _token_present():
    return bool(os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN'))


def check_github(run=subprocess.run):
    """Return dict: ok, reason, login (optional). Never includes token values."""
    if not shutil.which('gh'):
        return {
            'ok': False,
            'reason': 'cli_missing',
            'connected': False,
            'login': None,
        }
    status = _run(['gh', 'auth', 'status'], run, timeout=15)
    if status.returncode == 0:
        login = None
        combined = (status.stdout or '') + (status.stderr or '')
        for line in combined.splitlines():
            # "Logged in to github.com account NAME"
            if 'account ' in line.lower():
                parts = line.strip().split()
                if 'account' in [p.lower() for p in parts]:
                    try:
                        idx = [p.lower() for p in parts].index('account')
                        login = parts[idx + 1]
                    except (ValueError, IndexError):
                        pass
        return {'ok': True, 'reason': 'authenticated', 'connected': True, 'login': login}

    # Token-only environments often skip interactive auth status.
    if _token_present():
        api = _run(['gh', 'api', 'user', '--jq', '.login'], run, timeout=30)
        if api.returncode == 0 and (api.stdout or '').strip():
            return {
                'ok': True,
                'reason': 'token_env',
                'connected': True,
                'login': api.stdout.strip().splitlines()[0].strip(),
            }
        return {
            'ok': False,
            'reason': 'token_invalid',
            'connected': False,
            'login': None,
        }
    return {
        'ok': False,
        'reason': 'not_authenticated',
        'connected': False,
        'login': None,
    }


def check_codex(run=subprocess.run):
    if not shutil.which('codex'):
        return {'ok': False, 'reason': 'cli_missing', 'connected': False}
    probe = _run(['codex', 'login', 'status'], run, timeout=15)
    combined = ((probe.stdout or '') + (probe.stderr or '')).lower()
    if probe.returncode == 0 and 'not logged in' not in combined:
        return {'ok': True, 'reason': 'authenticated', 'connected': True}
    if 'not logged in' in combined or 'logged in: false' in combined:
        return {'ok': False, 'reason': 'not_authenticated', 'connected': False}
    # Older/newer CLIs may not support this subcommand; treat binary as present.
    if probe.returncode == 2 or 'usage' in combined:
        return {'ok': True, 'reason': 'cli_present', 'connected': True}
    return {'ok': False, 'reason': 'not_authenticated', 'connected': False}


def check_claude(run=subprocess.run):
    if not shutil.which('claude'):
        return {'ok': False, 'reason': 'cli_missing', 'connected': False}
    probe = _run(['claude', 'auth', 'status'], run, timeout=15)
    combined = (probe.stdout or '') + (probe.stderr or '')
    compact = combined.lower().replace(' ', '').replace('_', '')
    if '"loggedin":true' in compact:
        return {'ok': True, 'reason': 'authenticated', 'connected': True}
    if '"loggedin":false' in compact or 'not logged' in combined.lower():
        return {'ok': False, 'reason': 'not_authenticated', 'connected': False}
    if probe.returncode == 2 or 'usage' in combined.lower():
        return {'ok': True, 'reason': 'cli_present', 'connected': True}
    # CLI present but auth unclear — do not claim connected.
    return {'ok': False, 'reason': 'not_authenticated', 'connected': False}


def github_missing_message(github, language=None):
    if github.get('reason') == 'cli_missing':
        return message_for(_GITHUB_CLI_MISSING, language)
    return message_for(_GITHUB_MISSING, language)


def require_github(language=None, run=subprocess.run):
    github = check_github(run=run)
    if github['ok']:
        return github
    raise WatsonError(github_missing_message(github, language))


def require_codex(language=None, run=subprocess.run):
    codex = check_codex(run=run)
    if codex['ok']:
        return codex
    if codex.get('reason') == 'not_authenticated':
        raise WatsonError(message_for(_CODEX_LOGIN, language))
    raise WatsonError(message_for(_CODEX_MISSING, language))


def codex_status_message(codex, language=None):
    if codex.get('ok'):
        mapping = {
            'en': 'Codex available.',
            'pt': 'Codex disponível.',
        }
        return message_for(mapping, language)
    if codex.get('reason') == 'not_authenticated':
        return message_for(_CODEX_LOGIN, language)
    return message_for(_CODEX_MISSING, language)


def claude_status_message(claude, language=None):
    if claude.get('ok'):
        mapping = {
            'en': 'Claude Code available.',
            'pt': 'Claude Code disponível.',
        }
        return message_for(mapping, language)
    if claude.get('reason') == 'not_authenticated':
        return message_for(_CLAUDE_LOGIN, language)
    return message_for(_CLAUDE_MISSING, language)



def _onboarding_copy(github, codex, claude, checklist_en, checklist_pt, setup_messages):
    """Ready-to-send first-greeting text. Model should relay, not invent."""
    gh_ok = bool(github.get('ok'))
    after_ready_en = (
        'Once GitHub is connected I can investigate an issue by number or link, '
        'and open a **draft PR** for a fix (I never merge). '
        'Text `/help` for commands anytime.'
    )
    after_ready_pt = (
        'Quando o GitHub estiver conectado, posso investigar uma issue por número '
        'ou link e abrir um **draft PR** com a correção (nunca faço merge). '
        'Manda `/help` se quiser a lista de comandos.'
    )
    if gh_ok:
        en = (
            "Hey — I'm Watson, your engineering teammate for GitHub issue triage.\n"
            '\n'
            f'{checklist_en}\n'
            '\n'
            'Paste an issue number or link and I will investigate. '
            'Fixes always go out as a **draft PR** (I never merge). '
            'Text `/help` for commands.'
        )
        pt = (
            'Oi — sou o Watson, seu colega de engenharia pra triagem de issues '
            'do GitHub.\n'
            '\n'
            f'{checklist_pt}\n'
            '\n'
            'Manda o número ou o link da issue que eu investigo. '
            'Correção = sempre **draft PR** (nunca faço merge). '
            'Se quiser os comandos, manda `/help`.'
        )
    else:
        setup_en = (setup_messages or {}).get('en', '')
        setup_pt = (setup_messages or {}).get('pt', '')
        en = (
            "Hey — I'm Watson, your engineering teammate for GitHub issue triage.\n"
            '\n'
            'Not ready to investigate yet. Current gaps:\n'
            '\n'
            f'{checklist_en}\n'
            '\n'
            f'{setup_en}\n'
            '\n'
            f'{after_ready_en}'
        )
        pt = (
            'Oi — sou o Watson, seu colega de engenharia pra triagem de issues '
            'do GitHub.\n'
            '\n'
            'Ainda não dá pra investigar. O que falta:\n'
            '\n'
            f'{checklist_pt}\n'
            '\n'
            f'{setup_pt}\n'
            '\n'
            f'{after_ready_pt}'
        )
    return {'en': en, 'pt': pt}


def capabilities_report(language=None, run=subprocess.run):
    github = check_github(run=run)
    codex = check_codex(run=run)
    claude = check_claude(run=run)
    lang = normalize_language(language) if language not in (None, '') else None

    if github['ok']:
        gh_line_en = (
            'GitHub connected'
            + (f" as {github['login']}" if github.get('login') else '')
            + '.'
        )
        gh_line_pt = (
            'GitHub conectado'
            + (f" como {github['login']}" if github.get('login') else '')
            + '.'
        )
    else:
        gh_line_en = 'GitHub not connected.'
        gh_line_pt = 'GitHub não conectado.'

    codex_en = codex_status_message(codex, 'en')
    codex_pt = codex_status_message(codex, 'pt')
    claude_en = claude_status_message(claude, 'en')
    claude_pt = claude_status_message(claude, 'pt')

    # Plain-text numbered checklist with blank lines between items — iMessage
    # collapses markdown lists; blank lines survive.
    checklist_en = (
        f'1. GitHub — {gh_line_en}\n'
        f'\n'
        f'2. Codex CLI — {codex_en}\n'
        f'\n'
        f'3. Claude Code CLI — {claude_en}'
    )
    checklist_pt = (
        f'1. GitHub — {gh_line_pt}\n'
        f'\n'
        f'2. Codex CLI — {codex_pt}\n'
        f'\n'
        f'3. Claude Code CLI — {claude_pt}'
    )

    if github['ok']:
        summary = {
            'en': f'Ready to investigate.\n\n{checklist_en}',
            'pt': f'Pronto para investigar.\n\n{checklist_pt}',
        }
    else:
        summary = {
            'en': (
                'Not ready to investigate: GitHub is missing.\n'
                '\n'
                f'{checklist_en}'
            ),
            'pt': (
                'Ainda não dá para investigar: falta o GitHub.\n'
                '\n'
                f'{checklist_pt}'
            ),
        }

    setup_messages = None
    if not github['ok']:
        setup_messages = {
            'en': github_missing_message(github, 'en'),
            'pt': github_missing_message(github, 'pt'),
        }

    onboarding = _onboarding_copy(
        github, codex, claude, checklist_en, checklist_pt, setup_messages)

    report = {
        'github': {
            'connected': github['ok'],
            'reason': github['reason'],
            'login': github.get('login'),
        },
        'codex': {
            'connected': codex['ok'],
            'reason': codex['reason'],
        },
        'claude': {
            'connected': claude['ok'],
            'reason': claude['reason'],
        },
        'ready_to_investigate': bool(github['ok']),
        'summary': message_for(summary, lang),
        'messages': summary if lang is None else {lang: summary[lang]},
        # First-greeting copy the chat agent should relay (not invent).
        'onboarding': message_for(onboarding, lang),
        'onboarding_messages': (
            onboarding if lang is None else {lang: onboarding[lang]}
        ),
    }
    if setup_messages:
        report['setup'] = (
            message_for(setup_messages, lang)
            if lang
            else setup_messages
        )
    return report
