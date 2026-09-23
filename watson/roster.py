"""Watson's squad ("tropa"): which engine/model/effort plays each role.

Two teams write and review each other: Team Codex (OpenAI via the Codex CLI) and
Team Claude (Anthropic via the Claude Code CLI). The owner sees and changes the
squad over chat; the choice lives in <home>/roster.json. A role whose team is not
logged in falls back to the other team and the squad says so.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .core import WatsonError, both, now, private_json

ROLES = ('selector', 'investigator', 'reviewer')
ROLE_NAMES = {
    'selector': {'pt': 'Seletor de arquivos', 'en': 'File selector'},
    'investigator': {'pt': 'Investigador', 'en': 'Investigator'},
    'reviewer': {'pt': 'Revisor', 'en': 'Reviewer'},
}
ROLE_JOBS = {
    'selector': {'pt': 'escolhe os arquivos que a investigação lê',
                 'en': 'picks the files the investigation reads'},
    'investigator': {'pt': 'investiga a issue com as evidências',
                     'en': 'investigates the issue with the evidence'},
    'reviewer': {'pt': 'confere cada achado contra as evidências antes da resposta',
                 'en': 'checks every finding against the evidence before replying'},
}
ROLE_ALIASES = {
    'selector': 'selector', 'seletor': 'selector', 'files': 'selector', 'arquivos': 'selector',
    'investigator': 'investigator', 'investigador': 'investigator', 'investigacao': 'investigator',
    'investigação': 'investigator', 'triage': 'investigator', 'triagem': 'investigator',
    'reviewer': 'reviewer', 'revisor': 'reviewer', 'review': 'reviewer', 'revisao': 'reviewer',
    'revisão': 'reviewer', 'critic': 'reviewer', 'critico': 'reviewer', 'crítico': 'reviewer',
}
TEAMS = {'codex': {'pt': 'Time Codex', 'en': 'Team Codex'},
         'claude': {'pt': 'Time Claude', 'en': 'Team Claude'}}

DEFAULT = {
    'selector': {'engine': 'codex', 'model': 'gpt-6-luna', 'effort': 'max'},
    'investigator': {'engine': 'codex', 'model': 'gpt-6-sol', 'effort': 'medium'},
    'reviewer': {'engine': 'claude', 'model': 'claude-sonnet-5', 'effort': 'medium'},
}
# Used when a role's team is not logged in: the other team covers it.
FALLBACK = {
    'claude': {'engine': 'codex', 'model': 'gpt-6-luna', 'effort': 'high'},
    'codex': {'engine': 'claude', 'model': 'claude-sonnet-5', 'effort': 'medium'},
}

CLAUDE_MODELS = {
    'claude-opus-5-5': {'label': 'Opus 5.5', 'efforts': ('low', 'medium', 'high', 'xhigh', 'max')},
    'claude-sonnet-5': {'label': 'Sonnet 5', 'efforts': ('low', 'medium', 'high', 'xhigh', 'max')},
    'claude-haiku-4-5-20251001': {'label': 'Haiku 4.5', 'efforts': ('low', 'medium', 'high')},
}
CODEX_FALLBACK_MODELS = {
    'gpt-6-sol': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
    'gpt-5.6-sol': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
    'gpt-5.6-luna': ('low', 'medium', 'high', 'xhigh', 'max'),
}
MODEL_ALIASES = {
    'sol': 'gpt-6-sol', 'luna': 'gpt-6-luna', 'sol 6': 'gpt-6-sol', 'luna 6': 'gpt-6-luna',
    'sol 5.6': 'gpt-5.6-sol', 'luna 5.6': 'gpt-5.6-luna',
    'opus': 'claude-opus-5-5', 'opus 5.5': 'claude-opus-5-5',
    'sonnet': 'claude-sonnet-5', 'sonnet 5': 'claude-sonnet-5',
    'haiku': 'claude-haiku-4-5-20251001', 'haiku 4.5': 'claude-haiku-4-5-20251001',
}
EFFORT_ALIASES = {'baixo': 'low', 'medio': 'medium', 'médio': 'medium', 'alto': 'high',
                  'altissimo': 'xhigh', 'altíssimo': 'xhigh', 'maximo': 'max', 'máximo': 'max'}
_FILE = 'roster.json'
_EFFORT_PT = {'low': 'baixo', 'medium': 'médio', 'high': 'alto', 'xhigh': 'altíssimo', 'max': 'máximo',
              'ultra': 'ultra'}


def effort_label(effort, lang):
    return _EFFORT_PT.get(effort, effort) if lang == 'pt' else effort


def codex_models(home=None):
    """Codex slugs Watson accepts: the known ones plus what the account's model cache lists
    (hidden ones excluded). The cache is the Codex CLI's private file: parsed defensively."""
    models = dict(CODEX_FALLBACK_MODELS)
    for base in ([Path(home).parent / '.codex'] if home else []) + [Path.home() / '.codex']:
        try:
            data = json.loads((base / 'models_cache.json').read_text())
            entries = data.get('models') if isinstance(data, dict) else data
            found = {}
            for m in entries or []:
                if not isinstance(m, dict) or not isinstance(m.get('slug'), str) or m.get('visibility') == 'hide':
                    continue
                levels = [e.get('effort') if isinstance(e, dict) else e for e in m.get('supported_reasoning_levels') or []]
                found[m['slug']] = tuple(x for x in levels if isinstance(x, str)) or ('low', 'medium', 'high')
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if found:
            models.update(found)
            break
    return models


