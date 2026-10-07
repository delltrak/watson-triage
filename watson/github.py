from __future__ import annotations

import base64
import json
import re
import subprocess
from urllib.parse import quote

from .core import WatsonError, repo_name, safe_source


class GitHubError(WatsonError):
    """A refused read and GitHub's HTTP status, the difference between a wrong
    repository (404), a wrong login (422) and revoked access (401)."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    """Read-only interface. No arbitrary URL, shell string or write method."""
    def __init__(self, repositories, run=subprocess.run):
        self.repositories = {repo_name(r) for r in repositories}
        self.run = run

    def get(self, repo, resource):
        if repo not in self.repositories:
            raise WatsonError(f'Repositório fora da lista configurada: {repo}')
        if (not re.fullmatch(r'(issues|pulls|commits|git/trees|contents|actions/runs)(/[^\s?#]*)?(\?[^\s#]*)?', resource)
                or '..' in resource.split('?')[0].split('/')):
            raise WatsonError('Recurso GitHub não permitido.')
        result = self.run(['gh', 'api', '--method', 'GET', f'repos/{repo}/{resource}'],
                          capture_output=True, text=True, timeout=90)
        if result.returncode:
            # Only the status is kept: gh's output quotes whatever GitHub sent.
            code = re.search(r'\(HTTP (\d{3})\)', result.stderr)
            # gh exits 4 when it holds no credential at all; the owner hears it as a 401.
            status = int(code[1]) if code else 401 if result.returncode == 4 else None
            raise GitHubError(f'Não foi possível ler {repo}/{resource.split("?")[0]} no GitHub'
                              + (f' (HTTP {status}).' if status else '.'), status)
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
        raise WatsonError('Conversa grande demais para a consulta completa; nenhum avanço foi salvo.')

    def assigned(self, repo, assignee):
        return [x for x in self.pages(repo, f'issues?state=open&assignee={quote(assignee,safe="")}&sort=updated')
                if 'pull_request' not in x]

    def issue(self, repo, number):
        if not isinstance(number, int) or number < 1:
            raise WatsonError('Número de issue inválido.')
        item = self.get(repo, f'issues/{number}')
        if 'pull_request' in item:
            raise WatsonError('O primeiro MVP acompanha issues, não PRs como assunto principal.')
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
            raise WatsonError(f'Árvore de {repo} truncada; limite explicitamente o escopo.')
        paths = [x['path'] for x in tree['tree'] if x['type'] == 'blob' and safe_source(x['path'])]
        if len(paths) > 6000:
            raise WatsonError('Repositório grande demais para o seletor atual de arquivos.')
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
                limitations.append(f'Referência a {repo}/{kind}/{ref} fora do escopo configurado.')
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
                        limitations.append(f'Commit {ref}: somente os primeiros 8 arquivos foram considerados.')
                    for item in files[:8]:
                        if not safe_source(item['filename']):
                            limitations.append(f'Commit {ref}: arquivo fora dos tipos permitidos foi omitido.')
                        elif not item.get('patch') or len(item['patch']) > 18000:
                            limitations.append(f'Diff de {item["filename"]} ausente ou limitado a 18000 caracteres.')
                    refs.append({'kind': 'commit', 'url': commit['html_url'], 'repository': repo,
                                 'sha': commit['sha'], 'files': [
                                     {'path': f['filename'], 'patch': f.get('patch', '')[:18000]}
                                     for f in files[:8] if safe_source(f['filename'])],
                                 'scope': 'Diff do commit citado; não confirma implantação ou estado atual.'})
            except WatsonError as e:
                limitations.append(str(e))
        if len(found) > 8:
            limitations.append('Somente as primeiras 8 referências explícitas foram consultadas.')
        return refs, limitations
