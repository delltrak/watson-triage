"""Measured inference usage exported through the unmodified official Index client.

The compatibility table mirrors the collector's input schema; it contains only
Watson invocation usage, not fabricated Hermes conversations or unrelated usage.
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from .core import private_json


def record_usage(home, label, model, usage):
    folder=Path(home)/'metrics'; folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    invocation=uuid.uuid4().hex; at=time.time()
    # Keep independent attempts rather than overwrite a run's audit file.
    private_json(folder/f'{invocation}.json',{'id':invocation,'label':label,'model':model,'usage':usage,'at':at})
    if not model: return  # Never invent a model name for leaderboard attribution.
    conn=sqlite3.connect(folder/'state.db'); os.chmod(folder/'state.db',0o600)
    conn.execute('''CREATE TABLE IF NOT EXISTS session_model_usage(
        session_id TEXT PRIMARY KEY,model TEXT,input_tokens INTEGER,output_tokens INTEGER,
        cache_read_tokens INTEGER,cache_write_tokens INTEGER,first_seen REAL,last_seen REAL)''')
    total=sum(max(0,int(u.get('input_tokens',0))) for u in usage)
    cached=sum(max(0,int(u.get('cached_input_tokens',0))) for u in usage)
    output=sum(max(0,int(u.get('output_tokens',0))) for u in usage)
    # input_tokens includes the cached part (see normalize_usage). The Index sums these categories.
    conn.execute('INSERT INTO session_model_usage VALUES(?,?,?,?,?,?,?,?)',
                 (invocation,model,max(0,total-cached),output,min(cached,total),0,at,at))
    conn.commit(); conn.close()


def index_client(home, *, register=False, dry_run=False):
    import subprocess
    import sys
    from .core import load_config, WatsonError
    from .delivery import Plow
    config=load_config(Path(home)); agent=config.get('agent_index_id')
    if not agent: raise WatsonError('Defina agent_index_id antes de usar o Agent Index.')
    folder=Path(home).resolve()/'metrics'
    isolated=folder/'client-home'; isolated.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not (folder/'state.db').exists():
        raise WatsonError('Ainda não há uso medido com modelo identificado para reportar.')
    token=Plow.from_config(config).token
    # Isolate the official collector from unrelated personal Hermes histories.
    env={'PATH':os.environ['PATH'],'HOME':str(isolated),'HERMES_HOME':str(folder),
         'PLOW_AGENT_TOKEN':token,'AGENT_ID':agent}
    command=[sys.executable,str(Path(__file__).parent/'vendor'/'agent_index_client.py'),'--agent',agent]
    if register:
        command+=['--register','--name','Watson','--blurb','GitHub issues investigated with memory, test evidence, and iMessage updates. Never merges.',
                  '--repo',config['agent_repository_url'],'--runtime','Hermes',
                  '--install-url',config.get('agent_install_url',config['agent_repository_url']+'/blob/main/README.md')]
    elif dry_run: command+=['--dry-run']
    result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=120)
    if result.returncode: raise WatsonError('O cliente oficial do Agent Index falhou; nenhum sucesso foi presumido.')
    return {'ok':True,'agent':agent,'operation':'register' if register else 'preview' if dry_run else 'report',
            'output':result.stdout.replace(token,'[redacted]')[:3000]}
