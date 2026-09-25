from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing, nullcontext
from pathlib import Path

from .analysis import PlowInference, render, triage
from .core import Store, WatsonError, load_config, login, now, private_json, repo_name
from .delivery import Plow, deliver
from .githubapp import describe, read_status, request
from .speech import generate_voice
from .github import GitHub


def last_sync(home):
    marker = Path(home) / 'baseline.json'
    return json.loads(marker.read_text()) if marker.exists() else {}


def sync(store, github, config):
    repo = config['repository']
    # A baseline belongs to one repository and login: after `config` changes
    # either, the next pass records a new one instead of tracking a backlog.
    previous = last_sync(store.home)
    baseline = (previous.get('repository'), previous.get('assignee')) == (repo, config['assignee'])
    assigned = github.assigned(repo, config['assignee'])
    new = []
    for issue in assigned:
        existing = store.db.execute('SELECT observed,assigned FROM issues WHERE repo=? AND number=?',
                                    (repo, issue['number'])).fetchone()
        store.observe(repo, issue)
        if baseline and (existing is None or not existing['assigned']):
            store.track(repo, issue['number'])
            new.append(issue['number'])
    store.db.execute('UPDATE issues SET assigned=0 WHERE repo=?', (repo,))
    store.db.executemany('UPDATE issues SET assigned=1 WHERE repo=? AND number=?',
                        [(repo, item['number']) for item in assigned])
    store.db.commit()
    private_json(store.home / 'baseline.json', {'repository': repo, 'assignee': config['assignee'], 'at': now()})
    return {'assigned_open': len(assigned), 'newly_tracked': new, 'initial_baseline': not baseline}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Watson: triagem com evidências e memória.')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('WATSON_HOME', '.watson')))
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--repo', required=True)
    init.add_argument('--assignee', required=True)
    init.add_argument('--related', action='append', default=[])
    init.add_argument('--model')
    init.add_argument('--delivery', choices=['text', 'both'], default='text')
    init.add_argument('--notify-owner', action='store_true',
                      help='Enviar a atualização de cada issue ao dono da linha Plow.')
    init.add_argument('--language', choices=['en', 'pt'], default='en',
                      help="The language of the messages Watson sends the owner on its own.")
    for cmd in ('track', 'untrack', 'triage'):
        sub.add_parser(cmd).add_argument('number', type=int)
    sub.add_parser('sync')
    conf = sub.add_parser('config', help='Change the repository, login or language of a configured install.')
    conf.add_argument('--repo')
    conf.add_argument('--assignee')
    conf.add_argument('--language', choices=['en', 'pt'])
    sub.add_parser('status')
    sub.add_parser('cycle', help='Rodada completa com efeitos explicitamente configurados.')
    access = sub.add_parser('access', help='Guardar acesso de teste localmente; nunca publicar na issue.')
    access.add_argument('number', type=int)
    repair = sub.add_parser('repair', help='Propor correção, testar isoladamente e abrir PR draft; nunca merge.')
    repair.add_argument('number', type=int)
    metrics = sub.add_parser('index', help='Cliente oficial do Agent Index; registro e métricas explícitos.')
    metrics_mode = metrics.add_mutually_exclusive_group()
    metrics_mode.add_argument('--register', action='store_true')
    metrics_mode.add_argument('--dry-run', action='store_true')
    sub.add_parser('mcp', help='Ponte stdio para Hermes; sem ferramentas de envio ou escrita GitHub.')
    github = sub.add_parser('github', help='Connect GitHub by a code the owner approves at github.com, never a token, '
                                           'or disconnect it.')
    github.add_argument('action', choices=['connect', 'disconnect', 'announce'])
    github.add_argument('--language', choices=['en', 'pt'],
                        help='The language the owner connects in, for the message that says it worked.')
    watch = sub.add_parser('watch', help='Uma rodada; não instala agendamento nem envia mensagens.')
    watch.add_argument('--once', action='store_true', required=True)
    watch.add_argument('--limit', type=int, default=3)
    show = sub.add_parser('show')
    show.add_argument('run_id', type=int)
    show.add_argument('--format', choices=['json', 'markdown'], default='markdown')
    say = sub.add_parser('voice')
    say.add_argument('run_id', type=int)
    say.add_argument('--output', type=Path, required=True)
    say.add_argument('--provider', choices=['chatgpt', 'macos'])
    send = sub.add_parser('deliver', help='Envio explícito ao dono da linha Plow configurada.')
    send.add_argument('run_id', type=int)
    send.add_argument('--audio', type=Path)
    args = parser.parse_args(argv)
    try:
        args.home = args.home.resolve()
        if args.command == 'index':
            from .metrics import index_client
            print(json.dumps(index_client(args.home,register=args.register,dry_run=args.dry_run),ensure_ascii=False,indent=2))
            return 0
        if args.command == 'cycle':
            from .workflow import cycle
            output = cycle(args.home)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 1 if output['errors'] else 0
        if args.command == 'access':
            import getpass
            from .browser import save_access
            if args.number < 1: raise WatsonError('Número de issue inválido.')
            print(json.dumps(save_access(args.home,args.number,input('Usuário de teste: '),getpass.getpass('Senha de teste: '))))
            return 0
        if args.command == 'repair':
            from .repair import repair
            print(json.dumps(repair(args.home,args.number),ensure_ascii=False,indent=2))
            return 0
        if args.command == 'mcp':
            from .mcp import serve
            serve(args.home)
            return 0
        if args.command == 'github':
            if args.action == 'announce':  # the service's, as the agent: sent once per connection
                from .workflow import announce
                print(json.dumps(announce(args.home), ensure_ascii=False, indent=2))
                return 0
            # No Store and no lock: this only asks root and reads its answer, so
            # it runs before init, and during a pass it reports `queued`.
            if args.language:
                args.home.mkdir(mode=0o700, parents=True, exist_ok=True)
                private_json(args.home / 'connect.json', {'language': args.language})
            print(json.dumps(request(args.action), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'status' and not (args.home / 'config.json').exists():
            # Setup starts from `status`, so it answers before init too, and
            # creates no database to do it.
            print(json.dumps({'configured': False, 'github': describe(read_status())}, ensure_ascii=False, indent=2))
            return 0
        store = Store(args.home)
        # The owner's commands do not wait for a pass, which holds the lock for
        # minutes: "track 123" mid-pass used to fail in chat. A running pass
        # finishes on the config it loaded. `show` only reads a finished run.
        with closing(store.db), \
                nullcontext() if args.command in {'track', 'untrack', 'status', 'config', 'show'} else store.lock():
            if args.command == 'init':
                if (args.home / 'config.json').exists():
                    raise WatsonError('Already configured; use watson config to change the repository, login or language.')
                config = {'repository': repo_name(args.repo), 'assignee': login(args.assignee),
                          'related_repositories': [repo_name(r) for r in args.related],
                          'model': args.model, 'delivery': args.delivery,
                          'notify_owner': args.notify_owner, 'language': args.language,
                          'speech_provider': 'chatgpt', 'speech_voice': 'sol', 'audio_mode': 'native'}
                private_json(args.home / 'config.json', config)
                output = {'initialized': str(args.home), 'config': config}
            else:
                config = load_config(args.home)
                github = GitHub([config['repository']] + config['related_repositories'])
                if args.command in {'track', 'untrack'}:
                    if args.number < 1:
                        raise WatsonError('Invalid issue number.')
                    store.track(config['repository'], args.number, args.command == 'track', explicit=True)
                    output = {'number': args.number, 'tracked': args.command == 'track'}
                elif args.command == 'sync':
                    output = sync(store, github, config)
                elif args.command == 'config':
                    if (args.repo, args.assignee, args.language) == (None, None, None):
                        raise WatsonError('Nothing to change: give --repo, --assignee or --language.')
                    if args.repo is not None:
                        config['repository'] = repo_name(args.repo)
                    if args.assignee is not None:
                        config['assignee'] = login(args.assignee)
                    if args.language is not None:
                        config['language'] = args.language
                    private_json(args.home / 'config.json', config)
                    output = {'config': config}
                elif args.command == 'triage':
                    # Built here, not above: `status`, `show`, `sync`, `track`,
                    # `voice` and `deliver` need no inference, and constructing
                    # it eagerly made an absent or malformed plow_credential_file
                    # fail commands that never touch the model.
                    output = triage(store, github,
                                    PlowInference.from_config(args.home, config),
                                    config, args.number)
                elif args.command == 'watch':
                    if not 1 <= args.limit <= 20:
                        raise WatsonError('O limite deve ficar entre 1 e 20.')
                    output = {'sync': sync(store, github, config), 'runs': [], 'skipped': []}
                    rows = store.db.execute('''SELECT number,explicit FROM issues
                        WHERE repo=? AND tracked=1 ORDER BY COALESCE(checked,'') ASC,number''',
                        (config['repository'],)).fetchall()
                    for row in rows[:args.limit]:
                        fresh = github.issue(config['repository'], row['number'])
                        if not row['explicit'] and config['assignee'] not in fresh['assignees']:
                            store.track(config['repository'], row['number'], False)
                            output['skipped'].append({'number': row['number'], 'reason': 'Não está mais atribuída ao usuário.'})
                            continue
                        run = triage(store, github,
                                     PlowInference.from_config(args.home, config),
                                     config, row['number'])
                        output['runs'].append({'run_id': run['run_id'], 'cached': run['cached']})
                        if fresh['state'] == 'closed':
                            store.track(config['repository'], row['number'], False)
                elif args.command == 'status':
                    last = args.home / 'last-cycle.json'
                    output = {'configured': True, 'github': describe(read_status()), 'config': config,
                              'last_cycle': json.loads(last.read_text()) if last.exists() else None,
                              **store.history()}
                elif args.command in {'show', 'voice'}:
                    run = store.run(args.run_id)
                    if run['status'] != 'complete':
                        raise WatsonError('Investigação ainda não concluída.')
                    result = json.loads(run['result'])
                    if args.command == 'voice':
                        output = {'audio': generate_voice(result['voice_script'], args.output, config, provider=args.provider)}
                    elif args.format == 'markdown':
                        print(render(result))
                        return 0
                    else:
                        output = result
                elif args.command == 'deliver':
                    if config['delivery'] == 'both' and not args.audio:
                        raise WatsonError('A preferência é texto e áudio; forneça --audio.')
                    output = deliver(store, args.run_id, Plow.from_config(config), args.audio,
                                     audio_mode=config.get('audio_mode', 'native'))
            print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (WatsonError, OSError, ValueError, KeyError) as exc:
        print(f'Watson: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
