"""Small stdio MCP bridge. Exposes inspection only, never delivery or Git writes."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from .analysis import Codex, triage
from .capabilities import capabilities_report, normalize_language, require_codex, require_github
from .core import Store, WatsonError, load_config
from .issue_ref import resolve_issue_ref
from .github import GitHub


TOOLS = [
    {'name': 'watson_status',
     'description': 'Check whether GitHub, Codex, and Claude are connected, plus tracked '
                    'issues and triage history. ALWAYS call this on the first user greeting '
                    '(oi/olá/hey/hi) BEFORE answering, and pass language=pt when the user '
                    'wrote Portuguese (including short openers like "oi"). Returns '
                    '`onboarding` — ready-to-send first-greeting copy to relay (do not invent '
                    'a "Plow assistant" pitch or an owner name). '
                    'Optional language: en, pt, or auto.',
     'inputSchema': {
         'type': 'object',
         'properties': {
             'language': {
                 'type': 'string',
                 'description': 'Reply language for status/setup text: en, pt, or auto.',
             },
         },
         'additionalProperties': False,
     }},
    {'name': 'watson_investigate',
     'description': 'Investigate a GitHub issue with current code and evidence. '
                    'Accepts number (e.g. 12), #12 (uses the configured default repository), '
                    'or a full GitHub issue URL '
                    '(e.g. https://github.com/owner/project/issues/12) — that URL\'s repository '
                    'is investigated, not only the default. '
                    'Repo homepage URLs without /issues/N are rejected with a clear ask for the issue link. '
                    'If GitHub is not connected, returns clear setup instructions instead of pretending. '
                    'May use the Codex subscription. Does not send messages or write to GitHub.',
     'inputSchema': {
         'type': 'object',
         'properties': {
             'issue': {
                 'type': 'string',
                 'description': 'Issue link, #123, or number as text.',
             },
             'number': {
                 'type': 'integer',
                 'minimum': 1,
                 'description': 'Issue number (alternative to issue).',
             },
             'language': {
                 'type': 'string',
                 'description': 'Language for error/setup messages: en, pt, or auto.',
             },
         },
         'additionalProperties': False,
     }},
]


def _coerce_arguments(arguments):
    """Hermes sometimes passes tool arguments as a JSON string."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            loaded = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise WatsonError('Invalid arguments.') from exc
        if not isinstance(loaded, dict):
            raise WatsonError('Invalid arguments.')
        return loaded
    raise WatsonError('Invalid arguments.')


def _issue_argument(arguments):
    keys = set(arguments) - {'language'}
    if keys == {'number'}:
        return arguments['number']
    if keys == {'issue'}:
        return arguments['issue']
    if keys == {'issue', 'number'}:
        raise WatsonError('Send only the link/number in "issue" or only "number", not both.')
    if not keys:
        raise WatsonError('Tell me the issue number or paste the GitHub link.')
    raise WatsonError('To investigate, use only "issue" (link or number) or only "number".')


def _language_argument(arguments):
    if 'language' not in arguments:
        return None
    return normalize_language(arguments.get('language'))


def _status_result(store, language):
    caps = capabilities_report(language=language)
    history = store.history()
    result = {**history, 'capabilities': caps}
    # Honest top-level flags so agents never invent "all good".
    result['github_connected'] = caps['github']['connected']
    result['ready_to_investigate'] = caps['ready_to_investigate']
    result['status_summary'] = caps['summary']
    # Ready-to-send first greeting — relay this, do not invent a Plow pitch.
    if 'onboarding' in caps:
        result['onboarding'] = caps['onboarding']
    if 'setup' in caps:
        result['setup'] = caps['setup']
    return result


def _investigate_result(home, store, config, raw, language):
    # Validate the issue ref first so a repo homepage asks for /issues/N
    # instead of being overshadowed by GitHub/Codex preflight messages.
    repo, number = resolve_issue_ref(raw, config)
    require_github(language=language)
    require_codex(language=language)
    github = GitHub([repo] + config['related_repositories'] + [config['repository']])
    return triage(store, github, Codex(home, config.get('model')), config, number, repo=repo)


def dispatch(home, message):
    method = message.get('method')
    if method == 'initialize':
        supported = {'2024-11-05', '2025-03-26', '2025-06-18'}
        requested = message.get('params', {}).get('protocolVersion')
        return {'protocolVersion': requested if requested in supported else '2025-06-18',
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'watson-triage', 'version': '0.1.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method != 'tools/call':
        raise LookupError('Method not supported.')
    params = message.get('params', {})
    name, arguments = params.get('name'), params.get('arguments', {})
    try:
        arguments = _coerce_arguments(arguments)
        language = _language_argument(arguments)
        if name == 'watson_status':
            extra = set(arguments) - {'language'}
            if extra:
                raise WatsonError('Status only accepts optional "language".')
            raw = None
        elif name == 'watson_investigate':
            raw = _issue_argument(arguments)
        else:
            raise WatsonError('Tool not available.')
        store = Store(home)
        try:
            config = load_config(Path(home))
            if name == 'watson_status':
                # Read-only: do not take the exclusive worker lock.
                result = _status_result(store, language)
            else:
                with store.lock():
                    result = _investigate_result(home, store, config, raw, language)
            return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], 'isError': False}
        finally:
            store.db.close()
    except WatsonError as exc:
        return {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
    except Exception as exc:
        # Runtime errors may include URLs or credentials; only expose type name.
        print(traceback.format_exc(), file=sys.stderr, flush=True)
        text = f'Internal failure ({type(exc).__name__}). Check the local install.'
        return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def serve(home, incoming=sys.stdin, outgoing=sys.stdout):
    for line in incoming:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('Invalid JSON-RPC request.')
            if 'id' not in message:
                continue
            response = {'jsonrpc': '2.0', 'id': message['id'], 'result': dispatch(home, message)}
        except (ValueError, LookupError) as exc:
            response = {'jsonrpc': '2.0', 'id': message.get('id') if isinstance(message, dict) else None,
                        'error': {'code': -32601 if isinstance(exc, LookupError) else -32600, 'message': str(exc)}}
        outgoing.write(json.dumps(response, ensure_ascii=False) + '\n')
        outgoing.flush()
