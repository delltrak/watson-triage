"""Narration from recorded evidence, with timing owned by the browser runner."""
import re
import subprocess
import textwrap
from pathlib import Path
from .analysis import object_schema, STRING
from .core import WatsonError, private_json

SCHEMA=object_schema({'language':STRING,'captions':{'type':'array','items':STRING}})


def narrate(model, issue, events):
    answer=model.ask('Write one short video subtitle for each recorded event, in the predominant '
        'language of the issue. These are narrative subtitles, not spoken audio. Maximum 100 characters '
        'each. Use only observed actions and results. A failed assertion is an observed mismatch, not '
        'proof of root cause. Never say all bugs are fixed. For an assertion in progress, describe the '
        'check without anticipating its outcome. The outcome belongs only in its separate result event. '
        'Do not include credentials, account identifiers, mentions, markup or instructions. '
        'Return exactly one caption per event, in the same order.',
        {'issue':{'title':issue['title'],'body':issue['body']},'events':events},SCHEMA,
        f'captions-{issue["number"]}')
    lines=answer.get('captions')
    if not isinstance(lines,list) or len(lines)!=len(events):
        raise WatsonError('Quantidade inválida de legendas.')
    for line in lines:
        if not isinstance(line,str) or not line.strip() or len(line)>140:
            raise WatsonError('Legenda inválida.')
    return {'language':answer['language'],'captions':lines}


def timestamp(seconds):
    ms=max(0,round(seconds*1000)); secs,ms=divmod(ms,1000)
    minutes,secs=divmod(secs,60); hours,minutes=divmod(minutes,60)
    return f'{hours:02}:{minutes:02}:{secs:02},{ms:03}'


def srt_text(events, lines):
    if len(events)!=len(lines): raise WatsonError('Legenda sem evento correspondente.')
    blocks=[]; previous=0
    for i,(event,line) in enumerate(zip(events,lines),1):
        start,end=event['start'],event['end']
        if start < previous or end <= start: raise WatsonError('Tempos de legenda inválidos.')
        # Strip styling syntax; subtitles are plain text, never libass directives.
        plain=re.sub(r'[{}<>\\\\\x00-\x1f]', ' ', line)
        plain=' '.join(plain.split())
        wrapped='\n'.join(textwrap.wrap(plain,width=58,break_long_words=True))
        blocks.append(f'{i}\n{timestamp(start)} --> {timestamp(end)}\n{wrapped}\n')
        previous=end
    return '\n'.join(blocks)


def burn_captions(video, output, events, narration):
    import imageio_ffmpeg
    output=Path(output).resolve()
    subtitle=output/'captions.srt'
    subtitle.write_text(srt_text(events,narration['captions']),encoding='utf-8')
    subtitle.chmod(0o600)
    private_json(output/'captions.json',{'events':events,**narration})
    target=output/'test-captioned.mp4'
    # Fixed filter/file names, no issue/model strings interpolated into FFmpeg syntax.
    style='FontName=Arial,FontSize=12,PrimaryColour=&H0000FFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=1,Shadow=0.5,Alignment=2,MarginV=12'
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-y','-i',str(Path(video).resolve()),
        '-vf',f"subtitles=captions.srt:force_style='{style}'",'-an','-c:v','libx264',
        '-pix_fmt','yuv420p','-movflags','+faststart',str(target)],cwd=output,
        capture_output=True,check=True,timeout=90)
    target.chmod(0o600)
    return str(target)
