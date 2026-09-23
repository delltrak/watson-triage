"""Cross-team review of an investigation (Knight-style critic).

The reviewer — by default Team Claude, the other model family from the
investigator — checks every finding against the evidence it cites. Code applies
the verdicts: unsupported findings leave the answer, weak observed ones become
hypotheses. Fail-open: a review that cannot run never blocks the investigation;
the note says the findings were not cross-checked.
"""
from __future__ import annotations

from .roster import TEAMS, label

REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['verdicts', 'notes'],
    'properties': {
        'verdicts': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['index', 'verdict', 'note'],
            'properties': {'index': {'type': 'integer'},
                           'verdict': {'type': 'string', 'enum': ['supported', 'weak', 'unsupported']},
                           'note': {'type': 'string'}}}},
        'notes': {'type': 'array', 'items': {'type': 'string'}},
    },
}
_INSTRUCTION = (
    'Você é o revisor do Watson, de outro time de modelos. Para CADA finding (pelo index), confira se as '
    'evidências citadas em evidence_ids sustentam a afirmação: supported (as evidências dizem isso), weak '
    '(sustenta em parte ou com incerteza) ou unsupported (as evidências citadas não dizem isso). note: uma '
    'frase curta explicando. Em notes, no máximo 3 pontos importantes que a triagem deixou passar, somente '
    'se estiverem nas evidências; senão lista vazia. Não invente fatos nem IDs. '
)
_RULE = {'pt': 'Escreva note e notes em português brasileiro.', 'en': 'Write note and notes in English.'}
VERDICTS = ('supported', 'weak', 'unsupported')
_MIN_SECONDS = 60  # below this the review is skipped (fail-open) instead of racing the MCP timeout


def _payload(result, issue, evidence):
    """Exactly the cited evidence the investigator saw (already within triage's size limit)."""
    findings = [{'index': i, 'claim': f['claim'], 'certainty': f['certainty'], 'evidence_ids': f['evidence_ids']}
                for i, f in enumerate(result['findings'])]
    cited = dict.fromkeys(x for f in result['findings'] for x in f['evidence_ids'])
    return {'issue': {'title': issue['title'], 'url': issue['url']}, 'findings': findings,
            'evidence': {key: evidence[key] for key in cited if key in evidence}}


def _who(review, lang):
    team = TEAMS.get(review.get('team'), {}).get(lang, review.get('team') or '?')
    who = f'{team} ({label(review["model"])})' if review.get('model') else team
    if review.get('fallback_from'):
        missing = TEAMS[review['fallback_from']][lang]
        who += (f', cobrindo o {missing}, que está sem login' if lang == 'pt'
                else f', covering for {missing}, which is not logged in')
    return who


def note(review, lang):
    """One deterministic chat line about who investigated and reviewed (relayed verbatim)."""
    pt = lang == 'pt'
    inv = review.get('investigator') or {}
    parts = []
    if inv.get('fallback_from'):
        parts.append(f'{"Investigado pelo" if pt else "Investigated by"} {_who(inv, lang)}.')
    parts.append(_verdict_line(review, lang))
    if review['status'] == 'done' and inv.get('team') and inv.get('team') == review.get('team'):
        parts.append('O mesmo time investigou e revisou: não houve revisão cruzada.' if pt
                     else 'The same team investigated and reviewed: no cross-team review.')
    return ' '.join(parts)


