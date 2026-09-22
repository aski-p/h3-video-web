"""Explicit opt-in wardrobe edits. Separate receipts; never an original-pixel fallback."""
import hashlib,importlib.util,json,math,shutil,subprocess,time,urllib.request,uuid
from pathlib import Path
POLICY='wardrobe-motion-v1-20260922'
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
    scale=min(1,768/max(width,height))
    return max(16,round(width*scale/16)*16),max(16,round(height*scale/16)*16)

def chunks(count):
    if not isinstance(count,int) or count<2 or count>1800:raise ValueError('invalid_frame_count')
    done=0
    while done<count:
        overlap=int(done>0);take=min(81-overlap,count-done)
        length=1+4*math.ceil((take+overlap-1)/4)
        yield {'offset':done,'take':take,'length':length,'overlap':overlap}
        done+=take

def node(kind,**inputs):return {'class_type':kind,'inputs':inputs}
def anchor_graph(image,choice,width,height,prefix):
    choice=normalize(choice)
    if choice=='original':raise ValueError('wardrobe_choice_required')
    prompt='Edit only the clothing of this adult woman (25–28): replace her outfit with '+CHOICES[choice]+'. Preserve exactly her face, hair, pose, hands, body proportions, skin tone, background, lighting, framing and camera. Natural opaque fabric and realistic folds. Non-sexual adult fashion presentation. Secure garment coverage, no nudity. Do not add people, text or accessories.'
    return {
     '1':node('UNETLoader',unet_name='qwen_image_edit_2511_fp8mixed.safetensors',weight_dtype='default'),
     '2':node('CLIPLoader',clip_name='qwen_2.5_vl_7b_fp8_scaled.safetensors',type='qwen_image',device='default'),
     '3':node('VAELoader',vae_name='qwen_image_vae.safetensors'),
     '4':node('LoadImage',image=image),
     '5':node('TextEncodeQwenImageEditPlus',clip=['2',0],vae=['3',0],image1=['4',0],prompt=prompt),
     '6':node('TextEncodeQwenImageEditPlus',clip=['2',0],vae=['3',0],image1=['4',0],prompt='changed face, altered pose, different background, malformed hands, extra limbs, transparent fabric, text, watermark'),
     '7':node('EmptySD3LatentImage',width=width,height=height,batch_size=1),
     '8':node('ModelSamplingAuraFlow',model=['1',0],shift=3),
     '9':node('CFGNorm',model=['8',0],strength=1),
     '10':node('KSampler',model=['9',0],seed=9222026,steps=20,cfg=4,sampler_name='euler',scheduler='simple',positive=['5',0],negative=['6',0],latent_image=['7',0],denoise=1),
     '11':node('VAEDecode',samples=['10',0],vae=['3',0]),
     '12':node('SaveImage',images=['11',0],filename_prefix=prefix)}

