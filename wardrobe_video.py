"""Explicit opt-in wardrobe edits. Separate receipts; never an original-pixel fallback."""
import json,math,shutil,subprocess,time,urllib.request,uuid
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
    prompt=("<Picture 1> defines the exact adult facial identity. <Video 1> defines only the body movement, timing, full head-to-knee framing, background and stationary camera. Generate the same motion with the face from Picture 1. The adult woman is 28. CHANGE the video outfit to "+CHOICES[choice]+". No cardigan. Non-sexual ordinary fashion. Natural skin with soft highlights, visible subtle fabric texture and realistic folds. Preserve the entire head with margin above the hair. No zoom, no reframing, no additional action. Do not copy the video person's face. No text or logo.")
    g['5']['inputs'].update(prompt=prompt,width=width,height=height,length=length)
    g['15']['inputs']['image']=image;g['16']['inputs']['file']=video
    g['14']['inputs']['filename_prefix']=prefix
    return g

def api(path,data=None):
    req=urllib.request.Request(URL+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as r:return json.load(r)

def render(graph,output_node,record,check):
    if record.exists():raise ValueError('existing_generation_requires_review')
    record.write_text(json.dumps({'status':'submitting','client':str(uuid.uuid4())}))
    (record.with_suffix('.graph.json')).write_text(json.dumps(graph))
    pid=None
    try:
        check()
        response=api('/prompt',{'prompt':graph,'client_id':json.loads(record.read_text())['client']})
        pid=response['prompt_id'];record.write_text(json.dumps({'status':'submitted','promptId':pid}))
        for _ in range(2160):
            check()
            h=api('/history/'+pid).get(pid)
            if h:
                if h['status']['status_str']!='success':raise ValueError('wardrobe_generation_failed')
                entries=h.get('outputs',{}).get(str(output_node),{}).get('images',[])
                if not entries or entries[0].get('type')!='output':raise ValueError('wardrobe_output_missing')
                e=entries[0];path=(OUTPUT/e.get('subfolder','')/e['filename']).resolve()
                if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():raise ValueError('wardrobe_output_path')
                record.write_text(json.dumps({'status':'done','promptId':pid,'output':str(path)}));return path
            time.sleep(5)
        raise ValueError('wardrobe_timeout')
    except BaseException:
        if pid:
            try:api('/queue',{'delete':[pid]});api('/interrupt',{'prompt_id':pid})
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
    if len(stats.get('rawSkinDeltas',[]))!=source['frames'] or stats.get('model')!='hyperswap_1b_256' or stats.get('expressionFactor')!=0 or stats.get('appliedLabDelta') is not None:raise ValueError('wardrobe_face_coverage_failed')
    return {'policy':POLICY,'wardrobe':choice,'timingVerified':True,'faceCoverage':1,'sampleIdentityMean':sum(scores)/len(scores),'sampleIdentityMin':min(scores),'regeneratedFrames':True,'originalPixelsPreserved':False,'visualReview':'required','publishApproved':False,'dimensions':output,'engine':'minimax_h3_ref2va','model':MODEL,'steps':20,'faceModel':'hyperswap_1b_256','nativeMotionPreserved':False}

def process(folder,repo,cfg,choice,child,check):
    choice=normalize(choice);r=folder/'render';work=r/'wardrobe';work.mkdir()
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
    python=Path(cfg['engine'])/'.venv/bin/python';out=work/'face.mp4'
    child([str(python),str(repo/'ops/face-quality/trial.py'),'--engine',cfg['engine'],'--source',str(raw),'--portrait',str(folder/'portrait.jpg'),'--output',str(out),'--model','hyperswap_1b_256'],folder,'wardrobe-face.log',85)
    identity.rename(r/'original-face.mp4');source.rename(r/'original-source.mp4')
    # Comparison uses the same 24fps grid. Preserve archived originals and do not stretch time.
    child(['ffmpeg','-v','error','-y','-i',str(r/'original-source.mp4'),'-vf',f'fps=24,scale={width}:{height}','-frames:v',str(plan['frames']),'-an','-c:v','libx264','-crf','16',str(source)],folder,'wardrobe-source.log',90)
    child(['ffmpeg','-v','error','-y','-i',str(out),'-i',str(r/'original-source.mp4'),'-map','0:v:0','-map','1:a?','-c','copy','-t',str(duration),'-movflags','+faststart',str(identity)],folder,'wardrobe-mux.log',92)
    child(['ffmpeg','-v','error','-i',str(identity),'-f','null','-'],folder,'wardrobe-decode.log',94)
    review=work/'review';review.mkdir();(review/'output.mp4').symlink_to(identity.resolve())
    child([str(python),str(repo/'ops/face-quality/evaluate.py'),'--engine',cfg['engine'],'--source',str(source),'--portrait',str(folder/'portrait.jpg'),'--folder',str(review)],folder,'wardrobe-quality.log',95)
    output=probe(identity)
    if output!={'width':width,'height':height,'fps':24.0,'frames':plan['frames']}:raise ValueError('wardrobe_output_mismatch')
    receipt=verify(json.loads((review/'metrics.json').read_text()),json.loads(out.with_suffix('.stats.json').read_text()),probe(source),output,choice)
    receipt.update(sourceDimensions=meta,sourceDuration=meta['frames']/meta['fps'],outputDuration=duration,generatedFrames=plan['generatedFrames'],sourceTimingPreserved=False,comparisonTimingVerified=True)
    child(['ffmpeg','-v','error','-y','-i',str(source),'-i',str(identity),'-filter_complex','hstack=inputs=2','-an','-c:v','libx264','-crf','18','-movflags','+faststart',str(r/'comparison.mp4')],folder,'wardrobe-comparison.log',98)
    check()
    return receipt