def _verdict_line(review, lang):
    pt = lang == 'pt'
    if review['status'] != 'done':
        if review.get('reason') == 'unavailable':
            return ('Sem revisão cruzada desta vez: nenhum time está logado para revisar.' if pt
                    else 'No cross-review this time: no team is logged in to review.')
        return (f'A revisão do {_who(review, lang)} não rodou desta vez; os achados não foram conferidos.' if pt
                else f'The review by {_who(review, lang)} did not run this time; the findings were not '
                     f'cross-checked.')
    lead = 'Revisado pelo' if pt else 'Reviewed by'
    if not review.get('checked'):
        return (f'{lead} {_who(review, lang)}: o revisor não avaliou os achados.' if pt
                else f'{lead} {_who(review, lang)}: the reviewer did not assess the findings.')
    if review.get('all_unsupported'):
        return (f'{lead} {_who(review, lang)}: o revisor não achou suporte nas evidências para nenhum '
                f'achado; todos ficaram como hipótese.' if pt
                else f'{lead} {_who(review, lang)}: the reviewer found no support in the evidence for any '
                     f'finding; all were kept as hypotheses.')
    parts = [f'{review["supported"]} de {review["total"]} achados confirmados' if pt
             else f'{review["supported"]} of {review["total"]} findings confirmed']
    if review['weak']:
        parts.append(f'{review["weak"]} com ressalva' if pt else f'{review["weak"]} with caveats')
    if review['removed']:
        n = len(review['removed'])
        parts.append((f'{n} removido por falta de evidência' if n == 1 else f'{n} removidos por falta de evidência')
                     if pt else f'{n} removed for lack of evidence')
    return f'{lead} {_who(review, lang)}: {", ".join(parts)}.'


def review(result, issue, evidence, crew, language, run_id):
    spec = dict(crew.effective.get('reviewer') or {})
    inv = dict(crew.effective.get('investigator') or {})
    info = {'team': spec.get('engine'), 'model': spec.get('model'), 'fallback_from': spec.get('fallback_from'),
            'investigator': {'team': inv.get('engine'), 'model': inv.get('model'),
                             'fallback_from': inv.get('fallback_from')}}
    reviewer = crew.for_role('reviewer')
    left = crew.remaining() if hasattr(crew, 'remaining') else None
    if reviewer is None:
        result['review'] = {**info, 'status': 'skipped', 'reason': 'unavailable'}
    elif left is not None and left < _MIN_SECONDS:
        result['review'] = {**info, 'status': 'skipped', 'reason': 'no_time'}
    else:
        try:
            answer = reviewer.ask(_INSTRUCTION + _RULE[language], _payload(result, issue, evidence),
                                  REVIEW_SCHEMA, f'run-{run_id}-review')
        except Exception:  # noqa: BLE001 — fail-open, the note says it did not run
            answer = None
        if not isinstance(answer, dict):
            result['review'] = {**info, 'status': 'skipped', 'reason': 'failed'}
        else:
            result['review'] = {**info, **_apply(result, answer)}
    result['review']['note'] = {lang: note(result['review'], lang) for lang in ('pt', 'en')}
    return result


def _apply(result, answer):
    findings = result['findings']
    verdicts = {}
    for item in answer.get('verdicts') or []:
        if (isinstance(item, dict) and isinstance(item.get('index'), int) and not isinstance(item['index'], bool)
                and 0 <= item['index'] < len(findings) and item.get('verdict') in VERDICTS):
            verdicts[item['index']] = item
    kept, removed, supported, weak, all_unsupported = [], [], 0, 0, False
    for i, finding in enumerate(findings):
        verdict = verdicts.get(i, {}).get('verdict')
        if verdict == 'unsupported':
            removed.append({'claim': finding['claim'][:500], 'note': str(verdicts[i].get('note', ''))[:300]})
            continue
        if verdict == 'weak':
            weak += 1
            if finding['certainty'] == 'observed':
                finding = {**finding, 'certainty': 'hypothesis'}
        elif verdict == 'supported':
            supported += 1
        kept.append(finding)
    if not kept:  # never answer with no findings: keep them, all as hypotheses
        kept = [{**f, 'certainty': 'hypothesis'} for f in findings]
        removed, all_unsupported = [], True
    result['findings'] = kept
    notes = [str(x)[:300] for x in (answer.get('notes') or []) if isinstance(x, str) and x.strip()][:3]
    return {'status': 'done', 'total': len(findings), 'checked': len(verdicts), 'supported': supported,
            'weak': weak, 'removed': removed, 'notes': notes, 'all_unsupported': all_unsupported}
