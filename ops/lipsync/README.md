# Korean mouth synchronization

User authorization: 2026-10-01, natural story trio; user chose installation and
verification of lip synchronization before Korean dialogue videos.

LatentSync 1.6 official repository commit a229c3948406bc2cf6eaf4873e662e70c6a04746,
checkpoint ByteDance/LatentSync-1.6. Isolated environment at
/home/aski/PGX/archives/20261001-lipsync. ARM64 adaptations use OpenCV/librosa instead
of unavailable Decord, CPU ONNX face detection, pinned compatible Accelerate/PEFT,
and original 24 fps (not upstream automatic 25 fps conversion). Eight-frame inference
chunks reduce memory; both H3 generation and lip inference retain 20 diffusion steps.

Only a short, visible speaking interval is processed. Final composition keeps eyes,
nose, surrounding face/body and background from the source and replaces a feathered
mouth region. Exact Korean TTS audio replaces H3 audio. Video frame count, dimensions,
FPS and duration are checked. Detection failures fail closed, never silently revert
to dubbed footage. Technical completion does not approve or publish a video.

A three-second sample ran successfully. Full output and mouth-only comparison are
in sample/. Frame contact sheet was visually inspected. Temporal sync measurement
is pending, and perceptual lip quality remains subject to human review.

Deployment must follow Git push. Activation is a one-shot process with a frozen,
checksummed bundle, only after a 130-second idle interval and final active-job check.
It does not cancel/recreate current jobs or change prior packages. Studio capability
check keeps the new trio queued until the backend advertises the new policy.
