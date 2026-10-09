"""Saved adult body contract for fixed-photo generation, not source-video transfer."""
import base64
import json


def normalize_body(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"heightCm", "build", "bust"}:
        raise ValueError("Invalid fixed body fields")
    height = value["heightCm"]
    if height is not None and (type(height) is not int or not 140 <= height <= 210):
        raise ValueError("Invalid fixed body height")
    if value["build"] not in ("reference", "slim", "balanced", "curvy", "athletic"):
        raise ValueError("Invalid fixed body build")
    if value["bust"] not in ("reference", "A", "B", "C", "D", "E"):
        raise ValueError("Invalid fixed body bust")
    return {"heightCm": height, "build": value["build"], "bust": value["bust"]}


def decode_body(header):
    if not header:
        return None
    if len(header) > 512:
        raise ValueError("Fixed body header too long")
    return normalize_body(json.loads(base64.b64decode(header, validate=True)))


def body_prompt(value):
    b = normalize_body(value) or {"heightCm": None, "build": "reference", "bust": "reference"}
    height = ("Keep the reference height without inventing a numerical height." if b["heightCm"] is None
              else f'Target standing height {b["heightCm"]} cm; preserve relative scale.')
    builds = {"reference": "the same silhouette as the reference", "slim": "a consistent slim adult build",
              "balanced": "a consistent balanced adult build", "curvy": "a consistent curvy adult build",
              "athletic": "a consistent athletic adult build"}
    bust = ("Preserve the reference bust proportions." if b["bust"] == "reference" else
            f'Use a consistent {b["bust"]}-cup-like bust silhouette as an approximate styling target, not a measured bra size.')
    return ("FIXED ADULT BODY IDENTITY: Keep the same adult person, face and body proportions across shots and episodes. "
            f'{height} Maintain {builds[b["build"]]}. {bust} '
            "Keep shoulder width, waist, hips, limb lengths and bust proportions stable, without size inflation, anatomy distortion or sexualized framing. "
            "Fully opaque well-fitted clothing. Do not let outfit, camera angle or style LoRA redesign the body.")


def fixed_prompt(prompt, body):
    # Studio uploads have their own immutable per-job contract; only direct fixed refs call this.
    import re
    prompt = re.sub(r"FIXED ADULT BODY IDENTITY:[\s\S]*?Do not let outfit, camera angle or style LoRA redesign the body\.", "", prompt).strip()
    return prompt + "\n\n" + body_prompt(body)
