"""Background job architecture (directive sections 7, 8, 9, 42).

Layout:

* :mod:`app.jobs.states`   - the job state machine (pure)
* :mod:`app.jobs.cancel`   - cancellation tokens and child-process handling (pure)
* :mod:`app.jobs.progress` - measured progress reporting (pure)
* :mod:`app.jobs.spec`     - ``JobSpec`` / ``Job`` / ``JobResult`` and the single
  execution function ``execute_job`` (pure - usable from the CLI and tests)
* :mod:`app.jobs.worker`   - the Qt runnable that executes a job off the UI thread
* :mod:`app.jobs.manager`  - ``JobManager``: submission, duplicate rejection,
  cancellation, shutdown

The Qt modules are intentionally *not* imported here so that the command line
and the test suite can use the job core on a machine without a GUI stack.
"""
