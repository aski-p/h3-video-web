# Project instructions

## Original-video face workflow

For Studio original-video work, preserve the user-approved 2026-09-21 baseline in
`ops/face-quality/RESULTS.md` and `default-profile.json`. Production integration is
`original_video.py`; the Studio counterpart documents the full contract in
`docs/original-video-quality.md`.

Never replace this workflow with H3 synthesis, lower the selected model/masks,
change source timing or dimensions, or replace tracked texture restoration with
blur as an automatic fallback. Unreviewed source segments, missing originals or
fixed portraits, and failed quality checks must remain blocked/failed. Technical
checks do not grant publication approval; keep original/result comparison.

Changes to this path require the original-video quality gate tests. Push to
GitHub before runtime application. The PGX runtime server can contain separate
changes: compare it with GitHub and apply scoped changes rather than overwriting
its whole file without inspection.

New daily work must not reuse completed or active source hashes/post IDs across
requests. Idempotent delivery of the same request is allowed. Reviewed variable
segments up to 15 seconds must receive at least one quality sample per second
(minimum five); never assume the initial four-second test covers longer footage.

Explicit wardrobe changes are authorized via original (default), dress, sportswear,
casual, bikini, swimsuit, yoga, or Studio mix (dress/sportswear/casual).
New wardrobe work MUST use the user-approved H3 Ref2VA + HyperSwap 1b pipeline,
policy wardrobe-h3-ref2va-v2-20260922, 20 steps, no Turbo/optional LoRA.
Do not use Qwen Image Edit/Wan or silently fall back to another pipeline.
Declare regenerated motion, maximum 1280px/24fps and floor-to-frame output duration.
Native source FPS, exact movement or original pixels are NOT preserved in this
explicit opt-in path. Original-face mode, novelty and reviewed intervals remain intact.
Keep content filters active and publication approval separate from technical checks.
Existing completed v1 wardrobe results remain readable; never rewrite old snapshots.
Push to GitHub before runtime application. See the wardrobe quality contract and
ops/wardrobe-h3/README.md in h3-video-web for the approved sample evidence.

## Explicit user-approved replacement: registered archive selection (2026-09-22)
The user explicitly approved abolishing the manual source-review requirement and replacing it with random selection from downloaded registered-account videos. This supersedes the manual reviewed-source/segment requirements above. Use 2–15 second source-bounded intervals; preserve archive integrity, active-account eligibility, historical source deduplication, output quality checks and content filters. Manual catalog registration is no longer required. Do not claim unknown overlays have been removed.
