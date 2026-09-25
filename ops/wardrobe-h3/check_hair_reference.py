"""Reject an obvious mismatch between the fixed portrait's hair and final video.

This is a conservative color check. Shape and continuity still require visual review.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort


def detector():
    sys.path.insert(0, str(Path(sys.prefix).parent))
    from facefusion import face_detector, state_manager
    settings = {'execution_providers': ['cpu'], 'execution_device_ids': [0],
                'download_providers': ['github'], 'face_detector_model': 'retinaface',
                'face_detector_size': '640x640', 'face_detector_margin': (0, 0, 0, 0),
                'face_detector_score': .35}
    for key, value in settings.items():
        state_manager.init_item(key, value)
    return face_detector


def hair_lightness(frame, face_detector, parser):
    boxes, scores, _ = face_detector.detect_faces(frame)
    if not len(boxes):
        return None
    index = max(range(len(boxes)), key=lambda i: (boxes[i][2]-boxes[i][0]) * (boxes[i][3]-boxes[i][1]))
    x, y, right, bottom = map(float, boxes[index])
    fw, fh = right-x, bottom-y
    height, width = frame.shape[:2]
    left = max(0, round(x-.75*fw)); right = min(width, round(x+1.75*fw))
    top = max(0, round(y-.7*fh)); bottom = min(height, round(y+3*fh))
    if right <= left or bottom <= top:
        return None
    crop = cv2.resize(frame[top:bottom, left:right], (512, 512))
    prepared = crop[:, :, ::-1].astype(np.float32) / 255
    prepared = (prepared-np.array([.485,.456,.406],np.float32))/np.array([.229,.224,.225],np.float32)
    labels = parser.run(None,{parser.get_inputs()[0].name:prepared.transpose(2,0,1)[None]})[0][0].argmax(0)
    hair = labels == 17
    if np.count_nonzero(hair) < 512*512*.005:
        return None
    lab = cv2.cvtColor(crop, cv2.COLOR_BGR2LAB)
    return round(float(np.median(lab[:, :, 0][hair]))*100/255, 2)


def check(portrait, result):
    model = Path(sys.prefix).parent/'.assets/models/bisenet_resnet_34.onnx'
    if not model.is_file():
        raise ValueError('hair_parser_model_missing')
    parser = ort.InferenceSession(str(model), providers=['CPUExecutionProvider'])
    face_detector = detector()
    reference = cv2.imread(str(portrait))
    if reference is None:
        raise ValueError('hair_portrait_unreadable')
    target = hair_lightness(reference, face_detector, parser)
    if target is None:
        raise ValueError('hair_reference_unverifiable')
    video = cv2.VideoCapture(str(result))
    if not video.isOpened():
        raise ValueError('hair_result_unreadable')
    fps = video.get(cv2.CAP_PROP_FPS)
    frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT))
    if fps <= 0 or frames <= 0:
        raise ValueError('hair_result_unreadable')
    positions = [min(frames-1, round((i+.5)*fps)) for i in range(max(5, int(frames/fps)))]
    samples = []
    try:
        for position in positions:
            video.set(cv2.CAP_PROP_POS_FRAMES, position)
            okay, frame = video.read()
            samples.append(hair_lightness(frame, face_detector, parser) if okay else None)
    finally:
        video.release()
    usable = [value for value in samples if value is not None]
    if len(usable) < max(3, int(len(positions)*.6)):
        raise ValueError('hair_result_unverifiable')
    mean = sum(usable)/len(usable)
    report = {'referenceHairLightness': target, 'outputHairLightnessSamples': samples,
              'outputHairLightnessMean': round(mean,2), 'maximumLightnessDifference': 25,
              'shapeVisualReview': 'required'}
    if abs(mean-target)>25:
        raise ValueError('hair_reference_color_mismatch')
    return report


if __name__ == '__main__':
    arguments = argparse.ArgumentParser()
    arguments.add_argument('--portrait', type=Path, required=True)
    arguments.add_argument('--result', type=Path, required=True)
    arguments.add_argument('--report', type=Path, required=True)
    options = arguments.parse_args()
    receipt = check(options.portrait, options.result)
    options.report.write_text(json.dumps(receipt))
    print(json.dumps(receipt))
