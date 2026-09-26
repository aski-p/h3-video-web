"""Locate a stable account-name mark in an extracted source clip.

Only a repeated, small screen-positioned mark is eligible for automatic LaMa
restoration. Ambiguous or large graphics stop the job for review.
"""
import argparse
import json
import re
import subprocess
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import cv2


ALIASES = {'artgentokyo': ('artgentokyo', 'agtstudio'),
           'miacharmsss': ('miacharmsss', 'daki', 'cosplay')}


def normalized(value):
    return re.sub(r'[^a-z0-9@]', '', value.lower())


def text_lines(image, individual=False, psm=11):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scale=3 if psm==7 else 1
    if scale>1:gray=cv2.resize(gray,None,fx=scale,fy=scale,interpolation=cv2.INTER_CUBIC)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 7))
    variants = (gray, cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel))
    lines = []
    for variant in variants:
        encoded = cv2.imencode('.png', variant)[1].tobytes()
        result = subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', str(psm), 'tsv'],
                                input=encoded, capture_output=True, timeout=15, check=True)
        groups = defaultdict(list)
        for row in result.stdout.decode('utf-8', 'replace').splitlines()[1:]:
            cells = row.split('\t', 11)
            if len(cells) < 12 or not cells[11].strip():
                continue
            try:
                if float(cells[10]) < 25:
                    continue
                x, y, width, height = [round(int(value)/scale) for value in cells[6:10]]
            except ValueError:
                continue
            if width > 0 and height > 0:
                groups[tuple(cells[1:5])].append((x, y, width, height, cells[11]))
        candidates=([word] for words in groups.values() for word in words) if individual else groups.values()
        for words in candidates:
            x = min(v[0] for v in words)
            y = min(v[1] for v in words)
            right = max(v[0] + v[2] for v in words)
            bottom = max(v[1] + v[3] for v in words)
            lines.append((x, y, right - x, bottom - y, normalized(''.join(v[4] for v in words))))
    return lines


def matching_lines(image, username, adaptive=False, psm=11, full_size=None):
    height, width = image.shape[:2]
    limit_width,limit_height=full_size or (width,height)
    aliases = ALIASES.get(normalized(username), (normalized(username),))
    found = []
    for x, y, w, h, text in text_lines(image,individual=adaptive,psm=psm):
        alias_match = any(alias and (alias in text or
                          (len(alias) >= 7 and SequenceMatcher(None, alias, text.lstrip('@')).ratio() >= .82))
                          for alias in aliases)
        if adaptive and not alias_match:
            alias_match=any(len(text.lstrip('@'))>=7 and alias.startswith(text.lstrip('@')) for alias in aliases)
        if not alias_match and (adaptive or not re.search(r'@[a-z0-9_.]{4,}', text)):
            continue
        if y < height * .34 and not adaptive:
            raise ValueError('account_overlay_near_face')
        # A large graphic across a person is not safe to reconstruct automatically.
        if w > limit_width * .55 or h > limit_height * .11:
            raise ValueError('account_overlay_too_large')
        margin_x, margin_y = max(8, int(w * .06)), max(5, int(h * .2))
        found.append((max(0, x - margin_x), max(0, y - margin_y),
                      min(width, x + w + margin_x) - max(0, x - margin_x),
                      min(height, y + h + margin_y) - max(0, y - margin_y), text))
    return found


def detect(source, username):
    cap = cv2.VideoCapture(str(source))
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if count < 2:
        cap.release()
        raise ValueError('account_overlay_source_unreadable')
    observations = []
    fractions = (.1, .25, .4, .55, .7, .85, .95)
    for fraction in fractions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(count - 1, round((count - 1) * fraction)))
        ok, frame = cap.read()
        if not ok:
            cap.release()
            raise ValueError('account_overlay_source_unreadable')
        observations.append(matching_lines(frame, username))
    cap.release()
    groups = []
    for sample_index, sample in enumerate(observations):
        for candidate in sample:
            cx, cy = candidate[0] + candidate[2] / 2, candidate[1] + candidate[3] / 2
            group = next((g for g in groups if abs(g[0][1][0] + g[0][1][2] / 2 - cx) < 35
                          and abs(g[0][1][1] + g[0][1][3] / 2 - cy) < 35), None)
            if group is None:
                groups.append([(sample_index, candidate)])
            else:
                if sample_index not in {item[0] for item in group}:
                    group.append((sample_index, candidate))
    if not groups:
        return {'overlayROI': None, 'samples': len(fractions), 'result': 'no_matching_account_mark_detected'}
    stable = [g for g in groups if len(g) >= 2]
    if len(stable) != 1 or len(groups) != 1:
        raise ValueError('account_overlay_location_uncertain')
    group = [item[1] for item in stable[0]]
    x = min(v[0] for v in group)
    y = min(v[1] for v in group)
    right = max(v[0] + v[2] for v in group)
    bottom = max(v[1] + v[3] for v in group)
    return {'overlayROI': [x, y, right - x, bottom - y], 'samples': len(fractions),
            'matches': len(group), 'result': 'stable_account_mark', 'matchedText': group[0][4]}


