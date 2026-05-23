# TomCat – Outstanding work

Running list of follow-ups deferred from active sessions. Add as we go, check off as done.

## Code health

### Labeler debloat
- `tomcat/handlers/labeler.py` and `labeler.js` accumulated vibecoded patches over time. Symptoms: large file size, duplicated logic, over-engineered workarounds.
- **Goal**: read through, identify dead code / redundant caching layers / duplicated patches, prune without behavior change.
- **When**: post-Oracle DigitalOcean migration, dedicated session. Not part of cutover scope.

### `cache/gallery_retrain/work/` cleanup
- `services/gallery_updater.py` writes a fresh `work/<run_id>/crops/` directory per retrain run and `.replace()`s the latest one into `active_crops/`. Older `work/<run_id>` directories never get cleaned up.
- Result: ~15 GB on the laptop right now; same accumulation will happen on the droplet over time.
- **Fix**: after the `crop_root.replace(active_crops_root)` line in `gallery_updater.py` (~line 817), add logic to delete any sibling `work/<old_run_id>/` directories older than N days (or keep only the most recent K runs).
- Optional: one-time cleanup script to wipe existing `work/*` on both hosts.

## CV model upsize (post-migration)

- Modal T4 has way more headroom than the 1050 Ti prod box ever did. Retrain larger variants on Colab Pro, swap in on Modal.
- Candidates: YOLO12 m/l instead of s; bigger DINOv3 backbone; revisit SAM2 size.

## Deferred fixes from earlier sessions

### Droplet `.env` profile edit interval
- Local `.env` was updated to `PROFILE_UPDATE_EDIT_MIN_INTERVAL_SEC=4.5` (was `PROFILE_UPDATE_EDIT_DELAY_SEC=1.0`, which caused the 429s in `CH_CATS_ON_CAMPUS`).
- **Still to do**: confirm the droplet's `.env` is updated too (it was likely copied from the old laptop value).

## Droplet polish (post-cutover, low priority)

### Dependency drift audit (Windows vs requirements.txt)
- `modal==1.4.2` was pip-installed manually on Windows and never added to `requirements.txt` — caught on droplet cutover when the bot failed with `ModuleNotFoundError`.
- Likely other manually-installed packages exist on the laptop venv that aren't in either requirements file.
- **Fix**: run `pip list --format=freeze` in the Windows venv, diff against `requirements.txt`, add anything the bot actually imports (skip transitive deps and dev-only tools).

## Manual cleanup on the laptop (whenever)

- Delete `weights/SmolLM2-1.7B-Instruct-Q6_K.gguf` (1.4 GB)
- Delete `weights/tokenizer.json`
- `pip uninstall llama-cpp-python` from the venv
- (All leftover from the local-LLM removal; bot no longer touches them)

## Post-cutover validation

- After 24 h on the droplet: check Modal cost dashboard, confirm scaledown_window=900 is producing the expected ~$0.15/identify pattern
- After 1 week: check droplet disk usage growth — if `cache/PicsOfCats` or `logs/` are growing fast, plan move to DO Spaces
