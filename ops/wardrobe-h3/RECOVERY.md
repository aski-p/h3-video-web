# Durable production recovery

Generation keeps its original source hash, interval, portrait, policy and quality
thresholds. Temporary connection failures resume the recorded prompt; a lost
submission acknowledgement is reconciled by client ID and exact graph, never
blindly submitted again. On restart the worker resumes its durable job.

After verified face processing, an atomic, hash-bound checkpoint allows account
mark restoration and final checks to restart without repeating H3. A moving mark
uses per-frame OCR, with short, bidirectionally verified template tracks between
observations. Identity is rechecked after restoration. Uncertain marks still fail.

Quality failures first try local repair, then archive the render and logs and use
a new deterministic seed for another generation with the same model and 20 steps.
The durable queue retries with bounded backoff (maximum 15 minutes), without an
arbitrary attempt limit. Studio displays the attempt, phase and stage percentage.
Retries stop on explicit cancellation, released sources, invalid inputs, content
blocks, or a source whose facial identity cannot be tracked with valid evidence.
These are explicit input blockers, never successful output or silent completion.
Technical checks remain separate from human publication approval.

Regression checks: original-video, wardrobe-video and production-recovery tests.
