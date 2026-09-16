"""ChatGPT Read Aloud, adapted from DecaNode/internal/provider/read_aloud.go.

This is the internal subscription endpoint used by Deca, not the public TTS API.
It may change or ignore the requested voice. Credentials stay in Codex's store.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from .core import WatsonError
from .delivery import NoRedirect, voice as local_voice

MAX_AUDIO = 15_000_000


def codex_credentials():
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    try:
        tokens = json.loads((home / 'auth.json').read_text())['tokens']
        token = tokens['access_token']
        account = tokens.get('account_id')
        if not isinstance(token, str) or not token:
            raise ValueError()
        if account is not None and not isinstance(account, str):
            raise ValueError()
        return token, account
    except (OSError, ValueError, KeyError, TypeError):
        raise WatsonError('A voz requer a sessão ChatGPT do Codex em auth.json. Faça login no Codex.') from None


def read_aloud(text, destination, voice='sol', *, credentials=codex_credentials, opener=None):
    text = text.strip()
    if not text or len(text.encode()) > 128 * 1024:
        raise WatsonError('Texto de voz vazio ou grande demais.')
    if voice not in {'sol', 'cove', 'juniper', 'maple', 'spruce', 'ember', 'vale', 'breeze', 'arbor', 'rio', 'viola'}:
        raise WatsonError('Voz ChatGPT desconhecida.')
    destination = Path(destination).resolve()
    if destination.suffix.lower() != '.mp3':
        raise WatsonError('Use um destino .mp3 para a voz do ChatGPT.')
    if destination.exists():
        raise WatsonError('O áudio já existe; escolha outro destino.')
    token, account = credentials()
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
               'Accept': 'audio/mpeg', 'User-Agent': 'Watson/0.1', 'originator': 'codex_ios'}
    if account:
        headers['ChatGPT-Account-ID'] = account
    payload = {'pronunciation_audio_text': text, 'pronunciation_language': 'pt', 'speed': 1, 'text': text}
    url = 'https://chatgpt.com/backend-api/pronunciation/synthesize?' + urlencode({'format': 'mp3', 'voice': voice})
    request = urllib.request.Request(url, json.dumps(payload).encode(), headers, method='POST')
    opener = opener or urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=120) as response:
            if response.headers.get('cf-mitigated') == 'challenge':
                raise WatsonError('O ChatGPT exigiu uma verificação de acesso para gerar voz.')
            mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            data = response.read(MAX_AUDIO + 1)
    except urllib.error.HTTPError as exc:
        raise WatsonError(f'A leitura do ChatGPT retornou HTTP {exc.code}. Verifique sua sessão do Codex.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise WatsonError('O serviço de leitura do ChatGPT não respondeu.') from None
    # Reject HTML/JSON errors even if a proxy labels the response as audio.
    mp3 = data.startswith(b'ID3') or (len(data) > 1 and data[0] == 255 and data[1] & 224 == 224)
    if not data or len(data) > MAX_AUDIO or mime not in {'audio/mpeg', 'audio/mp3'} or not mp3:
        raise WatsonError('O ChatGPT não devolveu um MP3 válido dentro do limite de 15 MB.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids overwriting an existing recording.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return str(destination)


def generate_voice(text, destination, config, *, provider=None):
    selected = provider or config.get('speech_provider', 'chatgpt')
    if selected == 'chatgpt':
        return read_aloud(text, destination, config.get('speech_voice', 'sol'))
    if selected == 'macos':
        return local_voice(text, destination)
    raise WatsonError('Provedor de voz desconhecido. Use chatgpt ou macos.')
