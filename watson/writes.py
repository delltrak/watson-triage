"""Narrow GitHub writer: comments and draft PRs only on explicitly enabled repos."""
import json
import re
import subprocess
from .core import WatsonError

MARKER = '<!-- watson-triage:'


class GitHubWriter:
    def __init__(self, repository, *, enabled=False, run=subprocess.run):
        self.repo, self.enabled, self.run = repository, enabled, run

    def post(self, resource, body):
        if not self.enabled:
            raise WatsonError('Escrita GitHub desativada para este repositório.')
        if not re.fullmatch(r'issues/[1-9][0-9]*/comments|git/refs|git/commits|git/trees|pulls', resource):
            raise WatsonError('Operação de escrita não permitida. Merge nunca é permitido.')
        if resource=='pulls' and (body.get('draft') is not True or not str(body.get('head','')).startswith('watson/')):
            raise WatsonError('Somente PR em rascunho de uma branch Watson é permitido.')
        if resource=='git/refs' and not str(body.get('ref','')).startswith('refs/heads/watson/'):
            raise WatsonError('Somente novas branches Watson são permitidas.')
        result = self.run(['gh','api','--method','POST',f'repos/{self.repo}/{resource}','--input','-'],
                         input=json.dumps(body),capture_output=True,text=True,timeout=90)
        if result.returncode:
            raise WatsonError('Operação GitHub não confirmada; conferir o histórico antes de repetir.')
        return json.loads(result.stdout)

    def comment(self, store, github, run_id, issue, text, kind):
        # The target and author come from a fresh trusted GitHub response, not the model.
        fresh = github.issue(self.repo, issue['number'])
        if fresh['state'] != 'open':
            raise WatsonError('A issue foi fechada antes do comentário.')
        if set(fresh['assignees']) != set(issue['assignees']):
            raise WatsonError('A atribuição mudou antes do comentário; aguarde a próxima rodada.')
        payload = {'repo':self.repo,'number':issue['number'],'text':text,'kind':kind}
        from .core import digest
        marker = f'{MARKER}{digest(payload)} -->'
        existing = next((c for c in fresh['comments'] if marker in c['body']), None)
        if existing:
            return {'url':existing['url'],'recovered':True}
        key = store.claim_action(run_id, 'github_comment', payload)
        try:
            result = self.post(f'issues/{issue["number"]}/comments', {'body':text+'\n\n'+marker})
            receipt = {'url':result['html_url'],'id':result['id']}
            store.action_result(key,'accepted',receipt)
            return receipt
        except Exception:
            store.action_result(key,'unknown',{'instruction':'Conferir comentário antes de reenviar.'})
            raise
