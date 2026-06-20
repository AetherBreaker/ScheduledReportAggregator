# Standard library imports
from datetime import timedelta
from logging import getLogger
from re import compile
from typing import TYPE_CHECKING, Any, TextIO, override
from zoneinfo import ZoneInfo

# Third party imports
import apscheduler.executors.base as exec_base
from apscheduler.events import EVENT_JOB_ADDED, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED, JobEvent, JobExecutionEvent
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_RUNNING, STATE_STOPPED
from apscheduler.util import iscoroutinefunction_partial

# First party imports
from environment_init_vars import SETTINGS
from sft_ext.errors.err_handling import handle_fatal_exc_sync
from sft_ext.utils import get_now

if TYPE_CHECKING:
  # Standard library imports
  from concurrent.futures import Future
  from datetime import datetime

  # Third party imports
  from apscheduler.job import Job


logger = getLogger(__name__)

__all__ = ["Scheduler"]

DO_NOT_LOG_PATTERNS = [
  compile(r".*?_register_dropoff_.*"),
  compile(r"submit_queued_writes_to_pool"),
  compile(r".*?_register_pickup_.*"),
  compile(r"print_jobs"),
  compile(r"heartbeat"),
]


def run_job(job: Job, jobstore_alias: str, run_times: list[datetime], logger_name: str):
  """
  Called by executors to run the job. Returns a list of scheduler events to be dispatched by the
  scheduler.

  """
  events = []
  local_logger = getLogger(logger_name)
  for run_time in run_times:
    # See if the job missed its run time window, and handle
    # possible misfires accordingly
    if job.misfire_grace_time is not None:  # pyright: ignore[reportUnnecessaryComparison]
      now = get_now(ZoneInfo("UTC"))

      difference = now - run_time
      grace_time = timedelta(seconds=job.misfire_grace_time)
      if difference > grace_time:
        events.append(JobExecutionEvent(EVENT_JOB_MISSED, job.id, jobstore_alias, run_time))
        local_logger.warning(f'Run time of job "{job.id}" was missed by {difference}')
        continue

    if not any(pattern.match(job.id) for pattern in DO_NOT_LOG_PATTERNS):
      logger.info(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
      local_logger.info(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
    else:
      logger.debug(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
      local_logger.debug(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
    retval = job.func(*job.args, **job.kwargs)
    events.append(JobExecutionEvent(EVENT_JOB_EXECUTED, job.id, jobstore_alias, run_time, retval=retval))
    if not any(pattern.match(job.id) for pattern in DO_NOT_LOG_PATTERNS):
      local_logger.info(f'Job "{job.id}" executed successfully')
    else:
      local_logger.debug(f'Job "{job.id}" executed successfully')

  return events


async def run_coroutine_job(job: Job, jobstore_alias: str, run_times: list[datetime], logger_name: str):
  """Coroutine version of run_job()."""
  events = []
  local_logger = getLogger(logger_name)
  for run_time in run_times:
    # See if the job missed its run time window, and handle possible misfires accordingly
    if job.misfire_grace_time is not None:  # pyright: ignore[reportUnnecessaryComparison]
      now = get_now(ZoneInfo("UTC"))

      difference = now - run_time
      grace_time = timedelta(seconds=job.misfire_grace_time)
      if difference > grace_time:
        events.append(JobExecutionEvent(EVENT_JOB_MISSED, job.id, jobstore_alias, run_time))
        local_logger.warning(f'Run time of job "{job.id}" was missed by {difference}')
        continue

    if not any(pattern.match(job.id) for pattern in DO_NOT_LOG_PATTERNS):
      logger.info(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
      local_logger.info(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
    else:
      logger.debug(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
      local_logger.debug(f'Scheduler: Running job "{job.id}" (scheduled at {run_time})')
    retval = await job.func(*job.args, **job.kwargs)
    events.append(JobExecutionEvent(EVENT_JOB_EXECUTED, job.id, jobstore_alias, run_time, retval=retval))
    if not any(pattern.match(job.id) for pattern in DO_NOT_LOG_PATTERNS):
      local_logger.info(f'Job "{job.id}" executed successfully')
    else:
      local_logger.debug(f'Job "{job.id}" executed successfully')

  return events


exec_base.run_job = run_job
exec_base.run_coroutine_job = run_coroutine_job


class CustomAsyncIOExecutor(AsyncIOExecutor):
  @override
  def _do_submit_job(self, job: Job, run_times: list[datetime]):
    @handle_fatal_exc_sync
    def callback(f: Future[Any]):
      self._pending_futures.discard(f)
      # try:
      events = f.result()
      # except BaseException as e:
      #   self._run_job_error(job.id, *exc_info()[1:])
      #   raise e
      # else:
      self._run_job_success(job.id, events)

    if iscoroutinefunction_partial(job.func):
      coro = run_coroutine_job(job, job._jobstore_alias, run_times, self._logger.name)
      f = self._eventloop.create_task(coro)
    else:
      f = self._eventloop.run_in_executor(None, run_job, job, job._jobstore_alias, run_times, self._logger.name)

    f.add_done_callback(callback)
    self._pending_futures.add(f)


class Scheduler(AsyncIOScheduler):
  @classmethod
  def init_scheduler(cls) -> Scheduler:

    job_stores = {
      "default": MemoryJobStore(),
      "system_jobs": MemoryJobStore(),
      "general_jobs": MemoryJobStore(),
    }

    job_defaults = {
      "misfire_grace_time": 60,
      "coalesce": True,
    }

    executors = {
      "default": CustomAsyncIOExecutor(),
    }

    return cls(
      executors=executors,
      jobstores=job_stores,
      job_defaults=job_defaults,
      daemon=False,
      timezone=SETTINGS.tz,
    )

  @override
  def _real_add_job(self, job: Job, jobstore_alias: str, replace_existing: bool):
    """
    :param Job job: the job to add
    :param bool replace_existing: ``True`` to use update_job() in case the job already exists
        in the store

    """
    replacements = {key: value for key, value in self._job_defaults.items() if not hasattr(job, key)}
    # Calculate the next run time if there is none defined
    if not hasattr(job, "next_run_time"):
      now = get_now(self.timezone)

      replacements["next_run_time"] = job.trigger.get_next_fire_time(None, now)

    # Apply any replacements
    job._modify(**replacements)

    # Add the job to the given job store
    store = self._lookup_jobstore(jobstore_alias)
    try:
      store.add_job(job)
    except ConflictingIdError:
      if replace_existing:
        store.update_job(job)
      else:
        raise

    # Mark the job as no longer pending
    job._jobstore_alias = jobstore_alias

    # Notify listeners that a new job has been added
    event = JobEvent(EVENT_JOB_ADDED, job.id, jobstore_alias)
    self._dispatch_event(event)

    self._logger.info(f'Added job "{job.id}" to job store')

    # Notify the scheduler about the new job
    if self.state == STATE_RUNNING:
      self.wakeup()

  @override
  def print_jobs(self, jobstore: str | None = None, out: TextIO | None = None):
    """
    print_jobs(jobstore=None, out=sys.stdout)

    Prints out a textual listing of all jobs currently scheduled on either all job stores or
    just a specific one.

    :param str|unicode jobstore: alias of the job store, ``None`` to list jobs from all stores
    :param file out: a file-like object to print to (defaults to  **sys.stdout** if nothing is
        given)

    """
    lines = []
    with self._jobstores_lock:
      if self.state == STATE_STOPPED:
        lines.append("Pending jobs:")
        if self._pending_jobs:
          lines.extend(f"  {job.id}" for job, jobstore_alias, _ in self._pending_jobs if jobstore in (None, jobstore_alias))
        else:
          lines.append("  No pending jobs")
      else:
        for alias, store in sorted(self._jobstores.items()):
          if jobstore in (None, alias):
            lines.append(f"Jobstore {alias}:")
            if jobs := store.get_all_jobs():
              lines.extend(f"  {job.id}" for job in jobs)
            else:
              lines.append("  No scheduled jobs")

    logger.debug("\n".join(lines))
