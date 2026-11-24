# Worker runbook

Keep the private tracker, PDFs and account settings in the directory selected by `JOB_AUTOMATION_PRIVATE_ROOT`. `codex/jr -h` lists the available operations. The default worker budget and schedule are defined in the runtime. The owner configures the host scheduler independently.

## Startup and discovery

Read the configured dashboard, save its new snapshot locally, use `gate` to preview work and `begin` to acquire the worker lease. Use `brief` to identify the next operation. Record the observation and its time for each source.

## Applications

Use `form_session` on the enrolled opening. Preserve existing answers when they conflict with saved facts. Check the selected attachment against its local SHA-256. Human verification and missing discretionary answers require a handoff. Use `prepare`, a fresh dashboard read and `submit_started` before one final click. Follow `reconcile` with the observed result. A missing receipt leaves the attempt pending.

## Dashboard and completion

Snapshot the dashboard before preparing a mirror plan. Retain user-editable cells, use `mirror_verify` to check each preflight and final readback, and write the stage receipt. Use `finish` after the worker has accounted for its queue.
