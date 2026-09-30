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
LIPSYNC_POLICY = 'korean-script-lipsync-v2'

def validate(policy, text, seconds):
    if policy not in (POLICY,LIPSYNC_POLICY):
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
    if cfg['audio_policy']==LIPSYNC_POLICY:
        return _apply_lipsync(path,cfg,text)
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


def _apply_lipsync(path,cfg,text):
    if not text:raise ValueError('립싱크 영상에는 한국어 대사가 필요합니다.')
    root=os.environ.get('H3_LIPSYNC_ROOT','/home/aski/PGX/archives/20261001-lipsync/LatentSync')
    executable=os.path.join(os.path.dirname(root),'venv/bin/python')
    if not os.path.isfile(executable):raise RuntimeError('립싱크 처리 환경이 준비되지 않았습니다.')
    # Sampling has finished. Only unload idle ComfyUI models, never interrupt a prompt.
    import urllib.request
    queue=json.load(urllib.request.urlopen('http://127.0.0.1:8188/queue',timeout=10))
    if queue.get('queue_running') or queue.get('queue_pending'):raise RuntimeError('립싱크 GPU 처리 대기: 다른 ComfyUI 작업이 진행 중입니다.')
    request=urllib.request.Request('http://127.0.0.1:8188/free',data=json.dumps({'unload_models':True,'free_memory':True}).encode(),headers={'Content-Type':'application/json'})
    urllib.request.urlopen(request,timeout=30).close()
    with tempfile.TemporaryDirectory(prefix='studio-lipsync-',dir=os.path.dirname(path)) as folder:
        voice=os.path.join(folder,'voice.mp3');out=os.path.join(folder,'out.mp4');receipt_path=os.path.join(folder,'receipt.json')
        asyncio.run(_speak(text,voice))
        worker=os.path.join(os.path.dirname(__file__),'studio_lipsync_worker.py')
        subprocess.run([executable,worker,'--video',path,'--voice',voice,'--output',out,'--start',str(cfg['dialogue_start']),'--root',root,'--receipt',receipt_path],check=True,timeout=2100)
        checks=json.load(open(receipt_path))
        if abs(_duration(out)-_duration(path))>.1:raise ValueError('립싱크 결과의 영상 길이가 변경됐습니다.')
        os.replace(out,path)
    return {'policy':LIPSYNC_POLICY,'language':'ko','mode':'scripted_lipsync','voice':VOICE,'dialogue_sha256':hashlib.sha256(text.encode()).hexdigest(),'generated_audio_removed':True,'lipsync':checks}
