"""Track one visible head and build a moving H3 generation mask.

The mask includes room for loose hair along both shoulders. Ambiguous tracking
is held instead of allowing H3 to regenerate an unrelated person or scene.
"""
import argparse
import json
import math
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np


def detect_boxes(source):
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError('hair_source_unreadable')
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    boxes = []
    width = height = 0
    while True:
        okay, frame = capture.read()
        if not okay:
            break
        height, width = frame.shape[:2]
        found = detector.detectMultiScale(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                                          scaleFactor=1.1, minNeighbors=5,
                                          minSize=(max(48, width // 18), max(48, height // 24)))
        found = sorted(found, key=lambda box: int(box[2]) * int(box[3]), reverse=True)
        if len(found) > 1 and found[1][2] * found[1][3] >= found[0][2] * found[0][3] * .5:
            raise ValueError('hair_multiple_faces')
        boxes.append(tuple(map(float, found[0])) if len(found) else None)
    capture.release()
    if not boxes:
        raise ValueError('hair_source_empty')
    return boxes, width, height


def tracked_boxes(boxes):
    indices = [i for i, box in enumerate(boxes) if box is not None]
    missing = len(boxes) - len(indices)
    if len(indices) < math.ceil(len(boxes) * .9) or indices[0] > 3 or len(boxes) - 1 - indices[-1] > 3:
        raise ValueError('hair_tracking_incomplete')
    gaps = [right - left - 1 for left, right in zip(indices, indices[1:])]
    if gaps and max(gaps) > 4:
        raise ValueError('hair_tracking_gap')
    positions = np.arange(len(boxes))
    smooth = np.column_stack([np.interp(positions, indices, [boxes[i][axis] for i in indices]) for axis in range(4)])
    for axis in range(4):
        smooth[:, axis] = np.array([np.median(smooth[max(0, i - 2):min(len(boxes), i + 3), axis])
                                    for i in positions])
    return smooth, missing


def head_mask(width, height, box):
    x, y, face_width, face_height = map(float, box)
    left, top = x - .35 * face_width, y - .95 * face_height
    right, bottom = x + 1.8 * face_width, y + 2.65 * face_height
    if left < -width * .08 or top < -height * .08 or right > width * 1.08 or bottom > height * 1.08:
        raise ValueError('hair_head_out_of_frame')
    mask = np.zeros((height, width), dtype=np.uint8)
    def point(rx, ry):
        return round(x + rx * face_width), round(y + ry * face_height)
    cv2.ellipse(mask, (point(.725, .15), (round(2.15 * face_width), round(2.2 * face_height)), 0), 255, -1)
    cv2.fillPoly(mask, [np.array([point(-.35, .05), point(.3, -.2), point(.48, 1.1),
                                  point(.35, 2.65), point(-.35, 2.3)], dtype=np.int32)], 255)
    cv2.fillPoly(mask, [np.array([point(1.18, -.2), point(1.8, .05), point(1.8, 2.3),
                                  point(1.12, 2.65), point(1.02, 1.1)], dtype=np.int32)], 255)
    mask = cv2.GaussianBlur(mask, (0, 0), 5)
    if np.count_nonzero(mask > 127) / mask.size > .35:
        raise ValueError('hair_mask_too_wide')
    return mask


def build(source, output):
    boxes, width, height = detect_boxes(source)
    smoothed, missing = tracked_boxes(boxes)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aski-hair-mask-') as temp:
        for index, box in enumerate(smoothed):
            if not cv2.imwrite(str(Path(temp) / f'{index:06d}.png'), head_mask(width, height, box)):
                raise ValueError('hair_mask_frame_write_failed')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', '24', '-i', str(Path(temp) / '%06d.png'),
                        '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p', str(output)], check=True)
    return {'frames': len(boxes), 'detectedFrames': len(boxes) - missing,
            'interpolatedFrames': missing, 'width': width, 'height': height}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    arguments = parser.parse_args()
    result = build(arguments.source, arguments.output)
    if arguments.report:
        arguments.report.write_text(json.dumps(result))
    print(json.dumps(result))
