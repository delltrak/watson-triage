"""Parse GitHub issue numbers and URLs for chat/MCP/CLI."""
from __future__ import annotations

import re

from .core import WatsonError, both, repo_name


_ISSUE_URL = re.compile(
    r'^https?://(?:www\.)?github\.com/'
    r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/(\d+)/?'
    r'(?:[?#].*)?$',
    re.IGNORECASE,
)

# Repo homepage only — not /issues/, /pull/, /tree/, etc.
_REPO_URL = re.compile(
    r'^https?://(?:www\.)?github\.com/'
    r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?'
    r'(?:[?#].*)?$',
    re.IGNORECASE,
)


_ASK = both('Tell me the issue number or paste the GitHub link.',
             'Me diga o número da issue ou cole o link do GitHub.')
_POSITIVE = both('The issue number must be greater than zero.',
                 'O número da issue precisa ser maior que zero.')
_REPO_PAGE = both(
    'I could not tell which issue you mean. Paste the full issue link '
    '(e.g. https://github.com/owner/project/issues/12), not just the repository page.',
    'Não entendi qual issue você quer. Cole o link completo da issue '
    '(ex.: https://github.com/dono/projeto/issues/12), não só a página do repositório.')
_UNKNOWN = both(
    'I could not tell which issue you mean. Paste the link '
    '(e.g. https://github.com/owner/project/issues/12) or just the number.',
    'Não entendi qual issue você quer. Cole o link '
    '(ex.: https://github.com/dono/projeto/issues/12) ou só o número.')


def parse_issue_ref(value):
    """Aceita número, '#123' ou URL de issue do GitHub.

    Retorna (repositório_ou_None, número). O repositório só vem preenchido
    quando a entrada foi uma URL de issue.
    """
    if isinstance(value, bool):
        raise WatsonError(_ASK)
    if isinstance(value, int):
        if value < 1:
            raise WatsonError(_POSITIVE)
        return None, value
    if not isinstance(value, str):
        raise WatsonError(_ASK)
    text = value.strip()
    if not text:
        raise WatsonError(_ASK)
    match = _ISSUE_URL.fullmatch(text)
    if match:
        number = int(match.group(2))
        if number < 1:
            raise WatsonError(_POSITIVE)
        return repo_name(match.group(1)), number
    if _REPO_URL.fullmatch(text):
        raise WatsonError(_REPO_PAGE)
    match = re.fullmatch(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)', text)  # owner/repo#N
    if match:
        number = int(match.group(2))
        if number < 1:
            raise WatsonError(_POSITIVE)
        return repo_name(match.group(1)), number
    match = re.fullmatch(r'#?(\d+)', text)
    if match:
        number = int(match.group(1))
        if number < 1:
            raise WatsonError(_POSITIVE)
        return None, number
    raise WatsonError(_UNKNOWN)


def resolve_issue_ref(value, config):
    """Resolve referência para (repositório, número).

    URL de issue → usa o repositório da URL (qualquer um que o token alcance).
    Só número / #N → usa config['repository'].
    """
    repo, number = parse_issue_ref(value)
    if repo is None:
        repo = config['repository']
    return repo, number


def resolve_issue_number(value, config):
    """Resolve referência para o número da issue (wrapper fino)."""
    _, number = resolve_issue_ref(value, config)
    return number


def resolve_issue_target(value, config, recent_repo=None):
    """(repo, number, via): URL or owner/repo#N name their repo; a bare #N goes to the repo
    the owner investigated most recently, else config['repository']. via says which."""
    repo, number = parse_issue_ref(value)
    if repo is not None:
        return repo, number, 'explicit'
    if recent_repo:
        return recent_repo, number, 'recent'
    return config['repository'], number, 'default'


def repo_note(repo, number, via, language, default=None):
    """Deterministic line telling the owner which repo a bare #N was resolved to.

    Only when it is not the line's default repository: on a one-repo line it is noise.
    """
    if via == 'explicit' or (default is not None and repo == default):
        return None
    if language == 'en':
        why = 'the repository we investigated last' if via == 'recent' else 'the default repository'
        return f'Looking at {repo}#{number} ({why}). If you meant another one, send the link.'
    why = 'último repositório que investigamos' if via == 'recent' else 'repositório padrão'
    return f'Olhando {repo}#{number} ({why}). Se for outro, manda o link.'
