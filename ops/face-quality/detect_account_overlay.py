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


def text_lines(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 7))
    variants = (gray, cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel))
    lines = []
    for variant in variants:
        encoded = cv2.imencode('.png', variant)[1].tobytes()
        result = subprocess.run(['tesseract', 'stdin', 'stdout', '--psm', '11', 'tsv'],
                                input=encoded, capture_output=True, timeout=15, check=True)
        groups = defaultdict(list)
        for row in result.stdout.decode('utf-8', 'replace').splitlines()[1:]:
            cells = row.split('\t', 11)
            if len(cells) < 12 or not cells[11].strip():
                continue
            try:
                if float(cells[10]) < 25:
                    continue
                x, y, width, height = map(int, cells[6:10])
            except ValueError:
                continue
            if width > 0 and height > 0:
                groups[tuple(cells[1:5])].append((x, y, width, height, cells[11]))
        for words in groups.values():
            x = min(v[0] for v in words)
            y = min(v[1] for v in words)
            right = max(v[0] + v[2] for v in words)
            bottom = max(v[1] + v[3] for v in words)
            lines.append((x, y, right - x, bottom - y, normalized(''.join(v[4] for v in words))))
    return lines


def matching_lines(image, username):
    height, width = image.shape[:2]
    aliases = ALIASES.get(normalized(username), (normalized(username),))
    found = []
    for x, y, w, h, text in text_lines(image):
        alias_match = any(alias and (alias in text or
                          (len(alias) >= 7 and SequenceMatcher(None, alias, text.lstrip('@')).ratio() >= .82))
                          for alias in aliases)
        if not alias_match and not re.search(r'@[a-z0-9_.]{4,}', text):
            continue
        if y < height * .34:
            raise ValueError('account_overlay_near_face')
        # A large graphic across a person is not safe to reconstruct automatically.
        if w > width * .55 or h > height * .11:
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--username', required=True)
    args = parser.parse_args()
    print(json.dumps(detect(args.source, args.username)))
