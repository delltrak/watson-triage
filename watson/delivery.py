from __future__ import annotations

import json
import mimetypes
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .core import WatsonError, digest


def voice(script, destination, name='Luciana'):
    """Offline macOS prototype. Linux deployments need a separate TTS adapter."""
    destination = Path(destination).resolve()
    if destination.suffix != '.m4a':
        raise WatsonError('Use um destino .m4a para o áudio.')
    if destination.exists():
        raise WatsonError('O áudio já existe; escolha outro destino.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.aiff')
    if temporary.exists():
        raise WatsonError('Já existe um arquivo temporário com esse nome.')
    try:
        subprocess.run(['say', '-v', name, '-r', '165', '-o', str(temporary)],
                       input=script, text=True, check=True, capture_output=True, timeout=120)
        subprocess.run(['afconvert', '-f', 'm4af', '-d', 'aac', str(temporary), str(destination)],
                       check=True, capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.SubprocessError):
        destination.unlink(missing_ok=True)
        raise WatsonError('Áudio indisponível: esta opção requer macOS, say e afconvert.') from None
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)


def credential_values(config):
    """The `plow-agents mint` credential file, parsed as data, or {} when the
    config names none. One owner: chat delivery and inference are the same
    credential, and a second parser beside this one would let them disagree
    about which token a local install is holding.
    """
    path = config.get('plow_credential_file')
    if not path:
        return {}
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in {'PLOW_AGENT_TOKEN', 'PLOW_API_BASE'}:
            raise WatsonError('Arquivo de credencial Plow inválido.')
        parts = shlex.split(value, comments=True)
        if len(parts) != 1:
            raise WatsonError('Valor inválido no arquivo de credencial.')
        values[key] = parts[0]
    if values.get('PLOW_API_BASE', 'https://api.plow.co').rstrip('/') != 'https://api.plow.co':
        raise WatsonError('Esta versão aceita somente a API oficial do Plow.')
    if not values.get('PLOW_AGENT_TOKEN'):
        raise WatsonError('Credencial sem token de agente.')
    return values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WatsonError('Redirecionamento recusado na entrega.')


def post_json(method, url, data=None, headers=None, timeout=45, opener=None):
    """The one HTTPS JSON transport: no redirects, no plaintext, parsed result.

    Both callers here carry the same Plow bearer to the same host, so a second
    copy of this would be two places for the redirect refusal and the scheme
    check to disagree.
    """
    if urlparse(url).scheme != 'https':
        raise WatsonError('O transporte exige HTTPS.')
    send = opener or urllib.request.build_opener(NoRedirect).open
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with send(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


class Plow:
    """Owner-DM only. No recipient/URL supplied by model output is accepted."""
    def __init__(self, token=None, request=None):
        self.token = token or os.environ.get('PLOW_AGENT_TOKEN')
        if not self.token:
            raise WatsonError('Conecte uma linha do Plow; PLOW_AGENT_TOKEN não está configurado.')
        self.base = 'https://api.plow.co'
        self.request = request or self._request

    @classmethod
    def from_config(cls, config):
        values = credential_values(config)
        return cls(token=values['PLOW_AGENT_TOKEN']) if values else cls()

    def _request(self, method, url, data=None, headers=None):
        return post_json(method, url, data, headers)

    def api(self, method, path, body=None):
        for attempt in range(3):
            try:
                return self.request(method, self.base + path,
                                    json.dumps(body).encode() if body is not None else None,
                                    {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
            except urllib.error.HTTPError as exc:
                if method != 'GET' or exc.code not in {429, 502, 503, 504} or attempt == 2:
                    raise WatsonError(f'Plow retornou HTTP {exc.code}; operação não confirmada.') from None
                time.sleep((2, 5)[attempt])

    def owner_chat(self):
        identity = self.api('GET', '/v1/agents/cloud/me')
        line = identity['line']['uid']
        matches = []
        for chat in identity['chats']:
            participants = chat.get('participants', [])
            agents = [p for p in participants if p.get('type') == 'agent']
            members = [p for p in participants if p.get('type') == 'member']
            if (chat.get('status') == 'active' and len(participants) == 2
                    and len(agents) == 1 and len(members) == 1
                    and agents[0].get('relationship') == 'self'
                    and agents[0].get('line', {}).get('uid') == line
                    and members[0].get('role') == 'owner'):
                matches.append(chat['uid'])
        if len(matches) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]+', matches[0]):
            raise WatsonError('O Plow não identificou uma conversa única com o dono da linha.')
        return matches[0]

    def send(self, chat, body, audio=None):
        payload = {'body': body}
        if audio:
            path = Path(audio)
            data = path.read_bytes()
            if len(data) > 15000000:
                raise WatsonError('Mídia maior que o limite local de 15 MB.')
            # Python/macOS may map .m4a to audio/mp4a-latm. Our files are AAC
            # in an MPEG-4 container, so declare the portable audio/mp4 type.
            content_type = ({'.m4a': 'audio/mp4', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.aac': 'audio/aac'}
                            .get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            declared = self.api('POST', f'/v1/chats/{chat}/attachments', {
                'filename': path.name, 'content_type': content_type,
                'size_bytes': len(data)})
            upload = urlparse(declared['upload_url'])
            if upload.scheme != 'https' or upload.username or upload.password:
                raise WatsonError('Endereço de upload inválido.')
            # Only the upload headers from Plow. Never forward the agent bearer.
            self.request('PUT', declared['upload_url'], data, declared['upload_headers'])
            payload['attachment_uids'] = [declared['uid']]
        result = self.api('POST', f'/v1/chats/{chat}/messages', payload)
        if not isinstance(result.get('uid'), str) or not result['uid']:
            raise WatsonError('Resposta sem confirmação de mensagem.')
        return {'message_uid': result['uid'], 'chat_uid': chat}


def deliver(store, run_id, plow, audio=None, *, audio_mode='native'):
    if audio and audio_mode != 'attachment':
        # Plow's published API exposes media attachments, but no Linq voicememo
        # operation. Never silently substitute a file for a requested voice memo.
        raise WatsonError('Mensagem de voz nativa indisponível na API publicada do Plow. '
                          'Nenhum anexo foi enviado. O Plow precisa expor o envio voicememo do Linq.')
    run = store.run(run_id)
    if run['status'] != 'complete':
        raise WatsonError('Somente uma investigação concluída pode ser enviada.')
    result = json.loads(run['result'])
    body = f'Watson · #{result["number"]}\n\n{result["summary"]}\n\n{result["issue_url"]}'
    chat = plow.owner_chat()  # Resolve from trusted identity immediately before sending.
    audio_hash = digest(Path(audio).read_bytes().hex()) if audio else None
    key = store.claim_action(run_id, 'plow_delivery', {'chat': chat, 'body': body, 'audio': audio_hash})
    try:
        receipt = plow.send(chat, body, audio)
    except Exception:
        # Includes process crashes, timeouts, malformed responses. No automatic replay.
        store.action_result(key, 'unknown', {'instruction': 'Conferir histórico do chat antes de reenviar.'})
        raise WatsonError('Entrega não confirmada. Confira o chat; Watson não tentará repetir automaticamente.') from None
    store.action_result(key, 'accepted', receipt)
    return receipt
