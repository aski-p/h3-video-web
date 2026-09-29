"""Replace a tracked account mark with an opaque creator label, without inpainting."""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')

def validate_track(track,width,height,frames,fps):
    boxes=track.get('textBoxes') or track.get('boxes')
    if track.get('frames')!=frames or track.get('width')!=width or track.get('height')!=height or abs(track.get('fps',0)-fps)>.01 or not boxes or len(boxes)!=frames:
        raise ValueError('branding_track_timing_mismatch')
    for box in boxes:
        if len(box)!=4 or any(type(v)!=int for v in box):raise ValueError('branding_track_invalid')
        x,y,w,h=box
        if not(0<=x<x+w<=width and 0<=y<y+h<=height) or w>width*.55 or h>height*.16:raise ValueError('branding_track_bounds_invalid')
    return boxes

def label_geometry(boxes,width,height,handle):
    if not re.fullmatch(r'@[A-Za-z0-9_.]{1,30}',handle):raise ValueError('branding_handle_invalid')
    font=ImageFont.truetype(str(FONT),max(18,round(width*.039)))
    bounds=font.getbbox(handle);tw,th=bounds[2]-bounds[0],bounds[3]-bounds[1]
    median_w=float(np.median([b[2] for b in boxes]));median_h=float(np.median([b[3] for b in boxes]))
    anchors=[i for i,b in enumerate(boxes) if median_w*.9<=b[2]<=median_w*1.1 and median_h*.7<=b[3]<=median_h*1.25]
    # OCR sometimes merges a shirt wrinkle with text. Stabilize short outliers
    # between measured label positions; reject long uncertain spans.
    if len(anchors)<len(boxes)*.6 or anchors[0]>2 or len(boxes)-1-anchors[-1]>2 or any(b-a>12 for a,b in zip(anchors,anchors[1:])):
        raise ValueError('branding_label_geometry_uncertain')
    w=max(max(boxes[i][2] for i in anchors)+20,tw+32);h=max(max(boxes[i][3] for i in anchors)+16,th+24)
    if w>width*.62 or h>height*.19:raise ValueError('branding_label_too_large')
    centers_x=np.interp(range(len(boxes)),anchors,[boxes[i][0]+boxes[i][2]/2 for i in anchors])
    centers_y=np.interp(range(len(boxes)),anchors,[boxes[i][1]+boxes[i][3]/2 for i in anchors])
    rects=[]
    for cx,cy in zip(centers_x,centers_y):
        left=max(0,min(width-w,round(cx-w/2)));top=max(0,min(height-h,round(cy-h/2)))
        rects.append((left,top,w,h))
    return font,rects

def paint(frame,rect,font,handle):
    image=Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB));draw=ImageDraw.Draw(image)
    x,y,w,h=rect
    draw.rounded_rectangle((x,y,x+w-1,y+h-1),radius=10,fill=(28,31,38),outline=(80,84,94),width=1)
    bounds=draw.textbbox((0,0),handle,font=font);tw,th=bounds[2]-bounds[0],bounds[3]-bounds[1]
    draw.text((x+(w-tw)/2-bounds[0],y+(h-th)/2-bounds[1]),handle,font=font,fill=(248,248,250))
    return cv2.cvtColor(np.asarray(image),cv2.COLOR_RGB2BGR)

def render(source,output,track,handle):
    if source.resolve()==output.resolve():raise ValueError('branding_source_must_be_preserved')
    cap=cv2.VideoCapture(str(source));fps=cap.get(cv2.CAP_PROP_FPS);count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    boxes=validate_track(track,width,height,count,fps);font,rects=label_geometry(boxes,width,height,handle)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='branding-') as tmp:
        silent=Path(tmp)/'silent.mp4';muxed=Path(tmp)/'output.mp4'
        writer=subprocess.Popen(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','bgr24','-s',f'{width}x{height}','-r',str(fps),'-i','-','-an','-c:v','libx264','-crf','16','-pix_fmt','yuv420p',str(silent)],stdin=subprocess.PIPE)
        try:
            for rect in rects:
                ok,frame=cap.read()
                if not ok:raise ValueError('branding_source_truncated')
                writer.stdin.write(paint(frame,rect,font,handle).tobytes())
            if cap.read()[0]:raise ValueError('branding_source_extra_frames')
            writer.stdin.close()
            if writer.wait()!=0:raise ValueError('branding_encode_failed')
            subprocess.run(['ffmpeg','-v','error','-y','-i',str(silent),'-i',str(source),'-map','0:v:0','-map','1:a?','-c','copy','-movflags','+faststart',str(muxed)],check=True)
            muxed.replace(output)
        finally:
            cap.release()
            if writer.poll() is None:writer.kill();writer.wait()
    receipt={'method':'tracked_opaque_creator_label_v1','handle':handle,'frames':count,'fps':fps,'width':width,'height':height,'inpainting':False,'rectangles':rects,'sourceSha256':hashlib.sha256(source.read_bytes()).hexdigest(),'visualReview':'required'}
    output.with_suffix('.branding.json').write_text(json.dumps(receipt))
    return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True,type=Path);p.add_argument('--output',required=True,type=Path);p.add_argument('--track-json',required=True,type=Path);p.add_argument('--handle',required=True);a=p.parse_args()
    receipt=render(a.source,a.output,json.loads(a.track_json.read_text()),a.handle)
    print(json.dumps({k:v for k,v in receipt.items() if k!='rectangles'}))
