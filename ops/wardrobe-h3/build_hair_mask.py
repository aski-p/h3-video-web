"""Track one visible head and build a moving H3 generation mask.

The mask includes room for loose hair along both shoulders. Ambiguous tracking
is held instead of allowing H3 to regenerate an unrelated person or scene.
"""
import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


def duplicate_detection(primary, other):
    """Haar can return offset boxes for the same face in a single frame."""
    ax, ay, aw, ah = map(float, primary)
    bx, by, bw, bh = map(float, other)
    overlap = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    smaller = min(aw * ah, bw * bh)
    centers = math.hypot(ax + aw / 2 - bx - bw / 2, ay + ah / 2 - by - bh / 2)
    return bool(smaller and overlap / smaller >= .2 and centers <= .9 * ((aw + ah + bw + bh) / 4))


def face_detector():
    engine = Path(sys.prefix).parent
    sys.path.insert(0, str(engine))
    from facefusion import face_detector as detector, state_manager
    settings = {'execution_providers': ['cpu'], 'execution_device_ids': [0],
                'download_providers': ['github'], 'face_detector_model': 'retinaface',
                'face_detector_size': '640x640', 'face_detector_margin': (0, 0, 0, 0),
                'face_detector_score': .2}
    for key, value in settings.items():
        state_manager.init_item(key, value)
    return detector


def detect_boxes(source):
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError('hair_source_unreadable')
    detector = face_detector()
    boxes = []
    width = height = 0
    while True:
        okay, frame = capture.read()
        if not okay:
            break
        height, width = frame.shape[:2]
        detected, scores, _ = detector.detect_faces(frame)
        found = sorted(((float(x1), float(y1), float(x2-x1), float(y2-y1), float(score))
                        for (x1, y1, x2, y2), score in zip(detected, scores)),
                       key=lambda box: box[2] * box[3], reverse=True)
        if found and found[0][4] >= .5 and any(box[4] >= .5 and
                         box[2] * box[3] >= found[0][2] * found[0][3] * .5 and
                         not duplicate_detection(found[0][:4], box[:4]) for box in found[1:]):
            raise ValueError('hair_multiple_faces')
        boxes.append(found[0][:4] if found else None)
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


def head_mask(width, height, box, hair=None):
    x, y, face_width, face_height = map(float, box)
    if x < -width * .08 or y < -height * .08 or x + face_width > width * 1.08 or y + face_height > height * 1.08:
        raise ValueError('hair_head_out_of_frame')
    mask = np.zeros((height, width), dtype=np.uint8)
    def point(rx, ry):
        return round(x + rx * face_width), round(y + ry * face_height)
    cv2.ellipse(mask, (point(.5, .45), (round(1.5 * face_width), round(1.95 * face_height)), 0), 255, -1)
    if hair is not None:
        mask = cv2.max(mask, hair)
    mask = cv2.GaussianBlur(mask, (0, 0), 5)
    if np.count_nonzero(mask > 127) / mask.size > .35:
        raise ValueError('hair_mask_too_wide')
    return mask


def hair_parser():
    import onnxruntime as ort
    model = Path(sys.prefix).parent / '.assets/models/bisenet_resnet_34.onnx'
    if not model.is_file():
        raise ValueError('hair_parser_model_missing')
    return ort.InferenceSession(str(model), providers=['CPUExecutionProvider'])


def parsed_hair(frame, box, parser):
    height, width = frame.shape[:2]
    x, y, face_width, face_height = box
    left = max(0, round(x - .75 * face_width))
    right = min(width, round(x + 1.75 * face_width))
    top = max(0, round(y - .7 * face_height))
    bottom = min(height, round(y + 3 * face_height))
    if right <= left or bottom <= top:
        return None
    crop = frame[top:bottom, left:right]
    prepared = cv2.resize(crop, (512, 512))[:, :, ::-1].astype(np.float32) / 255
    prepared = (prepared - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
    labels = parser.run(None, {parser.get_inputs()[0].name: prepared.transpose(2, 0, 1)[None]})[0][0].argmax(0)
    binary = (labels == 17).astype(np.uint8)
    count, components, stats, centers = cv2.connectedComponentsWithStats(binary, 8)
    if count < 2:
        return None
    largest = max(stats[1:, cv2.CC_STAT_AREA])
    candidates = [index for index in range(1, count)
                  if stats[index, cv2.CC_STAT_AREA] >= max(512 * 512 * .0005, largest * .05)]
    if not candidates:
        return None
    selected = np.isin(components, candidates).astype(np.uint8) * 255
    hair = np.zeros((height, width), dtype=np.uint8)
    hair[top:bottom, left:right] = cv2.resize(selected, (right-left, bottom-top))
    radius = max(5, round(min(face_width, face_height) * .1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1))
    return cv2.dilate(hair, kernel)


def build(source, output):
    boxes, width, height = detect_boxes(source)
    smoothed, missing = tracked_boxes(boxes)
    parser = hair_parser()
    capture = cv2.VideoCapture(str(source))
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='aski-hair-mask-') as temp:
            for index, box in enumerate(smoothed):
                okay, frame = capture.read()
                if not okay:
                    raise ValueError('hair_source_frame_missing')
                hair = parsed_hair(frame, box, parser)
                if not cv2.imwrite(str(Path(temp) / f'{index:06d}.png'), head_mask(width, height, box, hair)):
                    raise ValueError('hair_mask_frame_write_failed')
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-framerate', '24', '-i', str(Path(temp) / '%06d.png'),
                            '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p', str(output)], check=True)
    finally:
        capture.release()
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
