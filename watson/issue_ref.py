"""Parse GitHub issue numbers and URLs for chat/MCP/CLI."""
from __future__ import annotations

import re

from .core import WatsonError, repo_name


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


def parse_issue_ref(value):
    """Aceita número, '#123' ou URL de issue do GitHub.

    Retorna (repositório_ou_None, número). O repositório só vem preenchido
    quando a entrada foi uma URL de issue.
    """
    if isinstance(value, bool):
        raise WatsonError('Me diga o número da issue ou cole o link do GitHub.')
    if isinstance(value, int):
        if value < 1:
            raise WatsonError('O número da issue precisa ser maior que zero.')
        return None, value
    if not isinstance(value, str):
        raise WatsonError('Me diga o número da issue ou cole o link do GitHub.')
    text = value.strip()
    if not text:
        raise WatsonError('Me diga o número da issue ou cole o link do GitHub.')
    match = _ISSUE_URL.fullmatch(text)
    if match:
        number = int(match.group(2))
        if number < 1:
            raise WatsonError('O número da issue precisa ser maior que zero.')
        return repo_name(match.group(1)), number
    if _REPO_URL.fullmatch(text):
        raise WatsonError(
            'Não entendi qual issue você quer. Cole o link completo da issue '
            '(ex.: https://github.com/dono/projeto/issues/12), '
            'não só a página do repositório.')
    match = re.fullmatch(r'#?(\d+)', text)
    if match:
        number = int(match.group(1))
        if number < 1:
            raise WatsonError('O número da issue precisa ser maior que zero.')
        return None, number
    raise WatsonError(
        'Não entendi qual issue você quer. Cole o link '
        '(ex.: https://github.com/dono/projeto/issues/12) ou só o número.')


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
