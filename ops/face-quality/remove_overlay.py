"""Restore a fixed text overlay in a video with LaMa; preserve unmasked pixels.

Requires torch, OpenCV, NumPy and a local trusted LaMa TorchScript model.
The ROI must enclose only the requested overlay. Restored texture is estimated.
"""
import argparse
import json
import subprocess
from pathlib import Path

import cv2
import numpy as np
import torch

p = argparse.ArgumentParser()
p.add_argument('--source', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--model', type=Path, required=True)
p.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'))
p.add_argument('--track-json',type=Path)
p.add_argument('--full-roi', action='store_true', help='Restore a stable account mark, including dark or translucent text')
p.add_argument('--limit', type=int, default=0)
a = p.parse_args()
if a.source.resolve() == a.output.resolve():
    p.error('source and output must differ')
torch.set_num_threads(4)
cv2.setNumThreads(1)
cap = cv2.VideoCapture(str(a.source))
fps = cap.get(cv2.CAP_PROP_FPS)
frames = []
while True:
    ok, frame = cap.read()
    if not ok:
        break
    frames.append(frame)
cap.release()
if not frames:
    raise ValueError('No source frames')
h, w = frames[0].shape[:2]
track=json.loads(a.track_json.read_text()) if a.track_json else None
if track:
    if track['frames']!=len(frames) or (track['width'],track['height'])!=(w,h) or abs(track['fps']-fps)>.01 or len(track['boxes'])!=len(frames):raise ValueError('overlay_track_timing_mismatch')
    for tx,ty,tw,th in track['boxes']:
        if not (0<=tx<tx+tw<=w and 0<=ty<ty+th<=h) or tw>w*.55 or th>h*.11:raise ValueError('overlay_track_bounds_invalid')
    a.roi=track['boxes'][0]
if not a.roi:p.error('ROI or track required')
x, y, rw, rh = a.roi
if not (0 <= x < x+rw <= w and 0 <= y < y+rh <= h):
    p.error('ROI outside source')
if track:
    positions=[(box[0],box[1],1.0) for box in track['boxes']]
elif a.full_roi:
    glyph = np.ones((rh, rw), np.uint8) * 255
    positions = [(x, y, 1.0)] * len(frames)
else:
    # Preserve the approved manual ROI path for bright, trackable text.
    gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
    template = gray0[y:y+rh, x:x+rw]
    glyph = (template > 210).astype(np.uint8)*255
    glyph = cv2.dilate(glyph, np.ones((7, 7), np.uint8))
    sx, sy = max(0,x-120), max(0,y-120)
    ex, ey = min(w,x+rw+120), min(h,y+rh+120)
    positions=[]
    for frame in frames:
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        match=cv2.matchTemplate((gray[sy:ey,sx:ex]>210).astype(np.uint8),(template>210).astype(np.uint8),cv2.TM_CCOEFF_NORMED)
        _,confidence,_,loc=cv2.minMaxLoc(match)
        if confidence < .55:
            raise ValueError(f'Overlay tracking confidence too low: {confidence}')
        positions.append((sx+loc[0],sy+loc[1],confidence))
print('tracking', positions[::30], flush=True)
model = torch.jit.load(str(a.model), map_location='cpu').eval()
raw = a.output.with_suffix('.silent.mp4')
writer = subprocess.Popen(['ffmpeg','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r',str(fps),'-i','-','-an','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-y',str(raw)], stdin=subprocess.PIPE)
try:
    for i, frame in enumerate(frames[:a.limit or None]):
        px,py,_=positions[i]
        if track:
            _,_,rw,rh=track['boxes'][i];glyph=np.ones((rh,rw),np.uint8)*255
        mask=np.zeros((h,w),np.uint8)
        mask[py:py+rh,px:px+rw]=glyph
        x0,y0=max(0,px-100)//8*8,max(0,py-110)//8*8
        x1,y1=min(w,(px+rw+107)//8*8),min(h,(py+rh+117)//8*8)
        local_mask=mask[y0:y1,x0:x1]
        m=torch.from_numpy(local_mask.copy()).float()[None,None]/255
        crop = cv2.cvtColor(frame[y0:y1,x0:x1], cv2.COLOR_BGR2RGB)
        im = torch.from_numpy(crop.copy()).float().permute(2,0,1)[None]/255
        with torch.inference_mode():
            result = model(im,m)[0].permute(1,2,0).numpy()
        result = cv2.cvtColor(np.clip(result*255,0,255).astype(np.uint8), cv2.COLOR_RGB2BGR)
        out = frame.copy()
        region = out[y0:y1,x0:x1]
        region[local_mask>0] = result[local_mask>0]
        writer.stdin.write(out.tobytes())
        if i % 15 == 0:
            print(f'{i+1}/{a.limit or len(frames)}', flush=True)
    writer.stdin.close()
    if writer.wait() != 0:
        raise RuntimeError('Video encoding failed')
except BaseException:
    writer.kill()
    raise
subprocess.run(['ffmpeg','-v','error','-i',str(raw),'-i',str(a.source),'-map','0:v:0','-map','1:a?','-c','copy','-shortest','-movflags','+faststart','-y',str(a.output)],check=True)
raw.unlink()
a.output.with_suffix('.json').write_text(json.dumps({'source':str(a.source),'method':'LaMa per-frame account ROI; estimated texture' if track else 'LaMa screen-fixed ROI; estimated texture' if a.full_roi else 'LaMa tracked glyph mask; estimated texture','tracking':positions,'roi':a.roi,'maskedPixels':int((mask>0).sum()),'frames':a.limit or len(frames),'fps':fps},indent=2))
cv2.imwrite(str(a.output.with_suffix('.mask.png')),mask)
