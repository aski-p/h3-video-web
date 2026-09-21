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