def label(model):
    if model in CLAUDE_MODELS:
        return CLAUDE_MODELS[model]['label']
    return model.replace('gpt-', 'GPT-').replace('-sol', '-Sol').replace('-luna', '-Luna')


def load(home):
    """Current squad: defaults overlaid with the owner's roster.json."""
    squad = {role: dict(spec) for role, spec in DEFAULT.items()}
    try:
        saved = json.loads((Path(home) / _FILE).read_text()).get('roles', {})
    except (OSError, ValueError, AttributeError):
        saved = {}
    if not isinstance(saved, dict):
        return squad
    for role, spec in saved.items():
        # A hand-edited or corrupt entry falls back to the default for that role.
        if (role in squad and isinstance(spec, dict) and spec.get('engine') in TEAMS
                and all(isinstance(spec.get(k), str) and spec.get(k) for k in ('model', 'effort'))
                and (spec['engine'] == 'claude') == (spec['model'] in CLAUDE_MODELS)):
            squad[role] = {k: spec[k] for k in ('engine', 'model', 'effort')}
    return squad


def save(home, squad):
    private_json(Path(home) / _FILE, {'roles': squad, 'at': now()})


def resolve(squad, connected):
    """Effective squad given which teams are logged in: {role: spec + 'fallback_from'}."""
    effective = {}
    for role, spec in squad.items():
        if connected.get(spec['engine']):
            effective[role] = dict(spec)
        else:
            other = FALLBACK[spec['engine']]
            effective[role] = {**other, 'fallback_from': spec['engine']} if connected.get(other['engine']) \
                else {**spec, 'unavailable': True}
    return effective


def signature(effective):
    """Stable identity of the models in play (part of the investigation cache key)."""
    return {role: [s['engine'], s['model'], s['effort']] for role, s in sorted(effective.items())}


def _normalize(text):
    return ' '.join(re.sub(r'[^\w\s.]', ' ', str(text or '').lower()).split())


def _model_forms(home=None):
    """Every accepted spelling of a model (aliases and slugs), normalized → slug."""
    forms = {_accentless(alias): slug for alias, slug in MODEL_ALIASES.items()}
    for slug in list(CLAUDE_MODELS) + list(codex_models(home)):
        forms[_accentless(slug)] = slug
    return forms


def resolve_model(name, home=None):
    return _model_forms(home).get(_accentless(name))


