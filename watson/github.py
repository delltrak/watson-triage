from __future__ import annotations

import base64
import json
import re
import subprocess
from urllib.parse import quote

from .core import WatsonError, both, repo_name, safe_source


class GitHub:
    """Read-only interface. No arbitrary URL, shell string or write method."""
    def __init__(self, repositories, run=subprocess.run):
        self.repositories = {repo_name(r) for r in repositories}
        self.run = run

    def get(self, repo, resource):
        if repo not in self.repositories:
            raise WatsonError(both(f'Repository outside the configured list: {repo}',
                                   f'Repositório fora da lista configurada: {repo}'))
        if (not re.fullmatch(r'(issues|pulls|commits|git/trees|contents|actions/runs)(/[^\s?#]*)?(\?[^\s#]*)?', resource)
                or '..' in resource.split('?')[0].split('/')):
            raise WatsonError('Recurso GitHub não permitido.')
        result = self.run(['gh', 'api', '--method', 'GET', f'repos/{repo}/{resource}'],
                          capture_output=True, text=True, timeout=90)
        if result.returncode:
            path = resource.split('?')[0]
            raise WatsonError(both(f'Could not read {repo}/{path} on GitHub.',
                                   f'Não foi possível ler {repo}/{path} no GitHub.'))
        return json.loads(result.stdout)

    def pages(self, repo, resource):
        all_items = []
        for page in range(1, 21):
            sep = '&' if '?' in resource else '?'
            batch = self.get(repo, f'{resource}{sep}per_page=100&page={page}')
            if not isinstance(batch, list):
                raise WatsonError('Resposta inesperada ao paginar o GitHub.')
            all_items.extend(batch)
            if len(batch) < 100:
                return all_items
        raise WatsonError(both('The conversation is too large to read in full; nothing was saved.',
                               'Conversa grande demais para a consulta completa; nenhum avanço foi salvo.'))

    def assigned(self, repo, assignee):
        return [x for x in self.pages(repo, f'issues?state=open&assignee={quote(assignee,safe="")}&sort=updated')
                if 'pull_request' not in x]

    def issue(self, repo, number):
        if not isinstance(number, int) or number < 1:
            raise WatsonError('Número de issue inválido.')
        item = self.get(repo, f'issues/{number}')
        if 'pull_request' in item:
            raise WatsonError(both(
                f'#{number} is a pull request, not an issue. Send the issue number it relates to '
                f'(e.g. the N in "Refs #N").',
                f'#{number} é um PR, não uma issue. Manda o número da issue ligada a ele '
                f'(ex.: o N de "Refs #N").'))
        comments = self.pages(repo, f'issues/{number}/comments')
        return {
            'repository': repo, 'number': number, 'title': item['title'], 'body': item.get('body') or '',
            'state': item['state'], 'state_reason': item.get('state_reason'),
            'author': item['user']['login'], 'assignees': [x['login'] for x in item['assignees']],
            'url': item['html_url'], 'updated_at': item['updated_at'],
            'comments': [{'id': c['id'], 'author': c['user']['login'], 'body': c.get('body') or '',
                          'url': c['html_url'], 'created_at': c['created_at'], 'updated_at': c['updated_at']}
                         for c in comments],
        }

    def source_index(self, repo):
        # commits without a ref returns the default branch's history.
        latest = self.get(repo, 'commits?per_page=1')
        sha = latest[0]['sha']
        tree = self.get(repo, f'git/trees/{sha}?recursive=1')
        if tree.get('truncated'):
            raise WatsonError(both(f'The file tree of {repo} is truncated; narrow the scope explicitly.',
                                   f'Árvore de {repo} truncada; limite explicitamente o escopo.'))
        paths = [x['path'] for x in tree['tree'] if x['type'] == 'blob' and safe_source(x['path'])]
        if len(paths) > 6000:
            raise WatsonError(both('The repository is too large for the current file selector.',
                                   'Repositório grande demais para o seletor atual de arquivos.'))
        return {'repository': repo, 'sha': sha, 'paths': paths}

    def ci(self, repo, sha):
        if not re.fullmatch('[a-f0-9]{40}', sha):
            raise WatsonError('Revisão inválida para consultar o CI.')
        runs = self.get(repo, f'actions/runs?head_sha={sha}&per_page=10').get('workflow_runs', [])
        evidence = []
        for run in runs[:10]:
            jobs = self.get(repo, f'actions/runs/{run["id"]}/jobs?per_page=100').get('jobs', [])
            artifacts = self.get(repo, f'actions/runs/{run["id"]}/artifacts?per_page=100').get('artifacts', [])
            evidence.append({'url':run['html_url'], 'sha':run['head_sha'], 'name':run['name'],
                'status':run['status'], 'conclusion':run['conclusion'],
                'jobs':[{'name':j['name'],'conclusion':j['conclusion'],'url':j['html_url'],
                         'failed_steps':[s['name'] for s in j.get('steps',[]) if s.get('conclusion')=='failure']} for j in jobs],
                'artifacts':[{'id':a['id'],'name':a['name'],'expired':a['expired']} for a in artifacts]})
        return evidence

    def file(self, repo, path, sha):
        if not safe_source(path) or not re.fullmatch('[a-f0-9]{40}', sha):
            raise WatsonError('Arquivo ou revisão não permitidos.')
        data = self.get(repo, f'contents/{quote(path,safe="/")}?ref={sha}')
        if not isinstance(data, dict) or data.get('encoding') != 'base64' or data.get('size', 0) > 70000:
            raise WatsonError('Arquivo não textual ou maior que o limite de leitura.')
        content = base64.b64decode(data['content']).decode('utf-8')
        return {'repository': repo, 'path': path, 'sha': sha,
                'url': f'https://github.com/{repo}/blob/{sha}/{quote(path,safe="/")}',
                'content': '\n'.join(f'{i}: {line}' for i, line in enumerate(content.splitlines(), 1))}

    def references(self, issue):
        conversation = '\n'.join([issue['body']] + [x['body'] for x in issue['comments']])
        pattern = r'https://github\.com/([\w.-]+/[\w.-]+)/(pull|commit)/([a-f0-9]+)'
        found = list(dict.fromkeys(re.findall(pattern, conversation)))
        refs, limitations = [], []
        for repo, kind, ref in found[:8]:
            if repo not in self.repositories:
                limitations.append(both(f'Reference to {repo}/{kind}/{ref} is outside the configured scope.',
                                        f'Referência a {repo}/{kind}/{ref} fora do escopo configurado.'))
                continue
            try:
                if kind == 'pull' and ref.isdigit():
                    pr = self.get(repo, f'pulls/{ref}')
                    refs.append({'kind': 'pull_request', 'url': pr['html_url'], 'repository': repo,
                                 'title': pr['title'], 'state': pr['state'], 'merged': pr['merged'],
                                 'merged_at': pr['merged_at'], 'base': pr['base']['ref'],
                                 'head_sha': pr['head']['sha'], 'merge_sha': pr['merge_commit_sha']})
                elif kind == 'commit' and re.fullmatch('[a-f0-9]{40}', ref):
                    commit = self.get(repo, f'commits/{ref}')
                    files = commit['files']
                    if len(files) > 8:
                        limitations.append(both(f'Commit {ref}: only the first 8 files were considered.',
                                            f'Commit {ref}: somente os primeiros 8 arquivos foram considerados.'))
                    for item in files[:8]:
                        if not safe_source(item['filename']):
                            limitations.append(both(f'Commit {ref}: a file outside the allowed types was left out.',
                                                f'Commit {ref}: arquivo fora dos tipos permitidos foi omitido.'))
                        elif not item.get('patch') or len(item['patch']) > 18000:
                            limitations.append(both(f'Diff of {item["filename"]} missing or capped at 18000 characters.',
                                                f'Diff de {item["filename"]} ausente ou limitado a 18000 caracteres.'))
                    refs.append({'kind': 'commit', 'url': commit['html_url'], 'repository': repo,
                                 'sha': commit['sha'], 'files': [
                                     {'path': f['filename'], 'patch': f.get('patch', '')[:18000]}
                                     for f in files[:8] if safe_source(f['filename'])],
                                 'scope': 'Diff do commit citado; não confirma implantação ou estado atual.'})
            except WatsonError as e:
                limitations.append(str(e))
        if len(found) > 8:
            limitations.append(both('Only the first 8 explicit references were read.',
                                    'Somente as primeiras 8 referências explícitas foram consultadas.'))
        return refs, limitations

    # --- Linked-PR awareness (all GETs stay inside the allowlist in get()) ---------------
    def bounded(self, repo, resource, max_pages):
        """Like pages(), but reports truncation instead of raising (tri-state, from Knight)."""
        items = []
        for page in range(1, max_pages + 1):
            sep = '&' if '?' in resource else '?'
            batch = self.get(repo, f'{resource}{sep}per_page=100&page={page}')
            if not isinstance(batch, list):
                raise WatsonError('Resposta inesperada ao paginar o GitHub.')
            items.extend(batch)
            if len(batch) < 100:
                return items, True
        return items, False

    def linked_pull_requests(self, repo, issue, head_sha, max_pulls=3):
        """PRs that cite the issue in its timeline. Limitations are both(en, pt)."""
        from .linked import link_kind, targets, TRUSTED
        number = issue['number']
        try:
            events, complete = self.bounded(repo, f'issues/{number}/timeline', 5)
        except Exception:  # noqa: BLE001 — enrichment only; unverifiable is not "no PR"
            return {'checked': False, 'complete': False, 'reopened_at': None, 'pulls': [],
                    'limitations': [both('Could not check PRs linked to the issue.',
                                         'Não consegui verificar PRs ligados à issue.')]}
        limitations = [] if complete else [both(
            'Long timeline; recent linked PRs may have been left out.',
            'Linha do tempo longa; PRs ligados recentes podem ter ficado de fora.')]
        found, foreign, reopened_at, first_ref = {}, set(), None, {}
        for event in events:
            kind = event.get('event')
            if kind == 'reopened':
                reopened_at = event.get('created_at')
            elif kind == 'connected':
                limitations.append(both('A PR is linked manually in the sidebar; the API does not say which.',
                                        'Há PR ligado manualmente pela barra lateral; a API não diz qual.'))
            if kind != 'cross-referenced':
                continue
            src = (event.get('source') or {}).get('issue') or {}
            if not src.get('pull_request') or not isinstance(src.get('number'), int):
                continue  # cross-reference from another issue, not a PR
            src_repo = (src.get('repository') or {}).get('full_name')
            if src_repo != repo:  # other repos (even configured ones): out of scope for now
                foreign.add(f'{src_repo}#{src.get("number")}')
                continue
            key, at = src['number'], event.get('created_at')
            found[key] = src
            if at and (key not in first_ref or at < first_ref[key]):
                first_ref[key] = at
        if foreign:
            limitations.append(both(f'{len(foreign)} PR(s) from other repositories cite the issue; out of scope.',
                                    f'{len(foreign)} PR(s) de outros repositórios citam a issue; fora do escopo.'))
        pulls = []
        for n, src in found.items():
            merged_at = src['pull_request'].get('merged_at')
            pulls.append({'kind': 'pull_request', 'repository': repo, 'number': n,
                          'url': src.get('html_url', ''), 'title': src.get('title', ''),
                          'author': (src.get('user') or {}).get('login'),
                          'author_association': src.get('author_association'),
                          'state': 'merged' if merged_at else src.get('state'), 'draft': bool(src.get('draft')),
                          'merged_at': merged_at, 'referenced_at': first_ref.get(n),
                          'link': link_kind(repo, number, repo, src.get('title'), src.get('body')),
                          'targets': targets(src.get('body')),
                          'body_excerpt': (src.get('body') or '')[:2000], 'hydrated': False})
        rank = {'open': 0, 'merged': 1, 'closed': 2}
        relevant = [p for p in pulls if p['link'] in {'closes', 'refs'} and p['state'] in {'open', 'merged'}
                    and (p['state'] == 'merged' or p['author_association'] in TRUSTED)]
        # Relevant (trusted/merged) PRs first, so untrusted ones can never push them out.
        pulls.sort(key=lambda p: (p not in relevant, p['link'] not in {'closes', 'refs'},
                                  rank.get(p['state'], 3), -p['number']))
        relevant.sort(key=lambda p: (rank.get(p['state'], 3), -p['number']))
        if len(relevant) > max_pulls:
            limitations.append(both(f'Only the {max_pulls} most relevant linked PRs were detailed.',
                                    f'Somente os {max_pulls} PRs ligados mais relevantes foram detalhados.'))
        for pr in relevant[:max_pulls]:
            try:
                pr.update(self.pull_detail(pr, head_sha))
            except Exception:  # noqa: BLE001 — never block triage on enrichment
                limitations.append(both(f'PR #{pr["number"]} could not be detailed.',
                                        f'PR #{pr["number"]} não detalhado.'))
        if len(pulls) > 8:
            limitations.append(both(f'{len(pulls)} PRs cite the issue; only 8 were considered.',
                                    f'{len(pulls)} PRs citam a issue; só 8 foram considerados.'))
        return {'checked': True, 'complete': complete, 'reopened_at': reopened_at,
                'pulls': pulls[:8], 'limitations': limitations}

    def pull_detail(self, pr, head_sha):
        from .linked import verdicts, formal_reviews
        repo, n = pr['repository'], pr['number']
        data = self.get(repo, f'pulls/{n}')
        detail = {'hydrated': True, 'base': data['base']['ref'],
                  'default_branch': data['base']['repo'].get('default_branch'),
                  'head_ref': data['head']['ref'], 'head_sha': data['head']['sha'],
                  'merge_sha': data.get('merge_commit_sha') if data.get('merged') else None,
                  'merged_by': (data.get('merged_by') or {}).get('login'),
                  'on_default_branch': None, 'reverted': None, 'ci_merge': None}
        comments, comments_complete = self.bounded(repo, f'issues/{n}/comments', 2)
        reviews, reviews_complete = self.bounded(repo, f'pulls/{n}/reviews', 2)
        detail['reviews'] = {'formal': formal_reviews(reviews, detail['head_sha']),
                             'verdicts': verdicts(comments, detail['head_sha'], pr['author']),
                             'complete': comments_complete and reviews_complete}
        detail['ci_head'] = self.ci_summary(repo, detail['head_sha'])
        if detail['merge_sha']:
            detail['ci_merge'] = self.ci_summary(repo, detail['merge_sha'])
            if detail['base'] != detail['default_branch']:
                detail['on_default_branch'] = False
            else:
                detail['on_default_branch'], detail['reverted'] = self.reachable(
                    repo, detail['merge_sha'], data['merged_at'], head_sha)
        return detail

    def ci_summary(self, repo, sha):
        from .linked import ci_summary
        if not re.fullmatch('[a-f0-9]{40}', sha):
            raise WatsonError('Revisão inválida para consultar o CI.')
        return ci_summary(self.get(repo, f'actions/runs?head_sha={sha}&per_page=10').get('workflow_runs', []))

    def reachable(self, repo, merge_sha, merged_at, head_sha):
        """Is the merge commit in the default-branch history Watson analysed? No compare API
        (not allowlisted): list HEAD history in a +/-1h window around merged_at. Committer date
        can precede merged_at by ~1s (PR #70/#41), hence the window."""
        from datetime import datetime, timedelta
        if merge_sha == head_sha:
            return True, False
        at = datetime.fromisoformat(merged_at.replace('Z', '+00:00'))
        fmt = lambda d: d.strftime('%Y-%m-%dT%H:%M:%SZ')  # noqa: E731
        window = self.get(repo, f'commits?sha={head_sha}&since={fmt(at - timedelta(hours=1))}'
                                f'&until={fmt(at + timedelta(hours=1))}&per_page=100')
        on_default = True if any(c['sha'] == merge_sha for c in window) else (None if len(window) >= 100 else False)
        after, complete = self.bounded(repo, f'commits?sha={head_sha}&since={fmt(at - timedelta(hours=1))}', 5)
        reverted = any(f'This reverts commit {merge_sha}' in (c.get('commit') or {}).get('message', '')
                       for c in after)
        return on_default, (reverted if complete or reverted else None)
