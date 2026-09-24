"""Explicit opt-in wardrobe edits. Separate receipts; never an original-pixel fallback."""
import json,math,re,shutil,subprocess,time,urllib.error,urllib.request,uuid
from pathlib import Path
POLICY='wardrobe-h3-ref2va-v2-20260922'
MODEL='minimax_h3_ref2va_pruned_int8_convrot.safetensors'
CHOICES={'dress':'an opaque navy blue knee-length short-sleeved dress with a crew neckline',
         'sportswear':'an opaque dark teal athletic crew-neck T-shirt and full-length black leggings',
         'casual':'an opaque white crew-neck T-shirt and relaxed blue jeans',
         'bikini':'an opaque navy blue two-piece bikini with secure straps and standard-coverage bottoms, ordinary swimwear',
         'swimsuit':'an opaque navy blue one-piece swimsuit with secure shoulder straps and standard coverage',
         'yoga':'an opaque muted sage fitted sleeveless yoga top and full-length high-waisted charcoal yoga leggings'}
COMFY=Path('/home/aski/ComfyUI');OUTPUT=Path('/home/aski/minimax-h3/output');URL='http://127.0.0.1:8188'
def normalize(value):
    if value is None:return 'original'
    if not isinstance(value,str) or value not in ('original',*CHOICES):raise ValueError('invalid_wardrobe')
    return value

def size(width,height):
    scale=min(1,1280/max(width,height))
    return max(16,math.floor(width*scale/16+.5)*16),max(16,math.floor(height*scale/16+.5)*16)

def frame_plan(meta):
    seconds=meta['frames']/meta['fps']
    if not math.isfinite(seconds) or not 2<=seconds<=15.001:raise ValueError('invalid_wardrobe_duration')
    frames=math.floor(seconds*24+1e-6)
    return {'frames':frames,'generatedFrames':5+17*math.ceil((frames-5)/17),'fps':24}

def motion_graph(repo,image,video,choice,width,height,length,prefix):
    choice=normalize(choice)
    if choice=='original':raise ValueError('wardrobe_choice_required')
    g=json.loads((repo/'ops/wardrobe-h3/graph.json').read_text())
    prompt=("<Picture 1> defines the exact adult facial identity. <Video 1> defines only the body movement, timing, full head-to-knee framing, background and stationary camera. Generate the same motion with the face from Picture 1. Exactly one adult woman, age 28. Her full face remains visible, unobstructed and large enough to recognize in every frame. No other people, faces, portraits, reflections or face-like background details. CHANGE the video outfit to "+CHOICES[choice]+". No cardigan. Non-sexual ordinary fashion. Natural skin with soft highlights, visible subtle fabric texture and realistic folds. Preserve the entire head with margin above the hair. No zoom, no reframing, no additional action. Do not copy the video person's face. No text or logo.")
    g['5']['inputs'].update(prompt=prompt,width=width,height=height,length=length)
    g['15']['inputs']['image']=image;g['16']['inputs']['file']=video
    g['14']['inputs']['filename_prefix']=prefix
    return g

