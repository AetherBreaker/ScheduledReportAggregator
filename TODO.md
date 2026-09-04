1. **HIGH PRIORITY — required before adopting the next aeth-devkit Docker standard.** `HOLDING_FOLDER`
   (`jobs/base.py`, `CWD / "file_holding"`) and `TIMECLOCK_PLAYGROUND` (`jobs/timeclock_job/__init__.py`,
   `CWD / "timeclock_playground"`) are entirely ephemeral scratch directories but live beside the code under
   `/app`. The standardized image keeps `/app` root-owned and runs the app as `nonroot`, and the entrypoint no
   longer honours `[tool.docker].mkdirs`, so the `.mkdir()` calls in `startup.py` and the timeclock job will
   raise `PermissionError` at container start. Move both to temp directories (`tempfile.mkdtemp()` /
   `Path(tempfile.gettempdir())`), then delete `mkdirs` from `[tool.docker]`.
2. update timeclock job to detect when a store has no allocated hours set so it can change the phrasing used in the alert email to be less alarming.
