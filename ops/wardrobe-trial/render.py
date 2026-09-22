"""Explicit wardrobe experiment; never a fallback for original-face production."""
import argparse, hashlib, json, shutil, subprocess, time, urllib.request
from pathlib import Path
BASE='http://127.0.0.1:8188'
COMFY=Path('/home/aski/ComfyUI')
OUT=Path('/home/aski/minimax-h3/output')
def api(path,data=None):
    req=urllib.request.Request(BASE+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as r:return json.load(r)
def node(kind,**inputs):return {'class_type':kind,'inputs':inputs}
def graph(image,video,prefix,frames=121):
    if frames<5 or frames>121 or frames%4!=1:raise ValueError('bounded_trial_frames_required')
    prompt='A fully clothed adult woman, same exact identity and clothing as the reference image, natural skin texture. Reproduce the driving video motion and timing. Stationary camera, same plain blue studio background, same composition and lighting. Realistic fabric follows the movement. No new action, no camera movement, no text.'
    return {
      '1':node('UNETLoader',unet_name='wan_animate_2_int8_convrot.safetensors',weight_dtype='default'),
      '2':node('LoraLoaderModelOnly',model=['1',0],lora_name='lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors',strength_model=1),
      '3':node('ModelSamplingSD3',model=['2',0],shift=5),
      '4':node('WanAnimate2Cache',model=['3',0],device='cpu',dtype='int8'),
      '5':node('CLIPLoader',clip_name='umt5_xxl_fp8_e4m3fn_scaled.safetensors',type='wan',device='default'),
      '6':node('CLIPTextEncode',clip=['5',0],text=prompt),
      '7':node('CLIPTextEncode',clip=['5',0],text='different identity, changing clothes, flicker, deformed hands, extra limbs, text, watermark, camera movement, plastic skin'),
      '8':node('VAELoader',vae_name='Wan2_1_VAE_bf16.safetensors'),
      '9':node('LoadImage',image=image),
      '10':node('ImageScale',image=['9',0],upscale_method='lanczos',width=432,height=768,crop='center'),
      '11':node('LoadVideo',file=video),
      '12':node('GetVideoComponents',video=['11',0]),
      '13':node('CLIPVisionLoader',clip_name='clip_vision_h.safetensors'),
      '14':node('CLIPVisionEncode',clip_vision=['13',0],image=['10',0],crop='none'),
      '15':node('WanAnimate2ToVideo',positive=['6',0],negative=['7',0],vae=['8',0],width=432,height=768,length=frames,batch_size=1,video_frame_offset=0,pose_strength=1,pose_start_percent=0,pose_end_percent=1,reference_image_strength=1,reference_image=['10',0],pose_video=['12',0],clip_vision_output=['14',0]),
      '16':node('KSampler',model=['4',0],seed=9222026,steps=6,cfg=1,sampler_name='lcm',scheduler='simple',positive=['15',0],negative=['15',1],latent_image=['15',2],denoise=1),
      '21':node('TrimVideoLatent',samples=['16',0],trim_amount=['15',3]),
      '17':node('VAEDecode',samples=['21',0],vae=['8',0]),
      '18':node('ImageFromBatch',image=['17',0],batch_index=0,length=frames-1),
      '19':node('CreateVideo',images=['18',0],fps=30),
      '20':node('SaveVideo',video=['19',0],filename_prefix=prefix,format='mp4',**{'format.codec':'h264'})}
def run(source,reference,dest):
    dest.mkdir(parents=True,exist_ok=True)
    receipt=dest/'trial.json'
    if receipt.exists():raise ValueError('trial_already_submitted_check_existing_job')
    ident=hashlib.sha256((str(source)+str(reference)+str(dest)).encode()).hexdigest()[:16]
    image='wardrobe-'+ident+reference.suffix;video='wardrobe-'+ident+'.mp4'
    shutil.copy2(reference,COMFY/'input'/image)
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(source),'-t','4','-vf','scale=432:768,fps=30','-an','-c:v','libx264','-crf','16',str(COMFY/'input'/video)],check=True)
    g=graph(image,video,'wardrobe-trial/'+ident)
    (dest/'graph.json').write_text(json.dumps(g,indent=2))
    result=api('/prompt',{'prompt':g,'client_id':'wardrobe-trial-'+ident})
    pid=result['prompt_id'];state={'promptId':pid,'source':str(source),'reference':str(reference),'status':'submitted','productionApproved':False,'model':'Wan Animate 2','frames':120,'fps':30,'width':432,'height':768}
    receipt.write_text(json.dumps(state,indent=2));print(json.dumps(state),flush=True)
    for _ in range(720):
        h=api('/history/'+pid).get(pid)
        if h:
            (dest/'history.json').write_text(json.dumps(h,indent=2))
            if h['status']['status_str']=='error':state['status']='failed';receipt.write_text(json.dumps(state,indent=2));raise ValueError('generation_failed')
            files=h.get('outputs',{}).get('20',{})
            entries=files.get('images',files.get('gifs',files.get('videos',[])))
            if not entries:raise ValueError('output_missing')
            v=entries[0];path=OUT/v.get('subfolder','')/v['filename'];shutil.copy2(path,dest/'output.mp4')
            subprocess.run(['ffmpeg','-v','error','-i',str(dest/'output.mp4'),'-f','null','-'],check=True)
            state['status']='rendered_requires_visual_review';receipt.write_text(json.dumps(state,indent=2));print(json.dumps({'result':str(dest/'output.mp4')}),flush=True);return
        time.sleep(5)
    raise TimeoutError('check_existing_prompt_before_retry')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--reference',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.source,a.reference,a.output)