def api(path,data=None):
    req=urllib.request.Request(URL+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as r:return json.load(r)

def _clock_seconds(value):
    parts=value.split(':')
    if not 1<=len(parts)<=3 or any(not p.isdigit() for p in parts):return None
    seconds=0
    for part in parts:seconds=seconds*60+int(part)
    return seconds

def _sampler_log(lines,total,submitted_at):
    """Read the last ComfyUI tqdm receipt after this prompt was submitted.

    The HTTP queue has no sampler counter. Journald is read-only evidence and may
    lag; callers must display its timestamp and never call it overall completion.
    """
    latest=None
    for row in lines:
        try:
            timestamp=int(row['__REALTIME_TIMESTAMP'])/1000000
            message=row.get('MESSAGE','')
            if isinstance(message,list):message=bytes(message).decode(errors='replace')
            if timestamp+1<submitted_at:continue
            if 'Prompt executed' in message:latest=None
            matches=list(re.finditer(r'(\d{1,3})%[^\r\n]*?(\d{1,3})/(\d{1,3}) \[(\d+:\d+(?::\d+)?)<(?:(\d+:\d+(?::\d+)?)|\?)',message))
            for match in matches:
                percent,step,maximum=map(int,match.group(1,2,3))
                if maximum!=total or not 0<=step<=total or percent!=round(step*100/total):continue
                latest={'step':step,'steps':total,'percent':percent,'remainingSeconds':_clock_seconds(match.group(5)) if match.group(5) else None,'observedAt':timestamp}
        except (KeyError,TypeError,ValueError):continue
    return latest

def generation_progress(folder):
    record=folder/'render/wardrobe/generation.json'
    if not record.is_file():return None
    try:
        generation=json.loads(record.read_text())
        prompt_id=generation.get('promptId')
        if not isinstance(prompt_id,str) or not re.fullmatch(r'[a-f0-9-]{36}',prompt_id):return None
        req=urllib.request.Request(URL+'/queue')
        with urllib.request.urlopen(req,timeout=3) as response:queue=json.load(response)
        running=any(entry[1]==prompt_id for entry in queue.get('queue_running',[]))
        pending=next((index+1 for index,entry in enumerate(queue.get('queue_pending',[])) if entry[1]==prompt_id),None)
        if not running:return {'status':'queued','queuePosition':pending} if pending is not None else None
        entry=next(entry for entry in queue['queue_running'] if entry[1]==prompt_id)
        submitted_at=entry[3].get('create_time',0)/1000 if len(entry)>3 and isinstance(entry[3],dict) else record.stat().st_mtime
        graph=json.loads(record.with_suffix('.graph.json').read_text())
        totals={int(node['inputs']['steps']) for node in graph.values() if 'steps' in node.get('inputs',{})}
        if len(totals)!=1:return {'status':'running'}
        total=totals.pop()
        if not 1<=total<=100:return {'status':'running'}
        result=subprocess.run(['journalctl','-u','comfyui-minimax-h3.service','--since','@'+str(max(0,int(submitted_at)-1)),'--no-pager','-o','json'],capture_output=True,text=True,timeout=3,check=False)
        if result.returncode:return {'status':'running'}
        lines=[]
        for line in result.stdout.splitlines():
            try:lines.append(json.loads(line))
            except json.JSONDecodeError:continue
        receipt=_sampler_log(lines,total,submitted_at)
        if not receipt:return {'status':'running','steps':total}
        return {'status':'running',**receipt,'stale':time.time()-receipt['observedAt']>5400}
    except (OSError,ValueError,KeyError,TypeError,IndexError,subprocess.TimeoutExpired,urllib.error.URLError):
        return None

def render(graph,output_node,record,check):
    graph_record=record.with_suffix('.graph.json')
    if record.exists():
        if not graph_record.exists() or json.loads(graph_record.read_text())!=graph:
            raise ValueError('existing_generation_requires_review')
        state=json.loads(record.read_text())
        if state.get('status')=='done':
            path=Path(state['output']).resolve()
            if path.is_relative_to(OUTPUT.resolve()) and path.is_file():return path
            raise ValueError('wardrobe_output_missing')
    else:
        state={'status':'submitting','client':str(uuid.uuid4()),'attempts':0}
        record.write_text(json.dumps(state))
        graph_record.write_text(json.dumps(graph))
    pid=state.get('promptId')
    missing=0
    try:
        while True:
            check()
            if not pid:
                if state.get('attempts',0)>=3:raise ValueError('wardrobe_submission_retries_exhausted')
                response=api('/prompt',{'prompt':graph,'client_id':state['client']})
                pid=response['prompt_id']
                state.update(status='submitted',promptId=pid,attempts=state.get('attempts',0)+1)
                record.write_text(json.dumps(state))
            h=api('/history/'+pid).get(pid)
            if h:
                if h['status']['status_str']!='success':raise ValueError('wardrobe_generation_failed')
                entries=h.get('outputs',{}).get(str(output_node),{}).get('images',[])
                if not entries or entries[0].get('type')!='output':raise ValueError('wardrobe_output_missing')
                e=entries[0];path=(OUTPUT/e.get('subfolder','')/e['filename']).resolve()
                if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():raise ValueError('wardrobe_output_path')
                state.update(status='done',output=str(path))
                record.write_text(json.dumps(state));return path
            queue=api('/queue')
            if any(entry[1]==pid for key in ('queue_running','queue_pending') for entry in queue.get(key,[])):
                missing=0
            else:
                missing+=1
                if missing>=3:
                    outputs=list((OUTPUT/'wardrobe-h3').glob(record.parent.parent.parent.name+'_*.mp4'))
                    if len(outputs)==1:
                        state.update(status='done',output=str(outputs[0].resolve()))
                        record.write_text(json.dumps(state));return outputs[0]
                    if outputs:raise ValueError('wardrobe_output_ambiguous')
                    state.update(status='submitting',promptId=None)
                    record.write_text(json.dumps(state))
                    pid=None;missing=0
            time.sleep(5)
    except ValueError as error:
        if str(error)!='cancelled':raise
        if pid:
            try:
                queue=api('/queue')
                if any(entry[1]==pid for entry in queue.get('queue_pending',[])):api('/queue',{'delete':[pid]})
                if any(entry[1]==pid for entry in queue.get('queue_running',[])):api('/interrupt',{'prompt_id':pid})
            except Exception:pass
        raise

def probe(path):
    from fractions import Fraction
    s=json.loads(subprocess.check_output(['ffprobe','-v','error','-count_frames','-select_streams','v:0','-show_entries','stream=width,height,avg_frame_rate,nb_read_frames','-of','json',str(path)]))['streams'][0]
    return {'width':s['width'],'height':s['height'],'fps':float(Fraction(s['avg_frame_rate'])),'frames':int(s['nb_read_frames'])}

def verify(report,stats,source,output,choice):
    if source!=output:raise ValueError('wardrobe_timing_failed')
    expected=max(5,math.ceil(source['frames']/source['fps']))
    scores=report.get('output',{}).get('referenceSimilaritySamples',[])
    if len(scores)!=expected or any(s is None or s<.45 for s in scores) or sum(scores)/len(scores)<.65:raise ValueError('wardrobe_identity_failed')
    if not __import__('original_video').face_coverage(stats,source['frames']) or stats.get('model')!='hyperswap_1b_256' or stats.get('expressionFactor')!=0 or stats.get('appliedLabDelta') is not None:raise ValueError('wardrobe_face_coverage_failed')
    return {'policy':POLICY,'wardrobe':choice,'timingVerified':True,'faceCoverage':1,'sampleIdentityMean':sum(scores)/len(scores),'sampleIdentityMin':min(scores),'regeneratedFrames':True,'originalPixelsPreserved':False,'visualReview':'required','publishApproved':False,'dimensions':output,'engine':'minimax_h3_ref2va','model':MODEL,'steps':20,'faceModel':'hyperswap_1b_256','nativeMotionPreserved':False}

def stage_review_output(output,review):
    # The durable job directory is on SMB, where symbolic links are unsupported.
    review.mkdir(exist_ok=True)
    destination=review/'output.mp4'
    shutil.copy2(output,destination)
    return destination

def process(folder,repo,cfg,choice,child,check):
    choice=normalize(choice);r=folder/'render';work=r/'wardrobe';work.mkdir(exist_ok=True)
    source=r/'source.mp4';identity=r/'output.mp4';meta=probe(source);width,height=size(meta['width'],meta['height'])
    plan=frame_plan(meta);duration=plan['frames']/24;ident=folder.name
    # Use the original cleaned source for motion, exactly as in the approved H3 trial.
    # workflow.py's verified face-only output is kept separately, never used as fallback.
    ref=COMFY/'input'/(ident+'-h3-portrait.jpg');shutil.copy2(folder/'portrait.jpg',ref)
    video=COMFY/'input'/(ident+'-h3-motion.mp4');shutil.copy2(source,video)
    check()
    graph=motion_graph(repo,ref.name,video.name,choice,width,height,plan['generatedFrames'],'wardrobe-h3/'+ident)
    path=render(graph,'14',work/'generation.json',check)
    expected={'width':width,'height':height,'fps':24.0,'frames':plan['generatedFrames']}
    if probe(path)!=expected:raise ValueError('wardrobe_h3_generation_mismatch')
    raw=work/'motion.mp4'
    child(['ffmpeg','-v','error','-y','-i',str(path),'-an','-frames:v',str(plan['frames']),'-c:v','libx264','-crf','16','-movflags','+faststart',str(raw)],folder,'wardrobe-trim.log',80)
    python=Path(cfg['engine'])/'.venv/bin/python'
    if identity.exists():identity.rename(r/'original-face.mp4')
    source.rename(r/'original-source.mp4')
    # Comparison uses the same 24fps grid. Preserve archived originals and do not stretch time.
    child(['ffmpeg','-v','error','-y','-i',str(r/'original-source.mp4'),'-vf',f'fps=24,scale={width}:{height}','-frames:v',str(plan['frames']),'-an','-c:v','libx264','-crf','16',str(source)],folder,'wardrobe-source.log',90)
    source_meta=probe(source);receipt=None
    for attempt,detector_score in enumerate((.5,.35,.2),1):
        out=work/f'face-{attempt}.mp4';review=work/f'review-{attempt}'
        child([str(python),str(repo/'ops/face-quality/trial.py'),'--engine',cfg['engine'],'--source',str(raw),'--portrait',str(folder/'portrait.jpg'),'--output',str(out),'--model','hyperswap_1b_256','--selector-mode','one','--detector-score',str(detector_score)],folder,f'wardrobe-face-{attempt}.log',84+attempt)
        stage_review_output(out,review)
        child([str(python),str(repo/'ops/face-quality/evaluate.py'),'--engine',cfg['engine'],'--source',str(source),'--portrait',str(folder/'portrait.jpg'),'--folder',str(review)],folder,f'wardrobe-quality-{attempt}.log',88+attempt)
        try:
            receipt=verify(json.loads((review/'metrics.json').read_text()),json.loads(out.with_suffix('.stats.json').read_text()),source_meta,probe(out),choice)
            receipt.update(faceRepairAttempt=attempt,faceDetectorScore=detector_score,faceSelectorMode='one')
            break
        except ValueError as error:
            if str(error) not in ('wardrobe_identity_failed','wardrobe_face_coverage_failed') or attempt==3:raise
    child(['ffmpeg','-v','error','-y','-i',str(out),'-i',str(r/'original-source.mp4'),'-map','0:v:0','-map','1:a?','-c','copy','-t',str(duration),'-movflags','+faststart',str(identity)],folder,'wardrobe-mux.log',92)
    child(['ffmpeg','-v','error','-i',str(identity),'-f','null','-'],folder,'wardrobe-decode.log',94)
    output=probe(identity)
    if output!={'width':width,'height':height,'fps':24.0,'frames':plan['frames']}:raise ValueError('wardrobe_output_mismatch')
    receipt.update(sourceDimensions=meta,sourceDuration=meta['frames']/meta['fps'],outputDuration=duration,generatedFrames=plan['generatedFrames'],sourceTimingPreserved=False,comparisonTimingVerified=True)
    child(['ffmpeg','-v','error','-y','-i',str(source),'-i',str(identity),'-filter_complex','hstack=inputs=2','-an','-c:v','libx264','-crf','18','-movflags','+faststart',str(r/'comparison.mp4')],folder,'wardrobe-comparison.log',98)
    check()
    return receipt
