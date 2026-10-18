import logging
import itertools
import math
import os
import threading
import time

from threading import Thread

from odoo import models, modules, sql_db, _
from odoo.tools.constants import PREFETCH_MAX
from odoo.tools import config
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class PropagatingThread(Thread):
    """This Thread extension helps:
    - return join() by the result of the `_target()`
    - raise error in threading mode
    """

    def run(self):
        self.exc = None
        try:
            self.ret = self._target(*self._args, **self._kwargs)
        except BaseException as e:
            self.exc = e

    def join(self, timeout=None):
        super(PropagatingThread, self).join(timeout)
        if self.exc:
            raise self.exc
        return self.ret


def pick_rebalanced_group(remaining, in_flight, home, cap):
    """Decide which group a freed worker thread should serve next.

    Serve the home group while it has work; once it drains, help the group with
    the most work left (ties: the one with the most idle capacity), never
    pushing any group above ``cap`` concurrent workers. Returns the chosen
    group key, or None when nothing is currently pullable - either everything
    is drained, or every remaining group is already at its cap.

    Pure function on purpose: multi-threading is disabled in Odoo's test mode,
    so this scheduling logic is what the tests exercise.

    :param dict remaining: group key -> number of queued (not yet started) batches
    :param dict in_flight: group key -> number of batches being processed right now
    :param home: the group key this worker is dedicated to
    :param int cap: maximum concurrent workers per group
    """

    def eligible(key):
        return remaining.get(key, 0) > 0 and in_flight.get(key, 0) < cap

    if eligible(home):
        return home
    candidates = [key for key in remaining if eligible(key)]
    if not candidates:
        return None
    return max(candidates, key=lambda key: (remaining[key], -in_flight.get(key, 0)))


class _ThreadingCallState:
    """Per-call state for ONE invocation of a `@Threading`-decorated method.

    `Threading.__call__` builds a SINGLE `Threading()` instance per decorated
    method, at class-definition time - every call to the decorated method
    (concurrent or sequential) shares that same instance forever. `dbname`,
    `total_records`, `ignore_error` and `processed` therefore cannot live on
    `self`: two calls would overwrite each other's values. This object is
    built fresh inside `threaded()` on every call and threaded down as a
    parameter into `_execute` / `_run_unthreaded` / `_execute_rebalanced`;
    it never touches `self`, so nothing here leaks between calls.
    """

    def __init__(self, dbname, total_records, ignore_error):
        self.dbname = dbname
        self.total_records = total_records
        self.ignore_error = ignore_error
        self.processed = 0
        self._lock = threading.Lock()

    def add_processed(self, count):
        """Thread-safely accumulate `processed` for this call only.

        `_execute` runs one call per threading group concurrently, each in
        its own `PropagatingThread`, all sharing this ONE
        `_ThreadingCallState`. The lock is scoped to a single call to the
        decorated method, never shared with another call.
        """
        with self._lock:
            self.processed += count


