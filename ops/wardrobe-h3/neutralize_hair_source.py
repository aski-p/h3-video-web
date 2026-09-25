"""Remove source-hair color cues inside the approved generation mask.

The original motion, mask and audio remain unchanged for quality comparison.
Only this temporary H3 conditioning video is neutralized.
"""
import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np


def neutralize(source, mask, output):
    motion = cv2.VideoCapture(str(source))
    coverage = cv2.VideoCapture(str(mask))
    if not motion.isOpened() or not coverage.isOpened():
        raise ValueError('hair_conditioning_input_unreadable')
    width = int(motion.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(motion.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = int(motion.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = motion.get(cv2.CAP_PROP_FPS)
    mask_meta=(int(coverage.get(cv2.CAP_PROP_FRAME_WIDTH)),
               int(coverage.get(cv2.CAP_PROP_FRAME_HEIGHT)),
               int(coverage.get(cv2.CAP_PROP_FRAME_COUNT)),
               round(coverage.get(cv2.CAP_PROP_FPS)))
    if (width,height,frames,round(fps))!=mask_meta or fps!=24:
        raise ValueError('hair_conditioning_timing_mismatch')
    output.parent.mkdir(parents=True, exist_ok=True)
    command=['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'{width}x{height}',
             '-r','24','-i','pipe:0','-i',str(source),'-map','0:v:0','-map','1:a:0',
             '-frames:v',str(frames),'-c:v','libx264','-crf','18','-preset','fast',
             '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-b:a','128k',
             '-t',f'{frames/24:.6f}',str(output)]
    worker=subprocess.Popen(command,stdin=subprocess.PIPE,stderr=subprocess.PIPE)
    masked=[]
    try:
        for _ in range(frames):
            okay, frame=motion.read();mask_okay, white=coverage.read()
            if not okay or not mask_okay:raise ValueError('hair_conditioning_frame_missing')
            alpha=(white[:,:,0].astype(np.float32)/255).clip(0,1)[:,:,None]
            masked.append(float(np.mean(alpha>.5)))
            conditioned=np.rint(frame.astype(np.float32)*(1-alpha)+127*alpha).astype(np.uint8)
            worker.stdin.write(conditioned.tobytes())
        worker.stdin.close()
        if worker.wait(timeout=120) or not output.is_file():
            raise ValueError('hair_conditioning_encode_failed')
    finally:
        motion.release();coverage.release()
        if worker.poll() is None:
            worker.kill();worker.wait()
    return {'frames':frames,'width':width,'height':height,
            'meanNeutralizedFraction':sum(masked)/len(masked)}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--mask',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    options=parser.parse_args()
    report=neutralize(options.source,options.mask,options.output)
    options.report.write_text(json.dumps(report))
    print(json.dumps(report))
