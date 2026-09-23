"""Full checklist or one short line? The greeting's memory lives in Watson's own state.

The gateway hook answers the owner's greetings without the LLM, so Hermes memory
(read by the model only, frozen per session) cannot decide this. <home>/owner.json
remembers what the owner last saw: the full checklist comes back on first contact,
for a new owner, when the connection picture changed, or when Watson cannot
investigate; otherwise the owner gets a short greeting.
"""
from __future__ import annotations

import json
from pathlib import Path

from .core import now, private_json

_FILE = 'owner.json'


def fingerprint(report):
    """The connection picture the owner saw (GitHub login, Codex, Claude)."""
    gh = report.get('github') or {}
    return (f'gh={int(bool(gh.get("connected")))}:{gh.get("login") or ""}'
            f'|codex={int(bool((report.get("codex") or {}).get("connected")))}'
            f'|claude={int(bool((report.get("claude") or {}).get("connected")))}')


def load(home):
    path = Path(home) / _FILE
    try:
        if path.is_symlink():
            return {}
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def ready(report):
    """Watson can investigate: GitHub plus at least one team."""
    return bool((report.get('github') or {}).get('connected')) and bool(
        (report.get('codex') or {}).get('connected') or (report.get('claude') or {}).get('connected'))


def decide(state, report, owner_uid=None):
    """'full' or 'short'. Any doubt means 'full'."""
    if not state or not ready(report):
        return 'full'
    if owner_uid is not None and state.get('owner_uid') != owner_uid:
        return 'full'
    return 'short' if state.get('shown_fingerprint') == fingerprint(report) else 'full'


def mark_shown(home, report, owner_uid):
    try:
        private_json(Path(home) / _FILE, {'owner_uid': owner_uid, 'shown_fingerprint': fingerprint(report),
                                          'shown_at': now()})
    except OSError:
        pass  # a greeting never fails because the state could not be saved


def reset(home):
    try:
        (Path(home) / _FILE).unlink()
    except OSError:
        pass
