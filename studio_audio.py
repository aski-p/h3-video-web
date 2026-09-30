"""Studio creative audio: discard H3 audio; read only validated Korean text."""
import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile

POLICY = 'korean-script-tts-v1'
VOICE = 'ko-KR-SunHiNeural'

def validate(policy, text, seconds):
    if policy != POLICY:
        raise ValueError('unsupported Studio audio policy')
    if not isinstance(text, str):
        raise ValueError('Korean dialogue must be text')
    text = text.strip()
    if text and (not re.search('[가-힣]', text) or re.search(r'''[^가-힣\s.,!?…。！？“”‘’"'—-]''', text) or len(text) > 300 or len(re.findall('[가-힣]', text)) > max(1, int((seconds - 3) * 4))):
        raise ValueError('한국어 대사는 한글 문장으로 짧게 입력해 주세요.')
    return text

def _duration(path):
    return float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',path], timeout=20))

async def _speak(text, path):
    import edge_tts
    await asyncio.wait_for(edge_tts.Communicate(text, VOICE).save(path), timeout=60)

def apply(path, cfg):
    if not cfg.get('audio_policy'):
        return None  # Original footage and existing jobs keep their audio.
    text = validate(cfg['audio_policy'], cfg.get('dialogue_ko', ''), cfg['seconds'])
    duration = _duration(path)
    with tempfile.TemporaryDirectory(prefix='studio-audio-', dir=os.path.dirname(path)) as folder:
        output = os.path.join(folder, 'final.mp4')
        args = ['ffmpeg','-v','error','-nostdin','-y','-i',path]
        if text:
            voice = os.path.join(folder, 'dialogue.mp3')
            asyncio.run(_speak(text, voice))
            if _duration(voice) > duration - 1.5:
                raise ValueError('한국어 음성이 영상보다 깁니다. 대사를 줄여 주세요. 음성을 잘라서 완료하지 않습니다.')
            args += ['-i',voice,'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-af','adelay=1000:all=1,apad','-t',str(duration)]
        else:
            args += ['-map','0:v:0','-c:v','copy','-an']
        subprocess.run(args+['-movflags','+faststart',output],check=True,timeout=90)
        # No video encoding, frame interpolation, speed change, or model-generated audio.
        streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',output],timeout=20))['streams']
        assert sum(s['codec_type']=='audio' for s in streams)==int(bool(text))
        if abs(_duration(output)-duration)>0.15:raise ValueError('audio mux duration mismatch')
        os.replace(output,path)
    return {'policy':POLICY,'language':'ko','mode':'scripted_tts' if text else 'silent','voice':VOICE if text else None,'dialogue_sha256':hashlib.sha256(text.encode()).hexdigest(),'generated_audio_removed':True}
