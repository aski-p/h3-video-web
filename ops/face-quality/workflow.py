"""Repeat the approved original-motion / fixed-face local video workflow.

Read a verified NAS manifest; defaults are versioned alongside this script.
Overlay ROI is source-specific and must be inspected, never reused blindly.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from fractions import Fraction

HERE = Path(__file__).resolve().parent

def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def video_meta(path):
    data = json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-count_frames','-show_entries','stream=width,height,avg_frame_rate,nb_read_frames','-of','json',str(path)]))['streams'][0]
    return {'width':int(data['width']), 'height':int(data['height']), 'fps':float(Fraction(data['avg_frame_rate'])), 'frames':int(data['nb_read_frames'])}

def original_from_manifest(root, manifest):
    if not manifest.get('decodeVerified'):
        raise ValueError('Verified original required')
    assets = manifest.get('assets', [])
    if len(assets) != 1:
        raise ValueError('Select a single-video manifest')
    asset = assets[0]
    original = (root / asset['path']).resolve()
    if not original.is_relative_to(root.resolve()):
        raise ValueError('Original path escapes NAS root')
    if not asset.get('decodeVerified') or digest(original) != asset['sha256']:
        raise ValueError('Original integrity check failed')
    return original

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    p.add_argument('--config', type=Path, default=Path.home()/'.config/aski-face/workflow.json')
    p.add_argument('--start', type=float, default=0)
    p.add_argument('--duration', type=float)
    p.add_argument('--reference-frame', type=int)
    p.add_argument('--reference-distance',type=float,default=.3)
    p.add_argument('--overlay-roi', nargs=4, type=int)
    p.add_argument('--no-account-overlay', action='store_true', help='Explicitly record that this source was inspected and has no account-name overlay')
    a = p.parse_args()
    if bool(a.overlay_roi) == a.no_account_overlay:
        p.error('Inspect the source, then choose --overlay-roi or --no-account-overlay')
    config = json.loads(a.config.read_text())
    profile = json.loads((HERE/'default-profile.json').read_text())
    manifest = json.loads(a.manifest.read_text())
    original = original_from_manifest(Path(config['nasRoot']), manifest)
    portrait = Path(config['portrait'])
    if digest(portrait) != config['portraitSha256']:
        raise ValueError('Fixed portrait changed; update configuration deliberately')
    original_meta=video_meta(original)
    available=original_meta['frames']/original_meta['fps']-a.start
    duration = min(15,available) if a.duration is None else a.duration
    if duration>available+1/original_meta['fps']:raise ValueError('Requested segment exceeds source duration')
    if a.start < 0 or not 2 <= duration <= 15:
        p.error('Use a 2–15 second segment with nonnegative start')
    r = a.output_dir.resolve()
    r.mkdir(parents=True, exist_ok=False)
    shutil.copy2(portrait, r/'portrait.jpg')
    source, swapped, output = r/'source.mp4', r/'swapped.mp4', r/'output.mp4'
    subprocess.run(['ffmpeg','-v','error','-ss',str(a.start),'-i',str(original),'-t',str(duration),'-map','0:v:0','-map','0:a?','-c:v','libx264','-crf','18','-preset','fast','-c:a','aac','-movflags','+faststart',str(source)],check=True)
    source_meta = video_meta(source)
    if abs(source_meta['frames']/source_meta['fps']-duration)>1/source_meta['fps']+1e-6:raise ValueError('Extracted duration differs from requested segment')
    reference = a.reference_frame if a.reference_frame is not None else min(round(source_meta['fps']*profile['referenceTimeSeconds']),source_meta['frames']-1)
    if not 0 <= reference < source_meta['frames']:
        raise ValueError('Reference frame outside clip')
    record = {'profile':profile,'sourceUrl':manifest['sourceUrl'],'originalSha256':digest(original),'portraitSha256':digest(portrait),'start':a.start,'duration':duration,'referenceFrame':reference,'overlayROI':a.overlay_roi,'overlayReviewed':True,'visualReview':'pending','source':source_meta}
    (r/'workflow.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    with (r/'render.log').open('w') as log:
        subprocess.run([sys.executable,str(HERE/'trial.py'),'--engine',config['engine'],'--source',str(source),'--portrait',str(r/'portrait.jpg'),'--output',str(swapped),'--model',profile['model'],'--reference-frame',str(reference),'--reference-distance',str(a.reference_distance)],stdout=log,stderr=subprocess.STDOUT,check=True)
    if a.overlay_roi:
        with (r/'restoration.log').open('w') as log:
            subprocess.run([config['inpaintPython'],str(HERE/'remove_overlay.py'),'--source',str(swapped),'--output',str(output),'--model',config['inpaintModel'],'--roi',*map(str,a.overlay_roi)],stdout=log,stderr=subprocess.STDOUT,check=True)
    else:
        shutil.copy2(swapped,output)
    meta = video_meta(output)
    if meta != source_meta:
        raise ValueError('Output timing or dimensions changed')
    subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(output),'-f','null','-'],check=True)
    subprocess.run(['ffmpeg','-v','error','-i',str(source),'-i',str(output),'-filter_complex',"[0:v]scale=360:-2,drawtext=text='ORIGINAL':x=12:y=20:fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6[a];[1:v]scale=360:-2,drawtext=text='FIXED FACE':x=12:y=20:fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6[b];[a][b]hstack=inputs=2[v]",'-map','[v]','-map','0:a?','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart',str(r/'comparison.mp4')],check=True)
    record.update(output=meta,timingVerified=True,outputSha256=digest(output))
    (r/'workflow.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    print(json.dumps({'output':str(output),'comparison':str(r/'comparison.mp4'),'sourceUrl':manifest['sourceUrl'],'visualReview':'pending'},ensure_ascii=False),flush=True)

if __name__ == '__main__':
    main()
