"""Track the source face and mask only its facial features for H3 editing."""
import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from build_hair_mask import detect_boxes, tracked_boxes


def face_mask(width, height, box):
    x, y, face_width, face_height = map(float, box)
    if x < -width * .08 or y < -height * .08 or x + face_width > width * 1.08 or y + face_height > height * 1.08:
        raise ValueError('face_head_out_of_frame')
    mask = np.zeros((height, width), dtype=np.uint8)
    center = (round(x + face_width * .5), round(y + face_height * .55))
    axes = (round(face_width * .58), round(face_height * .64))
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1)
    mask = cv2.GaussianBlur(mask, (0, 0), 3)
    coverage = np.count_nonzero(mask > 127) / mask.size
    if not .003 <= coverage <= .20:
        raise ValueError('face_mask_coverage_invalid')
    return mask


def build(source, output):
    boxes, width, height = detect_boxes(source)
    smoothed, missing = tracked_boxes(boxes)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aski-face-mask-') as temp:
        for index, box in enumerate(smoothed):
            if not cv2.imwrite(str(Path(temp) / f'{index:06d}.png'), face_mask(width, height, box)):
                raise ValueError('face_mask_frame_write_failed')
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', '24', '-i', str(Path(temp) / '%06d.png'),
                        '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p', str(output)], check=True)
    return {'frames': len(boxes), 'detectedFrames': len(boxes) - missing,
            'interpolatedFrames': missing, 'width': width, 'height': height, 'scope': 'face_only'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = build(args.source, args.output)
    if args.report:
        args.report.write_text(json.dumps(result))
    print(json.dumps(result))