def parse_change(role=None, model=None, effort=None, home=None):
    """Validate a squad change. Returns (role, spec) or raises a bilingual WatsonError."""
    role_key = ROLE_ALIASES.get(_normalize(role)) or ROLE_ALIASES.get(_accentless(role))
    if role_key is None:
        names = ', '.join(ROLE_NAMES[r]['en'].lower() for r in ROLES)
        nomes = ', '.join(ROLE_NAMES[r]['pt'].lower() for r in ROLES)
        raise WatsonError(both(f'Unknown role. Roles: {names}.', f'Papel desconhecido. Papéis: {nomes}.'))
    model_key = None
    if model:
        model_key = resolve_model(model, home)
        if model_key is None:
            options = ', '.join(sorted(MODEL_ALIASES))
            raise WatsonError(both(f'Unknown model "{model}". Try: {options}.',
                                   f'Modelo "{model}" desconhecido. Tente: {options}.'))
    return role_key, model_key, (EFFORT_ALIASES.get(_normalize(effort), _normalize(effort)) if effort else None)


def change(home, role=None, model=None, effort=None, reset=False):
    """Apply a squad change. Returns (before, after, role or None)."""
    before = load(home)
    if reset:
        save(home, {r: dict(s) for r, s in DEFAULT.items()})
        return before, load(home), None
    role_key, model_key, effort_key = parse_change(role, model, effort, home)
    spec = dict(before[role_key])
    if model_key:
        spec['engine'] = 'claude' if model_key in CLAUDE_MODELS else 'codex'
        spec['model'] = model_key
        if effort_key is None and spec['effort'] not in _efforts(spec, home):
            spec['effort'] = 'medium'
    if effort_key:
        spec['effort'] = effort_key
    allowed = _efforts(spec, home)
    if spec['effort'] not in allowed:
        raise WatsonError(both(f'{label(spec["model"])} does not support effort "{spec["effort"]}". '
                               f'Options: {", ".join(allowed)}.',
                               f'{label(spec["model"])} não aceita esforço '
                               f'"{effort_label(spec["effort"], "pt")}". '
                               f'Opções: {", ".join(effort_label(e, "pt") for e in allowed)}.'))
    after = {**before, role_key: spec}
    save(home, after)
    return before, after, role_key


def _efforts(spec, home):
    if spec['engine'] == 'claude':
        return CLAUDE_MODELS.get(spec['model'], {}).get('efforts', ('low', 'medium', 'high'))
    return codex_models(home).get(spec['model'], ('low', 'medium', 'high'))


def _line(role, chosen, spec, lang):
    """The owner's choice for the role, plus who really covers it right now."""
    name = ROLE_NAMES[role][lang]
    team = TEAMS[chosen['engine']][lang]
    text = f'**{name}** — {team}, {label(chosen["model"])} ({effort_label(chosen["effort"], lang)})'
    if spec.get('fallback_from'):
        cover = f'{TEAMS[spec["engine"]][lang]} cobre com' if lang == 'pt' else f'{TEAMS[spec["engine"]][lang]} covers with'
        model = f'{label(spec["model"])} ({effort_label(spec["effort"], lang)})'
        text += (f' — sem login; enquanto isso o {cover} {model}' if lang == 'pt'
                 else f' — not logged in; meanwhile {cover} {model}')
    elif spec.get('unavailable'):
        text += ' — sem login em nenhum time' if lang == 'pt' else ' — no team is logged in'
    return f'{text}\n{ROLE_JOBS[role][lang]}.'


