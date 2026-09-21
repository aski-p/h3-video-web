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
p.add_argument('--roi', type=int, nargs=4, required=True, metavar=('X', 'Y', 'W', 'H'))
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
x, y, rw, rh = a.roi
if not (0 <= x < x+rw <= w and 0 <= y < y+rh <= h):
    p.error('ROI outside source')
# Fixed glyph mask across the whole shot avoids changing mask edges each frame.
roi_stack = np.stack([cv2.cvtColor(f[y:y+rh, x:x+rw], cv2.COLOR_BGR2GRAY) for f in frames])
glyph = (np.percentile(roi_stack, 15, axis=0) > 150).astype(np.uint8)*255
glyph = cv2.dilate(glyph, np.ones((7, 7), np.uint8))
mask = np.zeros((h, w), np.uint8)
mask[y:y+rh, x:x+rw] = glyph
# Provide surrounding fabric/body context with dimensions divisible by eight.
x0, y0 = max(0, x-100)//8*8, max(0, y-110)//8*8
x1, y1 = min(w, (x+rw+107)//8*8), min(h, (y+rh+117)//8*8)
local_mask = mask[y0:y1, x0:x1]
model = torch.jit.load(str(a.model), map_location='cpu').eval()
m = torch.from_numpy(local_mask.copy()).float()[None, None]/255
raw = a.output.with_suffix('.silent.mp4')
writer = subprocess.Popen(['ffmpeg','-v','error','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r',str(fps),'-i','-','-an','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-y',str(raw)], stdin=subprocess.PIPE)
try:
    for i, frame in enumerate(frames[:a.limit or None]):
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
a.output.with_suffix('.json').write_text(json.dumps({'source':str(a.source),'method':'LaMa fixed glyph mask; estimated texture','roi':a.roi,'maskedPixels':int((mask>0).sum()),'frames':a.limit or len(frames),'fps':fps},indent=2))
cv2.imwrite(str(a.output.with_suffix('.mask.png')),mask)
