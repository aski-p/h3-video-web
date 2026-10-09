# PGX video mode admission fix

## Failure

`status()` combined the selected systemd service with an HTTP readiness probe.
During generation, a two-second ComfyUI timeout returned `loading` even though
the video service was active and Qwen was inactive. Admission interpreted this
as a different model mode and rejected requests with `PGX_MODE_BLOCKED`.

## Changes

- Determine the selected mode from mutually exclusive service states.
- Allow video queue admission only when video is active, Qwen is inactive/failed,
  and no controller switch is in progress. Fail closed on ambiguous states.
- Keep HTTP readiness separate. `ensure_comfyui()` still waits for readiness
  before actual execution; queue capacity and idempotency checks are unchanged.
- Preserve existing `mode` responses; add `selected_mode` and `ready` fields.
- Render the selected button during loading and refresh mode status on rejection.
- Preserve an existing job tracker when a subsequent submission is rejected.
- Preserve the selected service for recovery after a failed mode switch even
  when its HTTP readiness probe had timed out.

## Verification

- 14 Python mode/admission regression tests passed.
- Browser tests passed at 390px and 1365px: video-loading selection, Qwen
  selection, switching lockout, and no horizontal overflow.
- Read-only runtime check used actual PGX systemd states and a simulated failed
  HTTP probe: selected video, readiness false, admission allowed. No video
  was submitted and no service was switched by that check.
- Broader delivery suite: 175/176 passed. The unrelated existing
  `test_exact_worker_workflow_never_silently_omits_requested_loras` failure was
  reproduced on unmodified commit `303ebd6`; it is not claimed as passing.
- Vercel production HTML matched fixed source. Mode API and all three required
  frontend assets responded HTTP 200. The missing origin credential was added
  as a Production Secret, not as frontend configuration.

## Deployment

Application commit: `9c9c1ff`; browser test commit: `4f83867`.
Vercel deployment: `dpl_2w93Stc4QaLRgYa8mYUEARR22pcP`, promoted to
`https://h3-web.vercel.app`.

Only `pgx_mode.py` and the matching mode-render/error-handling snippets were
staged in the local runtime. Existing runtime-only server changes, credentials,
media, and job records were not overwritten. Backend restart must wait until
H3 jobs and the ComfyUI queue are idle, then the API must expose `selected_mode`
to confirm activation. File installation alone does not prove activation.
