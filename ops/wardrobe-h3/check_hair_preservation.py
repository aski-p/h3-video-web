"""Reject masked H3 renders that unexpectedly rewrite the scene outside the head."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def inspect(source, mask, result):
    streams = [cv2.VideoCapture(str(path)) for path in (source, mask, result)]
    if any(not stream.isOpened() for stream in streams):
        raise ValueError('hair_preservation_unreadable')
    try:
        counts = [round(stream.get(cv2.CAP_PROP_FRAME_COUNT)) for stream in streams]
        if len(set(counts)) != 1 or counts[0] < 5:
            raise ValueError('hair_preservation_timing_mismatch')
        samples = sorted({min(counts[0] - 1, round(counts[0] * ratio)) for ratio in (.1, .3, .5, .7, .9)})
        errors = []
        for index in samples:
            frames = []
            for stream in streams:
                stream.set(cv2.CAP_PROP_POS_FRAMES, index)
                okay, frame = stream.read()
                if not okay:
                    raise ValueError('hair_preservation_frame_missing')
                frames.append(frame)
            original, stencil, generated = frames
            if original.shape != generated.shape or stencil.shape[:2] != original.shape[:2]:
                raise ValueError('hair_preservation_dimensions_mismatch')
            # Exclude the feathered boundary, which the VAE is allowed to blend.
            outside = stencil[:, :, 0] < 8
            if outside.mean() < .5:
                raise ValueError('hair_preservation_mask_too_wide')
            difference = np.abs(original.astype(np.int16) - generated.astype(np.int16))
            error = float(difference[outside].mean())
            errors.append(error)
            if error > 12:
                raise ValueError('hair_preservation_scene_changed')
        return {'sampleFrames': samples, 'outsideMaskMeanAbsoluteRgb': errors,
                'outsideMaskLimit': 12}
    finally:
        for stream in streams:
            stream.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('source', 'mask', 'result', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    report = inspect(args.source, args.mask, args.result)
    args.report.write_text(json.dumps(report))
    print(json.dumps(report))