class Threading:
    """ Decorate a record-style method to run the method into multiple threading

        class SomeModel(models.Model):

            @Threading()
            def method_1(self, *args, **kwargs):
                ...

            @Threading(db_connection_percentage=50)
            def method_2(self, *args, **kwargs):
                ...

            @Threading(db_connection_percentage=50, threaded_by='field')
            def method_3(self, *args, **kwargs):
                ...

            @Threading(db_connection_percentage=50, threaded_by='field.subfield')
            def method_3(self, *args, **kwargs):
                ...

            @Threading(db_connection_percentage=50, threaded_by='field', auto_commit=True)
            def method_4(self, *args, **kwargs):
                ...

            @Threading(db_connection_percentage=50, threaded_by='field', max_batch_size=1000)
            def method_5(self, *args, **kwargs):
                ...

            @Threading(db_connection_percentage=50, threaded_by='field', max_threads_per_threaded=8, auto_commit=True, max_batch_size=1)
            def method_6(self, *args, **kwargs):
                ...

    Known issues:
        - Multi-thread will be disabled in test mode. In other words, multi-threading will not be able to test

    @TODO: implement thread pool to optimize resource. For example,
        1. we have 2000 records to process
        2. max threads is 10, it means each thread is assigned to process 200 records

        The problem here is some tasks could get done sooner and we will have new available threads while
        other records still in queue in other remaining threads.
        If we have thread pool, we can reassign the remaining records for new available threads
    """

    def __init__(
            self,
            db_connection_percentage=None,
            threaded_by=None,
            max_threads_per_threaded=1,
            auto_commit=False,
            max_batch_size=None,
            maintain_order=False,
            timeout=None,
            flush=True,
            ignore_error=False,
            rebalance=False
            ):
        """
        :param db_connection_percentage float: (optional) percentage of max db connection which is used to calculate number of threads.
            Default value is 10, that means 10% of the max db connections (but not exceed cpu count + 4) will be used for threading.
            For example, by default, Odoo starts with 64 connections as the max db connection. By passing 10 as
            db_connection_percentage, max. number of threads to be created will be 7, (taken from `math.ceil(64*10/100)`). But if the
            server has only 1 cpu, the number of threads will become 5 instead of 7.
        :param threaded_by str: Many2one relational field whose recordset will get threaded instead of the orginal records.
            Sub-field syntax (e.g. 'order_id.partner_id.country_id') is also supported
        :param max_threads_per_threaded int: when theading by the field specified in threaded_by,
            we can also set maximum threads for each threaded_by group. By default, max_threads_per_threaded is 1, it means that
            all the records of a single group run on a single thread only.
        :param auto_commit bool: if given with True value, cr.commit() will be called right after the the `method` is executed
        :param max_batch_size int: max number of records to process at once in the same thread. Default value is PREFETCH_MAX
        :param maintain_order bool: try to run the method in the same order as the given recordset by putting the high ordered records
            at the beginning of each sub process.
        :param timeout int
        :param flush bool: if enabled (by default), the sub-collection will be invalidate_recordset with flush
        :param ignore_error bool: if enabled, the error will be ignored and the process will continue
        :param rebalance bool: only meaningful together with `threaded_by`. Instead of statically
            splitting the records over threads up front, one worker thread is started per
            `threaded_by` group (capped by the thread budget) and the threads pull work
            dynamically: each serves its home group first, and once that group drains it helps
            the group with the most work left, so the last busy group ends up served by every
            thread - never more than `max_threads_per_threaded` workers on one group. Unlike the
            static mode, a failing record does not abort its thread's remaining queue: the first
            error is re-raised only after everything drained (unless ignore_error).
        """
        # setup properties from the given arguments
        if db_connection_percentage and (db_connection_percentage < 0 or db_connection_percentage > 80):
            raise ValidationError(_("db_connection_percentage should be greater than or equal to 0 and less than or equal to 80"))
        self.db_connection_percentage = db_connection_percentage
        self.threaded_by = threaded_by
        self.max_threads_per_threaded = max_threads_per_threaded or 1
        if not isinstance(self.max_threads_per_threaded, int):
            raise ValidationError(_("max_threads_per_threaded must be an integer."))
        self.auto_commit = auto_commit
        self.max_batch_size = max_batch_size or PREFETCH_MAX
        self.maintain_order = maintain_order
        self.timeout = timeout
        # other properties
        self.ignore_error = ignore_error
        self.flush = flush
        self.rebalance = rebalance

    def __call__(self, method):

        def _invalidate_records_cache(records, res_model_name, res_record_ids):
            records = records.env[res_model_name].browse(res_record_ids)
            records.invalidate_recordset()
            return records

        def validate_field(obj, field):
            """Validate if the given `field` which could be sub-field syntax
            (e.g. `order_id.partner_id.country_id`) is valid for the given model `obj`
            """
            field_split = field.split('.', 1)
            field_split_count = len(field_split)
            if field_split_count == 2:
                field, sub_field = field_split
            elif field_split_count == 1:
                field = field_split[0]
                sub_field = False
            else:
                return False
            if obj._fields.get(field):
                if obj._fields.get(field).type != 'many2one':
                    raise ValidationError(
                        _(
                            "Progamming Error! The field `%(field)s` is not a Many2one field of the model %(obj_name)s's",
                            field=field,
                            obj_name=obj._name
                        )
                    )
                if sub_field:
                    return validate_field(obj[field], sub_field)
                else:
                    return True
            else:
                raise ValidationError(
                    _(
                        "Progamming Error! The field `%(field)s` is not a field of the model %(obj_name)s's",
                        field=field,
                        obj_name=obj._name
                    )
                )

        def _get_max_available_db_conn():
            while True:
                try:
                    non_idle_connections = sum(1 for cnx in sql_db._Pool._connections if cnx._pool_in_use)
                    return config['db_maxconn'] - non_idle_connections
                except Exception:
                    time.sleep(1)

        def _set_max_threads():
            db_maxconn = _get_max_available_db_conn()
            # We use max_cpu_workers for default max_threads
            max_cpu_workers = (os.cpu_count() or 1) + 4 - threading.active_count()
            # ensure max_cpu_workers is greater than or equal to 1
            max_cpu_workers = max(max_cpu_workers, 1)
            if not self.db_connection_percentage:
                # limit max_threads to 10% max db connections but not more than max_cpu_workers
                max_threads = min(max_cpu_workers, math.ceil(db_maxconn * 0.1))
            else:
                if self.db_connection_percentage < 0 or self.db_connection_percentage > 80:
                    raise ValidationError(_("db_connection_percentage should be greater than or equal to 0 and less than or equal to 80"))
                # limit max_threads to db_connection_percentage% max db connections but not more than max_cpu_workers
                max_threads = min(max_cpu_workers, math.ceil(db_maxconn * self.db_connection_percentage / 100))
            # limit max_threads to 32 to avoid consuming surprisingly large resource
            return min(32, max_threads) or 1

        def _prepare_theading_groups(recordset, max_threads):
            """Group records in the given recordset so that each group will run on a single thread
            while `max_threads` (computed once per call by `_set_max_threads()` and passed in
            here - never stored on `self`) and self.threaded_by and self.max_threads_per_threaded
            are respected.
            """
            threading_groups = []
            splittor = recordset.env['to.base'].splittor
            if self.threaded_by and validate_field(recordset, self.threaded_by):
                split_groups = recordset.mapped(self.threaded_by)
                split_groups_count = len(split_groups) or 1
                # Make sure total threads by all group will not exceed max_threads
                max_threads_per_threaded = min(self.max_threads_per_threaded, (max_threads // split_groups_count) or 1)
                batch_count = math.ceil(split_groups_count / max_threads)
                for group in splittor(split_groups, batch_count):
                    grouped_records = recordset.filtered(lambda r: r.mapped(self.threaded_by).id in group.ids)
                    grouped_batch_count = math.ceil(len(grouped_records) / max_threads_per_threaded)
                    for smaller_grouped_records in splittor(grouped_records, grouped_batch_count, maintain_order=self.maintain_order):
                        threading_groups.append(smaller_grouped_records)
            else:
                batch_count = math.ceil(len(recordset) / max_threads)
                for smaller_recordset in splittor(recordset, batch_count):
                    threading_groups.append(smaller_recordset)
            return threading_groups

        def _execute(recordset, state, *args, **kwargs):
            rec_model = False
            rec_ids = []
            with recordset.pool.cursor() as cr:
                env = recordset.env(cr=cr)
                recordset = recordset.with_env(env)
                for records in env['to.base'].splittor(recordset, self.max_batch_size, flush=self.flush):
                    try:
                        with env.cr.savepoint(flush=self.flush):
                            res = method(records, *args, **kwargs)
                            if isinstance(res, models.Model):
                                if not rec_model:
                                    rec_model = res._name
                                rec_ids += res.ids
                        if self.auto_commit:
                            env.cr.commit()
                        state.add_processed(len(records))
                        _logger.debug("Finished multi-threading executing `%s` for %s/%s records", method, state.processed, state.total_records)
                    except Exception as e:
                        if self.auto_commit:
                            env.cr.rollback()
                        _logger.error("Error occurred when multi-threading executing %s. Here is debugging info: %s", method, str(e))
                        if not state.ignore_error:
                            raise e
            if not rec_model:
                rec_model = recordset._name
            return recordset.env[rec_model].browse(rec_ids)

        def _run_unthreaded(recordset, state, *args, **kwargs):
            """Run the method here, in the caller's cursor, still batch by batch.

            `max_batch_size` is not a property of threading: a method decorated
            with `max_batch_size=1` is written for a single record and says so
            with ensure_one(). Handing it the whole recordset merely because
            there happened to be one thread - which is what a loaded server
            gives us, the thread budget shrinking with the free db connections -
            turns a quiet cron into "Expected singleton".
            """
            if len(recordset) <= self.max_batch_size:
                # nothing to split: the method sees exactly what it always saw
                try:
                    with recordset.env.cr.savepoint(flush=self.flush):
                        return method(recordset, *args, **kwargs)
                except Exception as e:
                    _logger.error("Error occurred when multi-threading executing %s. Here is debugging info: %s", method, str(e))
                    if not state.ignore_error:
                        raise e
                    # the whole (single) call failed and was swallowed: nothing
                    # succeeded, so nothing must be reported back as if it had
                    return recordset.browse()
            rec_model, rec_ids = False, []
            # records of batches that did NOT raise, used as the fallback return
            # value when no batch returned anything trackable (`rec_model` stays
            # False) - keeps a swallowed batch's records out of the result even
            # though there is nothing trackable to exclude them from
            succeeded = recordset.browse()
            for records in recordset.env['to.base'].splittor(recordset, self.max_batch_size, flush=self.flush):
                try:
                    with recordset.env.cr.savepoint(flush=self.flush):
                        res = method(records, *args, **kwargs)
                        if isinstance(res, models.Model):
                            rec_model = rec_model or res._name
                            rec_ids += res.ids
                    succeeded |= records
                    state.add_processed(len(records))
                except Exception as e:
                    _logger.error("Error occurred when multi-threading executing %s. Here is debugging info: %s", method, str(e))
                    if not state.ignore_error:
                        raise e
            if rec_model:
                return recordset.env[rec_model].browse(rec_ids)
            return succeeded

        def _execute_rebalanced(recordset, state, max_threads, *args, **kwargs):
            """One worker per `threaded_by` group (within the thread budget), pulling
            work dynamically so drained groups lend their thread to the busiest one.
            """
            import collections

            # per-group FIFO queues of id-batches, record order preserved
            group_records = recordset.mapped(self.threaded_by)
            queues, homes = {}, []
            for group in group_records:
                grouped = recordset.filtered(lambda r: r.mapped(self.threaded_by).id == group.id)
                ids = grouped.ids
                queues[group.id] = collections.deque(
                    ids[i:i + self.max_batch_size] for i in range(0, len(ids), self.max_batch_size))
            orphans = recordset.filtered(lambda r: not r.mapped(self.threaded_by))
            if orphans:
                queues[False] = collections.deque(
                    orphans.ids[i:i + self.max_batch_size] for i in range(0, len(orphans.ids), self.max_batch_size))
            # the biggest queues get the dedicated home threads first
            homes = sorted(queues, key=lambda key: -len(queues[key]))
            cap = max(1, self.max_threads_per_threaded)
            total_batches = sum(len(queue) for queue in queues.values())
            # one thread per group, but never fewer than the per-group cap (so a
            # batch that lives on a single server still gets `cap` concurrent
            # workers - the same thing the drain phase converges to), never more
            # than there is work or budget for
            n_threads = max(1, min(max_threads, max(len(homes), cap), total_batches))
            cap = min(cap, n_threads)

            lock = threading.Lock()
            in_flight = dict.fromkeys(queues, 0)
            errors = []
            res_ids = []
            res_model = [False]
            # ids of batches that did NOT raise, used as the fallback return
            # value when no batch returned anything trackable (`res_model[0]`
            # stays False) - keeps a swallowed (ignore_error) batch's ids out
            # of the result even though there is nothing trackable to exclude
            # them from
            succeeded_ids = []

            def _worker(home_key):
                with recordset.pool.cursor() as cr:
                    env = recordset.env(cr=cr)
                    while True:
                        with lock:
                            remaining = {key: len(queue) for key, queue in queues.items()}
                            picked = pick_rebalanced_group(remaining, in_flight, home_key, cap)
                            if picked is None:
                                if not any(remaining.values()) and not any(in_flight.values()):
                                    return  # everything drained
                                busy_wait = True
                            else:
                                batch_ids = queues[picked].popleft()
                                in_flight[picked] += 1
                                busy_wait = False
                        if busy_wait:
                            # work remains but every group is at its cap (or only
                            # in-flight work is left): wait for a slot
                            time.sleep(0.5)
                            continue
                        try:
                            records = env[recordset._name].browse(batch_ids)
                            records.invalidate_recordset()
                            try:
                                with env.cr.savepoint(flush=self.flush):
                                    res = method(records, *args, **kwargs)
                                if self.auto_commit:
                                    env.cr.commit()
                                with lock:
                                    state.add_processed(len(batch_ids))
                                    succeeded_ids.extend(batch_ids)
                                    if isinstance(res, models.Model):
                                        res_model[0] = res_model[0] or res._name
                                        res_ids.extend(res.ids)
                            except Exception as e:
                                if self.auto_commit:
                                    env.cr.rollback()
                                _logger.error(
                                    "Error occurred when multi-threading executing %s. Here is debugging info: %s",
                                    method, str(e))
                                if not state.ignore_error:
                                    with lock:
                                        errors.append(e)
                        finally:
                            with lock:
                                in_flight[picked] -= 1

            _logger.debug(
                "Starting %s rebalancing threads over %s groups to process %s %s records",
                n_threads, len(queues), state.total_records, recordset._name)
            jobs = []
            home_cycle = itertools.cycle(homes)
            for _index in range(n_threads):
                home_key = next(home_cycle)
                t = PropagatingThread(
                    name="Rebalanced processing %s (home group %s)" % (recordset._name, home_key),
                    target=_worker, args=(home_key,), daemon=True)
                t.dbname = state.dbname
                t.start()
                jobs.append(t)
            for t in jobs:
                t.join(self.timeout)
            if errors:
                # the pool drained everything it could first; now surface the failure
                raise errors[0]
            if res_model[0]:
                return _invalidate_records_cache(recordset, res_model[0], res_ids)
            return _invalidate_records_cache(recordset, recordset._name, succeeded_ids)

        def threaded(recordset, *args, **kwargs):
            """
            :param records: the recordset to be executed by the wrapped instance method in multi-threads mode
            :param args: arbitrary arguments given by the wrapped instance method. For example,

                @Threading()
                def instance_method(self, *args):
                    ...

            :param kwargs: keywords arguments given by the wrapped instance method. For example,

                @Threading()
                def instance_method(self, *kwargs):
                    ...
            """
            dbname = recordset.env.cr.dbname
            total_records = len(recordset)
            # The call-time context overrides the constructor kwarg ONLY when the
            # context actually carries the `ignore_error` key. An absent key must
            # leave the constructor-set value (self.ignore_error, set in __init__)
            # standing - never force it to False. This is a LOCAL resolved fresh
            # for THIS call only: self.ignore_error (the constructor default) is
            # never written back to, so it stays the shared default every future
            # call reads, instead of getting overwritten by whatever this call
            # resolved.
            ignore_error = recordset.env.context.get('ignore_error', self.ignore_error)
            state = _ThreadingCallState(dbname=dbname, total_records=total_records, ignore_error=ignore_error)

            # distributes records in the given recordset into threading_groups for multi-thread operation
            max_threads = _set_max_threads()
            threading_groups = _prepare_theading_groups(recordset, max_threads)
            threading_groups_count = len(threading_groups)
            # disable multi-threading if there is only one threading group or in test_mode
            # In the test environment, running multi-threading will lead to many unpredictable
            # cursor and savepoint related errors
            test_mode = getattr(threading.current_thread(), 'testing', False) or modules.module.current_test
            if threading_groups_count <= 1 or test_mode:
                if threading_groups_count <= 1:
                    _logger.debug("There is only %s threading group. Multi-threading is disabled.", threading_groups_count)
                if test_mode:
                    _logger.debug("Multi-threading is not supported in test mode and is disabled.")
                return _run_unthreaded(recordset, state, *args, **kwargs)
            if self.rebalance and self.threaded_by:
                return _execute_rebalanced(recordset, state, max_threads, *args, **kwargs)
            _logger.debug(
                "Starting the %s threads to process %s %s records",
                threading_groups_count,
                total_records,
                recordset._name
                )
            jobs = []
            for records in threading_groups:
                thread_name = "Threaded processing %s %s records" % (len(records), records._name)
                t = PropagatingThread(
                    name=thread_name,
                    target=_execute,
                    args=(records, state, *args),
                    kwargs=kwargs,
                    daemon=True
                    )
                t.dbname = state.dbname
                try:
                    t.start()
                    _logger.debug(
                        "The thread `%s` has been started to process %s %s records",
                        thread_name,
                        len(records),
                        records._name
                        )
                    jobs.append(t)
                except Exception as e:
                    _logger.error(
                        "Error occurred when starting the thread `%s` to process %s %s records. Here is debugging info: %s",
                        thread_name,
                        len(records),
                        records._name,
                        str(e)
                        )
            # wait for all threads to finish
            res_record_ids = []
            res_model_name = False
            for j in jobs:
                res = j.join(self.timeout)
                if isinstance(res, models.Model):
                    res_record_ids += res.ids
                    if not res_model_name:
                        res_model_name = res._name
            if res_model_name:
                return _invalidate_records_cache(recordset, res_model_name, res_record_ids)
            else:
                return _invalidate_records_cache(recordset, recordset._name, recordset.ids)

        return threaded
