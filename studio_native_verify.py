"""Check H3's original speech against the Studio Korean dialogue."""
import json
import re
import sys

def compact(text):
    # Ignore punctuation/spacing, never discard foreign words or digits.
    return ''.join(re.findall(r'[^\W_]', text, flags=re.UNICODE)).casefold()


def matches_dialogue(heard, expected):
    return bool(compact(expected)) and compact(heard) == compact(expected)


def main():
    import torch
    from transformers import pipeline

    video, expected = sys.argv[1:3]
    recognizer = pipeline(
        'automatic-speech-recognition',
        model='openai/whisper-small',
        revision='973afd24965f72e36ca33b3055d56a652f456b4d',
        device=-1,
        dtype=torch.float32,
    )
    # Forcing Korean hid the unwanted opening in job 891a0782.
    heard = recognizer(video, generate_kwargs={'task': 'transcribe'})['text'].strip()
    print(json.dumps({'transcript': heard, 'matched': matches_dialogue(heard, expected)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
