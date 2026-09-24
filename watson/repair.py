"""Opt-in patch proposals for a trusted test command, with draft PRs and no merge."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from .analysis import PlowInference, object_schema, STRING
from .cases import Cases
from .core import Store, WatsonError, load_config, digest, private_json, safe_source
from .github import GitHub
from .writes import GitHubWriter

PATCH_SCHEMA=object_schema({'title':STRING,'explanation':STRING,
    'files':{'type':'array','items':object_schema({'path':STRING,'content':STRING})}})


def repair(home, number):
    home=Path(home).resolve(); config=load_config(home); policy=config.get('repair')
    if not policy or not policy.get('enabled'):
        raise WatsonError('Correções e PRs não habilitados para este repositório.')
    repo=config['repository']; gh=GitHub([repo]); writer=GitHubWriter(repo,enabled=True)
    store=Store(home)
    try:
        with store.lock():
            case=Cases(store).get(repo,number)
            if not case or case['state']!='reproduced':
                raise WatsonError('É necessário reproduzir o problema antes de propor uma correção.')
            index=gh.source_index(repo); sha=index['sha']
            if case['data']['head_sha']!=sha:
                raise WatsonError('O código mudou; execute uma nova validação antes de corrigir.')
            allowed=policy['allowed_files']
            if not allowed or len(allowed)>12 or any(not safe_source(p) for p in allowed):
                raise WatsonError('Lista de arquivos autorizados inválida.')
            files={p:gh.file(repo,p,sha)['content'] for p in allowed}
            model=PlowInference.from_config(home,config)
            proposal=model.ask('Propose the smallest source fix and a meaningful regression test for the reproduced bug. '
                'Return complete file contents without line-number prefixes. Change only allowed files. '
                'Never change authentication, dependencies, workflows, credentials, or unrelated code. '
                'The draft PR title/explanation must use the issue language. Describe the final change only; '
                'do not mention test execution status, which the caller will add from real results.',
                {'issue':gh.issue(repo,number),'validation':case['data']['validation'],'files':files},
                PATCH_SCHEMA,f'repair-{number}-{sha[:10]}')
            changes=proposal.get('files',[])
            if not changes or len(changes)>12 or len({f['path'] for f in changes})!=len(changes):
                raise WatsonError('Proposta vazia ou arquivos duplicados.')
            for f in changes:
                if f['path'] not in allowed or not isinstance(f['content'],str) or len(f['content'])>70000:
                    raise WatsonError('Proposta fora do escopo permitido.')
            if not any(f['path'].startswith('tests/') for f in changes):
                raise WatsonError('A proposta precisa incluir um teste de regressão.')
            # Materialize only textual, allowlisted files. No checkout hooks/submodules.
            folder=home/'repairs'/f'{number}-{sha[:12]}'
            if folder.exists(): raise WatsonError('Já existe proposta para esta revisão; revise-a antes de repetir.')
            folder.mkdir(parents=True,mode=0o700)
            for path in index['paths']:
                if path.endswith('.py') or path in allowed:
                    raw=gh.file(repo,path,sha)['content']
                    contents='\n'.join(line.partition(': ')[2] for line in raw.splitlines())+'\n'
                    dest=folder/path; dest.parent.mkdir(parents=True,exist_ok=True); dest.write_text(contents)
            baseline = {}
            for f in changes:
                dest=folder/f['path']; dest.parent.mkdir(parents=True,exist_ok=True); dest.write_text(f['content'])
                if not f['path'].startswith('tests/'):
                    baseline[f['path']] = '\n'.join(line.partition(': ')[2] for line in files[f['path']].splitlines())+'\n'
            private_json(folder/'proposal.json',proposal)
            # No network, no host credentials, no writable host mount, no Docker socket.
            command=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL',
                     '--security-opt','no-new-privileges','--pids-limit','128','--memory','256m',
                     '--cpus','1','--tmpfs','/tmp:rw,noexec,nosuid,size=32m',
                     '-e','PYTHONDONTWRITEBYTECODE=1','-v',f'{folder}:/app:ro','-w','/app',
                     policy.get('test_image','python:3.13-slim'),
                     'python','-m','unittest','discover','-s','tests','-v']
            tested=subprocess.run(command,capture_output=True,text=True,timeout=120)
            (folder/'test.log').write_text(tested.stdout+tested.stderr)
            if tested.returncode: raise WatsonError('A correção não passou nos testes isolados; nenhum PR criado.')
            try:
                for path, content in baseline.items(): (folder/path).write_text(content)
                negative=subprocess.run(command,capture_output=True,text=True,timeout=120)
                (folder/'regression-before.log').write_text(negative.stdout+negative.stderr)
            finally:
                for f in changes: (folder/f['path']).write_text(f['content'])
            if negative.returncode == 0:
                raise WatsonError('O teste novo também passa sem a correção; nenhum PR criado.')
            if 'FAILED (' not in negative.stdout+negative.stderr:
                raise WatsonError('O teste na base não falhou por asserção; nenhum PR criado.')
            # Ensure source hasn't advanced during inference/testing.
            if gh.source_index(repo)['sha']!=sha: raise WatsonError('A base mudou; nenhum PR criado.')
            key=store.claim_action(case['data']['run_id'],'draft_pr',{'repo':repo,'issue':number,'base':sha,'files':changes})
            branch=f'watson/issue-{number}-{digest(changes)[:10]}'
            try:
                base_tree=gh.get(repo,f'commits/{sha}')['commit']['tree']['sha']
                tree=writer.post('git/trees',{'base_tree':base_tree,'tree':[{'path':f['path'],'mode':'100644','type':'blob','content':f['content']} for f in changes]})
                commit=writer.post('git/commits',{'message':proposal['title'],'tree':tree['sha'],'parents':[sha]})
                writer.post('git/refs',{'ref':f'refs/heads/{branch}','sha':commit['sha']})
                # Repo default branch read through gh metadata, not model input.
                meta=subprocess.run(['gh','repo','view',repo,'--json','defaultBranchRef'],capture_output=True,text=True,check=True)
                base=json.loads(meta.stdout)['defaultBranchRef']['name']
                body=proposal['explanation']+f'\n\nRefs #{number}\n\nValidation: regression tests passed in an isolated container with no network or host credentials. Browser reproduction was recorded against base commit `{sha}`. The fix still requires browser verification and human review.\n\nCreated as a draft by Watson. Watson never merges.'
                pr=writer.post('pulls',{'title':proposal['title'],'head':branch,'base':base,'body':body,'draft':True})
                receipt={'url':pr['html_url'],'branch':branch,'sha':commit['sha'],'test_log':str(folder/'test.log'),'workspace':str(folder)}
                store.action_result(key,'accepted',receipt); private_json(folder/'receipt.json',receipt)
                return receipt
            except Exception:
                store.action_result(key,'unknown',{'branch':branch,'instruction':'Conferir branch e PR antes de repetir.'})
                raise
    finally: store.db.close()
