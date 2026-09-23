# Approved H3 wardrobe route

The user approved the 2026-09-22 H3 Ref2VA motion-reference yoga sample after
HyperSwap 1b identity correction. The graph template is copied from that executed
sample with private input names and prompt replaced by runtime arguments.
Native H3 reference-face similarity was insufficient; correction remains mandatory.
The corrected sample had 107 processed frames, five identity cosine samples
0.800–0.857 (mean 0.836). These are diagnostics, not accuracy percentages.

Model: minimax_h3_ref2va_pruned_int8_convrot.safetensors, 20 steps, seed 9222029,
res_multistep/simple, no Turbo or additional LoRA. Portrait defines identity;
video defines motion; the explicitly selected outfit is written into the prompt.
This is a synthesis path, not original-pixel face replacement.

Production outputs cap the long side at 1280px, use 24fps, and trim extra model-grid
frames without time stretching. The reviewed source interval remains at most 15s.
Original assets remain archived; comparison video uses matched 24fps frames.
Content filters and all-frame identity correction checks remain active.

Wardrobe jobs extract the archived motion segment directly and do not require the
unrelated original-person face swap to pass before H3 synthesis. After synthesis,
HyperSwap targets the largest single subject and may retry face detection at
0.50, 0.35, then 0.20. Every retry must still pass the unchanged all-frame
coverage and identity thresholds. Failed source hashes and Instagram post IDs
remain consumed so an unsuitable clip is not selected repeatedly.

Validation: graph/model/LoRA regression checks; duration boundaries; old-receipt
compatibility; source novelty regressions; real approved-output replay through
production trimming, HyperSwap, audio mux, decoding, face metrics and comparison.
Replay tests do not claim a second fresh H3 generation.
