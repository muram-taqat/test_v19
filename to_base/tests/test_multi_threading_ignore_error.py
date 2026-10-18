from odoo.exceptions import ValidationError
from odoo.tests import tagged, TransactionCase
from odoo.tools import mute_logger

from ..helper.multi_threading import Threading

# The message every injected failure below raises. Held in a constant rather
# than written as a literal at each `raise` site: a literal there trips
# pylint_odoo's `translation-required` (C8107), which Runbot enforces, and the
# obvious way to satisfy that checker - wrapping it in `_()` - is the worse
# outcome. `odoo.tools.translate` walks every `*.py` under a module with no
# `tests/` exclusion, so the string would be extracted into `to_base.pot` and
# handed to translators for text no user can ever see.
INJECTED_FAILURE_MESSAGE = 'boom'

# Every assertion below that expects the error to PROPAGATE still has to mute
# `odoo.addons.to_base.helper.multi_threading`. `Threading` logs at ERROR and
# only then decides whether to re-raise - the module's own long-standing shape,
# unchanged here - so the propagating path emits an ERROR line too. Runbot
# fails a build on any ERROR line in the run log, whether or not a test failed,
# so an unmuted expected-error test turns the whole build red while reporting
# every test as passing.


@tagged('post_install', '-at_install')
class TestThreadingIgnoreError(TransactionCase):
    """`ignore_error` must resolve context-over-kwarg with one precise rule:
    the call-time context overrides the constructor's `ignore_error` kwarg
    ONLY when the context actually carries the `ignore_error` key. Absence
    of the key must fall back to whatever the constructor stored, never
    force `False`. And whatever the resolved value is, it must actually be
    honoured wherever `Threading` runs the decorated method.

    Real threads and the `_execute`/`_execute_rebalanced` paths are
    unreachable from a `TransactionCase` - multi-threading is disabled
    whenever `env.registry.in_test_mode()`, which it always is here - so
    every case below is necessarily driven through `_run_unthreaded`. That
    function has two branches ("nothing to split": the whole recordset in
    one call, and the batch loop). When this file was first authored,
    neither branch read `ignore_error` at all: nothing either one ran was
    ever suppressed, regardless of how `ignore_error` resolved - that gap
    is what `test_01`-`test_07` below were originally written against.
    Both branches have since been fixed to read and honour `ignore_error`,
    each now wrapping a batch in its own savepoint. Both branches are
    exercised below.
    """

    def setUp(self):
        super(TestThreadingIgnoreError, self).setUp()
        self.records = self.env['res.partner'].create([
            {'name': 'Ignore Error %s' % i} for i in range(3)
        ])

    # -- suppression-expecting cases --
    # RED_MODE: measured. At the time these tests were originally authored,
    # `_run_unthreaded` had no try/except anywhere in its body (source
    # re-read directly, both branches), so any exception the decorated
    # method raised always escaped, contradicting "suppressed" below on
    # every one of these. Running each test node id before the fix landed
    # showed the assertion fail; running again after confirmed green. Both
    # branches now catch and swallow per `ignore_error`, per multi_threading.py.

    def test_01_kwarg_true_with_no_context_key_is_suppressed_on_the_single_call_branch(self):
        """kwarg `ignore_error=True`, context carries no `ignore_error` key at
        all: the constructor kwarg must govern since nothing overrides it, so
        the error must not escape. Exercises the early-return ("nothing to
        split") branch of `_run_unthreaded` - `len(recordset) <= max_batch_size`,
        the whole recordset handed to the method in a single call.
        """
        called = []

        @Threading(ignore_error=True)
        def method(records):
            called.append(records.ids)
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            method(self.records)  # must not raise
        self.assertTrue(called, 'the method must actually run for the suppression to mean anything')

    def test_02_kwarg_true_with_no_context_key_is_suppressed_and_the_run_continues(self):
        """Same resolution as above (kwarg True, context silent), but forced
        through the batch loop (`max_batch_size=1` over 3 records) so a
        failure on one batch must not stop the batches after it -
        `ignore_error` means the whole run keeps going, not merely that one
        exception gets swallowed.
        """
        seen = []
        failing_id = self.records[1].id

        @Threading(max_batch_size=1, ignore_error=True)
        def method(records):
            records.ensure_one()
            if records.id == failing_id:
                raise ValidationError(INJECTED_FAILURE_MESSAGE)
            seen.append(records.id)

        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            method(self.records)  # must not raise
        expected = (self.records - self.records[1]).ids
        self.assertEqual(seen, expected, 'batches other than the failing one must still run, in order')

    # -- propagation-expecting cases --
    # RED_MODE: constructed. These were NOT naturally red when originally
    # authored: `_run_unthreaded` propagated unconditionally regardless of
    # how `ignore_error` resolved at the time, so the blanket absence of
    # suppression back then happened to already match "propagates" here too
    # - measuring would have been a vacuous, uninformative green. Each is
    # instead built to fail against a specific, plausible WRONG fix (see its
    # own docstring for which one), which is what "constructed" asks for: a
    # scenario a wrong implementation of the rule cannot survive.

    def test_03_context_false_overrides_kwarg_true(self):
        """Context explicitly says `False`: it must win over the constructor's
        `True`, per the stated rule (context overrides the kwarg ONLY when
        the context actually carries the key - and here it does).

        Sensitive to: an implementer who OR-combines instead of letting a
        present context key override, e.g.
        `ignore_error = ignore_error_kwarg or context.get('ignore_error', False)`.
        Under that wrong rule this resolves to `True` (kwarg wins) and the
        error would be suppressed instead of propagating - this
        `assertRaises` would then fail.
        """
        @Threading(ignore_error=True)
        def method(records):
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        with mute_logger('odoo.addons.to_base.helper.multi_threading'), \
                self.assertRaises(ValidationError):
            method(self.records.with_context(ignore_error=False))

    def test_04_no_kwarg_no_context_key_propagates_the_constructor_default(self):
        """Neither the kwarg nor the context says anything about
        `ignore_error`: the constructor default (`False`) must be what is
        left standing, and the error must escape.

        Sensitive to: an implementer who defaults the "key absent" case to
        `True` instead of preserving whatever the constructor stored, e.g.
        `ignore_error = recordset._context.get('ignore_error', True)`. Under
        that wrong rule this resolves to `True` (kwarg unset -> constructor
        default `False`, but context-absent wrongly falls back to `True`)
        and the error would be suppressed instead of propagating - this
        `assertRaises` would then fail.
        """
        @Threading()
        def method(records):
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        with mute_logger('odoo.addons.to_base.helper.multi_threading'), \
                self.assertRaises(ValidationError):
            method(self.records)

    # -- regression: pin the two existing context-only callers' shape --
    # RED_MODE: measured, same reasoning as the suppression-expecting cases
    # above (`_run_unthreaded` never suppressed at the time, period). The
    # RESOLUTION rule itself (context key present -> context wins) was NOT
    # changed by this WI's fix and behaved identically before and after: the
    # kwarg-absent + context-True combination already resolved to `True`
    # even before the fix, because defect A's `context.get('ignore_error')
    # or False` only misresolved when the key was ABSENT, never when it was
    # present. What DID change end-to-end is that the resolved `True`
    # finally got HONOURED - before the fix it was silently discarded
    # because `_run_unthreaded` never read `ignore_error` at all. That
    # end-to-end change was this WI's intended fix, not a regression, so
    # both tests below were written from the intended, post-fix behaviour
    # rather than the pre-fix accidental one.
    #
    # `auto_commit` is intentionally omitted from both, even though the real
    # decorators use it - it fights `TransactionCase` savepoint isolation and
    # is orthogonal to `ignore_error`; the existing harness's own precedent
    # (`test_multi_threading_batches.py`, `test_multi_threading_rebalance.py`)
    # passes neither.

    def test_05_context_only_caller_shape_like_to_git_action_pull(self):
        """Mirrors `to_git/models/git_branch.py` `ToGitBranch._cron_scan()`:
        `branches.with_context(ignore_error=True).action_pull()` - the
        decorated method backing `action_pull` (`_threaded_pull`) never sets
        the `ignore_error` kwarg; the caller supplies it only via context.
        """
        @Threading()
        def method(records):
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            method(self.records.with_context(ignore_error=True))  # must not raise

    def test_06_context_only_caller_shape_like_to_attendance_device_fetch(self):
        """Mirrors `to_attendance_device/models/attendance_device.py`
        `AttendanceDevice._cron_download_device_attendance()`:
        `devices.with_context(ignore_error=True)._threaded_fetch_attendance_data()`
        - the decorated method never sets the `ignore_error` kwarg either,
        only `max_batch_size=1`. Exercises the batch loop, same as the real
        decorator: every device is fetched independently, one cron failure
        must not stop the rest from being attempted.
        """
        seen = []

        @Threading(max_batch_size=1)
        def method(records):
            records.ensure_one()
            seen.append(records.id)
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            method(self.records.with_context(ignore_error=True))  # must not raise
        self.assertEqual(seen, self.records.ids, 'every batch must still be attempted, cron-scan style')

    # -- follow-up defect: no savepoint around a batch in `_run_unthreaded` --
    # RED_MODE: measured. Round 1 fixed `_run_unthreaded` to catch and
    # swallow an exception when `ignore_error=True`, but at that point
    # neither of its branches yet opened a savepoint - unlike `_execute`/
    # `_execute_rebalanced`, which each open their OWN cursor and wrap every
    # batch in `with env.cr.savepoint(flush=self.flush):` (multi_threading.py
    # :271, :385; `grep -n savepoint` matched nowhere inside
    # `_run_unthreaded` at the time, lines 291-324). `_run_unthreaded` shares
    # the CALLER's cursor/transaction. A plain `raise ValidationError(...)`
    # (as in every case above) never touches SQL, so it never actually
    # exercised this gap - PostgreSQL only enters the ABORTED transaction
    # state after a genuine SQL-level error. This test forces one directly
    # against the cursor on the first batch, then asserts that later,
    # otherwise-valid batches still complete - which they could not while
    # the shared transaction stayed aborted with no savepoint to recover it.
    # A savepoint now wraps each batch (added in round 2), so this test
    # passes today.

    def test_07_unthreaded_batch_without_savepoint_lets_an_aborted_transaction_swallow_every_later_batch(self):
        """A genuine PostgreSQL-level error (not just a Python exception) on
        the first batch of the `_run_unthreaded` batch loop puts the shared
        transaction into PostgreSQL's ABORTED state. With no savepoint of its
        own around each batch, `_run_unthreaded` cannot recover from that for
        the batches after it: their own, otherwise-valid `write()` calls fail
        too (the aborted transaction poisons every later statement on that
        cursor), and `ignore_error=True` silently swallows those failures as
        well. The whole call must not raise, but every batch after the first
        must still actually complete its own write - it did not before a
        savepoint was added around each batch (round 2); it does today.
        """
        completed = []
        first_id = self.records[0].id

        @Threading(max_batch_size=1, ignore_error=True)
        def method(records):
            records.ensure_one()
            if records.id == first_id:
                # A genuine SQL-level error (unlike `raise ValidationError(...)`
                # elsewhere in this file) - this is what actually aborts the
                # PostgreSQL transaction.
                records.env.cr.execute("SELECT 1/0")
            else:
                records.write({'name': 'touched %s' % records.id})
                # Only recorded once the write has actually returned - lets
                # the assertion distinguish "never attempted" from "attempted
                # but failed".
                completed.append(records.id)

        with mute_logger('odoo.sql_db', 'odoo.addons.to_base.helper.multi_threading'):
            method(self.records)  # ignore_error=True: must not raise

        later_ids = (self.records - self.records[0]).ids
        self.assertEqual(
            completed, later_ids,
            'every batch after the first genuinely-aborting one must still '
            'be attempted AND actually succeed - `_run_unthreaded` opens no '
            'savepoint around a batch, so a PG-level abort on batch 1 '
            'poisons the shared transaction for every batch after it')

    # -- follow-up defect: a swallowed failure is reported back as a success --
    # RED_MODE: measured. Rounds 1-2 fixed suppression itself (both branches
    # now catch, swallow and savepoint-isolate) but never touched what
    # `_run_unthreaded` RETURNS on a swallow - a plain `raise
    # ValidationError(...)` is enough here, no PostgreSQL-level trick needed,
    # because the bug is purely about the return value, not transaction state.

    def test_08_swallowed_single_call_must_not_report_the_original_recordset_as_succeeded(self):
        """Exercises the early-return ("nothing to split") branch of
        `_run_unthreaded` (`len(recordset) <= max_batch_size`, the whole
        recordset handed to the method in one call, mirroring `test_01`).
        The WHOLE call raises and `ignore_error=True` swallows it - nothing
        succeeded - yet `_run_unthreaded` today does `return recordset`
        unconditionally on swallow (multi_threading.py, the single-call
        branch's `except` clause), claiming the entire input succeeded. The
        returned value must not be, or contain, the original input - and must
        be an actual empty recordset, not merely a falsy value, so a future
        `return None` regression is caught here too (see assertion message).
        """
        called = []

        @Threading(ignore_error=True)
        def method(records):
            called.append(records.ids)
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            result = method(self.records)  # must not raise
        self.assertTrue(called, 'the method must actually run for the assertion below to mean anything')
        self.assertEqual(
            result, self.records.browse(),
            'the whole call failed and was swallowed - nothing succeeded, so '
            'the returned value must be an EMPTY recordset of the same model '
            '(res.partner), not the original %s. Pinning the type as well as '
            'the emptiness matters: `assertFalse` alone also passes for `None`, '
            'but a real caller does `checked_out_branches - pulled_branches` '
            '(to_git/models/git_branch.py:665) - `BaseModel.__sub__` reaches '
            'into `other._ids`, which `None` lacks, so a `return None` '
            'regression would crash the caller with AttributeError instead of '
            'merely misreporting, and only this assertion would catch it.'
            % (self.records,))

    def test_09_swallowed_batch_must_not_leak_into_a_result_with_nothing_trackable(self):
        """Exercises the batch-loop branch of `_run_unthreaded`
        (`max_batch_size=1` over 3 records, one batch raising and swallowed).

        `rec_model`/`rec_ids` only ever accumulate from batches that did NOT
        raise, so a batch that returns a trackable `models.Model` instance
        (like `test_02`/`test_06` above) already excludes a failed sibling
        correctly - that shape is not actually broken. The real gap is when
        `rec_model` never gets set AT ALL: `test_multi_threading_batches.py`
        `test_03_a_method_returning_nothing_gets_its_recordset_back` pins
        that when EVERY batch succeeds but returns nothing trackable (e.g.
        `None`), the full original recordset must come back - correctly,
        there is nothing to exclude. But when the SAME "nothing trackable"
        shape is combined with a genuinely failed-and-swallowed batch,
        `_run_unthreaded` cannot tell the two situations apart today: it
        falls through to the very same `return recordset`, so the failed
        batch's id comes back too, indistinguishable from a success.

        The assertion below pins BOTH halves of the contract in one shot -
        the failed id excluded AND every surviving id included - not just
        exclusion. Exclusion alone is satisfied just as well by an empty
        result, so a broken fix that makes `succeeded` always empty (e.g.
        `succeeded = recordset.browse()` moved INSIDE the loop so it resets
        every iteration, or the `succeeded |= records` line dropped
        entirely) would leave `failing_id not in []` vacuously True while
        silently dropping every survivor too - exactly the shape
        `to_git`'s cron consumes as `failed_branches = checked_out_branches
        - pulled_branches`: if `pulled_branches` is wrongly always empty,
        every branch looks failed. `set(result.ids) == set(self.records.ids)
        - {failing_id}` cannot be satisfied by an empty result, so it
        catches that regression where the old `assertNotIn` alone would not.
        """
        failing_id = self.records[1].id

        @Threading(max_batch_size=1, ignore_error=True)
        def method(records):
            records.ensure_one()
            if records.id == failing_id:
                raise ValidationError(INJECTED_FAILURE_MESSAGE)
            return None

        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            result = method(self.records)  # must not raise
        self.assertEqual(
            set(result.ids), set(self.records.ids) - {failing_id},
            'the failed-and-swallowed batch must never be reported back as '
            'if it had succeeded, even when nothing else in the run is '
            'trackable either - AND every non-failing batch must still come '
            'back: a fix that only manages to always exclude everything '
            '(e.g. an always-empty `succeeded`) would satisfy mere exclusion '
            'of %s but must not pass here' % (failing_id,))

    # -- cross-call independence: two calls sharing the SAME decorated closure --
    # `test_01`-`test_09` above each decorate `method` FRESH inside their own
    # test, so every one of them gets its own, never-reused `Threading`
    # instance - none can show whether a SECOND call on the SAME decorated
    # closure resolves `ignore_error` independently of what a prior call on
    # that closure resolved. That is what the test below covers.

    def test_10_two_calls_on_the_same_decorated_closure_resolve_ignore_error_independently(self):
        """`Threading.__call__` builds ONE `Threading` instance per decorated
        method, at class-definition time - every call to that SAME decorated
        method (`method` below, decorated exactly once) shares it forever.
        Call 1 supplies `ignore_error=True` via context and suppresses its
        error, as intended. Call 2, on that very same closure, supplies
        neither a kwarg nor a context key at all - the constructor default
        (`False`, since no kwarg was ever given) must govern, and the error
        must propagate. `multi_threading.py` resolves `ignore_error` into a
        purely local, per-call value handed to `_ThreadingCallState` (never
        written back onto `self`), so call 2 must not inherit call 1's
        resolved `True`.
        """
        called = []

        @Threading()
        def method(records):
            called.append(records.ids)
            raise ValidationError(INJECTED_FAILURE_MESSAGE)

        # call 1: context explicitly asks to suppress - must not raise.
        with self.assertLogs('odoo.addons.to_base.helper.multi_threading', level='ERROR'):
            method(self.records[:1].with_context(ignore_error=True))

        # call 2: SAME decorated closure, no kwarg, no context key - the
        # constructor default (False) must govern and the error must escape.
        with mute_logger('odoo.addons.to_base.helper.multi_threading'), \
                self.assertRaises(ValidationError):
            method(self.records[1:2])

        self.assertEqual(
            len(called), 2,
            'both calls must have actually invoked the method for the '
            'suppression/propagation contrast above to mean anything')
