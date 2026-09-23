"""Chat-first OAuth / device-login for Codex and Claude Code.

Starts the CLI login in the background, captures a browser URL (+ optional
user code), and returns them for iMessage. Never returns tokens or secrets.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .capabilities import check_claude, check_codex, message_for, normalize_language
from .core import WatsonError

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_URL_RE = re.compile(r'https://[^\s<>\'\"\]\)]+')
# Codex device codes look like ABCD-EFGHI / TRWW-KJ1JA
_CODEX_USER_CODE_RE = re.compile(r'\b([A-Z0-9]{4,5}-[A-Z0-9]{4,6})\b')

PROVIDERS = ('codex', 'claude')
DEFAULT_TIMEOUT_SEC = 15 * 60
URL_WAIT_SEC = 45


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text or '')


def _oauth_dir(home: Path) -> Path:
    path = Path(home) / 'oauth'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _state_path(home: Path, provider: str) -> Path:
    return _oauth_dir(home) / f'{provider}.json'


def _log_path(home: Path, provider: str) -> Path:
    return _oauth_dir(home) / f'{provider}.log'


def _pid_path(home: Path, provider: str) -> Path:
    return _oauth_dir(home) / f'{provider}.pid'


def _load_state(home: Path, provider: str) -> dict | None:
    path = _state_path(home, provider)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_state(home: Path, provider: str, state: dict) -> None:
    path = _state_path(home, provider)
    tmp = path.with_suffix('.tmp')
    # Never persist tokens — only URL / user_code / status metadata.
    safe = {
        'provider': provider,
        'pid': state.get('pid'),
        'started_at': state.get('started_at'),
        'status': state.get('status'),
        'auth_url': state.get('auth_url'),
        'user_code': state.get('user_code'),
        'needs_paste_code': bool(state.get('needs_paste_code')),
        'log': str(_log_path(home, provider)),
        'error': state.get('error'),
    }
    tmp.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + '\n')
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_pid(pid: int | None) -> None:
    if not pid or pid <= 0:
        return
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _clear_session(home: Path, provider: str) -> None:
    state = _load_state(home, provider)
    if state:
        _terminate_pid(state.get('pid'))
    holder = _oauth_dir(home) / f'{provider}.stdin_holder.pid'
    try:
        holder_pid = int(holder.read_text().strip())
    except (OSError, ValueError):
        holder_pid = None
    _terminate_pid(holder_pid)
    for path in (
        _state_path(home, provider),
        _pid_path(home, provider),
        holder,
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _read_log(home: Path, provider: str) -> str:
    path = _log_path(home, provider)
    try:
        return _strip_ansi(path.read_text(errors='replace'))
    except OSError:
        return ''


def _parse_codex(log_text: str) -> tuple[str | None, str | None]:
    text = _strip_ansi(log_text)
    url = None
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip('.,;:)')
        if 'openai.com' in candidate or 'auth.openai' in candidate or 'codex' in candidate:
            url = candidate
            break
    if url is None:
        m = _URL_RE.search(text)
        if m:
            url = m.group(0).rstrip('.,;:)')
    user_code = None
    # Prefer code near "one-time" / "Enter this"
    lower = text.lower()
    anchor = -1
    for marker in ('one-time code', 'enter this', 'device code'):
        idx = lower.find(marker)
        if idx >= 0:
            anchor = idx
            break
    search = text[anchor:] if anchor >= 0 else text
    m = _CODEX_USER_CODE_RE.search(search)
    if m:
        user_code = m.group(1)
    return url, user_code


def _parse_claude(log_text: str) -> str | None:
    text = _strip_ansi(log_text)
    for match in _URL_RE.finditer(text):
        candidate = match.group(0).rstrip('.,;:)')
        if 'claude.com' in candidate or 'anthropic.com' in candidate:
            return candidate
    m = _URL_RE.search(text)
    return m.group(0).rstrip('.,;:)') if m else None


def _start_process(home: Path, provider: str, cmd: list[str]) -> subprocess.Popen:
    log = _log_path(home, provider)
    try:
        log.write_text('')
        os.chmod(log, 0o600)
    except OSError:
        pass
    # Detach from MCP stdio; keep stdin pipe for Claude code paste.
    with open(log, 'ab', buffering=0) as log_fh:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
    # Keep stdin fd alive via /proc when possible; store pid only.
    # For Claude we reopen stdin through /proc/<pid>/fd/0 later.
    _pid_path(home, provider).write_text(str(proc.pid) + '\n')
    os.chmod(_pid_path(home, provider), 0o600)
    # Do not close proc.stdin here for Claude — caller may need it.
    # Store the Popen object... we can't across processes. Use /proc fd.
    # Close our handle but leave the child's stdin open by duping to a fifo.
    return proc


def _wait_for_url(home: Path, provider: str, parse_fn, timeout: float = URL_WAIT_SEC):
    deadline = time.time() + timeout
    url = user_code = None
    while time.time() < deadline:
        log = _read_log(home, provider)
        if provider == 'codex':
            url, user_code = parse_fn(log)
        else:
            url = parse_fn(log)
            user_code = None
        if url:
            return url, user_code
        state = _load_state(home, provider)
        pid = state.get('pid') if state else None
        if pid and not _pid_alive(pid):
            break
        time.sleep(0.4)
    return url, user_code


_ALREADY = {
    'codex': {
        'en': 'Codex is already connected. Ask me for status anytime.',
        'pt': 'O Codex já está conectado. Pode pedir o status quando quiser.',
    },
    'claude': {
        'en': 'Claude Code is already connected. Ask me for status anytime.',
        'pt': 'O Claude Code já está conectado. Pode pedir o status quando quiser.',
    },
}

_CLI_MISSING = {
    'codex': {
        'en': 'Codex CLI is not available in this Watson install.',
        'pt': 'O Codex CLI não está disponível nesta instalação do Watson.',
    },
    'claude': {
        'en': 'Claude Code CLI is not available in this Watson install.',
        'pt': 'O Claude Code CLI não está disponível nesta instalação do Watson.',
    },
}

_URL_TIMEOUT = {
    'codex': {
        'en': 'Codex login started but no auth link appeared in time. Try again in a moment.',
        'pt': 'O login do Codex começou, mas o link não apareceu a tempo. Tente de novo em instantes.',
    },
    'claude': {
        'en': 'Claude login started but no auth link appeared in time. Try again in a moment.',
        'pt': 'O login do Claude começou, mas o link não apareceu a tempo. Tente de novo em instantes.',
    },
}


def _message_waiting_codex(url: str, user_code: str | None, language) -> str:
    mapping = {
        'en': (
            'Open this link on your phone or computer to connect Codex:\n'
            f'\n{url}\n'
            + (f'\nThen enter this one-time code:\n\n{user_code}\n' if user_code else '\n')
            + '\nWhen you finish in the browser, tell me and I will check status '
            '(or just ask "status"). Do not share tokens — only use the link/code above.'
        ),
        'pt': (
            'Abre este link no celular ou no computador pra conectar o Codex:\n'
            f'\n{url}\n'
            + (f'\nDepois digita este código de uso único:\n\n{user_code}\n' if user_code else '\n')
            + '\nQuando terminar no navegador, me avisa que eu confiro o status '
            '(ou manda "status"). Não compartilhe tokens — use só o link/código acima.'
        ),
    }
    return message_for(mapping, language)


def _message_waiting_claude(url: str, language) -> str:
    mapping = {
        'en': (
            'Open this link on your phone or computer to connect Claude Code:\n'
            f'\n{url}\n'
            '\nAfter you sign in, the page shows a code. Paste that code back '
            'here in chat and I will finish the connection. '
            'Do not share passwords or API keys.'
        ),
        'pt': (
            'Abre este link no celular ou no computador pra conectar o Claude Code:\n'
            f'\n{url}\n'
            '\nDepois de entrar, a página mostra um código. Cola esse código '
            'aqui no chat que eu termino a conexão. '
            'Não compartilhe senhas nem API keys.'
        ),
    }
    return message_for(mapping, language)


def _message_code_accepted(language) -> str:
    return message_for({
        'en': 'Got the code — finishing Claude login. I will confirm when watson_status shows connected.',
        'pt': 'Recebi o código — finalizando o login do Claude. Confirmo quando o status mostrar conectado.',
    }, language)


def _message_cancelled(provider: str, language) -> str:
    name = 'Codex' if provider == 'codex' else 'Claude Code'
    return message_for({
        'en': f'Cancelled the pending {name} login.',
        'pt': f'Cancelei o login pendente do {name}.',
    }, language)


def _refresh_session_status(home: Path, provider: str, state: dict) -> dict:
    """Update state if the login process already finished."""
    pid = state.get('pid')
    if state.get('status') in {'completed', 'failed', 'cancelled'}:
        return state
    if not _pid_alive(pid):
        # Process ended — check auth.
        if provider == 'codex':
            auth = check_codex()
        else:
            auth = check_claude()
        if auth.get('ok'):
            state['status'] = 'completed'
        else:
            state['status'] = 'failed'
            state['error'] = 'login_process_exited'
        _save_state(home, provider, state)
    return state


def _public_result(provider: str, state: dict, language, *, already=False, message=None) -> dict:
    result = {
        'provider': provider,
        'already_authenticated': bool(already),
        'status': 'authenticated' if already else state.get('status'),
        'auth_url': state.get('auth_url'),
        'user_code': state.get('user_code'),
        'needs_paste_code': bool(state.get('needs_paste_code')),
        'message': message,
    }
    # Drop empty optional fields for cleaner chat relay.
    if not result['auth_url']:
        result.pop('auth_url', None)
    if not result['user_code']:
        result.pop('user_code', None)
    if not result['needs_paste_code']:
        result.pop('needs_paste_code', None)
    return result


def connect_codex(home, language=None, *, restart=False, cancel=False):
    """Start or resume Codex device-code login. Returns chat-safe dict."""
    language = normalize_language(language) if language not in (None, '') else language
    home = Path(home)
    auth = check_codex()
    if auth.get('ok') and not restart and not cancel:
        return _public_result(
            'codex', {}, language, already=True,
            message=message_for(_ALREADY['codex'], language))

    if cancel:
        _clear_session(home, 'codex')
        return {
            'provider': 'codex',
            'already_authenticated': False,
            'status': 'cancelled',
            'message': _message_cancelled('codex', language),
        }

    if not auth.get('ok') and auth.get('reason') == 'cli_missing':
        raise WatsonError(message_for(_CLI_MISSING['codex'], language))

    existing = _load_state(home, 'codex')
    if existing and not restart:
        existing = _refresh_session_status(home, 'codex', existing)
        if existing.get('status') == 'completed' or check_codex().get('ok'):
            _clear_session(home, 'codex')
            return _public_result(
                'codex', {}, language, already=True,
                message=message_for(_ALREADY['codex'], language))
        if (
            existing.get('status') in {'waiting_browser', 'pending'}
            and existing.get('auth_url')
            and _pid_alive(existing.get('pid'))
        ):
            msg = _message_waiting_codex(
                existing['auth_url'], existing.get('user_code'), language)
            return _public_result('codex', existing, language, message=msg)

    _clear_session(home, 'codex')
    proc = _start_process(home, 'codex', ['codex', 'login', '--device-auth'])
    # Close stdin — device flow does not need it.
    try:
        if proc.stdin:
            proc.stdin.close()
    except OSError:
        pass
    state = {
        'pid': proc.pid,
        'started_at': time.time(),
        'status': 'pending',
        'auth_url': None,
        'user_code': None,
        'needs_paste_code': False,
    }
    _save_state(home, 'codex', state)

    url, user_code = _wait_for_url(home, 'codex', _parse_codex)
    if not url:
        _clear_session(home, 'codex')
        raise WatsonError(message_for(_URL_TIMEOUT['codex'], language))

    state.update({
        'auth_url': url,
        'user_code': user_code,
        'status': 'waiting_browser',
    })
    _save_state(home, 'codex', state)
    msg = _message_waiting_codex(url, user_code, language)
    return _public_result('codex', state, language, message=msg)


def _write_code_to_claude(pid: int, code: str) -> None:
    """Feed the browser paste-code into the waiting `claude auth login` stdin."""
    code = (code or '').strip()
    if not code or len(code) > 500:
        raise WatsonError(message_for({
            'en': 'That does not look like a valid Claude login code. Paste the code from the browser page.',
            'pt': 'Isso não parece um código válido do Claude. Cola o código que apareceu na página do navegador.',
        }, None))
    # Reject obvious secrets / tokens accidentally pasted.
    lower = code.lower()
    if lower.startswith('sk-') or 'api_key' in lower or '=' in code:
        raise WatsonError(message_for({
            'en': 'Please paste only the short login code from the browser — not an API key.',
            'pt': 'Cola só o código curto de login do navegador — não uma API key.',
        }, None))
    fd_path = f'/proc/{pid}/fd/0'
    if not os.path.exists(fd_path):
        raise WatsonError(message_for({
            'en': 'The Claude login session expired. Ask me to connect Claude again for a new link.',
            'pt': 'A sessão de login do Claude expirou. Peça pra conectar o Claude de novo pra eu mandar um link novo.',
        }, None))
    payload = (code.strip() + '\n').encode()
    with open(fd_path, 'wb', buffering=0) as stdin_fh:
        stdin_fh.write(payload)


def connect_claude(home, language=None, *, code=None, restart=False, cancel=False):
    """Start or resume Claude remote OAuth login; optional code completes it."""
    language = normalize_language(language) if language not in (None, '') else language
    home = Path(home)
    auth = check_claude()
    if auth.get('ok') and not restart and not cancel and not code:
        return _public_result(
            'claude', {}, language, already=True,
            message=message_for(_ALREADY['claude'], language))

    if cancel:
        _clear_session(home, 'claude')
        return {
            'provider': 'claude',
            'already_authenticated': False,
            'status': 'cancelled',
            'message': _message_cancelled('claude', language),
        }

    if not auth.get('ok') and auth.get('reason') == 'cli_missing':
        raise WatsonError(message_for(_CLI_MISSING['claude'], language))

    existing = _load_state(home, 'claude')

    if code:
        if not existing or not _pid_alive(existing.get('pid')):
            raise WatsonError(message_for({
                'en': 'No Claude login is waiting for a code. Ask me to connect Claude first — I will send a link.',
                'pt': 'Não há login do Claude esperando código. Peça pra conectar o Claude primeiro — eu mando o link.',
            }, language))
        _write_code_to_claude(int(existing['pid']), code)
        existing['status'] = 'code_submitted'
        existing['needs_paste_code'] = False
        _save_state(home, 'claude', existing)
        # Brief wait for CLI to finish exchanging the code.
        deadline = time.time() + 25
        while time.time() < deadline:
            if check_claude().get('ok'):
                existing['status'] = 'completed'
                _save_state(home, 'claude', existing)
                return _public_result(
                    'claude', {}, language, already=True,
                    message=message_for(_ALREADY['claude'], language))
            if not _pid_alive(existing.get('pid')):
                break
            time.sleep(0.5)
        existing = _refresh_session_status(home, 'claude', existing)
        if check_claude().get('ok'):
            return _public_result(
                'claude', {}, language, already=True,
                message=message_for(_ALREADY['claude'], language))
        return _public_result(
            'claude', existing, language,
            message=_message_code_accepted(language))

    if existing and not restart:
        existing = _refresh_session_status(home, 'claude', existing)
        if existing.get('status') == 'completed' or check_claude().get('ok'):
            _clear_session(home, 'claude')
            return _public_result(
                'claude', {}, language, already=True,
                message=message_for(_ALREADY['claude'], language))
        if (
            existing.get('status') in {'waiting_browser', 'waiting_code', 'pending'}
            and existing.get('auth_url')
            and _pid_alive(existing.get('pid'))
        ):
            msg = _message_waiting_claude(existing['auth_url'], language)
            return _public_result('claude', existing, language, message=msg)

    _clear_session(home, 'claude')
    proc = _start_process(
        home, 'claude', ['claude', 'auth', 'login', '--claudeai'])
    state = {
        'pid': proc.pid,
        'started_at': time.time(),
        'status': 'pending',
        'auth_url': None,
        'user_code': None,
        'needs_paste_code': True,
    }
    _save_state(home, 'claude', state)

    # Keep stdin write-end alive in a holder process for later code paste.
    _spawn_stdin_holder(home, 'claude', proc)

    url, _ = _wait_for_url(home, 'claude', _parse_claude)
    if not url:
        _clear_session(home, 'claude')
        raise WatsonError(message_for(_URL_TIMEOUT['claude'], language))

    state.update({
        'auth_url': url,
        'status': 'waiting_code',
        'needs_paste_code': True,
    })
    _save_state(home, 'claude', state)
    msg = _message_waiting_claude(url, language)
    return _public_result('claude', state, language, message=msg)


def _spawn_stdin_holder(home: Path, provider: str, proc: subprocess.Popen) -> None:
    """Keep login stdin open after the MCP tool call returns.

    Transfers the write-end to a short Python sleeper so Claude can still
    receive a pasted code later via /proc/<pid>/fd/0.
    """
    if proc.stdin is None:
        return
    holder = _oauth_dir(home) / f'{provider}.stdin_holder.pid'
    holder_proc = subprocess.Popen(
        [
            'python3', '-c',
            'import os, time, sys\n'
            'pid = int(sys.argv[1])\n'
            'while True:\n'
            '    try:\n'
            '        os.kill(pid, 0)\n'
            '    except ProcessLookupError:\n'
            '        break\n'
            '    time.sleep(2)\n',
            str(proc.pid),
        ],
        stdin=proc.stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        holder.write_text(str(holder_proc.pid) + '\n')
        os.chmod(holder, 0o600)
    except OSError:
        pass
    try:
        proc.stdin.close()
    except OSError:
        pass
