"""Small stdio MCP bridge. Exposes inspection only, never delivery or Git writes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .analysis import PlowInference, triage
from .core import Store, WatsonError, load_config
from .github import GitHub


TOOLS = [
    {'name': 'watson_status', 'description': 'Consultar issues acompanhadas e histórico de triagens.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'watson_investigate',
     'description': 'Investigar uma issue no repositório configurado, com código e evidências atuais. '
                    'Usa a inferência do Plow. Não envia mensagens nem escreve no GitHub.',
     'inputSchema': {'type': 'object', 'properties': {'number': {'type': 'integer', 'minimum': 1}},
                     'required': ['number'], 'additionalProperties': False}},
]


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
        raise LookupError('Método não suportado.')
    params = message.get('params', {})
    name, arguments = params.get('name'), params.get('arguments', {})
    try:
        if not isinstance(arguments, dict):
            raise WatsonError('Argumentos inválidos.')
        if name == 'watson_status' and arguments:
            raise WatsonError('Status não aceita argumentos.')
        if name == 'watson_investigate':
            if (set(arguments) != {'number'} or type(arguments['number']) is not int
                    or arguments['number'] < 1):
                raise WatsonError('Forneça somente um número inteiro positivo.')
        elif name != 'watson_status':
            raise WatsonError('Ferramenta não disponível.')
        store = Store(home)
        try:
            config = load_config(Path(home))
            with store.lock():
                if name == 'watson_status':
                    result = store.history()
                else:
                    github = GitHub([config['repository']] + config['related_repositories'])
                    result = triage(store, github, PlowInference.from_config(home, config), config, arguments['number'])
            return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], 'isError': False}
        finally:
            store.db.close()
    except Exception as exc:
        # Runtime errors may include URLs or credentials; only expose curated errors.
        text = str(exc) if isinstance(exc, WatsonError) else 'Falha interna; consulte a instalação local.'
        return {'content': [{'type': 'text', 'text': text}], 'isError': True}


def serve(home, incoming=sys.stdin, outgoing=sys.stdout):
    for line in incoming:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('Requisição JSON-RPC inválida.')
            if 'id' not in message:
                continue
            response = {'jsonrpc': '2.0', 'id': message['id'], 'result': dispatch(home, message)}
        except (ValueError, LookupError) as exc:
            response = {'jsonrpc': '2.0', 'id': message.get('id') if isinstance(message, dict) else None,
                        'error': {'code': -32601 if isinstance(exc, LookupError) else -32600, 'message': str(exc)}}
        outgoing.write(json.dumps(response, ensure_ascii=False) + '\n')
        outgoing.flush()