def motion_graph(repo,image,video,continuation,width,height,fps,part,prefix):
    spec=importlib.util.spec_from_file_location('wardrobe_trial',repo/'ops/wardrobe-trial/render.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    g=m.graph(image,video,prefix,frames=max(5,part['length']))
    g['10']['inputs'].update(width=width,height=height)
    g['15']['inputs'].update(width=width,height=height,length=part['length'],video_frame_offset=part['offset'])
    g['18']['inputs'].update(batch_index=part['overlap'],length=part['take'])
    g['19']['inputs']['fps']=fps
    g['6']['inputs']['text']='The same adult woman wearing exactly the outfit from the reference image. Non-sexual fashion presentation, secure garment coverage, no nudity. Exactly follow the driving video movement and timing. Preserve the reference background, lighting and camera framing. Realistic hands and stable garment texture, no new action, no text.'
    if continuation:
        g['22']=node('LoadImage',image=continuation);g['15']['inputs']['continue_motion']=['22',0]
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
        for _ in range(1080):
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
    if len(stats.get('rawSkinDeltas',[]))!=source['frames'] or stats.get('model')!='hyperswap_1b_256' or stats.get('expressionFactor')!=0:raise ValueError('wardrobe_face_coverage_failed')
    return {'policy':POLICY,'wardrobe':choice,'timingVerified':True,'faceCoverage':1,'sampleIdentityMean':sum(scores)/len(scores),'sampleIdentityMin':min(scores),'regeneratedFrames':True,'originalPixelsPreserved':False,'visualReview':'required','publishApproved':False,'dimensions':output}

def process(folder,repo,cfg,choice,child,check):
    choice=normalize(choice);r=folder/'render';work=r/'wardrobe';work.mkdir()
    source=r/'source.mp4';identity=r/'output.mp4';meta=probe(source);width,height=size(meta['width'],meta['height'])
    ident=folder.name
    # The original face-only output supplies an already verified adult identity.
    frame=COMFY/'input'/(ident+'-wardrobe-first.png')
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(identity),'-frames:v','1',str(frame)],check=True)
    anchor=render(anchor_graph(frame.name,choice,width,height,'wardrobe/'+ident+'/anchor'),'12',work/'anchor.json',check)
    ref=COMFY/'input'/(ident+'-wardrobe-anchor.png');shutil.copy2(anchor,ref);shutil.copy2(anchor,work/'anchor.png')
    video=COMFY/'input'/(ident+'-wardrobe-drive.mp4');shutil.copy2(identity,video)
    segments=[];continuation=None
    for i,part in enumerate(chunks(meta['frames'])):
        check()
        graph=motion_graph(repo,ref.name,video.name,continuation,width,height,meta['fps'],part,'wardrobe/'+ident+'/part'+str(i))
        path=render(graph,'20',work/('part'+str(i)+'.json'),check)
        expected={'width':width,'height':height,'fps':meta['fps'],'frames':part['take']}
        if probe(path)!=expected:raise ValueError('wardrobe_segment_timing_failed')
        local=work/('part'+str(i)+'.mp4');shutil.copy2(path,local);segments.append(local)
        last=COMFY/'input'/(ident+'-wardrobe-last-'+str(i)+'.png')
        subprocess.run(['ffmpeg','-v','error','-y','-sseof','-0.02','-i',str(local),'-frames:v','1',str(last)],check=True)
        if not last.is_file():
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(local),'-vf',f"select=eq(n\\,{part['take']-1})",'-frames:v','1',str(last)],check=True)
        continuation=last.name
    concat=work/'concat.txt';concat.write_text(''.join("file '"+p.name+"'\n" for p in segments))
    raw=work/'motion.mp4';subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(raw)],check=True)
    if probe(raw)!={'width':width,'height':height,'fps':meta['fps'],'frames':meta['frames']}:raise ValueError('wardrobe_concat_timing_failed')
    python=Path(cfg['engine'])/'.venv/bin/python';out=work/'face.mp4'
    child([str(python),str(repo/'ops/face-quality/trial.py'),'--engine',cfg['engine'],'--source',str(raw),'--portrait',str(folder/'portrait.jpg'),'--output',str(out),'--model','hyperswap_1b_256'],folder,'wardrobe-face.log',85)
    identity.rename(r/'original-face.mp4');source.rename(r/'original-source.mp4')
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(r/'original-source.mp4'),'-vf',f'scale={width}:{height}','-c:v','libx264','-crf','16','-c:a','copy',str(source)],check=True)
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(out),'-i',str(r/'original-source.mp4'),'-map','0:v:0','-map','1:a?','-c','copy','-movflags','+faststart',str(identity)],check=True)
    subprocess.run(['ffmpeg','-v','error','-i',str(identity),'-f','null','-'],check=True)
    review=work/'review';review.mkdir();(review/'output.mp4').symlink_to(identity.resolve())
    child([str(python),str(repo/'ops/face-quality/evaluate.py'),'--engine',cfg['engine'],'--source',str(source),'--portrait',str(folder/'portrait.jpg'),'--folder',str(review)],folder,'wardrobe-quality.log',95)
    receipt=verify(json.loads((review/'metrics.json').read_text()),json.loads(out.with_suffix('.stats.json').read_text()),probe(source),probe(identity),choice)
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(source),'-i',str(identity),'-filter_complex','hstack=inputs=2','-an','-c:v','libx264','-crf','18','-movflags','+faststart',str(r/'comparison.mp4')],check=True)
    return receipt
