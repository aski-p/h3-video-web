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

The explicit `portrait_hair` option keeps the original-face mode separate and
requires a 5–15 second source interval. Prompt-only Ref2VA trials were rejected:
one retained the source hairstyle; another copied the portrait scene into the
last frame. This path instead tracks the source head, masks room for loose hair,
and feeds the original 24fps video and audio as the H3 source latent. The fixed
portrait conditions Ref2VA; only masked video cells are generated. HyperSwap 1b
then corrects the face, and all existing technical quality gates remain active.
The original outfit and background are protected outside the mask, but the head,
shoulders and nearby background can change. Five frame samples outside the mask
must stay within a mean absolute RGB difference of 12/255; larger scene changes
hold the job. Hair matching still needs direct source/output/portrait review
before publication.

The `portrait_face` option keeps the source hairstyle and uses the same tracked
head mask and H3 latent. A narrow face-only mask produced an unintended black
face covering in one source, even with an explicit uncovered-face prompt; do not
restore that mask as a fallback. HyperSwap 1b still supplies the fixed portrait
identity, and every-frame face coverage, sampled identity, scene preservation,
and comparison checks remain required. A prior hair-mask render may be restored
only when its original technical verification and media files pass integrity
checks and the user explicitly accepts retaining the source hair.

The 2026-09-25 isolated 5.167-second sample used 124 frames at 704×1248 and
20 Ref2VA steps. Five outside-mask samples differed by 2.87–3.11 RGB levels
out of 255. After HyperSwap 1b, six portrait-similarity samples were 0.866–0.903
(mean 0.888), with 124/124 face corrections. A separate moving-mask graph trial
at 384×672 returned 3.47–4.03 outside-mask differences and showed long hair
across beginning, middle and end frames. These measurements support this one
trial and do not guarantee hair quality for every source; review remains required.

The mask graph requires the separate GPL-3.0
`ethanfel/ComfyUI-MiniMaxH3-PerRowMasking` custom-node package, pinned for the
verified trial at commit `d6a7964`. Install it as a separate ComfyUI custom node;
do not copy its code into this repository. Check that `MiniMaxH3SetGenerationMask`,
`MiniMaxH3MaskGridPreview`, and `MiniMaxH3PerRowMaskPatch` appear in
`/object_info` before enabling the option. The production worker holds the job
if the package is absent; it never falls back to whole-frame synthesis.

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
