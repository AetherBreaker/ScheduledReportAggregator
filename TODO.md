1. update timeclock job to detect when a store has no allocated hours set so it can change the phrasing used in the alert email to be less alarming.

2. build an external job-tracking database (likely google sheets) to record job completion and drive smarter rescheduling.
   - **bug (weekend crash, 2026-09-12/13):** a balance sheet source file was delivered with the wrong name during the week, so `bal_sheet_job` never matched it and kept failing. the run was completed by hand locally with an altered file matcher, but production had no way to know that, so `error_reschedule` kept pushing the job forward. the reschedule eventually rolled into the following week, where the matcher picked up a *new* FTX file instead of the mis-named ryo file it was originally waiting on. the wrong-week data raised an error that counted toward `err_max_threshold` (3 consecutive), tripping the shutdown path in `base.py`.
   - **root cause:** job state is process-local (`err_counter`, `active_args` on the singleton in `jobs/base.py`). there is no durable record of "the run for period X already completed", so a rescheduled job has no period identity and will happily bind to whatever file the matcher finds at the time it finally runs.
   - **what the tracker needs to provide:**
     - a row per (job, target period) with status, attempt count, last error, and completion timestamp/source.
     - jobs read it at startup so a manual/out-of-band completion cancels the pending reschedule instead of retrying forever.
     - reschedules stay pinned to the *original* target period; a job must refuse a file whose period doesn't match the row it is trying to satisfy, rather than silently succeeding into the wrong-week failure.
     - a bounded give-up path: mark the row failed/needs-attention and alert, instead of retrying until the consecutive-error threshold shuts the whole process down.
     - error counters keyed per (job, period) and persisted, so unrelated failures across different periods don't stack toward `err_max_threshold`.
   - also worth deciding: whether manual local runs should be able to write completion rows directly (cli flag / small script) so the dev-machine workaround used this time is a supported path.