def render(squad, connected, lang='pt', changed=None, before=None):
    """Chat copy for "minha tropa" / "my squad" (no backticks; blank lines for iMessage)."""
    lang = 'en' if lang == 'en' else 'pt'
    effective = resolve(squad, connected)
    lines = []
    if changed and before:
        old, new = before[changed], squad[changed]
        name = ROLE_NAMES[changed][lang]
        lines.append(f'✅ {name}: {label(old["model"])} ({effort_label(old["effort"], lang)}) → '
                     f'{label(new["model"])} ({effort_label(new["effort"], lang)}).')
        lines.append('')
    elif changed is None and before is not None:
        lines.append('✅ Tropa voltou ao padrão.' if lang == 'pt' else '✅ Squad reset to the default.')
        lines.append('')
    lines.append('🪖 **Minha tropa**' if lang == 'pt' else '🪖 **My squad**')
    lines.append('')
    for i, role in enumerate(ROLES, 1):
        lines.append(f'{i}. {_line(role, squad[role], effective[role], lang)}')
        lines.append('')
    lines.append(_cross_line(effective, lang))
    lines.append('')
    if lang == 'pt':
        lines.append('Pra trocar (só o dono, direto aqui no chat), manda assim: "coloca o revisor no opus", '
                     '"investigador no sol alto" ou "revisor esforço alto". "tropa padrão" volta ao original.')
    else:
        lines.append('To change it (owner only, right here in chat), send: "put the reviewer on opus", '
                     '"investigator on sol high" or "reviewer effort high". "default squad" resets it.')
    return '\n'.join(lines)


def _cross_line(effective, lang):
    """Say plainly whether the review is really cross-team right now."""
    pt = lang == 'pt'
    inv, rev = effective['investigator'], effective['reviewer']
    if inv.get('unavailable') or rev.get('unavailable'):
        return ('Sem revisão cruzada agora: falta login para investigar ou revisar.' if pt
                else 'No cross-review right now: a team needed to investigate or review is not logged in.')
    if inv['engine'] != rev['engine']:
        return ('Time Codex e Time Claude se revisam: quem investiga não é quem confere.' if pt
                else 'Team Codex and Team Claude check each other: whoever investigates is not who reviews.')
    team, model = TEAMS[inv['engine']][lang], label(inv['model'])
    if inv['model'] == rev['model']:
        return (f'Agora o mesmo modelo investiga e revisa ({team}, {model}): a revisão não é cruzada.' if pt
                else f'Right now the same model investigates and reviews ({team}, {model}): the review is not '
                     f'cross-checked.')
    return (f'Agora o {team} investiga e revisa, com modelos diferentes: a revisão não é entre times.' if pt
            else f'Right now {team} both investigates and reviews, with different models: the review is not '
                 f'cross-team.')


# ---------------------------------------------------------------- chat requests ----
_SHOW = {
    'pt': ('minha tropa', 'tropa', 'qual a minha tropa', 'qual e a minha tropa', 'mostra a tropa',
           'mostra minha tropa', 'ver tropa', 'ver a tropa', 'ver minha tropa'),
    'en': ('my squad', 'show my squad', 'show squad', 'whats my squad', 'what is my squad', 'show me my squad'),
}
_RESET = {
    'pt': ('tropa padrao', 'volta a tropa', 'volta a tropa padrao', 'resetar tropa', 'reseta a tropa'),
    'en': ('default squad', 'reset squad', 'reset my squad', 'reset the squad'),
}
_EN_WORDS = {'put', 'set', 'switch', 'move', 'make', 'use', 'the', 'on', 'to', 'with', 'effort'}
_VERB = r'(?:coloca|coloque|poe|bota|muda|mude|troca|troque|usa|use|deixa|deixe|put|set|switch|move|make)'
_ARTICLE = r'(?:o|a|os|as|the)'
_LINK = r'(?:no|na|pro|pra|para o|para a|para|com o|com a|com|em|por|pelo|pela|to|on|with|as)'
_POLITE = re.compile(r'(?: (?:por favor|pfv|pf|pls|plz|please|thanks|thank you|obrigado|obrigada|valeu))+$')
_PT_ROLE_WORDS = {'revisor', 'revisao', 'investigador', 'investigacao', 'seletor', 'arquivos', 'triagem', 'critico'}
_EN_ROLE_WORDS = {'reviewer', 'review', 'investigator', 'selector', 'files', 'triage', 'critic'}
_EFFORT = r'(?:low|medium|high|xhigh|max|ultra|baixo|medio|alto|altissimo|maximo)'


