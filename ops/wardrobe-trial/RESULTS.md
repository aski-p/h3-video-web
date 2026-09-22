# Wardrobe video trials — 2026-09-22

Three fully clothed comparison clips were rendered from the same previously
reviewed `always_portrait` four-second source, with the current Studio fixed adult
portrait. The current Studio image hash matched the portrait used for final face
processing. Bikini reference generation was rejected by the image service and is
not included. No alternate bikini generation was attempted.

Variants: navy short-sleeve dress; teal athletic T-shirt with black leggings;
white T-shirt with blue jeans. Reference frames were edited with imagegen.
Wan Animate 2 / Lightx2v / six steps / seed 9222026 transferred source motion.
HyperSwap 1b with the existing reference masks and pixel boost 512 then applied
the fixed face. Original audio was mapped into the final files when present.

All three final files decoded successfully: 432×768, 30 fps, 120 frames, 4 seconds.
These are low-resolution comparison trials, not replacements for full-resolution
original-frame production. Five evenly spaced face samples were inspected per
clip. Relative face-embedding means were dress 0.799, sportswear 0.802, casual
0.791 (source 0.822); these are diagnostics, not accuracy percentages.

Sampled poses and expressions broadly follow the source. Garments remain
consistent in the sampled frames. Hand/garment interaction and regenerated folds
differ from the source. Pixel preservation and production quality are not certified.
The existing Today workflow and its quality policy remain unchanged.

Private outputs, per-job graphs/receipts, technical diagnostics, comparisons and
ZIP are under `~/Documents/Codex/outfit-test/`; identity assets and videos are not
committed. The comparison gallery is `results/index.html`. The source provenance
is retained in the existing original-video job and in the local gallery.

Validation: original-video regression suite (11 tests); full video decode;
frame count/FPS/dimensions; five sampled face/expression diagnostics per variant;
visual review of source, wardrobe samples and final face comparison.
