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
        '1) Create a GitHub token with read access to your repositories '
        '(GitHub → Settings → Developer settings → Personal access tokens).\n'
        '2) In the watson-triage folder, create a private file named '
        '`github-credentials` with one line: GH_TOKEN=your_token '
        '(never share or commit this file).\n'
        '3) Restart Watson the same way you usually start this pilot, then ask me again.'
    ),
    'pt': (
        'O GitHub ainda não está conectado, então não consigo investigar issues.\n'
        '\n'
        'Conecte o GitHub uma vez no computador onde o Watson roda:\n'
        '1) Crie um token do GitHub com leitura dos seus repositórios '
        '(GitHub → Settings → Developer settings → Personal access tokens).\n'
        '2) Na pasta watson-triage, crie o arquivo privado `github-credentials` '
        'com uma linha: GH_TOKEN=seu_token (nunca compartilhe nem versione este arquivo).\n'
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
    lower = str(text).lower()
    pt_markers = (
        'ção', 'ções', 'ã', 'õ', 'você', 'voce', 'não', 'nao', 'obrigado',
        'preciso', 'investigar', 'conecte', 'por favor', 'está', 'esta',
        'também', 'tambem', 'issue', 'me diga', 'consegue',
    )
    # Prefer Portuguese when clear PT orthography / common words appear.
    if any(m in lower for m in ('ção', 'ções', 'ã', 'õ', 'você', 'não ', 'nao ')):
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
    # Best-effort: presence of CLI is enough for status honesty; login probed lightly.
    probe = _run(['codex', 'login', 'status'], run, timeout=15)
    if probe.returncode == 0:
        return {'ok': True, 'reason': 'authenticated', 'connected': True}
    # Older/newer CLIs may not support this subcommand; treat binary as present.
    if probe.returncode == 2 or 'usage' in ((probe.stderr or '') + (probe.stdout or '')).lower():
        return {'ok': True, 'reason': 'cli_present', 'connected': True}
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
    raise WatsonError(message_for(_CODEX_MISSING, language))


def capabilities_report(language=None, run=subprocess.run):
    github = check_github(run=run)
    codex = check_codex(run=run)
    lang = normalize_language(language) if language not in (None, '') else None

    if github['ok']:
        status_en = (
            f"GitHub connected"
            + (f" as {github['login']}" if github.get('login') else '')
            + '.'
        )
        status_pt = (
            f"GitHub conectado"
            + (f" como {github['login']}" if github.get('login') else '')
            + '.'
        )
    else:
        status_en = 'GitHub not connected.'
        status_pt = 'GitHub não conectado.'

    if codex['ok']:
        codex_en, codex_pt = 'Codex available.', 'Codex disponível.'
    else:
        codex_en, codex_pt = _CODEX_MISSING['en'], _CODEX_MISSING['pt']

    summary = {
        'en': f'{status_en} {codex_en}'.strip(),
        'pt': f'{status_pt} {codex_pt}'.strip(),
    }
    setup_messages = None
    if not github['ok']:
        setup_messages = {
            'en': github_missing_message(github, 'en'),
            'pt': github_missing_message(github, 'pt'),
        }
        summary['en'] = (
            'Not ready to investigate: GitHub is missing. '
            + status_en
            + ' '
            + codex_en
        )
        summary['pt'] = (
            'Ainda não dá para investigar: falta o GitHub. '
            + status_pt
            + ' '
            + codex_pt
        )

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
        'ready_to_investigate': bool(github['ok']),
        'summary': message_for(summary, lang),
        'messages': summary if lang is None else {lang: summary[lang]},
    }
    if setup_messages:
        report['setup'] = (
            message_for(setup_messages, lang)
            if lang
            else setup_messages
        )
    return report
