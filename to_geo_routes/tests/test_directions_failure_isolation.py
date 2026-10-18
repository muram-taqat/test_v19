import logging

from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

ROUTE_SECTION_MIXIN_LOGGER = 'odoo.addons.to_geo_routes.models.route_section_mixin'

# The persistence claim the first implementation of the aggregate report shipped, and
# which T6 now forbids. Pinned as data so the assertion reads as "these specific claims
# were withdrawn" rather than as a snapshot of the current wording: a rewording of the
# report must not fail T6, only a re-introduced promise must.
RETRACTED_PERSISTENCE_CLAIMS = ('still processed', 'were processed', 'were saved')


@tagged('post_install', '-at_install')
class TestDirectionsFailureIsolation(TransactionCase):
    """Protects the per-record failure isolation contract of
    ``route.section.mixin._get_directions`` (Viindoo/tvtmaaddons#14409 close-out).

    ## The rule under test, and why ARITY is the whole design

    ``_get_directions`` serves two callers with opposite needs, and the ONLY thing
    that separates them is how many records the invocation carries:

    - **One record** - the form-header ``Get Directions`` button
      (``route_route_views.xml:55``, ``route_section_views.xml:85``,
      ``route_section_transport_mode_views.xml:11``). The operator pressed a button
      about ONE route and needs to be told WHICH route cannot be measured, so they
      can go and fill in the address. Whatever ``_fetch_directions_data`` raises must
      propagate unchanged, of whatever type, with the same message. Anything else -
      an aggregated wrapper, a re-typed exception, a swallowed failure - takes away
      the only thing that button is for.

    - **Many records** - the batch caller
      (``viin_tracking_location_route/models/tracking_location_route_mixin.py``:
      ``routes.with_context(raise_if_failure=True)._get_directions(...)`` over an
      arbitrary-arity recordset). A production database may hold hundreds of
      thousands of coordinate-bearing records; one bad coordinate must not stop the
      rest from being attempted, and the operator needs EVERY failure named in one
      pass instead of one per press.

    Arity is intrinsic to the invocation and needs no caller cooperation, which is
    why the split is ``len(self)`` measured inside ``_get_directions`` and NOT a
    context flag (the one multi-record caller lives in a module this change does not
    touch - an opt-in flag would ship the isolation dead on arrival) and NOT
    ``ensure_one()`` (``_fetch_directions_data`` already calls that on ITSELF once
    per iteration, so it is true on every iteration of a 100,000-record loop and says
    nothing about the outer arity).

    ## THE TRAP THIS FILE EXISTS TO WARN THE NEXT EDITOR ABOUT

    **Never wrap the multi-record call in ``self.assertRaises`` when the test then
    inspects what the SURVIVING records wrote.** Odoo overrides ``assertRaises``
    (``odoo/tests/common.py``, ``_assertRaises``): it opens
    ``self.env.cr.savepoint()`` around the block and deliberately ROLLS IT BACK the
    moment the expected exception is raised - its own comment says so ("*moves* the
    cleanups ... this ensures the savepoint gets rolled back when ``yield`` raises
    ``exception``"). Every write the isolated batch made would therefore be undone
    before the assertion could look at it, and the test would go red against a
    completely correct implementation. MEASURED while writing this file: the good
    route's polyline is present in the database at the end of the loop and at the
    point of the raise, and gone the instant ``assertRaises`` exits.

    The batch tests below therefore capture the report with an explicit
    ``try``/``except`` (see :meth:`_run_batch_and_capture_report`). The two
    single-record guards may keep ``assertRaises`` - they assert only on the
    exception, never on surviving state.

    ## The other trap: per-PROVIDER tolerance already exists

    ``_fetch_directions_data`` ALREADY has failure tolerance - at the wrong
    granularity for this rule. It loops ``while remaining_providers:`` with a
    ``try/except Exception`` **per PROVIDER**, and raises ``UserError`` only after
    EVERY provider has failed for that ONE record, and only when
    ``raise_if_failure=True``. So per-PROVIDER tolerance exists; per-RECORD tolerance
    is what these tests are about. A "fix" pushed down into
    ``_fetch_directions_data`` would change the single-record contract too, and the
    two single-record guards below exist to catch exactly that.

    ## Why the failures are driven through a coordinate-less endpoint

    ``geo_direction_manual._get_directions`` opens with
    ``all_points._validate_coordinates()``, which raises ``ValidationError`` for a
    partner carrying no usable coordinates. That is the real, documented, operator-
    reachable failure this whole work-stream is about - not a synthetic exception -
    so every isolation assertion here is driven by real production code paths, never
    by patching the method under test.

    ## Why ``polyline`` is the "was this record processed" marker

    ``polyline`` is a plain ``fields.Text(readonly=True)`` on ``route.section.mixin``
    with NO compute and NO default: the ONLY writer anywhere in this module is
    ``_process_direction_data_for_update``, reached exclusively through a SUCCESSFUL
    provider fetch. A non-empty ``polyline`` therefore cannot be produced by record
    creation, by a compute, or by a failed fetch - it means, unambiguously, that this
    record was carried all the way through the provider round-trip. Distance and
    duration would NOT do: both are stored computes that already hold a haversine
    value the moment the route is created, so asserting on them would be green before
    and after any fix here and would prove nothing.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _make_partner(self, name, latitude=None, longitude=None):
        """Create a partner carrying ONLY the coordinate keys explicitly asked for.

        No address field is ever passed - `to_geo_routes` depends on
        `base_geolocalize`, whose `write()` resets both coordinates to (0,0)
        whenever an address field is written without both coordinates in the same
        vals, which would silently overwrite the coordinates under test. Omitting
        both coordinates stores SQL NULL, which reads back as `0.0` - the same value
        core itself writes to mean "not geolocated", and the value
        `res.partner._has_geo_coordinates` correctly reports as ABSENT.
        """
        vals = {'name': name}
        if latitude is not None:
            vals['partner_latitude'] = latitude
        if longitude is not None:
            vals['partner_longitude'] = longitude
        return self.env['res.partner'].create(vals)

    def _make_route(self, name, addresses):
        """Create a `route.route` over `addresses`, in order.

        Waypoints are created after the route rather than through `Form`, matching
        `tests/common.py`, because these fixtures deliberately include an address the
        Form's own onchange chain would try to route to.
        """
        route = self.env['route.route'].create({
            'name': name,
            'transport_mode': 'driving',
        })
        for address in addresses:
            self.env['route.waypoint'].create({
                'address_id': address.id,
                'route_id': route.id,
            })
        return route

    def _unlocated_destination_route(self, tag):
        """A route whose DESTINATION carries no coordinates - the failure every
        assertion in this file is driven by."""
        origin = self._make_partner(f'{tag} origin', latitude=21.010458, longitude=105.787416)
        destination = self._make_partner(f'{tag} unlocated destination')
        return self._make_route(f'{tag} route', (origin, destination))

    def _good_route(self, tag):
        """A route between two real, distinct, in-range coordinates (Hai Phong /
        Quang Ninh) - a pair the manual provider can genuinely route between."""
        origin = self._make_partner(f'{tag} located origin', latitude=20.861135, longitude=106.672549)
        destination = self._make_partner(f'{tag} located destination', latitude=20.9522972, longitude=107.0781387)
        return self._make_route(f'{tag} route', (origin, destination))

    def _run_batch_and_capture_report(self, batch):
        """Drive `batch` and return the aggregated report as a string.

        Deliberately NOT `self.assertRaises` - see the class docstring: Odoo's
        override rolls its own savepoint back when the expected exception is raised,
        which would undo every surviving record's write before the caller could
        assert on it.
        """
        report = None
        try:
            batch.with_context(raise_if_failure=True)._get_directions()
        except UserError as error:
            report = str(error)
        self.assertIsNotNone(
            report,
            "the batch reported nothing at all. Isolating a failure must never mean "
            "swallowing it - an operator who is told nothing has no way to learn that "
            "part of the batch could not be routed",
        )
        return report

    @staticmethod
    def _capture(callable_under_test):
        """Run and return whatever was raised, or ``None``.

        The complement of :meth:`_run_batch_and_capture_report`, which insists on a
        ``UserError``: the transaction-safety tests below assert that a
        transaction-level failure is NOT converted into one, so they need the raw
        exception rather than a report.
        """
        try:
            callable_under_test()
        except Exception as error:  # noqa: BLE001 - the type is what callers assert on
            return error
        return None

    # ------------------------------------------------------------------
    # T1 - THE HEADLINE. A failing record must not stop the batch.
    # ------------------------------------------------------------------
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER)
    def test_a_failing_record_must_not_stop_the_rest_of_a_multi_record_batch(self):
        """A multi-record invocation whose FIRST record cannot be routed must still
        carry the remaining records all the way through the provider round-trip.

        This is the whole point of the work: a production database may hold hundreds
        of thousands of coordinate-bearing records, and one bad coordinate must not
        stop the rest from being processed.

        RED mechanism on the unmodified tree (a genuine ASSERTION failure, not an
        import or attribute error): `_get_directions` is a bare `for r in self:` with
        no `try`/`except` around the loop body, so the `UserError`
        `_fetch_directions_data` raises for the bad record propagates out of the
        first iteration and the second record is NEVER TOUCHED - its polyline stays
        empty.
        """
        bad_route = self._unlocated_destination_route('T1 bad')
        good_route = self._good_route('T1 good')

        # browse() with an explicit id tuple, never `|`: the ORDER is load-bearing
        # here (the BAD record must be attempted FIRST, or the test would pass on
        # the unmodified tree for the wrong reason) and browse is the only spelling
        # that pins it independently of the model's `_order`.
        batch = self.env['route.route'].browse((bad_route.id, good_route.id))
        self.assertEqual(len(batch), 2, "precondition failed: expected a two-record batch")

        report = self._run_batch_and_capture_report(batch)

        self.assertNotIn(
            good_route.display_name, report,
            "the good route was reported as a failure. Isolating the bad record must "
            "not make its neighbours fail for someone else's reason",
        )
        self.env.flush_all()
        good_route.invalidate_recordset(['polyline'])
        self.assertTrue(
            good_route.polyline,
            "the good route was never routed: the batch aborted on the first bad "
            "coordinate and every record after it was silently skipped. A production "
            "database holding hundreds of thousands of coordinate-bearing records "
            f"would be stopped dead by one unfilled address. Report was: {report}",
        )

    # ------------------------------------------------------------------
    # T2 - THE PROHIBITION. The single-record contract is byte-for-byte unchanged.
    # ------------------------------------------------------------------
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER)
    def test_single_record_call_raises_exactly_what_fetch_raises_non_regression_guard(self):
        """GREEN ON ARRIVAL - and it must STAY green. This is not delivery evidence.

        The single-route button's contract is that the operator is told WHICH route
        cannot be measured. This asserts the strongest available form of "unchanged":
        a one-record `_get_directions()` raises an exception of the IDENTICAL type
        carrying the BYTE-IDENTICAL message that calling `_fetch_directions_data`
        directly on that same record produces. Nothing is wrapped, nothing is
        re-typed, nothing is aggregated, nothing is swallowed.

        Comparing against `_fetch_directions_data`'s own output rather than against a
        hard-coded string is deliberate: the message text may legitimately be
        reworded or re-translated, and this guard must fail only when the single-
        record path stops being a pass-through - never merely because the wording
        moved.
        """
        bad_route = self._unlocated_destination_route('T2 bad')

        with self.assertRaises(UserError) as direct_ctx:
            bad_route._fetch_directions_data(raise_if_failure=True)

        with self.assertRaises(UserError) as loop_ctx:
            bad_route.with_context(raise_if_failure=True)._get_directions()

        self.assertIs(
            type(loop_ctx.exception), type(direct_ctx.exception),
            "a one-record _get_directions() must propagate whatever "
            "_fetch_directions_data raises, of whatever type - re-typing it takes "
            "the single-route button's contract away",
        )
        self.assertEqual(
            str(loop_ctx.exception), str(direct_ctx.exception),
            "a one-record _get_directions() must propagate _fetch_directions_data's "
            "message unchanged - the operator needs to be told WHICH specific route "
            "cannot be measured so they can go and fill in the address",
        )

    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER)
    def test_single_record_call_is_not_wrapped_in_the_batch_report_non_regression_guard(self):
        """GREEN ON ARRIVAL - and it must STAY green. Not delivery evidence.

        The complement of the test above, stated as a prohibition rather than an
        equality so that it keeps biting even if both paths were ever changed
        together: a ONE-record invocation must never produce the aggregated
        multi-record report. The marker asserted absent is the report's own
        "N of M records" arithmetic, which only the batch path can produce.
        """
        bad_route = self._unlocated_destination_route('T2b bad')

        with self.assertRaises(UserError) as loop_ctx:
            bad_route.with_context(raise_if_failure=True)._get_directions()

        message = str(loop_ctx.exception)
        self.assertIn(
            bad_route.address_to_id.display_name, message,
            "the single-record failure must name the endpoint the operator has to go "
            "and fill in",
        )
        self.assertNotIn(
            ' of 1 ', message,
            "a one-record call produced the aggregated batch report. The batch report "
            "answers a different question ('which of my many records failed') and "
            "burying the single-route answer inside it is the regression the "
            "single-record prohibition exists to prevent",
        )

    # ------------------------------------------------------------------
    # T3 - the aggregated report names EVERY failed record.
    # ------------------------------------------------------------------
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER)
    def test_the_batch_report_names_every_failed_record_not_only_the_first(self):
        """With TWO bad records in one batch, BOTH must appear in the final report.

        A report that names only the first failure - or only a count - has
        reimplemented the original defect at a different granularity: the operator
        still has to press the button, fix one address, press it again, and discover
        the next one. Naming every failure in one pass is what turns N round-trips
        into one.

        RED mechanism on the unmodified tree: the loop aborts on the FIRST bad
        record, so the second bad record's name never reaches the message at all and
        the good record is never routed either.
        """
        first_bad = self._unlocated_destination_route('T3 first bad')
        second_bad = self._unlocated_destination_route('T3 second bad')
        good_route = self._good_route('T3 good')

        batch = self.env['route.route'].browse((first_bad.id, second_bad.id, good_route.id))
        self.assertEqual(len(batch), 3, "precondition failed: expected a three-record batch")

        report = self._run_batch_and_capture_report(batch)

        for failed_route in (first_bad, second_bad):
            self.assertIn(
                failed_route.display_name, report,
                f"the batch report does not name {failed_route.display_name!r}. An "
                "operator reading a report that stops at the first failure has to "
                "fix one address and press the button again to discover the next - "
                "which is the defect this work removes, moved one level down. "
                f"Report was: {report}",
            )

        self.env.flush_all()
        good_route.invalidate_recordset(['polyline'])
        self.assertTrue(
            good_route.polyline,
            "the good record in a batch with TWO failures was still not routed",
        )

    # ------------------------------------------------------------------
    # T4 - TRANSACTION SAFETY. The way this most plausibly ships broken.
    # ------------------------------------------------------------------
    # `odoo.sql_db` is muted alongside the module logger here and ONLY here: the
    # deliberate `SELECT 1 / 0` below makes core log `bad query` at ERROR, and Runbot
    # grades a build on the highest level reached in `ir.logging`. Core mutes exactly
    # this logger for exactly this reason wherever it provokes a rejected statement
    # (`product/models/product_product.py`, `l10n_pl_edi/models/account_move.py`).
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER, 'odoo.sql_db')
    def test_a_db_level_failure_on_one_record_must_not_poison_the_survivors(self):
        """A per-record failure that reached the DATABASE leaves the cursor in an
        aborted state, and without a SAVEPOINT every SURVIVING record then fails for
        a reason that has nothing to do with its own data - which would make the
        isolation strictly WORSE than the bug it replaces.

        The failure is injected at the PROVIDER seam, never at the method under test.
        A routing provider is by construction a pluggable external integration
        (`transport.mode.provider._get_directions` dispatches to
        `geo.direction.<provider>`), and a provider that touches the database on the
        way to its answer is ordinary - `_fetch_directions_data` savepoints only its
        own `_update_from_directions_data` write (`:309`), NOT the provider call, so
        a database error raised by the provider is caught by the per-provider
        `except Exception`, logged, and then re-surfaced as a `UserError` with the
        cursor still aborted. That is the exact state this test reproduces.

        RED mechanism on the unmodified tree: the loop aborts at the bad record, so
        the good record is never attempted and its polyline stays empty. A fix that
        isolates per record but omits the savepoint stays red for a DIFFERENT reason
        - the good record is attempted and fails on the poisoned cursor - which is
        precisely the distinction this test exists to draw.
        """
        bad_route = self._unlocated_destination_route('T4 bad')
        good_route = self._good_route('T4 good')
        poisoned_partner_id = bad_route.address_from_id.id

        manual_model_cls = type(self.env['geo.direction.manual'])
        original_get_directions = manual_model_cls._get_directions

        def provider_that_touches_a_failing_statement(provider_self, origin, destination, *args, **kwargs):
            if origin.id == poisoned_partner_id:
                # A statement PostgreSQL rejects: from here on every further
                # statement on this cursor fails with InFailedSqlTransaction until
                # someone rolls back to a savepoint.
                provider_self.env.cr.execute('SELECT 1 / 0')
            return original_get_directions(provider_self, origin, destination, *args, **kwargs)

        batch = self.env['route.route'].browse((bad_route.id, good_route.id))

        with patch.object(
            manual_model_cls, '_get_directions', provider_that_touches_a_failing_statement
        ):
            report = self._run_batch_and_capture_report(batch)

            self.env.flush_all()
            good_route.invalidate_recordset(['polyline'])
            self.assertTrue(
                good_route.polyline,
                "the good route was not routed after a DB-level failure on an earlier "
                "record. Either the batch aborted at the first failure, or it "
                "continued on a cursor left aborted by that failure - in which case "
                "every surviving record fails for someone else's reason and the "
                f"isolation is worse than the bug. Report was: {report}",
            )

    # ------------------------------------------------------------------
    # T5 - C2. The collected report must never escape at WARNING or above.
    # ------------------------------------------------------------------
    def test_the_collected_failure_report_is_never_logged_above_info(self):
        """Runbot grades a build on the HIGHEST log level reached in `ir.logging`,
        not on the test tally. A batch that correctly isolates 3 bad records out of
        200,000 must therefore not emit a WARNING for doing exactly what it was asked
        to do - the operator-facing report is the RAISE; the log line is an
        operational trace and belongs at INFO.

        The only records allowed at ERROR here are `_fetch_directions_data`'s own
        pre-existing per-provider `_logger.exception` calls, which are identified
        structurally by carrying `exc_info` rather than by matching their text.

        `assertLogs` (not `mute_logger`) is used deliberately: it replaces the
        logger's handlers AND sets `propagate = False`, so the captured records are
        inspectable here and still never reach the run log.
        """
        bad_route = self._unlocated_destination_route('T5 bad')
        good_route = self._good_route('T5 good')
        batch = self.env['route.route'].browse((bad_route.id, good_route.id))

        with self.assertLogs(ROUTE_SECTION_MIXIN_LOGGER, level='DEBUG') as log_ctx:
            self._run_batch_and_capture_report(batch)

        warnings = [rec for rec in log_ctx.records if rec.levelno == logging.WARNING]
        self.assertEqual(
            [rec.getMessage() for rec in warnings], [],
            "the batch isolation emitted a WARNING. Runbot grades on the highest log "
            "level in ir.logging, so a sweep that correctly skipped a handful of bad "
            "coordinates out of hundreds of thousands would redden the build for "
            "succeeding",
        )
        unexpected_errors = [
            rec for rec in log_ctx.records
            if rec.levelno >= logging.ERROR and rec.exc_info is None
        ]
        self.assertEqual(
            [rec.getMessage() for rec in unexpected_errors], [],
            "the batch isolation emitted an ERROR of its own. The only ERROR records "
            "this path may produce are _fetch_directions_data's pre-existing "
            "per-provider _logger.exception calls, which carry exc_info",
        )
        info_reports = [
            rec for rec in log_ctx.records
            if rec.levelno == logging.INFO and bad_route.display_name in rec.getMessage()
        ]
        self.assertTrue(
            info_reports,
            "the collected failures were never traced at INFO. The raise tells the "
            "operator; the INFO line is what tells whoever reads the server log which "
            "records a long-running batch skipped",
        )
        self.assertTrue(
            good_route.exists(),
            "precondition failed: the good route fixture is what makes this a MULTI-"
            "record batch, so the aggregated report path is the one being logged",
        )

    # ------------------------------------------------------------------
    # T6 - H1. The report must not promise a persistence that cannot happen.
    # ------------------------------------------------------------------
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER)
    def test_the_batch_report_must_not_tell_the_operator_the_survivors_were_kept(self):
        """The aggregate report may promise a COMPLETE failure list. It must NEVER
        promise that the records it did not name were persisted.

        `_get_directions`' terminal statement is `raise`, and BOTH reachable callers
        are RPC buttons (`route_section_mixin.py` `action_get_directions`, and
        `viin_tracking_location_route/models/tracking_location_route_mixin.py` via
        `action_refresh_tracking_map`). Traced to ground in core:

        - `odoo/service/model.py` - the only commit-ward step, `env.cr.flush()`,
          is reached ONLY after `result = func()` RETURNS. A `UserError` skips it, and
          it is not in the `except (IntegrityError, OperationalError)` clause at `:161`.
        - `odoo/http.py` - `with contextlib.closing(self.registry.cursor(...))`
          calls `cr.close()` and never commits.

        So the surviving records' writes ARE discarded. An operator told "the other
        199,997 were still processed" will not re-run the sweep, and the work they
        were just told was done is silently gone. That is strictly WORSE than saying
        nothing, because it removes the reason to try again.

        What the isolation genuinely buys is worth saying instead, and the sibling
        `to_geo_routes_searoutes` docstring already says it: every record is
        ATTEMPTED, so the whole batch is reported in ONE pass rather than one failure
        per press. That is the claim this test pins.

        Do NOT make the old sentence true by adding a commit boundary here. Which
        records survive a partial batch is the caller's transactional decision, and
        the method docstring is right to say this method never commits.

        RED mechanism before the fix (a genuine assertion failure): the message read
        "The other %(processed_count)s were still processed.", so `'still processed'`
        was present in the rendered report.
        """
        bad_route = self._unlocated_destination_route('T6 bad')
        good_route = self._good_route('T6 good')
        batch = self.env['route.route'].browse((bad_route.id, good_route.id))

        report = self._run_batch_and_capture_report(batch)
        lowered = report.lower()

        for retracted in RETRACTED_PERSISTENCE_CLAIMS:
            self.assertNotIn(
                retracted, lowered,
                f"the batch report still claims {retracted!r}. Neither reachable "
                "caller ever commits (odoo/service/model.py flushes only "
                "after func() RETURNS, and a UserError never returns; "
                "odoo/http.py closes the cursor without committing), so the "
                "survivors' writes are discarded. Telling the operator they were "
                "kept is what stops them re-running the sweep once the addresses "
                f"are fixed. Report was: {report}",
            )
        self.assertIn(
            'nothing was saved', lowered,
            "the batch report does not tell the operator that the operation was "
            "rolled back. Naming every failure in one pass is the whole value of the "
            "isolation, and it is only actionable if they also know the sweep has to "
            f"be run again. Report was: {report}",
        )
        self.assertTrue(
            good_route.exists(),
            "precondition failed: the good route fixture is what makes this a MULTI-"
            "record batch, so the aggregated report is the message being read",
        )

    # ------------------------------------------------------------------
    # T7 - M1. The savepoint's OWN entry flush is not a per-record failure.
    # ------------------------------------------------------------------
    # `odoo.sql_db` is muted alongside the module logger: the poisoned flush below
    # makes core log `bad query` at ERROR, and Runbot grades a build on the highest
    # level reached in `ir.logging` (same reason, and same precedent, as T4).
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER, 'odoo.sql_db')
    def test_a_failing_savepoint_entry_flush_is_not_blamed_on_the_batch_records(self):
        """`self.env.cr.savepoint()` is not a cheap marker - it FLUSHES first.

        `odoo/sql_db.py`::

            class _FlushingSavepoint(Savepoint):
                def __init__(self, cr):
                    cr.flush()                 # runs BEFORE the SAVEPOINT statement
                    super().__init__(cr)       # Savepoint.__init__ issues SAVEPOINT (:101)

        Evaluated INSIDE the collecting `try`, a database error raised by that ENTRY
        flush - pending work from before this method was ever entered - is swallowed
        by `except Exception` at a moment when NO savepoint exists yet and the cursor
        is already aborted. Every record behind the first then fails with
        `InFailedSqlTransaction`, and the operator is handed a report blaming a whole
        batch of perfectly good coordinates on one unrelated pending write.

        Constructing the savepoint OUTSIDE the `try` is the fix: a failure there is
        transaction-level and has to surface as itself. `Savepoint.__enter__` merely
        returns `self` (`odoo/sql_db.py`), so entering an already-constructed
        savepoint is exactly equivalent for every other case.

        The poison is a pending write on `res.country`, a core model NOTHING in this
        module touches - so it can only be read as somebody else's work - and its
        `code` uniqueness (`odoo/addons/base/models/res_country.py`) is a
        PostgreSQL constraint, so nothing but a flush can raise it.

        RED mechanism before the fix: `_get_directions` swallowed the `UniqueViolation`
        as record one's own failure and raised the aggregated `UserError` naming BOTH
        routes - or, if any name still had to be fetched to build that report, died
        with `InFailedSqlTransaction` instead. The two assertions below fail on the
        first shape and the second respectively, so either outcome is caught.
        """
        bad_route = self._unlocated_destination_route('T7 bad')
        good_route = self._good_route('T7 good')
        batch = self.env['route.route'].browse((bad_route.id, good_route.id))

        # Opened BEFORE the poison, so its own entry flush still sees a clean
        # transaction, and closed in `finally` so tearDown is handed a recoverable
        # cursor: `_FlushingSavepoint.rollback` (`odoo/sql_db.py`) runs
        # `cr.clear()` - which also drops the poisoned pending write - and then
        # `ROLLBACK TO SAVEPOINT`, both legal on an aborted transaction.
        guard = self.env.cr.savepoint()
        try:
            # Every name the aggregate report would render, warmed BEFORE the abort.
            # Without this the unmodified tree dies while BUILDING its report rather
            # than producing it, and the mis-attribution under test would be masked.
            self.assertEqual(
                len([route.display_name for route in batch]), 2,
                "precondition failed: expected a two-record batch",
            )
            self.env.ref('base.be').code = self.env.ref('base.fr').code
            raised = self._capture(
                lambda: batch.with_context(raise_if_failure=True)._get_directions()
            )
        finally:
            guard.close()

        self.assertIsNotNone(
            raised,
            "premise broken: the poisoned pending write no longer makes the entry "
            "flush fail, so this test can no longer prove anything",
        )
        self.assertNotIsInstance(
            raised, UserError,
            "a transaction-level failure was collected and re-reported as the "
            "records' own bad data. The savepoint's entry flush runs BEFORE any "
            "SAVEPOINT exists (odoo/sql_db.py), so there is nothing to roll "
            "back to: the cursor stays aborted and every record behind the first "
            "fails with InFailedSqlTransaction. The operator is then sent to fix "
            "coordinates that were never wrong, while the write that actually broke "
            f"the transaction goes unreported. Raised: {raised!r}",
        )
        self.assertIsInstance(
            raised, IntegrityError,
            "the transaction-level failure did not surface as itself. Constructing "
            "the savepoint outside the collecting `try` must let a database error "
            "from the entry flush propagate unchanged, so the caller's retry and the "
            f"server log both see what really happened. Raised: {raised!r}",
        )
        self.assertTrue(
            good_route.exists(),
            "precondition failed: the good route fixture is what makes this a MULTI-"
            "record batch, so the isolated branch is the one being exercised",
        )

    # ------------------------------------------------------------------
    # T8 - M2. The rendered failure list is capped; the COUNT never is.
    # ------------------------------------------------------------------
    @mute_logger(ROUTE_SECTION_MIXIN_LOGGER)
    def test_the_batch_report_caps_the_rendered_failure_list_but_never_the_count(self):
        """The report embeds one `display_name` plus one FULL exception string per
        failure, TWICE - into an `ir.logging` row and into a `UserError` that is
        JSON-serialised to the browser.

        This method's own docstring scopes the batch path to "hundreds of thousands of
        coordinate-bearing records". Uncapped, a 20% failure rate over 200,000 records
        is a multi-megabyte string written to the log table AND pushed down the wire
        to a browser that has to render it - for a message no operator can read.

        The cap is on the RENDERED LIST only. The exact failure count and the exact
        batch size stay in the prose, because those are what the operator uses to
        decide whether this is three bad addresses or a systemic outage, and a capped
        count would quietly answer a different question.

        The expected length is read from `MAX_REPORTED_FAILURES` rather than hard-coded
        so that raising or lowering the budget is a one-line change that this test
        follows, not a change this test fights.

        RED mechanism before the fix (a genuine assertion failure): the report
        rendered one line per failure with no slice at all, so a 53-failure batch
        produced 53 lines.
        """
        cap = self.env['route.route'].MAX_REPORTED_FAILURES
        overflow = 3
        bad_routes = self.env['route.route'].browse(tuple(
            self._unlocated_destination_route('T8 bad %s' % index).id
            for index in range(cap + overflow)
        ))
        self.assertEqual(
            len(bad_routes), cap + overflow,
            "precondition failed: the batch must overflow the cap for it to be visible",
        )

        report = self._run_batch_and_capture_report(bad_routes)
        rendered = [line for line in report.splitlines() if line.startswith('- ')]

        self.assertEqual(
            len(rendered), cap,
            f"the report rendered {len(rendered)} failure lines for a "
            f"{len(bad_routes)}-record batch. Uncapped, the same shape over a "
            "200,000-record sweep writes a multi-megabyte string into an ir.logging "
            "row and serialises it to the browser, for a list nobody can read",
        )
        self.assertIn(
            str(len(bad_routes)), report.splitlines()[0],
            "the exact failure count did not survive the cap into the report's own "
            "prose. Capping the COUNT as well as the list would silently answer a "
            "different question - the operator could no longer tell three bad "
            "addresses from a systemic outage. (Asserted against the headline rather "
            "than the whole report so a digit appearing inside a fixture name or a "
            "provider message cannot satisfy it by accident.)",
        )
        truncation_notice = [line for line in report.splitlines() if line.startswith('...')]
        self.assertEqual(
            len(truncation_notice), 1,
            "the report does not say that it left failures out, so it reads as a "
            "COMPLETE list when it is not - a report that stops early without saying "
            f"so is the exact defect this whole file exists to prevent. Report was: "
            f"{report}",
        )
        self.assertIn(
            str(len(bad_routes) - cap), truncation_notice[0],
            "the truncation notice does not carry the exact number of failures it "
            f"omitted. Notice was: {truncation_notice[0]!r}",
        )