def _accentless(text):
    import unicodedata
    text = unicodedata.normalize('NFKD', str(text or '').lower())
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s.]", ' ', text.replace("'", '').replace('\u2019', '').replace('\u2018', ''))
    return ' '.join(text.split()).strip(' .')


def _alternation(words):
    return '|'.join(sorted((re.escape(w) for w in words), key=len, reverse=True))


def parse_request(text, home=None):
    """Deterministic squad request from the owner: {'action', 'lang', ...} or None.

    'minha tropa' / 'my squad' → show; 'tropa padrão' / 'default squad' → reset;
    'coloca o revisor no opus', 'investigador no sol high', 'put the reviewer on opus'
    → set. Anything else is not a squad command.
    """
    raw = str(text or '')
    if not raw.strip() or len(raw) > 120 or '\n' in raw.strip():
        return None
    norm = _POLITE.sub('', _accentless(raw))
    for action, table in (('show', _SHOW), ('reset', _RESET)):
        for lang, phrases in table.items():
            if norm in phrases:
                return {'action': action, 'lang': lang}
    roles = _alternation(ROLE_ALIASES)
    models = _alternation(_model_forms(home))
    effort = rf'(?:(?:esforco|effort) (?:(?:no|em|to|at|de) )?)?(?P<effort>{_EFFORT})(?: (?:esforco|effort))?'
    effort_only = (rf'(?:(?:{_LINK} )(?:esforco |effort )?(?:(?:no|em|to|at|de) )?|(?:esforco|effort) '
                   rf'(?:(?:no|em|to|at|de) )?)(?P<effort_only>{_EFFORT})(?: (?:esforco|effort))?')
    pattern = (rf'^(?:{_VERB} )?(?:{_ARTICLE} )?(?P<role>{roles})(?: de [a-z]+)? '
               rf'(?:(?:{_LINK} )?(?:{_ARTICLE} )?(?P<model>{models})(?: (?:no |em |com |at |with )?{effort})?'
               rf'|{effort_only})$')
    match = re.match(pattern, norm)
    if not match:
        return None
    words = set(norm.split())
    if words & _PT_ROLE_WORDS:
        lang = 'pt'
    elif words & _EN_ROLE_WORDS:
        lang = 'en'
    else:
        lang = 'en' if words & _EN_WORDS and not words & {'no', 'na', 'coloca', 'o', 'pro', 'pra'} else 'pt'
    if raw.strip().endswith('?') and not re.match(rf'^{_VERB} ', norm):
        return {'action': 'show', 'lang': lang}  # "o revisor no opus?" asks, it does not order
    return {'action': 'set', 'lang': lang, 'role': match.group('role'),
            'model': match.group('model'), 'effort': match.group('effort') or match.group('effort_only')}


def reply(home, text, connected):
    """Chat answer for a squad request (None when the text is not one)."""
    request = parse_request(text, home)
    if request is None:
        return None
    lang = request['lang']
    if request['action'] == 'show':
        return render(load(home), connected, lang)
    if request['action'] == 'reset':
        before, after, _ = change(home, reset=True)
        return render(after, connected, lang, changed=None, before=before)
    try:
        before, after, role = change(home, request['role'], request['model'], request['effort'])
    except WatsonError as exc:
        from .core import pick
        return pick(str(exc), lang)
    return render(after, connected, lang, changed=role, before=before)


def connected_teams(run=None):
    """Which teams are logged in right now (Codex / Claude CLI auth probes)."""
    from .capabilities import check_claude, check_codex
    kwargs = {'run': run} if run else {}
    return {'codex': bool(check_codex(**kwargs).get('ok')), 'claude': bool(check_claude(**kwargs).get('ok'))}
