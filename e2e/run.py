#!/usr/bin/env python3
"""Watson end-to-end suite over real iMessage (macOS + the live container).

Every case sends a real message from the owner's Messages app to the Watson line
and checks what Watson actually sent. Issue scenarios use the fixture repo built
by e2e/lab_setup.py (delltrak/watson-e2e-lab).

  python3 e2e/run.py                 # everything, in order
  python3 e2e/run.py --only C01,S01  # a subset (ids below)
  python3 e2e/run.py --list          # print the cases
  python3 e2e/run.py --offline       # issue scenarios only, decided locally (no iMessage, no Watson)

Reports land in e2e/reports/<timestamp>.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from watson.chat_help import help_text  # noqa: E402

REVIEW_LINE = ('Revisado pelo', 'Reviewed by', 'A revisão do', 'The review by', 'Sem revisão',
               'No cross-review', 'Investigado pelo', 'Investigated by')
SECRETS = ('github-credentials', 'GH_TOKEN', 'Personal access')


@dataclass
class Case:
    id: str
    send: str
    kind: str = 'turn'  # turn | command | setup
    exact: str | None = None  # 'speak:pt' / 'speak:en' → current greeting copy
    startswith: list = field(default_factory=list)
    contains: list = field(default_factory=list)
    absent: list = field(default_factory=list)
    reply_len: int | None = None
    review_line: bool = False
    tool: str | None = None
    expect_kind: str | None = None  # offline check of the linked-PR decision
    issue: int | None = None


def cases(lab):
    repo = lab['repo']
    url = lambda n: f'https://github.com/{repo}/issues/{n}'  # noqa: E731
    s = lambda key: lab[key]  # noqa: E731
    return [
        Case('C00', 'reset onboarding', kind='setup'),
        Case('C01', 'Oi', exact='greet:pt', contains=['1. **GitHub**']),  # first contact: full checklist
        Case('C02', 'hey', exact='greet:en', absent=['1. **GitHub**']),  # already seen: short
        Case('H01', 'o que você faz?', contains=['🔧 **Watson — como eu te ajudo**', 'Minha tropa']),
        Case('H02', 'what can you do?', contains=['🔧 **Watson — how I can help**', 'My squad']),
        Case('C03', '/help', kind='command', reply_len=len(help_text('en'))),
        Case('C04', 'minha tropa', contains=['🪖 **Minha tropa**', '**Investigador** —', '**Revisor** —']),
        Case('C05', 'coloca o revisor no opus', contains=['✅ Revisor:', '→ Opus 5.5']),
        Case('C06', 'my squad', contains=['🪖 **My squad**', 'Opus 5.5']),
        Case('C07', 'tropa padrão', contains=['Tropa voltou ao padrão', 'Time Claude, Sonnet 5']),
        Case('C08', 'Olá, tudo bem?', exact='greet:pt', absent=['1. **GitHub**']),
        Case('C09', '/help', kind='command', reply_len=len(help_text('pt'))),
        Case('C10', 'o github tá conectado?', contains=['delltrak'], absent=list(SECRETS)),
        Case('C11', 'qual modelo revisa as investigações?', tool='mcp__watson__watson_squad'),
        # --- issue scenarios (Portuguese) ---
        Case('S01', url(s('s01_refs_merged')['issue']), expect_kind='suggest_close', review_line=True,
             issue=s('s01_refs_merged')['issue'],
             startswith=[f'O PR #{s("s01_refs_merged")["pr"]} já foi mergeado na main em'],
             contains=[f'Sugiro fechar a #{s("s01_refs_merged")["issue"]}']),
        Case('S02', url(s('s02_closes_reopened')['issue']), expect_kind='review_close', review_line=True,
             issue=s('s02_closes_reopened')['issue'], contains=['foi reaberta', 'Confira antes de fechar']),
        Case('S03', url(s('s03_pr_open')['issue']), expect_kind='pr_open', review_line=True,
             issue=s('s03_pr_open')['issue'],
             startswith=[f'Já existe o PR #{s("s03_pr_open")["pr"]} em rascunho para a #{s("s03_pr_open")["issue"]}']),
        Case('S04', url(s('s04_umbrella')['issue']), expect_kind='merged_partial', review_line=True,
             issue=s('s04_umbrella')['issue'], absent=['Sugiro fechar', 'Confira antes de fechar']),
        Case('S05', url(s('s05_negated')['issue']), expect_kind='none', review_line=True,
             issue=s('s05_negated')['issue'], absent=['Sugiro fechar', 'Confira antes de fechar']),
        Case('S06', url(s('s06_unchecked')['issue']), expect_kind='review_close', review_line=True,
             issue=s('s06_unchecked')['issue'], contains=['1 item sem marcar', 'Confira antes de fechar']),
        Case('S07', url(s('s07_later_pr')['issue']), expect_kind='review_close', review_line=True,
             issue=s('s07_later_pr')['issue'],
             contains=[f'o PR #{s("s07_later_pr")["later_pr"]}, mergeado depois, também cita a '
                       f'#{s("s07_later_pr")["issue"]} sem fechá-la']),
        Case('S08', url(s('s08_ci_failed')['issue']), expect_kind='review_close', review_line=True,
             issue=s('s08_ci_failed')['issue'], contains=['o CI na main falhou depois do merge']),
        Case('S09', url(s('s09_closed')['issue']), expect_kind='none', review_line=True,
             issue=s('s09_closed')['issue'], absent=['Sugiro fechar']),
        Case('S10', url(s('s01_refs_merged')['pr']), contains=[f'#{s("s01_refs_merged")["pr"]}', 'PR']),
        Case('S11', url(s('s11_bug_no_pr')['issue']), expect_kind='none', review_line=True,
             issue=s('s11_bug_no_pr')['issue'], contains=['quantidade'], absent=['Sugiro fechar']),
        Case('S12', url(s('s12_needs_info')['issue']), expect_kind='none', review_line=True,
             issue=s('s12_needs_info')['issue'], absent=['Sugiro fechar']),
        Case('R01', f'#{s("s06_unchecked")["issue"]}', review_line=True,
             startswith=[f'Olhando {repo}#{s("s06_unchecked")["issue"]} (último repositório que investigamos)']),
        # --- English ---
        Case('E01', f'can you check {url(s("s13_en_refs_merged")["issue"])} for me?', expect_kind='suggest_close',
             issue=s('s13_en_refs_merged')['issue'], review_line=True,
             startswith=[f'PR #{s("s13_en_refs_merged")["pr"]} was already merged into main on'],
             contains=[f'I suggest closing #{s("s13_en_refs_merged")["issue"]}']),
        Case('E02', f'what about {url(s("s01_refs_merged")["issue"])}?', expect_kind='suggest_close',
             issue=s('s01_refs_merged')['issue'], review_line=True,
             startswith=[f'PR #{s("s01_refs_merged")["pr"]} was already merged into main on'],
             absent=['Sugiro fechar']),
        Case('R02', f'check #{s("s13_en_refs_merged")["issue"]} please', review_line=True,
             startswith=[f'Looking at {repo}#{s("s13_en_refs_merged")["issue"]} (the repository we investigated last)']),
    ]


def check(case, outcome, speak):
    """List of failure strings (empty = pass)."""
    failures = []
    if case.kind == 'command':
        if outcome.get('reply_len') != case.reply_len:
            failures.append(f'reply_len {outcome.get("reply_len")} != {case.reply_len}')
        return failures
    reply = outcome.get('reply')
    if reply is None:
        return ['message never reached Watson (Plow/iMessage transport)' if outcome.get('transport')
                else 'no reply (timeout)']
    if case.exact and reply != speak:
        failures.append('reply is not the exact greeting copy')
    for prefix in case.startswith:
        if not reply.startswith(prefix):
            failures.append(f'does not start with {prefix!r}')
    for piece in case.contains:
        if piece not in reply:
            failures.append(f'missing {piece!r}')
    for piece in case.absent:
        if piece in reply:
            failures.append(f'must not contain {piece!r}')
    if case.review_line and not reply.strip().splitlines()[-1].startswith(REVIEW_LINE):
        failures.append('last line is not the review note')
    if case.tool and case.tool not in outcome.get('tools', []):
        failures.append(f'tool {case.tool} not called')
    if '`' in reply:
        failures.append('backticks in the reply (iMessage shows them literally)')
    return failures


def offline(lab, selected):
    from watson.github import GitHub
    from watson.linked import decide
    repo = lab['repo']
    gh = GitHub([repo])
    head = gh.source_index(repo)['sha']
    rows = []
    for case in selected:
        if not case.expect_kind:
            continue
        issue = gh.issue(repo, case.issue)
        kind = decide(issue, gh.linked_pull_requests(repo, issue, head))['kind']
        rows.append({'id': case.id, 'ok': kind == case.expect_kind,
                     'failures': [] if kind == case.expect_kind else [f'kind {kind} != {case.expect_kind}']})
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    lab = json.loads((HERE / '.lab-state.json').read_text())
    selected = cases(lab)
    if args.only:
        wanted = set(args.only.split(','))
        selected = [c for c in selected if c.id in wanted]
    if args.list:
        for c in selected:
            print(f'{c.id}  {c.send}')
        return 0
    if args.offline:
        rows = offline(lab, selected)
    else:
        import imessage
        rows = []
        for case in selected:
            if case.kind == 'setup':
                imessage.reset_onboarding()
                print(f'SETUP {case.id}  {case.send}', flush=True)
                continue
            # The expected greeting depends on state the greeting itself changes: compute it first.
            speak = imessage.expected_greeting(case.exact.split(':')[1]) if case.exact else None
            outcome = imessage.command(case.send) if case.kind == 'command' else imessage.turn(case.send)
            failures = check(case, outcome, speak)
            rows.append({'id': case.id, 'sent': case.send, 'ok': not failures, 'failures': failures,
                         'seconds': outcome.get('seconds'), 'reply': outcome.get('reply'),
                         'reply_len': outcome.get('reply_len'), 'tools': outcome.get('tools')})
            status = 'PASS' if not failures else 'FAIL'
            print(f'{status}  {case.id}  ({outcome.get("seconds")}s)  {case.send}', flush=True)
            for failure in failures:
                print(f'      - {failure}', flush=True)
    passed = sum(r['ok'] for r in rows)
    print(f'\n{passed}/{len(rows)} passed')
    reports = HERE / 'reports'
    reports.mkdir(exist_ok=True)
    out = reports / f'{time.strftime("%Y%m%d-%H%M%S")}{"-offline" if args.offline else ""}.json'
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    print(f'report: {out}')
    return 0 if passed == len(rows) else 1


if __name__ == '__main__':
    sys.exit(main())