def bridge_text_gaps(frames, boxes):
    """Track pixels through OCR gaps, bidirectionally; ambiguous spans stay missing."""
    result=list(boxes)
    known=[i for i,box in enumerate(boxes) if box is not None]
    for left,right in zip(known,known[1:]):
        if right-left<=1 or right-left>48:continue
        def follow(start,end):
            step=1 if end>start else -1
            x,y,w,h=map(int,boxes[start]);template=frames[start][y:y+h,x:x+w]
            tracked={}
            for index in range(start+step,end+step,step):
                frame=frames[index];height,width=frame.shape
                sx=max(0,x-24);sy=max(0,y-24);ex=min(width,x+w+24);ey=min(height,y+h+24)
                window=frame[sy:ey,sx:ex]
                if window.shape[0]<h or window.shape[1]<w:return None
                _,score,_,loc=cv2.minMaxLoc(cv2.matchTemplate(window,template,cv2.TM_CCOEFF_NORMED))
                if score<.8:return None
                x,y=sx+loc[0],sy+loc[1];tracked[index]=[x,y,w,h]
            return tracked
        forward=follow(left,right);backward=follow(right,left)
        if not forward or not backward:continue
        consistent=True
        for index in range(left+1,right):
            a,b=forward[index],backward[index]
            if abs(a[0]+a[2]/2-b[0]-b[2]/2)>12 or abs(a[1]+a[3]/2-b[1]-b[3]/2)>8:
                consistent=False;break
        if not consistent:continue
        for index in range(left+1,right):
            a,b=forward[index],backward[index]
            x=min(a[0],b[0]);y=min(a[1],b[1]);r=max(a[0]+a[2],b[0]+b[2]);bottom=max(a[1]+a[3],b[1]+b[3])
            result[index]=[x,y,r-x,bottom-y]
    return result


def detect_track(source, username):
    """Measure the requested mark on every frame; interpolate only short OCR gaps."""
    import numpy as np
    cap=cv2.VideoCapture(str(source));fps=cap.get(cv2.CAP_PROP_FPS)
    total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT));width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    boxes=[];frames=[];last_box=None
    try:
        while True:
            okay,frame=cap.read()
            if not okay:break
            frames.append(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY))
            found=matching_lines(frame,username,adaptive=True)
            if not found and last_box is not None:
                # Whole-frame OCR can merge the label with a moving hand. A
                # local single-line pass still requires the actual account text.
                x,y,w,h=last_box;left=max(0,x-16);top=max(0,y-8)
                crop=frame[top:min(height,y+h+8),left:min(width,x+w+16)]
                local=matching_lines(crop,username,adaptive=True,psm=7,full_size=(width,height))
                found=[(a+left,b+top,c,d,t) for a,b,c,d,t in local]
            if found:
                x=min(v[0] for v in found);y=min(v[1] for v in found)
                right=max(v[0]+v[2] for v in found);bottom=max(v[1]+v[3] for v in found)
                if right-x>width*.55 or bottom-y>height*.11:raise ValueError('account_overlay_location_uncertain')
                last_box=[x,y,right-x,bottom-y];boxes.append(last_box)
            else:boxes.append(None)
    finally:cap.release()
    if len(boxes)!=total or total<2:raise ValueError('account_overlay_source_unreadable')
    known=[i for i,b in enumerate(boxes) if b is not None]
    if not known:return {'overlayROI':None,'boxes':[],'frames':total,'fps':fps,'width':width,'height':height,'result':'no_matching_account_mark_detected'}
    boxes=bridge_text_gaps(frames,boxes)
    known=[i for i,b in enumerate(boxes) if b is not None]
    if len(known)<total*.6 or known[0]>3 or total-1-known[-1]>3 or any(b-a>max(4,round(fps*.25)) for a,b in zip(known,known[1:])):
        raise ValueError('account_overlay_tracking_incomplete')
    values=np.array([[np.interp(i,known,[boxes[k][axis] for k in known]) for axis in range(4)] for i in range(total)]).round().astype(int).tolist()
    return {'overlayROI':values[0],'boxes':values,'frames':total,'fps':fps,'width':width,'height':height,'observedFrames':len(known),'result':'tracked_account_mark'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--username', required=True)
    parser.add_argument('--per-frame',action='store_true')
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    result=(detect_track if args.per_frame else detect)(args.source,args.username)
    if args.report:args.report.write_text(json.dumps(result))
    print(json.dumps(result))
