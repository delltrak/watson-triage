"""Small stdio MCP bridge. Inspection + chat-first OAuth connect; never delivery/Git writes."""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from .analysis import MCP_BUDGET, Crew, triage
from .capabilities import (
    capabilities_report, drop_unresolved_secrets, normalize_language, require_codex, require_github,
)
from .core import Store, WatsonError, load_config
from .issue_ref import repo_note, resolve_issue_target
from . import roster
from .language import preferred_language
from .github import GitHub
from .oauth_connect import connect_claude, connect_codex


TOOLS = [
    {'name': 'watson_status',
     'description': 'Check whether GitHub, Codex, and Claude are connected, plus tracked '
                    'issues and triage history. ALWAYS call this on ANY greeting '
                    '(oi/olá/hey/hi), status ask, or "what\'s missing" BEFORE answering, '
                    'and pass language=pt when the user wrote Portuguese (including short '
                    'openers like "oi") or language=en for English. Returns `speak_this` / `user_message` / `onboarding` '
                    '(same ready-to-send copy — your entire reply MUST be exactly '
                    'speak_this, character-for-character; no paraphrase or added setup), '
                    'plus `do_not_invent: true` and `instruction`. NEVER invent connection '
                    'status; only report fields from this JSON. Optional language: en, pt, or auto.',
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
                    'Accepts number (e.g. 12) or #12 (the repository investigated most recently, in the last '
                    '24h, else the configured default), owner/repo#12, or a full GitHub issue URL '
                    '(e.g. https://github.com/owner/project/issues/12) — that URL\'s repository '
                    'is investigated, not only the default. '
                    'Repo homepage URLs without /issues/N are rejected with a clear ask for the issue link. '
                    'If GitHub is not connected, returns a short not-connected note (never a token/setup '
                    'tutorial) instead of pretending. '
                    'Reads the issue timeline: when a linked PR already delivers the issue (or is still open), '
                    'the result has speak_first — start your reply with speak_first exactly, then summarize '
                    'the rest in the same language. Always pass language (en or pt) matching the user; the '
                    'investigation text comes back in that language. '
                    'May use the Codex subscription. Does not send messages or write to GitHub '
                    '(Watson suggests closing an issue; it never closes it).',
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
                 'description': 'Language of the investigation text and messages: en or pt '
                                '(mirror the user).',
             },
         },
         'additionalProperties': False,
     }},
    {'name': 'watson_squad',
     'description': 'Show Watson\'s squad (tropa): which team/model/effort plays each role — file '
                    'selector, investigator, reviewer — and who is logged in. Team Codex and Team Claude '
                    'check each other. Returns speak_this: relay it exactly. You cannot change the squad: '
                    'the owner changes it with a short phrase such as "coloca o revisor no opus", '
                    '"investigador no sol high", "tropa padrão" (EN: "put the reviewer on opus", '
                    '"default squad"); tell them that phrase.',
     'inputSchema': {
         'type': 'object',
         'properties': {
             'language': {'type': 'string', 'description': 'Reply language: en or pt (mirror the user).'},
         },
         'additionalProperties': False,
     }},
    {'name': 'watson_connect_codex',
     'description': 'Start Codex device-code login and return a clickable auth URL '
                    '(+ one-time code) for the user to open on their phone. Use when '
                    'the user asks to connect/login Codex. Relays over iMessage — '
                    'paste auth_url plainly with https visible. A background waiter '
                    'will proactively ping the chat when login completes — user does '
                    'not need to say pronto/ready. Safe to call twice (resumes or '
                    'restarts). Pass cancel=true to abort. Never invent success; '
                    'confirm later with watson_status or the auto ping.',
     'inputSchema': {
         'type': 'object',
         'properties': {
             'language': {
                 'type': 'string',
                 'description': 'Reply language: en, pt, or auto.',
             },
             'restart': {
                 'type': 'boolean',
                 'description': 'Force a new login even if one is already pending.',
             },
             'cancel': {
                 'type': 'boolean',
                 'description': 'Cancel any pending Codex login.',
             },
         },
         'additionalProperties': False,
     }},
    {'name': 'watson_connect_claude',
     'description': 'Start Claude Code remote browser login and return a clickable '
                    'auth URL for iMessage. After the user signs in, they paste the '
                    'browser code back in chat — call again with code= that value to '
                    'finish. A background waiter pings the chat when auth completes. '
                    'Paste auth_url plainly (https visible). Pass cancel=true to abort. '
                    'Never invent success; confirm with watson_status or the auto ping.',
     'inputSchema': {
         'type': 'object',
         'properties': {
             'language': {
                 'type': 'string',
                 'description': 'Reply language: en, pt, or auto.',
             },
             'code': {
                 'type': 'string',
                 'description': 'Paste-code from the Claude browser page to finish login.',
             },
             'restart': {
                 'type': 'boolean',
                 'description': 'Force a new login even if one is already pending.',
             },
             'cancel': {
                 'type': 'boolean',
                 'description': 'Cancel any pending Claude login.',
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
    caps = capabilities_report(language=language, squad=roster.load(store.home))
    history = store.history()
    result = {**history, 'capabilities': caps}
    # Honest top-level flags so agents never invent "all good".
    result['github_connected'] = caps['github']['connected']
    result['ready_to_investigate'] = caps['ready_to_investigate']
    result['status_summary'] = caps['summary']
    # Ready-to-send copy — relay speak_this, do not invent connection facts.
    speak = caps.get('speak_this') or caps.get('user_message') or caps.get('onboarding')
    if speak is not None:
        result['speak_this'] = speak
        result['user_message'] = speak
        result['onboarding'] = caps.get('onboarding', speak)
    elif 'onboarding' in caps:
        result['onboarding'] = caps['onboarding']
        result['speak_this'] = caps['onboarding']
        result['user_message'] = caps['onboarding']
    if 'setup' in caps:
        result['setup'] = caps['setup']
    result['do_not_invent'] = bool(caps.get('do_not_invent', True))
    result['instruction'] = caps.get(
        'instruction',
        'Your entire reply MUST be exactly speak_this, character-for-character. '
        'No paraphrase, no added setup steps, no reordering. '
        'Do not invent connection facts.',
    )
    return result


def _investigate_result(home, store, config, raw, language):
    # Validate the issue ref first so a repo homepage asks for /issues/N
    # instead of being overshadowed by GitHub/Codex preflight messages.
    started = time.monotonic()
    repo, number, via = resolve_issue_target(raw, config, recent_repo=store.recent_repo())
    # Explicit language (the chat mirrors the owner) > remembered owner language > line default.
    language = language or preferred_language(home, config)
    require_github(language=language)
    # The squad decides who investigates and who reviews; a team without login is covered by the other.
    effective = roster.resolve(roster.load(home), roster.connected_teams())
    if any(effective[r].get('unavailable') for r in ('selector', 'investigator')):
        require_codex(language=language)  # raises the "connect Codex here in chat" message
    github = GitHub([repo] + config['related_repositories'] + [config['repository']])
    crew = Crew(home, effective, deadline=started + MCP_BUDGET)  # one budget for the whole call
    out = triage(store, github, crew, config, number, repo=repo, language=language)
    action = out['result'].get('issue_action') or {}
    rules = []
    note = repo_note(repo, number, via, language, default=config['repository'])
    lead = None
    if action.get('lead') and action.get('say'):
        lead = action['say'].get(language) or action['say']['pt']
    if lead:
        # Deterministic lead (like speak_this): the chat model must not paraphrase it away.
        # repo_note goes inside it, so the reply has exactly one "start with" rule.
        out['speak_first'] = f'{note}\n\n{lead}' if note else lead
        rules.append('Start your reply with speak_first exactly, character-for-character, then summarize '
                     'the rest in the same language. Watson never closes issues and never opens a second '
                     'draft PR when a linked PR is already open.')
    elif note:
        out['repo_note'] = note
        rules.append('Start your reply with repo_note exactly, as its own first line.')
    review = out['result'].get('review') or {}
    if isinstance(review.get('note'), dict):
        out['review_note'] = review['note'].get(language) or review['note']['pt']
        # The chat sees the reviewed findings, never the rejected claims or the reviewer's free text.
        chat = dict(out['result'])
        chat['review'] = {k: v for k, v in review.items() if k not in ('removed', 'notes')}
        chat['review']['removed_count'] = len(review.get('removed') or [])
        if review.get('status') == 'done' and (review.get('removed') or review.get('weak')):
            for key in ('summary', 'voice_script', 'next_steps'):
                chat.pop(key, None)
            rules.append('The cross-team review removed or downgraded findings: describe ONLY result.findings '
                         '(say it is a hypothesis when certainty is hypothesis) and state no other claim about '
                         'the issue.')
        out['result'] = chat
        rules.append('End your reply with review_note exactly, as its own last line.')
    if rules:
        out['instruction'] = ' '.join(rules)
    return out


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
        elif name == 'watson_squad':
            extra = set(arguments) - {'language'}
            if extra:
                raise WatsonError('Squad only accepts optional "language".')
            raw = None
        elif name == 'watson_connect_codex':
            extra = set(arguments) - {'language', 'restart', 'cancel'}
            if extra:
                raise WatsonError('Connect Codex only accepts language, restart, cancel.')
            raw = None
        elif name == 'watson_connect_claude':
            extra = set(arguments) - {'language', 'code', 'restart', 'cancel'}
            if extra:
                raise WatsonError('Connect Claude only accepts language, code, restart, cancel.')
            raw = None
        else:
            raise WatsonError('Tool not available.')
        store = Store(home)
        try:
            config = load_config(Path(home))
            # Explicit language (the chat mirrors the owner) > remembered owner language > line default.
            language = language or preferred_language(home, config)
            if name == 'watson_status':
                # Read-only: do not take the exclusive worker lock.
                result = _status_result(store, language)
            elif name == 'watson_squad':
                speak = roster.render(roster.load(home), roster.connected_teams(), language)
                result = {'speak_this': speak, 'user_message': speak,
                          'instruction': 'Your entire reply MUST be exactly speak_this.'}
            elif name == 'watson_connect_codex':
                result = connect_codex(
                    home,
                    language=language,
                    restart=bool(arguments.get('restart')),
                    cancel=bool(arguments.get('cancel')),
                )
            elif name == 'watson_connect_claude':
                result = connect_claude(
                    home,
                    language=language,
                    code=arguments.get('code'),
                    restart=bool(arguments.get('restart')),
                    cancel=bool(arguments.get('cancel')),
                )
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
    # Unset mcp_servers env vars arrive as a literal `${VAR}`; treat them as absent.
    drop_unresolved_secrets()
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
