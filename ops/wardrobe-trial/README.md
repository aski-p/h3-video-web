# Explicit wardrobe video experiments

This is a separate, user-requested motion-transfer experiment, not the original-face
production workflow. Wan Animate 2 regenerates frames: original pixels, timing,
identity and clothes are not assumed verified. Never mark these outputs as passing
`original-face-v1` or silently use this renderer for Today production.

Reference images must already show the requested approved wardrobe and fixed adult
identity. The test holds seed, input motion, camera prompt, size and settings constant
across variants. All outputs require visual comparison. A submitted job receipt is
written before waiting; interrupted submissions must be inspected, never blindly
resubmitted. Run each GPU job sequentially.

Based on the official Comfy-Org `video_wan_animate2.json` template:
https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan_animate2.json
