# Face-only comparison — 2026-09-21

Selected trial: `hyperswap_1b_256`, pixel boost 512, reference selector distance 0.3, reference frame 30, box + occlusion + region masks. No expression restoration or additional tone correction. This is a tested trial recipe, not a production-default change.

Same 4-second 720×1280 source, 30 fps, 120 frames and fixed fictional adult portrait for all full trials. The source Instagram reel itself is labeled AI content. This test transfers identity; it does not train a model or establish realism of the original footage.

| Model | Mean reference cosine similarity, 5 frames |
|---|---:|
| Previous GHOST | 0.628 |
| GHOST with reference masks | 0.587 |
| HyperSwap 1a | 0.695 |
| HyperSwap 1a with tone | 0.690 |
| HyperSwap 1b | 0.698 |
| HyperSwap 1c | 0.695 |

HyperSwap 1a and 1b better preserved closed eyes than GHOST in visual review. Small differences between HyperSwap identity scores are not conclusive. Selected 1b balances identity and eye preservation; 1a has lower sampled mouth-shape error. Scores are not accuracy percentages.

Lower-body mean absolute pixel difference is about 1.63/255 after re-encoding. Original movement and frame count remain intact; pixel-perfect preservation is not claimed.

A diagnostic of five extracted frames compared 1a + tone with/without LivePortrait expression restoration at 80. Restoration improved sampled mouth geometry but lowered mean reference similarity from 0.690 to 0.657 and took about 43 seconds for five frames on CPU. This is not a continuous-video evaluation; the full restoration render was canceled and is not counted as completed. Restoration remains optional, excluded from the selected result.

Validation: three tone unit tests; full-frame count/dimension/fps evaluation; five-frame face and expression diagnostics; visual comparison. Videos and identity assets remain local/NAS and are not committed.
