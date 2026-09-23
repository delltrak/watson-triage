"""Linked-PR awareness. Pure functions: classify PRs that cite an issue and decide one
deterministic suggestion. Watson only suggests; it never closes issues or merges.

All patterns are linear (no nested quantifiers over newlines): PR and issue bodies
come from anyone who can open a PR or edit an issue.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

TRUSTED = {'OWNER', 'MEMBER', 'COLLABORATOR'}
MAX_BODY = 20000  # bodies are capped before any regex
CLOSING = r'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)'
# Only a literal Refs/References may become a close suggestion (the #71 case).
REFS = r'(?:refs?|references?)'
# Weaker citations count as targets (multi-issue PRs) but never as delivery.
CITES = rf'(?:{REFS}|related to|relates? to|part of|see|ver|relacionad[ao] (?:a|com))'
NEGATION = re.compile(r"(?i)(sem encerr|n[ãa]o encerr|sem fechar|n[ãa]o fecha|not clos|does not close|"
                      r"doesn't close|without closing|parcial|partial|\bpart of\b|\bpart \d+ of \d+|"
                      r"\bparte d[eo]\b|\bparte \d+ de \d+)")
VERDICT = re.compile(r'(?i)(?:veredito|verdict)[\s:*]*(aprovar com ressalvas|aprovar|aprovado|'
                     r'reprovar|bloquear|request changes|approved?)')
REJECT = {'REPROVAR', 'BLOQUEAR', 'REQUEST CHANGES'}
VERDICT_LABEL = {  # (pt, en)
    'APROVAR': ('aprovada', 'approved'), 'APROVADO': ('aprovada', 'approved'),
    'APPROVE': ('aprovada', 'approved'), 'APPROVED': ('aprovada', 'approved'),
    'APROVAR COM RESSALVAS': ('aprovada com ressalvas', 'approved with caveats'),
    'REPROVAR': ('com pedido de mudanças', 'changes requested'),
    'REQUEST CHANGES': ('com pedido de mudanças', 'changes requested'),
    'BLOQUEAR': ('bloqueada', 'blocked'),
}
SHA_NEAR = re.compile(r'(?i)\bcommit\s+`?([0-9a-f]{7,40})\b')
PR_QUESTION = re.compile(r'(?i)\b(qual|quais|which|what)\b[^?\n]{0,200}\b(PR|pull request)s?\b')
UNCHECKED = re.compile(r'(?m)^[^\S\n]*[-*][^\S\n]+\[ \]')
BRT = timezone(timedelta(hours=-3))  # Brasil sem horário de verão desde 2019.
LEAD = {'suggest_close', 'review_close', 'pr_open', 'merged_elsewhere'}


def _cap(text):
    return (text or '')[:MAX_BODY]


def _ref(issue_repo, number, same_repo):
    qualified = rf'{re.escape(issue_repo)}#{number}|https://github\.com/{re.escape(issue_repo)}/issues/{number}'
    # A bare #N inside ANOTHER repo's PR means that repo's own #N.
    return rf'(?:(?<!\w)#{number}|{qualified})(?!\d)' if same_repo else rf'(?:{qualified})(?!\d)'


def link_kind(issue_repo, number, pr_repo, title, body):
    ref = _ref(issue_repo, number, pr_repo == issue_repo)
    text = f'{title or ""}\n{_cap(body)}'
    kinds = set()
    for line in text.splitlines():
        if not re.search(ref, line):
            continue
        if NEGATION.search(line):
            kinds.add('mention_negated')
        elif re.search(rf'(?i)\b{CLOSING}:?[^\S\n]+{ref}', line):
            kinds.add('closes')
        elif re.match(rf'(?i)[^\w\n]*{REFS}:?[^\S\n]+{ref}', line.strip()):
            kinds.add('refs')
        else:
            kinds.add('mention')
    # No match in title/body: the cross-reference came from a PR comment.
    return next((k for k in ('mention_negated', 'closes', 'refs', 'mention') if k in kinds), 'mention')


def targets(body):
    """Every issue number on a Closes/Refs/See… line, plus inline closing keywords."""
    found = set()
    for line in re.findall(rf'(?im)^[^\w\n]*(?:{CLOSING}|{CITES}):?[^\S\n]+#\d+[^\n]*', _cap(body)):
        found.update(re.findall(r'#(\d+)\b', line))
    found.update(re.findall(rf'(?i)\b{CLOSING}:?[^\S\n]+#(\d+)\b', _cap(body)))
    return sorted(int(x) for x in found)


def verdicts(comments, head_sha, pr_author):
    """Knight-style: only trusted commenters, and a verdict counts for the SHA it names."""
    found = []
    for c in comments:
        if c.get('author_association') not in TRUSTED:
            continue
        body = _cap(c.get('body'))
        m = VERDICT.search(body)
        if not m:
            continue
        sha = SHA_NEAR.search(body)
        prefix = sha.group(1) if sha else None
        found.append({'verdict': m.group(1).upper(), 'sha': prefix,
                      'on_head': None if not prefix else head_sha.startswith(prefix),
                      'author': c['user']['login'], 'association': c['author_association'],
                      'same_as_pr_author': c['user']['login'] == pr_author,
                      'url': c['html_url'], 'excerpt': body[:1200]})
    return found[-2:]


def formal_reviews(reviews, head_sha):
    return [{'state': r['state'], 'on_head': r.get('commit_id') == head_sha,
             'author': (r.get('user') or {}).get('login'), 'association': r.get('author_association'),
             'url': r.get('html_url')}
            for r in reviews if r.get('state') in {'APPROVED', 'CHANGES_REQUESTED'}
            and r.get('author_association') in TRUSTED][-2:]


def ci_summary(runs):
    """From actions/runs?head_sha. Only the newest run per workflow counts: concurrency with
    cancel-in-progress leaves superseded 'cancelled' runs on the same SHA."""
    latest = {}
    for r in runs[:30]:
        key = r.get('workflow_id') or r['name']
        order = (r.get('run_number') or 0, r.get('id') or 0)  # API order is not newest-first
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, r)
    runs = [{'name': r['name'], 'status': r['status'], 'conclusion': r['conclusion'], 'url': r['html_url']}
            for _, r in latest.values()
            if not (r['status'] == 'completed' and r['conclusion'] == 'cancelled')]
    if not runs:
        return {'conclusion': 'none', 'runs': []}
    if any(r['status'] != 'completed' for r in runs):
        overall = 'pending'
    elif any(r['conclusion'] in {'failure', 'timed_out', 'action_required', 'startup_failure'} for r in runs):
        overall = 'failure'
    else:
        overall = 'success'
    return {'conclusion': overall, 'runs': runs}


def decide(issue, linked, closure=None):
    """Code is the gate: the model can only DOWNGRADE (closure.assessment == 'pending')."""
    n, repo = issue['number'], issue.get('repository')
    if issue['state'] != 'open' or not linked.get('checked'):
        return {'kind': 'none', 'lead': False}
    pulls = [p for p in linked['pulls'] if p.get('hydrated') and (repo is None or p['repository'] == repo)]
    strong = [p for p in pulls if p['link'] in {'closes', 'refs'}]
    open_prs = [p for p in strong if p['state'] == 'open' and p['author_association'] in TRUSTED]
    if open_prs:
        return {'kind': 'pr_open', 'lead': True, 'pulls': [p['number'] for p in open_prs],
                'primary': open_prs[0]['number'], 'blockers': []}
    merged = [p for p in pulls if p['state'] == 'merged' and p['link'] != 'mention_negated']
    delivering = [p for p in merged if p['link'] in {'closes', 'refs'} and p['targets'] in ([], [n])
                  and p['on_default_branch'] is True and p.get('reverted') is not True]
    if delivering:
        last = max(delivering, key=lambda p: p['merged_at'])
        blockers = []
        unchecked = len(UNCHECKED.findall(_cap(issue.get('body'))))
        if unchecked:
            blockers.append(('unchecked', unchecked))
        # A later multi-issue PR that also cites #n without closing it: the work may continue there.
        later = sorted(q['number'] for q in merged if q not in delivering and n in q['targets']
                       and q['merged_at'] >= last['merged_at'])
        if later:
            blockers.append(('later_pr', later))
        late = sorted(p['number'] for p in delivering if (p.get('referenced_at') or '') > p['merged_at'])
        if late:  # the link was edited in after the merge: not a delivery signal
            blockers.append(('link_after_merge', late))
        untrusted = sorted(p['number'] for p in delivering if p.get('author_association') not in TRUSTED)
        if untrusted:  # the author can still edit a merged PR's body
            blockers.append(('untrusted_author', untrusted))
        if linked.get('reopened_at') and linked['reopened_at'] > last['merged_at']:
            blockers.append(('reopened', linked['reopened_at']))
        if (last.get('ci_merge') or {}).get('conclusion') == 'failure':
            blockers.append(('ci_failed', last['default_branch']))
        if any(v['verdict'] in REJECT and v['on_head'] for v in last['reviews']['verdicts']) or any(
                r['state'] == 'CHANGES_REQUESTED' and r['on_head'] for r in last['reviews']['formal']):
            blockers.append(('review_rejected', None))
        if not last['reviews'].get('complete', True):
            blockers.append(('reviews_incomplete', None))
        if any(p.get('reverted') is None for p in delivering):
            blockers.append(('revert_unknown', None))
        if not linked.get('complete'):
            blockers.append(('timeline_incomplete', None))
        if closure and closure.get('assessment') == 'pending':
            blockers.append(('model', closure.get('reason', '')[:300]))
        return {'kind': 'review_close' if blockers else 'suggest_close', 'lead': True,
                'pulls': sorted(p['number'] for p in delivering), 'primary': last['number'], 'blockers': blockers}
    elsewhere = [p for p in merged if p['link'] in {'closes', 'refs'} and p['on_default_branch'] is False
                 and p['base'] != p['default_branch']]
    if elsewhere:
        return {'kind': 'merged_elsewhere', 'lead': True, 'pulls': [elsewhere[0]['number']],
                'primary': elsewhere[0]['number'], 'blockers': []}
    if merged:
        return {'kind': 'merged_partial', 'lead': False, 'pulls': sorted(p['number'] for p in merged),
                'primary': None, 'blockers': []}
    return {'kind': 'none', 'lead': False}


CI_LABEL = {'pt': {'success': 'CI verde', 'failure': 'CI falhando', 'pending': 'CI ainda rodando',
                   'none': 'sem CI registrado'},
            'en': {'success': 'green CI', 'failure': 'failing CI', 'pending': 'CI still running',
                   'none': 'no CI recorded'}}


def _date(iso, lang):
    at = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(BRT)
    return at.strftime('%d/%m') if lang == 'pt' else at.strftime('%b %d').replace(' 0', ' ')


def _review(p, lang):
    v = (p['reviews']['verdicts'] or [None])[-1]
    f = (p['reviews']['formal'] or [None])[-1]
    if v:
        where = {True: {'pt': ' no último commit', 'en': ' on the latest commit'},
                 False: {'pt': ' em um commit anterior', 'en': ' on an earlier commit'}}.get(v['on_head'], {})
        pt, en = VERDICT_LABEL.get(v['verdict'], ('registrada', 'recorded'))
        return (f'revisão {pt}' if lang == 'pt' else f'review {en}') + where.get(lang, '')
    if f:
        return {('APPROVED', 'pt'): 'aprovado no GitHub', ('APPROVED', 'en'): 'approved on GitHub',
                ('CHANGES_REQUESTED', 'pt'): 'com pedido de mudanças',
                ('CHANGES_REQUESTED', 'en'): 'changes requested'}[(f['state'], lang)]
    return ('sem veredito de revisão nos comentários do PR' if lang == 'pt'
            else 'no review verdict in the PR comments')


def _nums(numbers, lang):
    items = [f'#{x}' for x in numbers]
    joiner = ' e ' if lang == 'pt' else ' and '
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + joiner + items[-1]


def _blocker(kind, value, n, lang, run_lang='pt'):
    pt = lang == 'pt'
    if kind == 'model':  # the model writes its reason in the run's language
        if lang == run_lang:
            return value
        return ('a triagem achou itens que o PR não cobre (veja os achados)' if pt
                else 'the triage found items the PR does not cover (see findings)')
    if kind == 'unchecked':
        if pt:
            return f'a #{n} ainda tem {value} {"item" if value == 1 else "itens"} sem marcar na lista'
        return f'#{n} still has {value} unchecked {"item" if value == 1 else "items"}'
    if kind == 'later_pr':
        one = len(value) == 1
        if pt:
            return (f'{"o PR" if one else "os PRs"} {_nums(value, lang)}, {"mergeado" if one else "mergeados"} '
                    f'depois, {"também cita" if one else "também citam"} a #{n} sem fechá-la')
        return (f'{"PR" if one else "PRs"} {_nums(value, lang)}, merged later, also '
                f'{"cites" if one else "cite"} #{n} without closing it')
    if kind == 'link_after_merge':
        return (f'a citação à #{n} entrou no PR {_nums(value, lang)} depois do merge' if pt
                else f'the reference to #{n} was added to PR {_nums(value, lang)} after the merge')
    if kind == 'untrusted_author':
        return (f'o PR {_nums(value, lang)} é de alguém sem acesso de escrita ao repositório' if pt
                else f'PR {_nums(value, lang)} is by someone without write access to the repository')
    if kind == 'reopened':
        return (f'a #{n} foi reaberta em {_date(value, lang)}, depois do merge' if pt
                else f'#{n} was reopened on {_date(value, lang)}, after the merge')
    if kind == 'ci_failed':
        return f'o CI na {value} falhou depois do merge' if pt else f'CI on {value} failed after the merge'
    if kind == 'review_rejected':
        return 'a última revisão pediu mudanças' if pt else 'the latest review requested changes'
    if kind == 'reviews_incomplete':
        return ('não consegui ler todas as revisões do PR' if pt
                else 'I could not read every review on the PR')
    if kind == 'revert_unknown':
        return ('não consegui confirmar que o merge não foi revertido depois' if pt
                else 'I could not confirm the merge was not reverted later')
    return ('não consegui ler a linha do tempo inteira da issue' if pt
            else 'I could not read the whole issue timeline')


def say(issue, linked, action, lang):
    """Chat copy for iMessage: no backticks, no markdown, absolute dates (cache-safe)."""
    n, url, kind = issue['number'], issue['url'], action['kind']
    if kind in {'none', 'merged_partial'}:
        return ''
    repo = issue.get('repository')
    by = {p['number']: p for p in linked['pulls'] if repo is None or p['repository'] == repo}
    p = by[action['primary']]
    if kind == 'pr_open':
        ci = CI_LABEL[lang][p['ci_head']['conclusion']]
        if lang == 'pt':
            state = 'em rascunho' if p['draft'] else 'aberto'
            return (f'Já existe o PR #{p["number"]} {state} para a #{n} ({ci}). Não vou abrir outro rascunho; '
                    f'o próximo passo é revisar o #{p["number"]}: {p["url"]}')
        state = 'open as a draft' if p['draft'] else 'open'
        return (f'PR #{p["number"]} is already {state} for #{n} ({ci}). I will not open another draft; '
                f'the next step is reviewing #{p["number"]}: {p["url"]}')
    if kind == 'merged_elsewhere':
        if lang == 'pt':
            return (f'O PR #{p["number"]} foi mergeado na {p["base"]}, não na {p["default_branch"]}; por isso a '
                    f'#{n} continua aberta. Quando a mudança chegar na {p["default_branch"]}, dá para fechar a #{n}.')
        return (f'PR #{p["number"]} was merged into {p["base"]}, not {p["default_branch"]}, so #{n} is still open. '
                f'Once the change reaches {p["default_branch"]}, #{n} can be closed.')
    facts = ', '.join([CI_LABEL[lang][(p.get('ci_merge') or {}).get('conclusion', 'none')], _review(p, lang)])
    when = _date(p['merged_at'], lang)
    others = [x for x in action['pulls'] if x != p['number']]
    run_lang = action.get('language', 'pt')
    if lang == 'pt':
        head = (f'O PR #{p["number"]} já foi mergeado na {p["default_branch"]} em {when} ({facts}).' if not others else
                f'Os PRs {_nums(action["pulls"], lang)} já foram mergeados na {p["default_branch"]} e citam só a '
                f'#{n}; o último, #{p["number"]}, em {when} ({facts}).')
        if kind == 'review_close':
            why = '; '.join(_blocker(k, v, n, lang, run_lang) for k, v in action['blockers'])
            return f'{head} Mas {why}. Confira antes de fechar a #{n}: {url}'
        why = (f' O PR usou Refs #{n}, que não fecha a issue automaticamente.'
               if p['link'] == 'refs' else f' O PR diz Closes #{n}, mas a issue continua aberta.')
        return f'{head}{why} Sugiro fechar a #{n}: {url}'
    head = (f'PR #{p["number"]} was already merged into {p["default_branch"]} on {when} ({facts}).' if not others else
            f'PRs {_nums(action["pulls"], lang)} were already merged into {p["default_branch"]} and cite only '
            f'#{n}; the latest, #{p["number"]}, on {when} ({facts}).')
    if kind == 'review_close':
        why = '; '.join(_blocker(k, v, n, lang, run_lang) for k, v in action['blockers'])
        return f'{head} But {why}. Please check before closing #{n}: {url}'
    why = (f' The PR used Refs #{n}, which does not auto-close the issue.'
           if p['link'] == 'refs' else f' The PR says Closes #{n}, but the issue is still open.')
    return f'{head}{why} I suggest closing #{n}: {url}'


def apply(result, issue, linked, closure, language='pt'):
    """Deterministic post-validation step in triage(): attach issue_action, fix contradictions."""
    action = decide(issue, linked, closure)
    action['language'] = language
    action['say'] = {lang: say(issue, linked, action, lang).replace('`', '') for lang in ('pt', 'en')}
    if action['kind'] != 'none':
        # Watson found the PR itself: never ask the author which PR it is.
        dropped = [q for q in result['questions_for_author'] if PR_QUESTION.search(q)]
        result['questions_for_author'] = [q for q in result['questions_for_author'] if q not in dropped]
        if dropped:
            result['limitations'].append(
                'Question about the PR removed: Watson found the linked PRs.' if language == 'en'
                else 'Pergunta sobre o PR removida: o Watson encontrou os PRs ligados.')
    if action['kind'] in {'suggest_close', 'review_close', 'pr_open'}:
        result['branch_recommendation'] = 'not_needed'  # never a duplicate branch/draft
    if action['kind'] == 'suggest_close' and result['status'] not in {'resolved', 'awaiting_release'}:
        result['status'] = 'awaiting_release'  # merged on the default branch; release is not proven
    cited = {i for f in result['findings'] for i in f['evidence_ids']}
    if action['kind'] != 'none' and not any(f'pull:{x}' in cited for x in action['pulls']):
        claim = action['say'][language] or (
            f'Merged PRs cite the issue: {_nums(action["pulls"], "en")}.' if language == 'en'
            else f'PRs mergeados citam a issue: {_nums(action["pulls"], "pt")}.')
        result['findings'].insert(0, {'claim': claim, 'evidence_ids': [f'pull:{x}' for x in action['pulls']],
                                      'certainty': 'observed'})
    result['issue_action'] = action
    return result
