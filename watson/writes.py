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

    def prepare(self, store, github, run_id, issue, text, kind):
        """Everything fallible that has no side effect: the fresh read, the
        state checks, the idempotency marker.

        Split from the send because the caller checkpoints between them. A
        failure here used to land AFTER that checkpoint, which marked the issue
        handled with nothing posted and no next look -- see the ordering note
        in workflow.cycle().
        """
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
            return {'existing':{'url':existing['url'],'recovered':True}}
        # STAGED, not committed: the claim rides the caller's cursor commit, so
        # the two land together or neither does. Claiming after the checkpoint
        # loses the update when it fails; claiming in its own transaction before
        # the checkpoint strands a key that every retry then collides with.
        key = store.stage_action(run_id, 'github_comment', payload)
        return {'payload':payload,'marker':marker,'text':text,'number':issue['number'],
                'key':key,'existing':None}

    def send(self, store, pending):
        """Post the claimed comment. The only uncertainty left after the
        checkpoint, which is what the claim above exists to adjudicate."""
        if pending['existing']:
            return pending['existing']
        key = pending['key']
        try:
            result = self.post(f'issues/{pending["number"]}/comments',
                               {'body':pending['text']+'\n\n'+pending['marker']})
            receipt = {'url':result['html_url'],'id':result['id']}
            store.action_result(key,'accepted',receipt)
            return receipt
        except Exception:
            store.action_result(key,'unknown',{'instruction':'Conferir comentário antes de reenviar.'})
            raise
