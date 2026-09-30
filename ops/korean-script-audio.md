# Korean script audio (prepared; runtime activation pending)

Creative Studio requests use `audio_policy=korean-script-tts-v1` and typed `dialogue_ko`.
H3-generated audio is discarded before NAS atomic publication. Nonempty Hangul text
is read by Edge TTS `ko-KR-SunHiNeural`; empty text produces a silent video. The video
stream is copied unchanged. Existing jobs without the policy and original-video
workflows retain their existing audio. Online TTS failures fail the job, never fall
back to native H3 speech. Scripts are not truncated to fit.

Studio checks server capability before submission and verifies the final policy,
text hash and output mode before technical completion. This is a processing receipt,
not an ASR pronunciation check or lip-sync quality guarantee; human review is needed.

Verification: three Python tests; live Korean TTS and 15-second mux test, decoded video
frame hashes unchanged. Studio audio and original-video regression tests: 17 passing.

Activation: push both repositories first. Apply only this branch's scoped server
changes to `/home/aski/h3-web/server.py`, copy `studio_audio.py`, install pinned
requirements. Restart backend only with authorization consistent with active batch
instructions and no active generation. Verify `/api/health.audio_policy`, then merge
Studio branch to production. Never rewrite existing completed/active job configs.
As of 2026-10-01 00:46 KST, running job 8b70b6fd prevents activation under the current
no-service-restart instruction. No runtime server restart has been performed.
