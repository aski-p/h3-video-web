"""Local LatentSync 1.6 runner. Only the speaking mouth region is composited."""
import argparse, json, math, os, subprocess, tempfile
from pathlib import Path

def probe(path):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]))

def composite(original, synced, output, start_frame, app, max_frames=None):
    import cv2
    import numpy as np
    a,b=cv2.VideoCapture(str(original)),cv2.VideoCapture(str(synced))
    fps=a.get(cv2.CAP_PROP_FPS);w,h=int(a.get(3)),int(a.get(4));count=int(a.get(cv2.CAP_PROP_FRAME_COUNT));n=int(b.get(cv2.CAP_PROP_FRAME_COUNT))
    if max_frames is not None:n=min(n,max_frames)
    if abs(fps-24)>.01:raise ValueError('Lip-sync input must retain 24 fps')
    if start_frame+n>count+1:raise ValueError('Speech window exceeds source video')
    writer=subprocess.Popen(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r',str(fps),'-i','pipe:0','-an','-c:v','libx264','-crf','17','-preset','fast',str(output)],stdin=subprocess.PIPE)
    edited=0
    try:
        for index in range(count):
            ok,frame=a.read()
            if not ok:raise ValueError('Source frame missing')
            if start_frame<=index<start_frame+n:
                ok,lip=b.read()
                if not ok:raise ValueError('Synced frame missing')
                faces=app.get(frame)
                if len(faces)!=1 or faces[0].det_score<.6:raise ValueError('Speaking face must remain clearly visible')
                face=faces[0];mouth=(face.kps[3]+face.kps[4])/2
                span=float(np.linalg.norm(face.kps[3]-face.kps[4]));gap=float(mouth[1]-face.kps[2][1])
                if gap<5 or span<10:raise ValueError('Speaking face angle unsuitable')
                mask=np.zeros((h,w),np.float32)
                cv2.ellipse(mask,tuple(mouth.astype(int)),(int(span*.95),int(max(span*.38,gap*.78))),0,0,360,1,-1)
                # Never replace eyes, brows or nose; feather only around the mouth.
                mask[:int(face.kps[2][1]+gap*.15)]=0
                mask=cv2.GaussianBlur(mask,(11,11),2)[:,:,None]
                frame=(frame*(1-mask)+lip*mask).clip(0,255).astype(np.uint8)
                edited+=1
            writer.stdin.write(frame.tobytes())
        writer.stdin.close()
        if writer.wait(timeout=120):raise RuntimeError('Video composition failed')
    finally:
        a.release();b.release()
        if writer.poll() is None:writer.kill()
    return {'source_frames':count,'edited_frames':edited,'fps':fps,'region':'mouth_only','original_eyes_nose_background_retained':True}

def run(video,voice,out,start,root):
    from insightface.app import FaceAnalysis
    p=probe(video);stream=next(s for s in p['streams'] if s['codec_type']=='video')
    duration=float(p['format']['duration']);vd=float(probe(voice)['format']['duration'])
    clip_duration=math.ceil((vd+.6)*24)/24
    start=round(start*24)/24
    if start+clip_duration>duration:raise ValueError('Korean sentence does not fit the speaking window')
    with tempfile.TemporaryDirectory(prefix='lipsync-',dir=str(Path(out).parent)) as folder:
        f=Path(folder);clip=f/'clip.mp4';wav=f/'speech.wav';synced=f/'synced.mp4';visual=f/'visual.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-ss',str(start),'-i',str(video),'-t',str(clip_duration),'-an','-c:v','libx264','-crf','17',str(clip)],check=True)
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(voice),'-af','adelay=300:all=1,apad','-t',str(clip_duration),'-ar','16000','-ac','1',str(wav)],check=True)
        command=[str(root.parent/'venv/bin/python'),'-m','scripts.inference','--unet_config_path','configs/unet/studio_512_low_memory.yaml','--inference_ckpt_path','checkpoints/latentsync_unet.pt','--inference_steps','20','--guidance_scale','1.2','--video_path',str(clip),'--audio_path',str(wav),'--video_out_path',str(synced),'--temp_dir',str(f/'inference-temp')]
        with open(f/'inference.log','w') as log:
            result=subprocess.run(command,cwd=root,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
        if result.returncode:raise RuntimeError('LatentSync failed: '+(f/'inference.log').read_text()[-1500:])
        app=FaceAnalysis(allowed_modules=['detection'],root=str(root/'checkpoints/auxiliary'),providers=['CPUExecutionProvider']);app.prepare(ctx_id=-1,det_size=(512,512))
        checks=composite(video,synced,visual,round(start*24),app,round(clip_duration*24))
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(visual),'-i',str(voice),'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-af',f'adelay={round((start+.3)*1000)}:all=1,apad','-t',str(duration),'-movflags','+faststart',str(out)],check=True)
        op=probe(out);osv=next(s for s in op['streams'] if s['codec_type']=='video')
        if int(osv['nb_frames'])!=checks['source_frames'] or int(osv['width'])!=int(stream['width']) or int(osv['height'])!=int(stream['height']):raise ValueError('Output video dimensions/frame count changed')
        checks.update(model='LatentSync-1.6',steps=20,speech_start=start+.3,speech_duration=vd)
        return checks

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--video',required=True);a.add_argument('--voice',required=True);a.add_argument('--output',required=True);a.add_argument('--start',type=float,required=True);a.add_argument('--root',required=True);a.add_argument('--receipt',required=True);x=a.parse_args()
    receipt=run(Path(x.video),Path(x.voice),Path(x.output),x.start,Path(x.root));Path(x.receipt).write_text(json.dumps(receipt))
