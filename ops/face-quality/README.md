# Face-only quality experiment

Run `trial.py` using a local FaceFusion Python environment, providing `--engine`,
`--source`, `--portrait`, `--output`, and `--model`. Compare GHOST and HyperSwap
using identical inputs, reference-person selection, masks, duration and settings.
No production model is changed. Official model downloads and engine safety checks
remain enabled. Models have their own licenses; this harness is a local experiment.

The first pass measures robust Lab skin differences in aligned original/result
faces. Run the selected model again with `--tone-from <first-pass.stats.json>` to
apply one bounded shot-level correction, limited to skin inside the face mask.
It excludes non-skin regions and avoids per-frame color correction flicker. It is
not a guarantee of perfect lighting or expression preservation. Optional
`--expression 80` adds the engine's original-expression restoration for comparison.

The supplied experiment uses frame 30 to choose the target person, 30fps, CPU
execution, and a short single-shot clip. Adapt reference selection before using
other footage. Do not treat female/largest-face selection as identity verification.
Verify the selected person and each output visually before use.

Tests: `python ops/face-quality/test_tone.py` in the FaceFusion environment.
