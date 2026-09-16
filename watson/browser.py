"""Execute owner-approved browser scenarios, never commands from issue text."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from .core import WatsonError, now, private_json


def origin(url):
    p=urlparse(url)
    if p.username or p.password or p.scheme not in {'http','https'} or not p.hostname:
        raise WatsonError('URL de homologação inválida.')
    if p.scheme=='http' and p.hostname not in {'127.0.0.1','localhost','::1'}:
        raise WatsonError('Homologação remota exige HTTPS.')
    return f'{p.scheme}://{p.netloc}'


def validate_profile(profile):
    origin(profile['url'])
    if not profile.get('test_environment'):
        raise WatsonError('O perfil precisa declarar um ambiente de teste.')
    steps=profile.get('steps',[])
    if profile.get('auto_plan') and not steps:
        return
    if not 1 <= len(steps) <= 30 or not any(s.get('action')=='expect_text' for s in steps):
        raise WatsonError('Cenário requer 1–30 passos e uma asserção de resultado.')
    for s in steps:
        if s.get('action') not in {'fill','click','expect_text'} or not isinstance(s.get('selector'),str):
            raise WatsonError('Passo não permitido no cenário.')
        if len(s['selector'])>300 or len(str(s.get('value','')))>3000:
            raise WatsonError('Passo grande demais.')
        if s.get('action')=='fill' and any(t in s['selector'].lower() for t in ['password','passwd','token','secret']):
            raise WatsonError('Credenciais só podem ser preenchidas na etapa privada de login.')


def credentials_path(home, number):
    return Path(home)/'access'/f'issue-{number}.json'


def save_access(home, number, username, password):
    if not username or not password:
        raise WatsonError('Informe usuário e senha de teste.')
    path=credentials_path(home,number)
    private_json(path,{'username':username,'password':password})
    os.chmod(path,0o600)
    return {'stored':True,'issue':number}


def run_browser(profile, access_path, output, sha, plan_builder=None, caption_builder=None):
    validate_profile(profile)
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=False,mode=0o700)
    allowed=origin(profile['url'])
    result={'status':'error','sha':sha,'url':profile['url'],'started_at':now(),
            'steps':[],'screenshots':[],'video':None,'trace':None,'scope':'Owner-approved scenario in a test environment; not production.'}
    with sync_playwright() as p:
        # A system proxy may intercept localhost. Only bypass it for loopback fixtures.
        args=['--no-proxy-server'] if urlparse(profile['url']).hostname in {'127.0.0.1','localhost','::1'} else []
        browser=p.chromium.launch(headless=True,args=args)
        context=None
        try:
            # Log in with a separate context: no login recording or traces.
            auth=browser.new_context(service_workers='block')
            def route(req):
                parsed=urlparse(req.request.url)
                if parsed.scheme in {'http','https'} and f'{parsed.scheme}://{parsed.netloc}'==allowed:
                    req.continue_()
                else:
                    req.abort()
            auth.route('**/*',route)
            page=auth.new_page(); page.goto(profile['url'],wait_until='domcontentloaded',timeout=20000)
            login=profile.get('login')
            if login:
                if not access_path or not Path(access_path).exists():
                    raise WatsonError('Aguardando acesso de teste pelo canal privado.')
                secrets=json.loads(Path(access_path).read_text())
                page.locator(login['username']).fill(secrets['username'])
                page.locator(login['password']).fill(secrets['password'])
                page.locator(login['submit']).click()
                page.locator(login['ready']).wait_for(state='visible',timeout=10000)
                del secrets
            if profile.get('auto_plan') and not profile.get('steps'):
                if not plan_builder:
                    raise WatsonError('Planejador não configurado para este cenário.')
                controls=page.locator('input, button, [role=status], [role=alert]').evaluate_all('''els => els
                    .filter(e => e.getClientRects().length && e.type !== 'password' && !e.closest('#login'))
                    .map(e => ({selector: e.id && /^[a-zA-Z][\\w-]*$/.test(e.id) ? '#'+e.id :
                        (e.tagName === 'BUTTON' && e.closest('form')?.id ? '#'+e.closest('form').id+' button' : null),
                        tag:e.tagName, type:e.type || '', text:e.innerText || '',
                        label:e.labels ? Array.from(e.labels).map(l=>l.innerText).join(' ') : ''}))
                    .filter(e=>e.selector)''')
                steps=plan_builder(controls)
                allowed_selectors={c['selector'] for c in controls}
                if any(s.get('selector') not in allowed_selectors for s in steps):
                    raise WatsonError('Planejador selecionou um controle não observado.')
                profile={**profile,'steps':steps,'auto_plan':False}
                validate_profile(profile)
                result['generated_plan']=steps
            state=auth.storage_state()  # in memory only, never written or sent to a model
            auth.close()
            context=browser.new_context(storage_state=state,service_workers='block',viewport={'width':1100,'height':760},
                                        record_video_dir=str(output/'video'),record_video_size={'width':1100,'height':760})
            context.route('**/*',route)
            # Login requests and storage state are not traced. Traces can still contain
            # authenticated request metadata: keep them local/private, never upload automatically.
            context.tracing.start(screenshots=True,snapshots=True,sources=False)
            started=time.monotonic()
            page=context.new_page(); page.goto(profile['url'],wait_until='domcontentloaded',timeout=20000)
            if login: page.locator(login['ready']).wait_for(state='visible')
            events=[]
            if caption_builder:
                intro=time.monotonic()-started
                page.wait_for_timeout(2500)
                events.append({'start':intro,'end':time.monotonic()-started,
                               'event':'Test environment ready; test account signed in.' if login else 'Test environment ready.'})
            result['status']='passed'
            for i,step in enumerate(profile['steps'],1):
                step_started=time.monotonic()-started
                record={'step':i,'action':step['action'],'description':step.get('description',step['action'])}
                target=page.locator(step['selector'])
                try:
                    if step['action']=='fill': target.fill(step.get('value',''))
                    elif step['action']=='click': target.click(timeout=10000)
                    else:
                        from playwright.sync_api import expect
                        expect(target).to_have_text(step['value'],timeout=4000)
                    record['status']='passed'
                except AssertionError:
                    record['status']='failed'; record['expected']=step['value']; record['actual']=target.inner_text()[:2000]
                    result['status']='failed'
                result['steps'].append(record)
                if caption_builder:
                    if step['action']=='expect_text':
                        checked=time.monotonic()-started
                        # Hold the observed result long enough to read its subtitle.
                        page.wait_for_timeout(3500)
                        if checked-step_started > .3:
                            events.append({'start':step_started,'end':checked,'event':'Checking the expected result.'})
                        else: checked=step_started
                        events.append({'start':checked,'end':time.monotonic()-started,'event':record})
                    else:
                        page.wait_for_timeout(2500)
                        events.append({'start':step_started,'end':time.monotonic()-started,
                                       'event':{'action':step['action'],'description':record['description'],'status':record['status']}})
                else: page.wait_for_timeout(650)
                shot=output/f'step-{i:02}.png'; page.screenshot(path=str(shot)); result['screenshots'].append(str(shot))
                if result['status']=='failed': break
            page.wait_for_timeout(1200)
            video=page.video
            context.tracing.stop(path=str(output/'trace.zip')); result['trace']=str(output/'trace.zip')
            context.close(); context=None
            if video: result['video']=str(video.path())
            result['caption_events']=events
        except (WatsonError,BrowserTimeout) as e:
            result['status']='blocked'; result['error']='Acesso ou navegação indisponível; cenário não validado.'
        except Exception:
            result['status']='error'; result['error']='Falha no navegador; não confirma o bug.'
        finally:
            if context: context.close()
            browser.close()
    if result['video']:
        try:
            import imageio_ffmpeg
            mp4=output/'test.mp4'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-i',result['video'],'-an','-c:v','libx264',
                            '-pix_fmt','yuv420p','-movflags','+faststart',str(mp4)],capture_output=True,check=True,timeout=90)
            result['video']=str(mp4)
        except (ImportError,subprocess.SubprocessError):
            result['video_note']='MP4 indisponível; WebM mantido localmente.'
        if caption_builder and result.get('caption_events'):
            try:
                from .captions import burn_captions
                narration=caption_builder(result['caption_events'])
                result['video']=burn_captions(result['video'],output,result['caption_events'],narration)
                result['captions']={'language':narration['language'],'style':'yellow with dark outline','file':str(output/'captions.srt')}
            except Exception:
                result['caption_note']='Legendas indisponíveis; vídeo original preservado.'
    result['finished_at']=now()
    for file in output.rglob('*'):
        if file.is_file(): os.chmod(file,0o600)
    private_json(output/'result.json',result)
    return result
