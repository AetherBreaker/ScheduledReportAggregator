# aeth-ext v8 Migration Audit — `feat/aeth-ext-v8`

Runtime/behavioral audit of the migration to aeth-ext 8.0.6. Static checks pass clean; the findings
below are all things pyright, ruff and an import smoke test structurally cannot reach.

Method: 17 agents across 5 runtime dimensions, each finding put through two adversarial verification
lenses (code-truth and production-reality). Refuted findings dropped. Headline findings re-verified by
hand against source.

## Status

| Must-fix | State |
| --- | --- |
| Root cause — job bodies block the event loop | **fixed** — `run_job` offloads sync bodies via `to_thread`; verified the loop ticks during a blocking body and `FTP_CVAR` survives the hop |
| 2. Password in fatal alerts | **fixed** — dial failures no longer reach the fatal renderer (see #3); upstream scrub filed as aeth-ext TODO #13 |
| 3. Transient SFTP outage kills container | **fixed** — `PoolClosedError` / `OSError` / `SSHException` arms in `run_job`; all six error types verified to reschedule rather than escalate |
| 5. `err_counter` never reset | **fixed** — reset on clean run; threshold now alerts via `trigger_shutdown` instead of exiting silently |
| 6. False fatal on `docker stop` | **fixed** — executor done-callback swallows the nudge's `KeyboardInterrupt`/`SystemExit` |
| 8. Unguarded entrypoint exit code | **fixed** — `run_app` guards `initialize()` and the deferred import |
| 7. Pool holds week-idle transports | **fixed** — replaced class-level pooled adapters with a per-session `JobBase.ftp_session()`; `connect_timeout=30` set. Severity was overstated: checkout *does* revalidate, so a job was never handed a dead channel; the real cost was an unbounded `_validate` listdir outside `acquire_timeout` |
| 1. Deploy pins `GIT_TAG: v2.4.5` | **open** — needs a version bump + tag push (deploy action) |
| 4. Log-server boot coupling | **reassigned to aeth-ext** — filed as TODO #14; consumers should not each wrap `initialize()` |

Fixing #7 also removed the import-time credential reads, which unmasked and then closed two more of
the same class (should-fix: the Google service-account key, and the employee-list `iterdir()`). All
credential and input-file resolution is now lazy, so a missing secret surfaces as a reschedulable job
error where logging and alerting exist, instead of a bare stderr traceback during module import.

**Result: the `-O` production path now imports all 14 modules cleanly with no secrets and no container
paths present — it was 8 failures at the start of this work.**

Library-side findings filed in `../aeth_ext/TODO.md` as items **13–16**: credential disclosure via
`show_locals`, the socket-logging boot probe, `handle_fatal_exc_sync` vs. the exit nudge, and unbounded
SMTP on the fatal path.

Gates after these changes: `ruff check src/` clean, `pyright` 0 errors, all 14 modules import, and the
`-O` path unchanged from baseline.

## Already proven good — do not re-litigate

- **pyright: 0 errors, 0 warnings** against real v8. (Never actually ran before — the venv was stale at 6.3.1.)
- **ruff: all checks passed**, after restoring `known-first-party` for the in-house libs that
  aeth-devkit's template dropped.
- **All 14 modules import cleanly** — no `PydanticUserError`, no annotation-resolution failure, no
  unset `ContextVar`. `FTP_CVAR` is set at `jobs/base.py:264` and `bal_sheet_job.py:275`.
- **Settings inheritance is correct.** Under `-O` with env vars, `persisted_dir_loc` →
  `/app/persisted_data`, matching pre-migration. The `default_factory` MRO ordering works.
- **The bundled font ships in the wheel** (`errors/fonts/JetBrainsMono-Regular.ttf`), so dropping
  `fonts-dejavu-core` from the Dockerfile is safe.
- **`entrypoint.sh` `exec`s**, so SIGTERM from `docker stop` reaches Python and v8's signal ladder.

---

## The root-cause finding

### Every job body blocks the event loop — `jobs/base.py:258`

`bal_sheet_job.py` and `employee_disc_job.py` define `async def main_job` with **zero** `await`
expressions. `flip_sheet_job.py`'s single `await` is inside `main_test()` (dead code). Only
`TimeclockJob` genuinely awaits. `scheduler_config.py:137` always takes the `create_task` branch
because `run_job`'s wrapper is itself `async def` — so blocking paramiko/pandas/gspread/SMTP calls
all run on the single loop thread.

Three symptoms share this root cause:

1. `await SHUTDOWN` in `main()` cannot resolve while a job runs, so the whole shutdown tail
   (heartbeat cancel, `scheduler.pause()`, `scheduler.shutdown()`, `await SHUTDOWN_COMPLETE`) is
   unreachable on the signal path, and the 7s graceful budget collapses to a ~0.25s nudge.
2. The 60s heartbeat cannot tick during a long job, so the healthcheck measures loop responsiveness,
   not liveness, and flips the container unhealthy mid-report.
3. It is the enabling condition for the false-fatal alert (must-fix #6).

**Fix:** in `run_job`'s wrapper, `return await to_thread(func, *args, **kwargs)`, carrying ContextVars
with `copy_context()` so `FTP_CVAR` still resolves. Highest-leverage change on the branch.

---

## Must fix before merge

1. **Deploy ships unmigrated code** — `docker/compose.yaml:8` pins `GIT_TAG: v2.4.5`. The Dockerfile
   clones `src/` and `pyproject.toml` from that tag while copying the entrypoint from the build
   context, so a redeploy *builds and comes up healthy* running v2.4.5's `logging=True` +
   `FATAL_EVENT` code. Confirmed: `git show v2.4.5:pyproject.toml` requires `aeth-ext>=6.2.2,<7`.
   Gates every other item. Bump version → tag → push → update `GIT_TAG`.

2. **Fatal alerts ship the live SFTP password in plaintext.** `aeth_ext/ftp/sftp_connector.py:79`
   passes `password=...get_secret_value()` as a *keyword arg*, binding plaintext as a frame local in
   paramiko's `SSHClient.connect`. `err_handling.py:118` and `traceback_image.py:97` both render with
   `show_locals=True`. `alert()` fans out to email **and Pushover**, so the credential leaves the org.
   `BalanceSheetJob`/`TimeclockJob` reach this path bare — any past dial failure already mailed it.
   Fix by catching dial failures as `CanRescheduleJobError`; also correct the false docstring at
   `base.py:51-56`; file upstream to scrub secret-shaped locals.

3. **A transient SFTP outage kills the container.** No `except` clause anywhere in `src/` names
   `ConnectionError`, `OSError`, `TimeoutError` or `PoolClosedError` — exactly what v8's pool raises.
   A server reboot reaches `base.py:278`'s bare `except Exception: raise` → `handle_fatal_exc_sync`
   → `run_shutdown(FATAL)` → exit 1 with `restart: no`. `EmployeeDiscountsJob` absorbs the same
   exception into a reschedule; the divergence is accidental. Add `except PoolClosedError: return`
   and `except (ConnectionError, TimeoutError, OSError, SSHException)` → `CanRescheduleJobError` at
   each `start_session()` boundary.

4. **Boot is hard-coupled to the log server; `restart: no` makes failure permanent.**
   `logging/setup.py:606-613` does one `create_connection(timeout=5.0)` and raises `RuntimeError`
   unconditionally; nothing on the `initialize` → `init_logging_socket` path catches it, and it fires
   before logging/alerting/heartbeat exist. Same coupling at runtime — an `ApplyFailure` calls
   `trigger_shutdown(FATAL)` mid-job. Add bounded retry + fallback; change `restart: no` →
   `on-failure`.

5. **`err_counter` is never reset.** Only three sites exist: declare (`base.py:156`), increment (314),
   compare (316). So `err_max_threshold = 3  # consecutive` counts *lifetime* errors on a singleton.
   Three ordinary "file not posted yet" mornings across months trip it. The exit is silent —
   `run_shutdown` does not alert; alerting lives in `_handle_fatal`/`trigger_shutdown`. Reset on clean
   return; switch to `trigger_shutdown(...)` so the threshold exit alerts.

6. **A routine `docker stop` during a job pages a fake fatal.** The exit nudge raises
   `KeyboardInterrupt` inside the blocking body; `except Exception` misses it; `Runner.close()` drains
   it into the done-callback where `f.result()` re-raises into `handle_fatal_exc_sync` → alert email +
   Pushover + `run_shutdown(FATAL)`. `SHUTDOWN.request` escalates GRACEFUL→FATAL, so exit becomes 1
   and the budget drops 7.0s → 1.0s. Guard the callback with `except (KeyboardInterrupt, SystemExit): return`.

7. **Pools hold week-idle SSH transports with no keepalive and no read deadline.**
   `keepalive_interval=None` short-circuits the keepalive thread; `connect_timeout` is unset (~130s SYN
   budget). A transport with one idle channel is invisible to the 30s TTL pruner, which only considers
   `channel_count == 0`. Jobs touch ryo/sas weekly; a 7-day idle session across NAT is likely dropped
   while `is_active()` still says True — and the stall lands on the loop thread. Simplest fix: stop
   pooling across runs (`with create_ftp_adapter(...)`), which is effectively what v6 did.

8. **Only `run(main())` is protected from the exit nudge.** `sys_exit(...)` sits outside the `try`, and
   `__main__.run_app` guards neither `initialize()` nor the deferred import. A boot-time config
   rejection exits **130** rather than the intended 1. Move `sys_exit` into a `finally:` and extend the
   guard.

---

## Should fix

- **The library and the app hold two different settings objects.** Verified: `aeth_ext.utils.SETTINGS
  is SETTINGS` → False (same for `logging.setup`, `err_handling`). ~15 aeth_ext modules bind
  `BaseSettings.get_settings()` at module scope, and `initialize()` runs before `Settings` is
  constructed. Newly load-bearing *because* this migration deleted the consumer's own `model_config`
  and `persisted_dir_loc`. Values agree only until someone overrides an inherited field.
- **Untimed SMTP on the fatal path** (`utils.py:237`) runs *before* `run_shutdown(FATAL)` — a hung
  relay means the shutdown is never requested.
- **No queue in front of the socket handler, and no local log sink** — every log call is an inline
  `sendall` on the loop under an RLock; `docker logs` carries nothing. Regression vs `logging=True`.
- **Heartbeat runs on the loop it monitors**, so it cannot distinguish "busy" from "wedged".
- **Nothing creates `persisted_data/logs` in socket mode** — bites the first fresh volume only.
- **Dead-man's-switch ping unconfigured** — for a hang there is no outbound signal at all.
- **TimeclockJob's email batch is non-atomic and unrecorded** — needs a ledger; semantics is your call.
- **EmployeeDiscountsJob commits the destructive half of its rewrite first** — cleanup, not data loss.
- **TimeclockJob's subprocess plumbing blocks the loop and is never torn down**; the drainer's
  `__aexit__` drain is dead code (a `mp.Queue` has no `join`/`task_done`), so subprocess failure
  diagnostics are discarded.
- **Skipped/interrupted runs are invisible and never retried** (all `MemoryJobStore`). A missed Sunday
  `FlipSheetJob` silently arms a Thursday outage of all four jobs.
- **SFTP host keys auto-added and never pinned** — `known_hosts_path` only became expressible in v8.
- **`client_log_history` grows without bound** on the same volume as secrets and reports.
- **Employee list resolved once at import** — an updated CSV is silently ignored for the container's life.
- **Jobs cannot see a shutdown request** — `grep -rn SHUTDOWN src/` returns only `startup.py`.

---

## Pre-merge runbook

Ordered; step 1 gates the rest. Failure states are written so a *current-code* failure confirms a
finding.

1. Cut a real `v2.5.0-rc1` tag and rebuild. **Gate:** container reports aeth-ext 8.0.6 *and* socket
   line uncommented. If 6.2.x, you are testing the unmigrated app — stop.
2. Confirm `sys.flags.optimize == 1` and `__debug__ is False`. Otherwise every gated path is inert.
3. Pre-flight the bind mount: 3 creds JSONs + Google key in `secrets/`, ≥1 CSV in
   `timeclock_employee_input/`. Missing `logs/` predicts permanent unhealthy.
4. Boot with the log server **up**. **Gate:** no `RuntimeError`, container Up, *and* today's records
   visible on the server. A rejected handshake is protocol skew — resolve before merge.
5. Heartbeat advances ~60s; health reports `healthy`.
6. Stop the log server, then boot. **Gate:** current code exits ~5s / code 1 / no restart. Fixed code
   stays Up.
7. Restart the log server mid-flight. **Gate:** aggregator stays Up and backlog replays; if it exits,
   a log-server restart can kill the scheduler mid-week.
8. Fire `BalanceSheetJob`. **Record wall-clock duration** — it decides whether the healthcheck window
   and shutdown budget are realistic.
9. Count port-22 connections and threads after run 1 vs run 2. Two persisting sessions is the known
   idle-transport behaviour; higher counts after run 2 is a leak.
10. Blackhole the SFTP peers (`iptables -j DROP`, not REJECT) against a warm pool. **Gate:** current
    code freezes the heartbeat for minutes; fixed code fails in ~30s.
11. SIGTERM while **idle** — baseline: ~1s, code 0, `Shutdown requested (GRACEFUL)`, no alert.
12. SIGTERM **mid-transfer** — any of these confirms the blockers: exit 1; "Fatal exception in
    callback" email; shutdown tail absent; exit within ~1s; truncated CSV left behind.
13. Deliberate FATAL (creds → unreachable host). **Gate:** exit 1, alert arrives, ~1s. A missing
    `logger.critical` on the server means the socket flush was cut off by the FATAL budget.
14. **Open that alert email and search it for the SFTP password** — body *and* attached PNG, plus
    whether Pushover carried it. Pass only once no fatal alert is generated at all.
15. Confirm the silent threshold exit: current code exits 1 with **no alert of any kind**.
16. TimeclockJob end to end — watch for a frozen heartbeat at drainer construction; after a mid-run
    stop, check the server for the subprocess's final records (absence confirms the dead drain).
17. Fresh-volume check: current code never creates `heartbeat.txt`, health stays unhealthy forever.
18. Post-run volume audit: per-program history dirs alongside orphaned flat v6 files; record size as
    growth baseline.
19. Restore deploy inputs; cut real `v2.5.0`; update `GIT_TAG`. A deploy still reading `v2.4.5` has
    shipped nothing.

---

## Accepted risks (examined, deliberately not fixed here)

- Bytecode compiled without `-O` while runtime sets `PYTHONOPTIMIZE=1` — startup latency only; belongs
  upstream in the shared Dockerfile template.
- `remote_per_run.toml` dead config, stale `logging_config_schema.json`, orphaned pre-v7 flat history
  files — all inert. The schema needs regenerating before anyone writes a socket-mode config file.
- `assert`s stripped under `-O` in `bal_sheet_job.py:92-94` — unreachable as configured (value is
  always 4 or 5 given `US/Eastern`); only real if TZ moves east of UTC.
- `chown -R` under `set -e`; inconsistent cleanup handlers between sibling jobs — no consequence.
- Untranslated `SFTPError` during listings — identical outcome to a raw error until must-fix #3 lands.

## Open questions

1. Ledger semantics for TimeclockJob's email batch: at-least-once (duplicates) or at-most-once (gaps)?
2. Should a runtime log-server `ApplyFailure` be fatal, or should the app run blind and keep working?
3. Keep pooling across runs at all, given jobs touch each server ~weekly?
4. Is `restart: no` deliberate? It converts transient failures into permanent outages and prevents
   return after a host reboot.
