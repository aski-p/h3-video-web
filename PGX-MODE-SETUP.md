# PGX mode switch + RTX 5080 long I2V deployment

Web deployment alone does not update the PGX Python backend or Windows worker.
Do not claim this is operational until both machines have been checked.

## Intended behavior

- Header: Video / Qwen 3.8 dedicated mode; PIN checked server-side (requested PIN: 1229).
- No plaintext PIN in browser assets. Installer stores a PBKDF2 hash separately from source.
- PGX pending/starting/running work blocks switching; it is not automatically killed.
- Switching first stops the other service and verifies it has stopped. Readiness requires the actual HTTP health endpoint.
- Failed start attempts restore the previously healthy mode. Failed recovery is displayed.
- RTX 5080 remains independent. Qwen API requests in flight may stop when switching back to video.
- H3 API remains running. PGX generation admission is rejected outside the video-ready mode.

## Hermes: perform these steps on the actual machines

1. Inspect running PGX/5080 jobs. Do not restart their services while jobs are in progress. Record current revisions and service files for rollback.
2. Update `aski-p/h3-video-web` master in `/home/aski/h3-web`; include `server.py` AND `pgx_mode.py`. Keep `.env`, tokens, references and media intact.
3. Prepare the exact requested native recipe:
   https://github.com/vcruz305/Qwen3.8-Flash-Next-EXL3-DGX-Spark-recipe
   Reviewed recipe revision: `a455f8ec2d5051ff6fd0b6d0426c28a827577532`.
   Use `scripts/exl3_native/serve_openai.sh`, the compatible tuned ExLlamaV3 engine described there, and `turboderp/Qwen3.8-Flash-Next-exl3` revision `3.05bpw_h5_ng5` (~80 GB).
   Reuse an existing complete compatible download. Check disk space and architecture before downloading/building. Do not use the vLLM-modified config for native mode without the recipe's native-view conversion.
4. Confirm actual engine venv, model directory, and recipe directory. Installer requires all three to exist; it does NOT install the engine or download weights.
   Example, with actual paths substituted:
   ```bash
   sudo env H3_SERVICE_USER=aski QWEN_RECIPE_DIR=/home/aski/Qwen3.8-Flash-Next-EXL3-DGX-Spark-recipe EXL3_ROOT=/home/aski/exllamav3 MODEL_DIR=/home/aski/models/Qwen3.8-Flash-Next-EXL3 python3 scripts/install-pgx-mode.py
   ```
   Enter `1229` at the hidden PIN prompt. No credential goes in browser JavaScript or the command line.
5. The installer creates a Qwen system service bound to `127.0.0.1:8899`, uses 262144 context as in the recipe, adds narrowly scoped start/stop authorization for ONLY ComfyUI and Qwen, and adds a mode environment file to the existing user H3 backend service. It does not stop/start models during installation. Inspect other GPU-resident LLM services; disable their auto-respawn only after identifying them, so “Qwen only” is actually exclusive.
6. Once idle, as `aski`, run `systemctl --user daemon-reload` and restart `h3-web-backend.service`. Ensure the actual backend unit has the drop-in. Confirm `/api/pgx-mode` through the existing authorized proxy. Do not expose the origin token or bypass origin authentication.
7. Update the actual installed Windows worker's shared `server.py`, new `pgx_mode.py`, and `h3_worker.py` from `windows-worker/`. Back up first; preserve config and encrypted token. Restart only the worker task when idle. New install packages must include `pgx_mode.py` (installer file list was updated).
8. Verify PIN rejection, PGX busy rejection, video → Qwen (`/v1/models` and one short completion), and Qwen → video (`/system_stats`). Check there is never simultaneous GPU residency from these two services. The UI must never show ready while still loading.
9. On RTX 5080 run one image-reference 10-second continuous sample first; verify output duration and frame count, then 15 seconds. Single-take cap is now 15 seconds instead of forcing >5 seconds into split segments. I2V maximum remains 15 seconds. Longer T2V retains splitting. Worker timeout scales from 6 hours at 124 frames up to 24 hours; cancellation/lease checks remain active. This does not guarantee that 16 GB VRAM can fit every resolution/LoRA combination; report OOM honestly and do not silently shorten the video.
10. Report actual service states, model revision, successful tests, and remaining blocks. Repository tests are not hardware validation.

## Rollback

Restore backed-up backend/worker files and restart only when idle. Stop Qwen before restoring video mode. Remove `30-pgx-mode.conf` and `/etc/polkit-1/rules.d/49-h3-pgx-mode.rules` if disabling control; keep the model download. Do not delete user media or tokens.
