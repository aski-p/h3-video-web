"""Make a face-and-hair reference without the portrait's outfit or background.

The source photo remains intact in the durable job. An uncertain face location is
held for review instead of falling back to the full portrait.
"""
import argparse
import json
from pathlib import Path

import cv2


def crop(source, output):
    image = cv2.imread(str(source))
    if image is None:
        raise ValueError('hair_portrait_unreadable')
    height, width = image.shape[:2]
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    faces = detector.detectMultiScale(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY),
                                      scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))
    if len(faces) != 1:
        raise ValueError('hair_portrait_face_uncertain')
    x, y, face_width, face_height = map(int, faces[0])
    center_x = x + face_width / 2
    left = max(0, round(center_x - face_width * .9))
    right = min(width, round(center_x + face_width * .9))
    top = max(0, round(y - face_height * .6))
    bottom = min(height, round(y + face_height * 1.7))
    if right - left < 300 or bottom - top < 400:
        raise ValueError('hair_portrait_crop_too_small')
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), image[top:bottom, left:right], [cv2.IMWRITE_JPEG_QUALITY, 95]):
        raise ValueError('hair_portrait_crop_failed')
    return {'face': [x, y, face_width, face_height], 'crop': [left, top, right - left, bottom - top]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(crop(args.source, args.output)))
