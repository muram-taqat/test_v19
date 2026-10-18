from collections import defaultdict
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from odoo.tests import tagged, Form, TransactionCase
from odoo.tools import mute_logger

# The two module loggers whose WARNINGs this file DELIBERATELY provokes.
#
# Viindoo Runbot grades a build by the HIGHEST log level present in it, not by the
# test tally, so a deliberate provocation left to propagate reddens every build
# that runs this file even though every test passes and nothing is wrong. Neither
# logger is silenced at its source: `distance.mixin`'s R3 guard and
# `route.section.transport.mode`'s self-heal diagnostics still fire, unchanged, for
# any real caller anywhere else in the codebase. What changes is that each
# provocation HERE is now bracketed at the point it is provoked - preferring
# `assertLogs`, which additionally ASSERTS the diagnostic actually fired (R3 is
# log-only per design amendment A2, so its message IS its entire observable
# contract) and, as a side effect of `propagate = False`, keeps it off the build
# log.
DISTANCE_MIXIN_LOGGER = 'odoo.addons.to_geo_routes.models.distance_mixin'
TRANSPORT_MODE_LOGGER = 'odoo.addons.to_geo_routes.models.route_section_transport_mode'


@tagged('post_install', '-at_install')
class TestRouteSectionCache(TransactionCase):
    """Protects `route.section.transport.mode` - the routing cache every
    `route.section` value is a *projection* of and every `route.segment` value is a
    stored *mirror* of (Viindoo/tvtmaaddons#14279).

    Root cause (design § 2.1): `route.segment` mirrors `route.section.transport.mode`
    and writes back into it via two unguarded inverses. Whenever the mirror cannot
    resolve a value it emits 0.0, and the write-back persists that 0.0 into the
    administrator-configured cache row. Once there it can never be selected again
    (`route_section.py` only considers a truthy `ave_speed`), so the corruption is
    self-sustaining.

    These tests assert on the CACHE ROW (`route.section.transport.mode.ave_speed`),
    never on the segment mirror alone - the mirror can legitimately read 0 for an
    instant; the cache row must not.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.to_base = cls.env['to.base']
        cls.manual_driving_provider = cls.env.ref('to_geo_routes.manual_input_driving')

        # Three brand-new addresses, disjoint from any demo/other-test fixture in the
        # module, so every route.section this file touches is created from scratch by
        # the test itself - never reusing a section some other fixture already seeded
        # (design B2: the reproduction must work "from scratch").
        cls.address_danang = cls.env['res.partner'].create({
            'name': 'Da Nang',
            'partner_latitude': 16.0544,
            'partner_longitude': 108.2022,
        })
        cls.address_hue = cls.env['res.partner'].create({
            'name': 'Hue',
            'partner_latitude': 16.4637,
            'partner_longitude': 107.5909,
        })
        cls.address_dongha = cls.env['res.partner'].create({
            'name': 'Dong Ha',
            'partner_latitude': 16.8163,
            'partner_longitude': 107.1005,
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _configured_speed(self, provider):
        """The speed the administrator configured for `provider`, round-tripped
        through the module's own km/h <-> m/s conversion - the exact value the normal
        creation path produces (60.00000000000001 for driving, never the bare literal
        60 - design § 8 float-identity risk; `route_section_transport_mode.py`
        and `geo.direction._compute_haversine_matrix` perform the same round trip)."""
        return self.to_base.ms_to_kmh(self.to_base.kmh_to_ms(provider.ave_speed))

    def _manual_driving_row(self, section):
        return section.transport_mode_ids.filtered(
            lambda mode_record: mode_record.provider == 'manual' and mode_record.mode == 'driving'
        )

    @contextmanager
    def _assert_duration_write_discarded(self, record):
        """The block MUST provoke `distance.mixin`'s R3 derived-duration guard on
        `record`, and the WARNING it emits MUST name the offending model, record id
        and discarded key.

        R3 is LOG-ONLY (design amendment A2 - `_normalize_derived_duration_vals`
        pops the key and derives nothing), so that message is the guard's ENTIRE
        observable contract: "a violating caller is diagnosable instead of silently
        corrected". Nothing asserted it before, which left the only evidence of the
        contract sitting in the build log - where Runbot reads it as a defect. This
        asserts it instead, and (because `assertLogs` sets `propagate = False` for
        the block) stops a deliberate provocation from reddening the build. It
        asserts the DIAGNOSABILITY requirement, not the phrasing: model name, record
        id, and the key - a reword stays green, a message that stops naming who
        wrote what goes red."""
        with self.assertLogs(DISTANCE_MIXIN_LOGGER, level='WARNING') as captured:
            yield captured
        emitted = '\n'.join(captured.output)
        self.assertIn(
            record._name, emitted,
            "R3's warning must name the offending MODEL so a real violating caller "
            f"is diagnosable, got: {emitted}",
        )
        self.assertIn(
            str(record.id), emitted,
            "R3's warning must name the offending RECORD so a real violating caller "
            f"is diagnosable, got: {emitted}",
        )
        self.assertIn(
            'duration', emitted,
            f"R3's warning must name the discarded key, got: {emitted}",
        )

    @contextmanager
    def _assert_degenerate_speed_reported(self, expected_count):
        """The block MUST make `_update_missing_distances_ave_speed` report exactly
        `expected_count` rows as a zero-time (degenerate) observation, and MUST NOT
        report any row as having no distance data at all.

        The two conditions have different remedies - "the provider returned nothing"
        vs "the provider returned a distance in zero time" - which is precisely why
        Tightening 2 (design amendment § A-1.3) split them into two messages: "A
        distinct message keeps an operator from chasing the wrong failure." Asserting
        the split holds is asserting that documented behaviour, and it turns the
        provocation into a checked observable instead of build-log noise."""
        with self.assertLogs(TRANSPORT_MODE_LOGGER, level='WARNING') as captured:
            yield captured
        emitted = '\n'.join(captured.output)
        self.assertIn(
            f"for {expected_count} records", emitted,
            f"expected the self-heal sweep to report {expected_count} degenerate "
            f"row(s), got: {emitted}",
        )
        self.assertNotIn(
            'No distance data found', emitted,
            "the sweep reported a row as having NO distance data when the provider "
            "did return a distance - the two conditions must stay distinguishable "
            f"(Tightening 2), got: {emitted}",
        )

    def _stub_distance_matrix(self, degenerate_rows, usable_ave_speed_ms):
        """A `_get_distance_matrix` stub that answers EVERY row the sweep hands it:
        a degenerate (positive distance, zero speed) observation for
        `degenerate_rows`, a usable one for all the rest.

        `_update_missing_distances_ave_speed` is an `@api.model` sweep - it SEARCHES
        the whole database for rows still inside the self-heal domain, so the batch
        it feeds the provider holds every unresolved row in the DB, not just the one
        under test (in a demo database that is dozens of route sections between
        `viin_logistics`/`viin_freight_management` demo locations that ship without
        coordinates). A stub that answers only the row under test therefore leaves
        all of those unanswered and makes the method warn "No distance data found for
        N records" - a report about the STUB's coverage, not about anything the code
        under test did. Answering the whole batch removes that artifact AND makes the
        oracle stronger: the degenerate row must now be isolated from USABLE siblings
        resolved in the SAME batch, which is the contract ruling 1 actually states."""
        degenerate_ids = set(degenerate_rows.ids)

        def _matrix(records, res_model=None, res_id=None):
            return {
                row: {
                    'distance': 5000.0,
                    'ave_speed': 0.0 if row.id in degenerate_ids else usable_ave_speed_ms,
                }
                for row in records
            }

        return _matrix

    def _build_two_waypoint_route(self, address_from, address_to, name):
        route = self.env['route.route']
        with Form(route) as route_form:
            route_form.name = name
            route_form.transport_mode = 'driving'
            with route_form.waypoint_ids.new() as line:
                line.address_id = address_from
            with route_form.waypoint_ids.new() as line:
                line.address_id = address_to
            route = route_form.save()
        return route

    def _build_route_through_waypoints(self, addresses, name):
        """Like `_build_two_waypoint_route` but for an arbitrary ordered chain of
        3+ addresses in ONE route - needed to share a `route.section` with another
        route WITHOUT tripping `route.route._check_waypoint_ids`
        (`route_route.py`), which forbids a second route whose waypoint
        chain is the EXACT SAME (transport_mode, ordered-address-tuple) as an
        existing one (`_group_by_transport_mode_and_addresses`,
        `route_mixin.py`, keys on the FULL tuple, not any sub-chain of
        it). A strictly longer chain that merely CONTAINS a shorter route's leg as
        its first (or any) consecutive pair has a different tuple and therefore a
        different key - not a collision - while still reusing that leg's
        `route.section` via `_search_route_sections`
        (`route_section_mixin.py`, `waypoint_mixin.py`), both of
        which match on consecutive address PAIRS, never the whole-route tuple."""
        route = self.env['route.route']
        with Form(route) as route_form:
            route_form.name = name
            route_form.transport_mode = 'driving'
            for address in addresses:
                with route_form.waypoint_ids.new() as line:
                    line.address_id = address
            route = route_form.save()
        return route

    # ------------------------------------------------------------------
    # B1 - the oracle: after install, every manual cache row retains the speed
    # configured on its provider.
    # ------------------------------------------------------------------
    def _seed_cascade_rows(self, name):
        """Drive the corrupting cascade with data this test owns, and return the route.

        Waypoints are attached one at a time on purpose: that is how the module's own
        data file seeds a route, and it is the shape that re-fires `_compute_segment_ids`
        with a partial waypoint set - the mechanism behind #14279. A single nested
        create would not reproduce it.
        """
        route = self.env['route.route'].create({'name': name, 'transport_mode': 'driving'})
        for address in (self.address_danang, self.address_hue, self.address_dongha):
            self.env['route.waypoint'].create({'address_id': address.id, 'route_id': route.id})
        # Reading segment_ids is what materialises the cascade: the segments (and the
        # sections and cache rows behind them) come from a compute, so a caller that
        # never touches it would sweep an empty table and pass for the wrong reason.
        self.assertEqual(
            len(route.segment_ids), 2,
            "seeding a 3-waypoint route must produce one segment per leg",
        )
        return route

    def test_manual_cache_rows_retain_their_configured_speed(self):
        """Every RESOLVED `route.section.transport.mode` row with provider='manual'
        must report the speed configured on its `transport.mode.provider` - not 0.
        The test seeds the corrupting cascade itself and then sweeps the whole table,
        so it catches corruption wherever it came from - the rows it just created, and
        any row another module's data brought in.

        A row is RESOLVED iff its `distance` is populated (> 0). The corruption this
        guards against is exactly the #14279 fingerprint - `distance` stays correct
        while `ave_speed` is zeroed - so it is only meaningful on resolved rows. An
        UNRESOLVED row (distance == 0/False) legitimately has `ave_speed == 0`: for
        the manual haversine engine that means its endpoint partners carry no valid
        geolocation, so no speed can be derived and 0.0 correctly encodes "unknown".
        At `post_install` scope this DB is co-installed with other modules that seed
        geoless sea-port sections (e.g. viin_logistics / viin_freight_management:
        'Jeddah Islamic Port - Rotterdam Port'); asserting a configured speed on those
        would be wrong, not a regression. Scoping to distance > 0 keeps the test
        catching every real corruption while not false-flagging correctly-unresolved
        rows."""
        self._seed_cascade_rows('B1 - swept cascade')
        manual_rows = self.env['route.section.transport.mode'].search([('provider', '=', 'manual')])
        self.assertTrue(
            manual_rows,
            "the seeded route should have produced manual cache rows to sweep",
        )
        corrupted = []
        for row in manual_rows:
            configured = self._configured_speed(row.transport_mode_provider_id)
            if row.distance and abs(row.ave_speed - configured) > 1e-9:
                corrupted.append(
                    "section=%r mode=%s expected=%s actual=%s" % (
                        row.route_section_id.display_name, row.mode, configured, row.ave_speed,
                    )
                )
        self.assertFalse(
            corrupted,
            "manual cache row(s) lost their administrator-configured speed (destroyed "
            "by the segment mirror's unguarded write-back): " + "; ".join(corrupted),
        )

    def test_two_partner_driving_section_reports_the_configured_speed(self):
        """Acceptance § 9: a plain two-partner driving section must show the speed
        configured on the manual/driving provider (60 km/h), never 0.

        The acceptance criterion originally named a seeded record. It is asserted here
        on a section this test builds instead, because a shipped record only exists on
        an install that loaded the module's optional data - the criterion is about the
        cache row's value, not about which record carries it.
        """
        route = self.env['route.route'].create({
            'name': 'B1b - two-partner driving',
            'transport_mode': 'driving',
        })
        for address in (self.address_danang, self.address_hue):
            self.env['route.waypoint'].create({'address_id': address.id, 'route_id': route.id})

        self.assertEqual(len(route.segment_ids), 1, "a 2-waypoint route has exactly one segment")
        section = route.segment_ids.route_section_id
        self.assertTrue(section, "the segment creation cascade must attach a route.section")
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(
            manual_driving,
            "expected the two-partner driving section to carry a manual/driving cache row",
        )
        configured = self._configured_speed(self.manual_driving_provider)
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg=(
                f"the driving/manual cache row of {section.display_name!r} is "
                f"{manual_driving.ave_speed}, expected the configured {configured}"
            ),
        )

    # ------------------------------------------------------------------
    # B2 - deterministic reproduction: building a route from brand-new addresses
    # must not corrupt the manual cache row(s) it creates along the way.
    # ------------------------------------------------------------------
    def test_building_route_from_new_addresses_keeps_manual_cache_row_intact(self):
        """Creating a route.route from three brand-new addresses, then attaching its
        waypoints one at a time (mirrors tests/common.py's own TestCommon.setUpClass
        fixture AND how `data/data_demo.xml` itself seeds every demo route - each
        `route.waypoint` is its own top-level `create()` call, not one nested
        `route.route.create()` with a `waypoint_ids` command list) must not corrupt the
        manual/driving cache row(s) the creation cascade brings into existence -
        independent of any demo data. This is the shape that actually reproduces
        #14279 (design § 2.2 mechanism 3 - `_compute_segment_ids` re-fires once per
        waypoint added, and an early pass with a partial waypoint set transits
        `route_section_id=False`); a single nested Form.save() does not reliably
        reproduce it, since it creates the route and all its waypoints in one shot."""
        route = self.env['route.route'].create({
            'name': 'B2 - fresh addresses',
            'transport_mode': 'driving',
        })
        self.env['route.waypoint'].create({
            'address_id': self.address_danang.id,
            'route_id': route.id,
        })
        self.env['route.waypoint'].create({
            'address_id': self.address_hue.id,
            'route_id': route.id,
        })
        self.env['route.waypoint'].create({
            'address_id': self.address_dongha.id,
            'route_id': route.id,
        })

        self.assertEqual(
            len(route.segment_ids), 2,
            "expected one segment per leg of the 3-waypoint route",
        )
        configured = self._configured_speed(self.manual_driving_provider)
        corrupted = []
        for segment in route.segment_ids:
            section = segment.route_section_id
            self.assertTrue(section, "the segment creation cascade must attach a route.section")
            manual_driving = self._manual_driving_row(section)
            self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")
            if abs(manual_driving.ave_speed - configured) > 1e-9:
                corrupted.append(
                    "section=%r expected=%s actual=%s" % (
                        section.display_name, configured, manual_driving.ave_speed,
                    )
                )
        self.assertFalse(
            corrupted,
            "route creation from brand-new addresses corrupted the manual/driving cache "
            "row(s): " + "; ".join(corrupted),
        )

    # ------------------------------------------------------------------
    # B3 - guard A, direct: a segment mirror that resolves to 0 must not be
    # written back into the shared cache row.
    # ------------------------------------------------------------------
    def test_segment_zero_speed_must_not_zero_the_manual_cache_row(self):
        """Guard A (#14279, `_inverse_distance_ave_speed`,
        `route_segment.py`): whenever `route.segment.ave_speed` resolves to a
        falsy value - e.g. from the route-mixin's re-entrant `_compute_segment_ids`
        recompute transiting `route_section_id=False` - the write-back to the shared
        `route.section.transport.mode` cache row must be skipped, never overwrite the
        administrator-configured speed with 0."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B3 - guard A')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving)

        # GIVEN a known-good speed on both sides of the mirror - independent of
        # whatever B1/B2 prove about the create-time cascade.
        segment.ave_speed = 45.0
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, 45.0, places=6,
            msg="precondition failed: could not establish a known-good cache row speed",
        )

        # WHEN the segment's mirror resolves to 0 - the exact write guard A must catch.
        segment.ave_speed = 0.0

        # THEN the shared cache row must still hold the last real speed, not 0.
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, 45.0, places=6,
            msg=(
                "segment.ave_speed = 0.0 was written back into the manual/driving cache "
                f"row (now {manual_driving.ave_speed}) - guard A must skip a falsy "
                "write-back"
            ),
        )

    # ------------------------------------------------------------------
    # B4 - the second zero-writer: a falsy duration must not zero ave_speed.
    # ------------------------------------------------------------------
    def test_segment_zero_duration_must_not_zero_ave_speed(self):
        """AMENDED RATIONALE (mixin write-back contract redesign, design
        `to-geo-routes-mixin-writeback-contract-18-2026-07-19.md` § 2.1/§ 2.3, ruling
        § 5(d) row B4 - premise changed, assertions unchanged).

        Business rule: `duration` is a pure DERIVED OUTPUT of distance and ave_speed
        (`doc/index.rst:26`: "the estimated time will be automatically computed") - it
        must never be able to write anything back into ave_speed, on the segment
        itself or, transitively (via the unprotected `Field.__set__` -> `write()`
        re-fire of `_inverse_distance_ave_speed`), on the shared cache row.

        The ORIGINAL rationale ("route.segment is the only route model that still
        inherits a LIVE `distance.mixin._inverse_duration`, because route.section and
        route.mixin neutralise it with a no-op override") is REFUTED (design C9): it
        framed this as a per-model GUARD GAP, but route.section.transport.mode never
        had a guard either (see the AMENDED rationale on the two B9 tests below), and
        a hand-maintained "which models need a guard" list is exactly the design
        smell the redesign eliminates (§ 2.3) by deleting `inverse='_inverse_duration'`
        at its SOURCE (`distance.mixin`) instead of neutralising it per model. Once
        that lands (R1, not yet in this tree), this scenario protects itself: there is
        no inverse left to fire, on ANY model, not because this or any other test
        keeps a guard alive."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B4 - guard B')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving)

        # GIVEN a known-good speed on both sides of the mirror.
        segment.ave_speed = 45.0
        segment.invalidate_recordset(['ave_speed'])
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment.ave_speed, 45.0, places=6,
            msg="precondition failed: could not establish a known-good segment speed",
        )
        self.assertAlmostEqual(
            manual_driving.ave_speed, 45.0, places=6,
            msg="precondition failed: could not establish a known-good cache row speed",
        )

        # WHEN duration resolves to 0 (e.g. a section not yet attached to the segment).
        # The assignment is a `write({'duration': 0.0})`, so R3 discards it and says
        # so - asserted here rather than left to propagate into the build log.
        with self._assert_duration_write_discarded(segment):
            segment.duration = 0.0

        # THEN neither the segment's own mirror nor the shared cache row may be zeroed.
        segment.invalidate_recordset(['ave_speed'])
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment.ave_speed, 45.0, places=6,
            msg=f"segment.duration = 0.0 zeroed segment.ave_speed (now {segment.ave_speed})",
        )
        self.assertAlmostEqual(
            manual_driving.ave_speed, 45.0, places=6,
            msg=(
                "segment.duration = 0.0 zeroed the manual/driving cache row via the "
                f"second zero-writer _inverse_duration (now {manual_driving.ave_speed})"
            ),
        )

    # ------------------------------------------------------------------
    # B5 - CONTROL: the documented manual-override UX must keep working.
    # Must be GREEN before AND after the fix - never forced red.
    # ------------------------------------------------------------------
    def test_user_edited_segment_speed_still_propagates_to_manual_cache_row(self):
        """CONTROL (design § 7 B5) - `doc/index.rst:26` promises that typing a speed
        into the Route form's Segments tab is a supported manual override ("you can add
        information about distance and average speed"). The fix for #14279 must not
        remove that capability (candidate D - dropping the inverse - is explicitly
        rejected by design § 2.3). This test must pass unmodified both before and
        after guards A and B land."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B5 - control')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving)

        with Form(route) as route_form:
            with route_form.segment_ids.edit(0) as segment_line:
                segment_line.ave_speed = 75.0
            route = route_form.save()

        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, 75.0, places=6,
            msg=(
                "editing a segment's speed on the Segments tab (doc/index.rst:26) must "
                f"still reach the manual cache row - got {manual_driving.ave_speed} "
                "instead of 75.0"
            ),
        )

    # ------------------------------------------------------------------
    # B7 - the third zero-writer (code review, defect 1, HIGH): a falsy
    # `distance` with a TRUTHY `duration` must not zero the segment's own
    # `ave_speed` mirror.
    # ------------------------------------------------------------------
    def test_segment_zero_distance_with_truthy_duration_must_not_zero_ave_speed(self):
        """AMENDED RATIONALE (design § 5(d) row B7 - "same as B4"; premise changed,
        assertions unchanged).

        Business rule: same contract as B4 - `duration` may never write back into
        `ave_speed`, in ANY distance/duration combination, including this
        falsy-distance/truthy-duration one.

        The ORIGINAL rationale read `route.segment._inverse_duration`'s `if
        r.duration:` guard as protecting the falsy-DURATION case (B4) but missing the
        mirror-image falsy-DISTANCE case (this test) - i.e. it framed the defect as a
        guard whose CONDITION was too narrow, implying the fix was a BETTER
        condition. Under the redesigned contract that framing no longer applies:
        there is no `_inverse_duration` guard to widen, because R1 removes the
        inverse entirely (design § 2.3). Once that lands, this exact
        distance=0/duration=2.0 assignment becomes INERT - not because some
        conditional check now also catches it, but because there is nothing left on
        the field to fire. An inert write-back is a strictly STRONGER guarantee than
        any per-condition guard could give (design § 5(d): "under R1 the assignment
        is inert, which is a stronger guarantee than the guard it was written
        against"), which is exactly why this test's assertions stay valid unchanged."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B7 - guard B distance=0')
        segment = route.segment_ids[:1]

        # GIVEN a known-good speed on the segment - independent of whatever the create
        # cascade produced.
        segment.ave_speed = 45.0
        segment.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment.ave_speed, 45.0, places=6,
            msg="precondition failed: could not establish a known-good segment speed",
        )

        # AND a falsy distance on the segment - the combination B4 does not cover.
        segment.distance = 0.0
        segment.invalidate_recordset(['distance'])
        self.assertEqual(
            segment.distance, 0.0,
            "precondition failed: could not establish a falsy segment distance",
        )

        # WHEN duration resolves to a TRUTHY value while distance is still falsy.
        with self._assert_duration_write_discarded(segment):
            segment.duration = 2.0

        # THEN the segment's own ave_speed mirror must not be zeroed by the division
        # 0 / 2.0 - _inverse_duration's `if r.duration:` guard checks `duration` alone
        # and misses the falsy `distance` half of the same expression.
        segment.invalidate_recordset(['ave_speed'])
        self.assertNotEqual(
            segment.ave_speed, 0.0,
            (
                "segment.duration = 2.0 with a falsy segment.distance zeroed "
                f"segment.ave_speed (now {segment.ave_speed}) - _inverse_duration's "
                "`r.ave_speed = r.distance / r.duration` still fires when `distance` is "
                "falsy and only `duration` is guarded"
            ),
        )

    # ------------------------------------------------------------------
    # B8 - the migration's repair call (code review, defect 2, CRITICAL): one
    # partner with an invalid coordinate must not blank the distance of every
    # OTHER row sharing the manual provider.
    # ------------------------------------------------------------------
    def test_repairing_rows_with_one_invalid_partner_must_not_blank_other_rows_distance(self):
        """`migrations/18.0.1.0.3/post-migration.py` repairs corrupted cache rows by
        calling `corrupted._update_distance_ave_speed()`
        (`route_section_transport_mode.py`). That method groups the rows it is
        given by `transport_mode_provider_id` alone (`_get_distance_matrix`,
        `route_section_transport_mode.py`) - there is only ONE manual-input-
        driving provider record, so EVERY manual-mode row handed to it lands in the
        SAME group and is resolved with a SINGLE call to
        `geo_direction_manual._get_distance_matrix`, which validates coordinates for
        the WHOLE combined address set with `raise_if_not_valid=True`
        (`geo_direction_manual.py`, via `res_partner.py _validate_coordinates`).
        If ANY partner in that combined set has an invalid coordinate, the call
        raises; the bare `except Exception` (`route_section_transport_mode.py`)
        swallows it and only logs, so `processed` stays empty for the ENTIRE group -
        not just the row that touches the bad partner. `remain = self - processed` is
        then the WHOLE group, and `remain.distance = False` blanks every row's
        distance, including rows whose own addresses were always valid. Net effect:
        one partner with a bad geocode destroys the distance of every OTHER route
        section sharing the manual provider - turning a repairable corruption into
        unrepairable data loss."""
        good_route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B8 - good route')
        bad_route = self._build_two_waypoint_route(self.address_danang, self.address_dongha, 'B8 - bad route')

        good_row = self._manual_driving_row(good_route.segment_ids[:1].route_section_id)
        bad_row = self._manual_driving_row(bad_route.segment_ids[:1].route_section_id)
        self.assertTrue(good_row, "expected the good route's section to carry a manual/driving cache row")
        self.assertTrue(bad_row, "expected the bad route's section to carry a manual/driving cache row")

        # PRECONDITION: both cache rows already hold the real, positive distance the
        # manual provider computed from valid coordinates at section-creation time
        # (route.section.create -> _generate_route_section_transport_modes_if_not_exists
        # -> _update_distance_ave_speed) - never a direct write to the field under test.
        self.assertGreater(good_row.distance, 0.0, "precondition failed: good_row must start with a real positive distance")
        self.assertGreater(bad_row.distance, 0.0, "precondition failed: bad_row must start with a real positive distance")
        good_distance_before = good_row.distance

        # WHEN the bad route's OWN endpoint - never the good route's - later gets an
        # invalid coordinate. This is a realistic data-quality event on a plain,
        # user-editable field (out-of-range latitude, e.g. from a bad geocode), not a
        # fabricated precondition on `distance`/`ave_speed` themselves. An out-of-range
        # value is used (rather than clearing the coordinate) to trigger
        # `_validate_coordinates` deterministically through its explicit range check,
        # regardless of how a falsy Float value round-trips through the ORM cache.
        self.address_dongha.partner_latitude = 999.0

        # AND the module repairs both rows together - exactly the call the migration
        # makes (post-migration.py: `corrupted._update_distance_ave_speed()`).
        (good_row + bad_row)._update_distance_ave_speed()

        # THEN good_row - whose own addresses (Da Nang, Hue) never lost their valid
        # coordinates - must keep its distance. A bad coordinate on an UNRELATED
        # route's endpoint must never blank a different, still-valid route section.
        good_row.invalidate_recordset(['distance'])
        self.assertAlmostEqual(
            good_row.distance, good_distance_before, places=6,
            msg=(
                f"good_row.distance was blanked (now {good_row.distance}) while "
                "repairing bad_row, whose OWN endpoint had an invalid coordinate - the "
                "bare `except Exception` in _get_distance_matrix swallowed the "
                "ValidationError for the WHOLE manual-provider group, so "
                "remain.distance = False wiped every row in the group, not just "
                "bad_row"
            ),
        )

    # ------------------------------------------------------------------
    # B9 - the third zero-writer (fresh code review, defect 3), this time on the
    # model that IS the source of truth: `route.section.transport.mode` inherits
    # `_inherit = 'route.section.mixin'`, which chains up to `distance.mixin`.
    # `route_section_transport_mode.py` redeclares `duration` with ONLY
    # `help=`; `route_section_mixin.py` does the same one level up. Neither
    # sets `compute=`/`store=`/`inverse=`, so per Odoo's field-attribute merge this
    # field keeps `compute='_compute_duration'`, `store=True` AND
    # `inverse='_inverse_duration'` straight from `distance_mixin.py` - the
    # LIVE, unguarded `_inverse_duration` (`distance_mixin.py`):
    #
    #     @api.onchange('duration')
    #     def _inverse_duration(self):
    #         for r in self:
    #             r.ave_speed = r.distance / r.duration if r.duration else 0.0
    #
    # AMENDED RATIONALE (design § 5(d) rows B9a/B9b - premise changed, assertions
    # unchanged). The paragraph above used to conclude that `route.section` and
    # `route.mixin` neutralise this inverse with a no-op override while
    # `route.section.transport.mode` is "ABSENT from that override list" - framing
    # this as ONE MORE model missing its own guard, the same per-model-guard shape
    # already refuted for B4/B7 above (design C9). It is not a coincidence that a
    # THIRD model (this one) turned out to need the same manual guard as
    # `route.segment` did (B4) - that recurrence is exactly why design § 2.3 moves
    # the fix to the field DECLARATION on `distance.mixin` itself instead of a
    # fourth per-model override: no model, present or future (including
    # `trip.segment`, design C5), needs anyone to remember to add anything. Once R1
    # lands, `route.section.transport.mode` - the CACHE ROW itself, not a mirror of
    # anything else - protects its own `ave_speed` for the same reason every other
    # model in the tree does: the hook no longer exists.
    # ------------------------------------------------------------------
    def test_writing_zero_duration_on_cache_row_must_not_zero_its_own_ave_speed(self):
        """AMENDED RATIONALE (design § 5(d) row B9a).

        Business rule: `route.section.transport.mode` is the SOURCE-OF-TRUTH cache
        row every `route.section` and `route.segment` reading projects/mirrors;
        writing a falsy `duration` straight onto it (e.g. `record.write({'duration':
        0.0})`) must never zero that same row's own `ave_speed`. Once zeroed the row
        is permanently unselectable (`route_section.py` only considers a truthy
        `ave_speed`), and the self-heal cron cannot repair it either
        (`_update_missing_distances_ave_speed` only searches `distance in (False,
        0.0)` - this row's `distance` stays intact, only `ave_speed` is destroyed).

        This no longer rests on "this model happens to be missing a guard other
        models have" (refuted, see the class comment above) - under the redesigned
        contract `duration` cannot write back on ANY model, this one included,
        because the write-back hook is removed at its declaration on
        `distance.mixin`, not patched per model."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B9 - direct write')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")

        # PRECONDITION: the row already carries the real, administrator-configured
        # speed and a real positive duration the create-time cascade computed from
        # it - never a direct write to ave_speed/duration themselves.
        configured = self._configured_speed(self.manual_driving_provider)
        manual_driving.invalidate_recordset(['duration', 'ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg="precondition failed: could not establish a known-good cache row speed",
        )
        self.assertGreater(
            manual_driving.duration, 0.0,
            "precondition failed: expected a real positive duration on the cache row "
            "before the edit",
        )

        # WHEN the cache row's own duration is written to a falsy value - the exact
        # write shape B4 already proved dangerous on route.segment, this time aimed
        # directly at the row that IS the source of truth, not a mirror of it.
        with self._assert_duration_write_discarded(manual_driving):
            manual_driving.write({'duration': 0.0})

        # THEN the cache row's own ave_speed must not be zeroed by the inherited,
        # unguarded distance.mixin._inverse_duration.
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertNotEqual(
            manual_driving.ave_speed, 0.0,
            (
                "writing duration=0.0 directly onto the route.section.transport.mode "
                f"cache row zeroed its own ave_speed (now {manual_driving.ave_speed}) - "
                "this model has no _inverse_duration override and inherits "
                "distance.mixin's live, unguarded one"
            ),
        )

    def test_form_editing_zero_duration_on_cache_row_must_not_zero_its_own_ave_speed(self):
        """RE-EXPRESSED (node N3b, design § 5(d) row B9b - premise changed, mechanism
        re-expressed, business rule intact).

        R1 has now landed: `distance.mixin.duration` lost `inverse='_inverse_duration'`
        and gained `readonly=True` (`distance_mixin.py`). No view sets
        `readonly="0"` on `duration` (`route_section_views.xml:52-54,76,107` all leave
        it at the field's own attribute-level readonly), so
        `odoo/tests/form.py`'s `assert not self._get_modifier(field_name,
        'readonly')` now fires BEFORE any value ever reaches the ORM - raising
        `AssertionError: can't write on readonly field 'duration'` instead of silently
        succeeding. Design acceptance criterion #4 names this outcome directly:
        "Nobody can type an estimated time."

        Business rule (unchanged from B9a/B9b's original intent): a user editing
        `duration` on the "Transportation Modes" tab nested in the `route.section`
        form (route_section_views.xml:60-88) must never be able to zero the edited
        row's own `ave_speed`. Under the OLD mechanism that meant asserting the value
        survived a silent write; under the NEW, stronger mechanism it means asserting
        the edit is rejected outright - and, as a fallback so this test still protects
        the business rule even if a future view regression ever grants `duration`
        writability again (readonly="0"), that IF the Form ever permits the
        assignment, the cache row's `ave_speed` must still not be zeroed."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'B9 - Form edit')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")

        configured = self._configured_speed(self.manual_driving_provider)
        manual_driving.invalidate_recordset(['duration', 'ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg="precondition failed: could not establish a known-good cache row speed",
        )
        self.assertGreater(
            manual_driving.duration, 0.0,
            "precondition failed: expected a real positive duration on the cache row "
            "before the edit",
        )

        # Locate the manual/driving row's position in the section's transport_mode_ids
        # - the same order (`_order = 'sequence, id'`) both the plain recordset read
        # and the Form's one2many proxy are seeded from.
        target_index = list(section.transport_mode_ids.ids).index(manual_driving.id)

        try:
            with Form(section, view='to_geo_routes.route_section_form') as section_form:
                with section_form.transport_mode_ids.edit(target_index) as line:
                    line.duration = 0.0
                section_form.save()
        except AssertionError as form_rejection:
            # THEN: duration is not user-writable - and specifically because it is
            # readonly (R1), not some unrelated Form assertion firing first.
            self.assertIn(
                'duration', str(form_rejection),
                "expected the Form to reject the edit on the 'duration' field "
                f"specifically, got: {form_rejection}",
            )
            self.assertIn(
                'readonly', str(form_rejection),
                "expected the rejection reason to be readonly (distance.mixin R1 - "
                f"duration lost its inverse and gained readonly=True), got: {form_rejection}",
            )
        else:
            # Fallback, only reachable if a future view regression ever grants
            # duration writability again: the ORIGINAL business rule must still
            # hold - the edit must not zero the cache row's own ave_speed.
            manual_driving.invalidate_recordset(['ave_speed'])
            self.assertNotEqual(
                manual_driving.ave_speed, 0.0,
                (
                    "the Form unexpectedly permitted editing 'duration' to 0 on the "
                    "Transportation Modes tab (readonly must have been dropped again) "
                    f"AND this zeroed the cache row's own ave_speed (now "
                    f"{manual_driving.ave_speed})"
                ),
            )

    # ------------------------------------------------------------------
    # INV-1 (design § 6, declaration invariant) - NEW, node N2. The mixin
    # write-back contract redesign's central claim (design § 2.3) is that this
    # must be enforced by FIELD DECLARATION, not a per-model guard: "no compute
    # and no inverse in the mixin may derive a value across the
    # distance/speed/duration triangle." Concretely: `duration` must carry NO
    # `inverse=` on ANY model that inherits `distance.mixin` - present or
    # future - while the two legitimate unit-pair inverses
    # (`distance_in_miles`, `ave_speed_in_miles`) must stay exactly as they are.
    # Walking the WHOLE registry (not a hand-maintained model list) is the
    # property the guard-per-model candidate (design § 2.7 alternative A,
    # rejected) could never have - its coverage was not readable from one file,
    # and it was wrong twice (route.section.transport.mode, then trip.segment,
    # design C5).
    # ------------------------------------------------------------------
    def test_distance_mixin_duration_field_declares_no_writeback_inverse_anywhere_in_the_tree(self):
        """INV-1 (design § 6). `distance.mixin.duration` still declares
        `inverse='_inverse_duration'` (`distance_mixin.py`) on this tree - R1
        has not landed - so this must fail for every model whose `duration` field
        resolves to that declaration, regardless of whether the model additionally
        neutralises the CALLBACK BODY with a `pass` override (`route.section`,
        `route.mixin` do; `route.segment`, `route.section.transport.mode` do not,
        since node N1 reverted their guarded overrides per design ruling § 5(a)). A
        `pass` body override changes only what the method DOES, never the field's
        own `inverse=` attribute - only removing `inverse=` from the declaration
        itself (R1) can, which is exactly why this walks `_fields['duration'].inverse`
        rather than probing runtime behaviour: it is the one check whose failure
        surface is not narrowed by any existing guard, live or reverted."""
        distance_mixin_cls = self.env.registry['distance.mixin']
        offenders = []
        for model_name in self.env.registry:
            model_cls = self.env.registry[model_name]
            if model_cls is not distance_mixin_cls and not issubclass(model_cls, distance_mixin_cls):
                continue
            duration_field = self.env[model_name]._fields.get('duration')
            if duration_field is not None and duration_field.inverse:
                offenders.append(
                    "%s.duration declares inverse=%r" % (model_name, duration_field.inverse)
                )

        self.assertFalse(
            offenders,
            (
                "distance.mixin contract (design § 2.3) violated - `duration` must be "
                "a pure derived output with NO write-back inverse on any model "
                "inheriting distance.mixin, so nothing can ever move a value "
                "backwards across the distance/speed/duration triangle: "
                + "; ".join(offenders)
            ),
        )

        # The two legitimate unit-pair inverses must stay intact everywhere in the
        # tree - INV-1 must not become a blunt instrument that also deletes the
        # documented miles-editing capability (design § 2.1, doc/index.rst:26).
        for model_name in self.env.registry:
            model_cls = self.env.registry[model_name]
            if model_cls is not distance_mixin_cls and not issubclass(model_cls, distance_mixin_cls):
                continue
            model = self.env[model_name]
            distance_in_miles_field = model._fields.get('distance_in_miles')
            if distance_in_miles_field is not None:
                self.assertEqual(
                    distance_in_miles_field.inverse, '_inverse_distance_in_miles',
                    f"{model_name}.distance_in_miles must keep its unit-conversion inverse",
                )
            ave_speed_in_miles_field = model._fields.get('ave_speed_in_miles')
            if ave_speed_in_miles_field is not None:
                self.assertEqual(
                    ave_speed_in_miles_field.inverse, '_inverse_ave_speed_in_miles',
                    f"{model_name}.ave_speed_in_miles must keep its unit-conversion inverse",
                )

    # ------------------------------------------------------------------
    # INV-2 (design § 6, write-isolation invariant) - NEW, node N2. Table-driven
    # over the two concrete models where the corruption actually happens (the
    # mirror `route.segment` and the cache row `route.section.transport.mode`
    # itself). `route.section`/`route.route` already neutralise every inverse
    # with a `pass` override (unaffected by node N1's revert) and are excluded
    # as uninteresting for this invariant. Design § 6: "subsumes B4, B7, B9a as
    # parametrised rows and covers the combinations nobody has enumerated."
    # ------------------------------------------------------------------
    def test_writing_the_distance_duration_triangle_fields_must_never_move_a_positive_ave_speed_to_zero(self):
        """INV-2 (design § 6). For {route.segment, route.section.transport.mode} x
        {distance, duration, distance_in_miles}, writing that ONE field to 0.0 from a
        known-good POSITIVE `ave_speed` baseline must never move `ave_speed` itself
        to 0. `ave_speed_in_miles` is deliberately EXCLUDED from this table: writing
        0 there is that field's own legitimate, documented unit-conversion capability
        (design § 2.1/§ 2.5 finding 1 - `mile2km(0) == 0` is a fixed point, not a
        corruption) - asserting against it would encode a false requirement, exactly
        the trap this run was warned not to fall into with the CONTROL test.

        Expected failures on the CURRENT tree (R1 not landed): the `duration` row
        for BOTH models - `distance.mixin._inverse_duration` is still live and
        unguarded on both (design ruling § 5(a): node N1 reverted the WIP guards),
        so `record.write({'duration': 0.0})` fires
        `r.ave_speed = r.distance / r.duration if r.duration else 0.0` = 0.0
        unconditionally. The `distance` and `distance_in_miles` rows are expected to
        stay green on the current tree - `route.segment`'s own `_inverse_distance_ave_speed`
        (guard A) only writes truthy values onward and never touches `ave_speed`
        itself, and `route.section.transport.mode.distance` has no inverse at all
        (verified against `odoo/models.py` - a stored computed field
        without an inverse is written as a plain value with no side effect)."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'INV-2 write isolation')
        segment = route.segment_ids[:1]
        cache_row = self._manual_driving_row(segment.route_section_id)
        self.assertTrue(cache_row, "expected a manual/driving cache row for a brand-new section")

        targets = {'route.segment': segment, 'route.section.transport.mode': cache_row}
        for model_name, record in targets.items():
            for field_name in ('distance', 'duration', 'distance_in_miles'):
                with self.subTest(model=model_name, field=field_name):
                    record.ave_speed = 45.0
                    record.invalidate_recordset(['ave_speed'])
                    self.assertAlmostEqual(
                        record.ave_speed, 45.0, places=6,
                        msg=(
                            f"precondition failed: could not (re)establish a "
                            f"known-good {model_name}.ave_speed before testing "
                            f"{field_name}"
                        ),
                    )

                    with ExitStack() as provocation:
                        if field_name == 'duration':
                            # The only row of this table that reaches R3: `distance`
                            # and `distance_in_miles` are genuine write inputs on
                            # both models, `duration` is the derived one the guard
                            # discards - and diagnoses - on every model in the tree.
                            provocation.enter_context(
                                self._assert_duration_write_discarded(record))
                        record.write({field_name: 0.0})

                    record.invalidate_recordset(['ave_speed'])
                    self.assertNotEqual(
                        record.ave_speed, 0.0,
                        (
                            f"writing {model_name}.{field_name} = 0.0 zeroed "
                            f"{model_name}.ave_speed (now {record.ave_speed}) - the "
                            "distance/duration triangle is not write-isolated from "
                            "ave_speed"
                        ),
                    )

    # ------------------------------------------------------------------
    # INV-3 / B10 (design § 6, form-cascade invariant) - NEW, node N2. Covers
    # path 4 - the gap design § 5(d) states plainly: "No test covers path 4
    # (_inverse_ave_speed_in_miles firing on recompute). Ten tests, four paths,
    # one uncovered." `distance.mixin._compute_ave_speed_in_miles` currently
    # re-derives the mile speed as `distance_in_miles / duration if duration
    # else 0.0` (`distance_mixin.py`) instead of a plain
    # `km2mile(ave_speed)` conversion (design § 2.2) - a SECOND, INDEPENDENT
    # producer of the speed quantity, wired back to `ave_speed` through its own
    # UNCONDITIONAL inverse `_inverse_ave_speed_in_miles`
    # (`r.ave_speed = mile2km(r.ave_speed_in_miles)`, no guard at all, unaffected
    # by node N1's revert since that inverse was never touched by the WIP).
    # ------------------------------------------------------------------
    def test_form_editing_zero_duration_must_not_zero_ave_speed_in_miles_either(self):
        """RE-EXPRESSED (node N3b, design § 5(d) row B9b's shape, applied to INV-3 /
        B10 per this run's brief - premise changed, mechanism re-expressed, business
        rule intact).

        R1 has landed: `distance.mixin.duration` is now `readonly=True` with no
        `inverse=`. No view sets `readonly="0"` on `duration`
        (`route_route_views.xml:139`, `route_section_views.xml:76`), so
        `Form.edit(...).duration = 0.0` is rejected by `odoo/tests/form.py`
        BEFORE it ever reaches the ORM - `AssertionError: can't write on readonly
        field 'duration'` - on BOTH edit surfaces this test drives. That closes path 4
        one step earlier than R2 does: the onchange cascade this test was written to
        exercise (design C7, `web/models/models.py`) can no longer even be
        entered through a `duration` edit, because the edit itself never lands.

        Business rule (unchanged from INV-3's original intent): editing `duration` on
        the documented Segments tab (`route.segment`) or Transportation Modes tab
        (`route.section.transport.mode`) must never zero `ave_speed_in_miles` - the
        field none of the other tests in this file look at. Re-expressed the same way
        as B9b: assert the edit is rejected as not user-writable, and, as a fallback
        so the business rule stays protected even if a future regression ever grants
        `duration` writability again, assert that IF the Form permits the assignment,
        `ave_speed_in_miles` is still not zeroed."""
        with self.subTest(model='route.segment'):
            route = self._build_two_waypoint_route(
                self.address_danang, self.address_hue, 'INV-3 segment miles pair',
            )
            segment = route.segment_ids[:1]
            segment.ave_speed = 45.0
            segment.invalidate_recordset(['ave_speed', 'ave_speed_in_miles'])
            self.assertGreater(
                segment.ave_speed_in_miles, 0.0,
                "precondition failed: expected a positive ave_speed_in_miles on the "
                "segment before the edit",
            )

            try:
                with Form(route) as route_form:
                    with route_form.segment_ids.edit(0) as segment_line:
                        segment_line.duration = 0.0
                    route_form.save()
            except AssertionError as form_rejection:
                self.assertIn(
                    'duration', str(form_rejection),
                    "expected the Form to reject the edit on the 'duration' field "
                    f"specifically, got: {form_rejection}",
                )
                self.assertIn(
                    'readonly', str(form_rejection),
                    "expected the rejection reason to be readonly (distance.mixin "
                    f"R1), got: {form_rejection}",
                )
            else:
                # Fallback, only reachable if a future view regression ever grants
                # duration writability again on the Segments tab.
                segment.invalidate_recordset(['ave_speed_in_miles'])
                self.assertNotEqual(
                    segment.ave_speed_in_miles, 0.0,
                    (
                        "the Form unexpectedly permitted editing 'duration' to 0 on "
                        "the Segments tab AND this zeroed segment.ave_speed_in_miles "
                        f"(now {segment.ave_speed_in_miles})"
                    ),
                )

        with self.subTest(model='route.section.transport.mode'):
            route = self._build_two_waypoint_route(
                self.address_hue, self.address_dongha, 'INV-3 cache row miles pair',
            )
            segment = route.segment_ids[:1]
            section = segment.route_section_id
            manual_driving = self._manual_driving_row(section)
            self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")

            configured = self._configured_speed(self.manual_driving_provider)
            manual_driving.invalidate_recordset(['duration', 'ave_speed', 'ave_speed_in_miles'])
            self.assertAlmostEqual(
                manual_driving.ave_speed, configured, places=6,
                msg="precondition failed: could not establish a known-good cache row speed",
            )
            self.assertGreater(
                manual_driving.ave_speed_in_miles, 0.0,
                "precondition failed: expected a positive ave_speed_in_miles on the "
                "cache row before the edit",
            )

            target_index = list(section.transport_mode_ids.ids).index(manual_driving.id)
            try:
                with Form(section, view='to_geo_routes.route_section_form') as section_form:
                    with section_form.transport_mode_ids.edit(target_index) as line:
                        line.duration = 0.0
                    section_form.save()
            except AssertionError as form_rejection:
                self.assertIn(
                    'duration', str(form_rejection),
                    "expected the Form to reject the edit on the 'duration' field "
                    f"specifically, got: {form_rejection}",
                )
                self.assertIn(
                    'readonly', str(form_rejection),
                    "expected the rejection reason to be readonly (distance.mixin "
                    f"R1), got: {form_rejection}",
                )
            else:
                # Fallback, only reachable if a future view regression ever grants
                # duration writability again on the Transportation Modes tab.
                manual_driving.invalidate_recordset(['ave_speed_in_miles'])
                self.assertNotEqual(
                    manual_driving.ave_speed_in_miles, 0.0,
                    (
                        "the Form unexpectedly permitted editing 'duration' to 0 on "
                        "the Transportation Modes tab AND this zeroed the cache "
                        f"row's ave_speed_in_miles (now {manual_driving.ave_speed_in_miles})"
                    ),
                )

    # ------------------------------------------------------------------
    # INV-4 (design § 6, unit round-trip invariant) - NEW, node N2. CONTROL,
    # analogous to B5 for the miles axis - must stay GREEN both before and
    # after this redesign (design § 6 RED-before-green paragraph: "INV-4 and B5
    # are controls... a control is not required to go red"). Pins the
    # CAPABILITY design § 2.1 proves is real and documented
    # (doc/index.rst:26, route_route_views.xml:75-98) so a future cleanup
    # cannot delete it by accident.
    # ------------------------------------------------------------------
    def test_unit_pair_round_trip_still_works_both_axes(self):
        """INV-4 (design § 6). For BOTH unit pairs (distance/distance_in_miles,
        ave_speed/ave_speed_in_miles), `mile2km(km2mile(x)) == x` within tolerance,
        and editing the mile-side field via the documented Form UI persists the
        km-side field - exactly mirroring what B5 already pins for the ave_speed
        side of the km axis. This must NOT be forced red: the design explicitly
        rejects deleting these inverses (design § 2.1, § 2.7 candidate C) as the
        wrong fix, and R2 only changes HOW `ave_speed_in_miles` is computed, never
        removing its inverse or the km-side capability this test protects."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'INV-4 round trip')
        segment = route.segment_ids[:1]

        km2mile = self.to_base.km2mile
        mile2km = self.to_base.mile2km

        for value in (60.00000000000001, 45.0, 14.8):
            self.assertAlmostEqual(
                mile2km(km2mile(value)), value, places=6,
                msg=f"mile2km(km2mile({value})) must round-trip to {value}",
            )

        # GIVEN a known-good positive speed/distance on the segment.
        segment.ave_speed = 45.0
        segment.distance = 100.0
        segment.invalidate_recordset(['ave_speed', 'distance', 'ave_speed_in_miles', 'distance_in_miles'])
        self.assertAlmostEqual(
            segment.ave_speed_in_miles, km2mile(45.0), places=6,
            msg="precondition failed: ave_speed_in_miles did not follow a fresh ave_speed",
        )
        self.assertAlmostEqual(
            segment.distance_in_miles, km2mile(100.0), places=6,
            msg="precondition failed: distance_in_miles did not follow a fresh distance",
        )

        # WHEN the user edits the MILE side directly on the documented Segments tab
        # (doc/index.rst:26) - the same capability B5 pins for the km side.
        with Form(route) as route_form:
            with route_form.segment_ids.edit(0) as segment_line:
                segment_line.ave_speed_in_miles = 30.0
                segment_line.distance_in_miles = 62.0
            route = route_form.save()

        segment.invalidate_recordset(['ave_speed', 'distance'])
        self.assertAlmostEqual(
            segment.ave_speed, mile2km(30.0), places=6,
            msg=(
                "editing ave_speed_in_miles on the Segments tab must persist "
                "mile2km(30.0) into ave_speed"
            ),
        )
        self.assertAlmostEqual(
            segment.distance, mile2km(62.0), places=6,
            msg=(
                "editing distance_in_miles on the Segments tab must persist "
                "mile2km(62.0) into distance"
            ),
        )

    # ------------------------------------------------------------------
    # INV-5 (node N3b, new regression this run's own fix may have introduced) -
    # covers the REAL `action_get_directions()` user action, the one write path
    # none of B1-B9/INV-1..4 ever drive (they all go through Form.save() or
    # create()). Traced from source, not executed (this node is AUTHOR-ONLY):
    #
    # `route.section.mixin.action_get_directions` (`route_section_mixin.py`)
    # -> `_get_directions` (`:265-268`) -> `_fetch_directions_data`
    # (`:126-153`) fetches a fresh response from the manual provider, then calls
    # `self._update_from_directions_data(data.get('processed', {}))`
    # (`:107-116`), which dispatches - polymorphically, since `self` is a
    # `route.route` - to `route.mixin._process_direction_data_for_update`
    # (`route_mixin.py`), NOT the plain `route.section.mixin` version
    # (`:95-105`) that R6 already stripped `duration` out of.
    #
    # `route.mixin`'s override builds `segment_update_cmd` via
    # `get_effective_vals(vals, segment)` (`route_mixin.py`), which keeps
    # every key of the provider's per-leg `section` dict that is also a field on
    # `route.segment`. A section dict always carries `distance` AND `duration`
    # (`required_section_keys`, `geo_direction.py`) and NEVER `ave_speed`
    # (confirmed by reading every provider section-builder: `geo_direction.py`'s
    # own docstring/validator and `geo_direction_manual.py`'s `legs.append`
    # - neither ever puts `'ave_speed'` in a section/leg dict). So the resulting
    # `Command.update(segment.id, {'distance': d, 'duration': t, 'polyline': p})`
    # writes `duration` on the SAME `write()` call as `distance`, with no
    # `ave_speed` key at all - and this exact write site is OUTSIDE design § 8's
    # enumerated "three" `duration`-write sites (`route_section_mixin.py`,
    # `route_section_transport_mode.py`, `waypoint_mixin.py`),
    # because that domain-4 grep (`grep -rn "'duration'\s*:\|\.duration\s*="`)
    # cannot match a dict built by a comprehension - `get_effective_vals` never
    # contains the literal substring `'duration':` anywhere in its source. This
    # is a real gap in the design's own enumerated domain, not a restatement of
    # it - flagged here (ODOO-AI-ETHOS #6) in addition to being tested.
    #
    # On `route.segment` (post R1): `distance = fields.Float(store=True,
    # inverse='_inverse_distance_ave_speed')` - HAS an inverse, so per
    # `odoo/models.py` it is PROTECTED while being written (the
    # written value survives). `duration` inherits `distance.mixin.duration`
    # unchanged by any override on this model - `compute='_compute_duration',
    # store=True, readonly=True`, NO inverse - so `field.inverse or
    # (field.compute and not field.readonly)` is `None or (True and not True)` =
    # `False`: NOT protected. `distance` is also a `@api.depends` of
    # `_compute_duration` (`distance_mixin.py`:
    # `r.duration = r.distance / r.ave_speed if r.ave_speed else 0.0`), so
    # writing `distance` in the same call marks `duration` dirty via
    # `self.modified(vals)` (`models.py`) REGARDLESS of the `duration`
    # value the same call just wrote. The next read of `duration` therefore
    # recomputes it as `distance_NEW / ave_speed_OLD` - the segment's OWN
    # currently-cached `ave_speed`, which this write never touches (no
    # `ave_speed` key was ever in `effective_vals`) - discarding the value the
    # provider itself reported (`t`) whenever `ave_speed_OLD` differs from the
    # speed the provider actually used to derive `t`.
    #
    # VERDICT: REPRODUCES, per this source trace (this node does not execute -
    # see the run report for the honesty note on what "reproduces" means here
    # and what remains execution-only).
    #
    # ave_speed_OLD == 0 EXACTLY: I could not construct a fixture that reaches
    # this without either (a) directly ORM-writing a computed field (forbidden
    # by this node's hard requirements) or (b) re-deriving one of the
    # already-closed B3/B9-style zero-write paths (which R1/guard A now close,
    # so reusing them here would be testing THIS design's own regression
    # coverage a second time, not a NEW path). The WEAKER, certain claim - that
    # `ave_speed_OLD` need only DIFFER from the provider's own speed for
    # `duration` to silently land on the wrong, self-inconsistent value, never
    # what the provider reported - is what this test constructs and pins,
    # because it is reachable through nothing but documented, realistic ORM
    # flows: (1) B5's own proven manual-override capability
    # (doc/index.rst:26 - typing a speed on the Segments tab is a supported
    # user action), and (2) two routes SHARING one leg's address pair without
    # being the exact same route, which `_search_route_sections`
    # (`route_section_mixin.py`, used by `route.segment._attach_section`)
    # and `waypoint.mixin._search_route_sections` (`waypoint_mixin.py`,
    # used by `_prepare_segment_vals_list` to pre-seed a new segment's own
    # `distance`/`ave_speed` at create time, `waypoint_mixin.py`) both
    # resolve purely by consecutive `(address_from_id, address_to_id)` pairs,
    # never by the route's full waypoint chain - a second, LONGER route whose
    # chain merely CONTAINS the first route's leg as one consecutive pair
    # reuses that leg's `route.section`, and therefore inherits whatever
    # `ave_speed` an administrator already typed onto it, exactly like an
    # exact-duplicate route would.
    #
    # FIXTURE CORRECTION (this run, after a live run caught it): an EARLIER
    # version of this fixture built route B over the exact SAME two-address
    # chain as route A (Da Nang -> Hue) and got an ERROR, not the target
    # AssertionError - `route.route._check_waypoint_ids` (`route_route.py:
    # 48-68`) raises `UserError` for a second route whose `(transport_mode,
    # tuple(waypoint addresses))` key EXACTLY matches an existing route's
    # (`_group_by_transport_mode_and_addresses`, `route_mixin.py`,
    # keyed on the FULL ordered tuple - it does not consider sub-chains). That
    # was a fixture bug, not a refutation: route B here is instead Da Nang ->
    # Hue -> Dong Ha, a three-address chain whose tuple
    # `(danang, hue, dongha)` never equals route A's `(danang, hue)` (different
    # length, so `_check_waypoint_ids` never raises), while its FIRST
    # consecutive pair (Da Nang, Hue) still resolves to route A's existing
    # `route.section` through the exact same pairwise `_search_route_sections`
    # lookup a duplicate-chain route would have used. Sharing the section is
    # what the reproduction needs; sharing the WHOLE route is not, and was
    # never load-bearing for the mechanism traced above.
    #
    # Per the load-bearing fact carried into this node: a single isolated
    # route with fresh addresses does not reproduce this (its segment's
    # `ave_speed_OLD` is never anything but the manual provider's own
    # configured speed, so `distance_NEW / ave_speed_OLD` coincides with what
    # the provider reports by construction - no observable mismatch); sharing
    # a section is what decouples the segment's cached `ave_speed` from the
    # value the provider is about to compute against.
    # ------------------------------------------------------------------
    def test_action_get_directions_must_not_desync_segment_duration_from_the_providers_response(self):
        """Drives the REAL user action (`route.route.action_get_directions()`, not
        `Form.save()`/`create()`) and proves `route.mixin._process_direction_data_for_update`
        writes a NEW `distance` and the manual provider's OWN `duration` onto the
        segment in the SAME `write()` call, with no `ave_speed` key at all. Because
        `distance` is protected (has an inverse) and `duration` is not (readonly, no
        inverse, post-R1), `duration` silently gets recomputed as `distance_NEW /
        ave_speed_OLD` instead of keeping the value the provider actually reported -
        a genuine, uncovered zero/desync-write path CREATED by this run's own fix
        (dropping `duration`'s inverse made it stop being protected from recompute;
        see design § 8 "Stored-write survival").

        `ave_speed_OLD` is decoupled from the provider's own speed the same way B5
        proves a user CAN legitimately set a custom cache-row speed: route A's
        (Da Nang -> Hue) segment is edited to a real, positive, non-configured speed
        (45.0, matching B5's own value) via the documented Segments-tab capability.
        Route B (Da Nang -> Hue -> Dong Ha) is built afterwards - a LONGER, DIFFERENT
        waypoint chain so `route.route._check_waypoint_ids` (route_route.py)
        does not reject it as a duplicate of route A - but its first leg (Da Nang ->
        Hue) still reuses route A's existing `route.section` (see the class-comment
        trace above) and therefore starts from that SAME administrator-typed 45.0 -
        not the manual provider's own configured driving speed. Calling
        `action_get_directions()` on route B then fetches a FRESH response from the
        manual provider for BOTH legs; the provider always derives each leg's
        duration from ITS OWN configured default speed for 'driving'
        (`geo.direction.manual._get_directions`), never from the cache row's 45.0
        override - so the write this test drives, on the SHARED first-leg segment,
        is exactly the mismatched-speed write the class-comment mechanism
        describes. The second leg (Hue -> Dong Ha) is incidental to route B's own
        shape and is not inspected by this test."""
        route_a = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'N3b - shared section owner')
        segment_a = route_a.segment_ids[:1]
        section = segment_a.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")
        configured = self._configured_speed(self.manual_driving_provider)
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg="precondition failed: could not establish the cache row's configured speed",
        )

        # GIVEN the documented manual-override capability (doc/index.rst:26, pinned
        # unchanged by B5) is exercised on route A - the shared cache row now holds
        # a real, POSITIVE, administrator-typed speed that DIFFERS from the manual
        # provider's own configured default for 'driving'.
        with Form(route_a) as route_a_form:
            with route_a_form.segment_ids.edit(0) as segment_line:
                segment_line.ave_speed = 45.0
            route_a_form.save()
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, 45.0, places=6,
            msg="precondition failed: could not establish the shared cache row's overridden speed",
        )

        # WHEN a SECOND, LONGER route is built whose FIRST leg is the SAME address
        # pair as route A - a realistic scenario (a longer journey that starts by
        # crossing the same city pair another journey already covers). Its waypoint
        # chain (Da Nang, Hue, Dong Ha) is a different, longer tuple than route A's
        # (Da Nang, Hue), so `route.route._check_waypoint_ids` does not treat it as
        # a duplicate route (route_route.py) - but the first leg still
        # resolves, pairwise, to route A's existing route.section (no per-route
        # section instancing anywhere in this module) and therefore starts from the
        # SAME overridden 45.0 km/h, not the provider's own configured default.
        route_b = self._build_route_through_waypoints(
            [self.address_danang, self.address_hue, self.address_dongha],
            'N3b - shared first leg, longer chain',
        )
        segment_b = route_b.segment_ids.filtered(lambda s: s.route_section_id == section)
        self.assertTrue(
            segment_b,
            "precondition failed: expected one of route B's segments to reuse route "
            "A's route.section (the shared Da Nang -> Hue leg)",
        )
        self.assertEqual(
            len(segment_b), 1,
            "precondition failed: expected exactly one of route B's segments to "
            "match route A's route.section, not more than one",
        )
        segment_b.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment_b.ave_speed, 45.0, places=6,
            msg=(
                "precondition failed: route B's shared-leg segment did not inherit "
                "the shared cache row's overridden speed"
            ),
        )

        # AND route B fetches real directions from the manual provider - the actual
        # user action this node exists to check
        # (route_section_mixin.py action_get_directions ->
        # route_mixin.py route.mixin._process_direction_data_for_update) - for
        # BOTH of its legs in one call; only the shared first leg (segment_b) is
        # inspected below.
        route_b.action_get_directions()

        segment_b.invalidate_recordset(['distance', 'duration', 'ave_speed'])
        self.assertGreater(
            segment_b.distance, 0.0,
            "precondition failed: action_get_directions() must populate a positive "
            "distance on the segment for this reproduction to mean anything",
        )
        expected_duration = segment_b.distance / configured

        # THEN the segment's duration must still be mutually consistent with what
        # the manual provider actually reported (distance / the speed the provider
        # itself used), and never zero given the provider returned a positive
        # distance and duration.
        self.assertNotEqual(
            segment_b.duration, 0.0,
            (
                "action_get_directions() zeroed segment_b.duration - "
                "route.mixin._process_direction_data_for_update writes 'duration' "
                "in the same write() call as 'distance', but distance.mixin.duration "
                "has no inverse (readonly=True, post-R1) so it is not protected "
                "(odoo/models.py) and gets silently recomputed as "
                "distance_NEW / ave_speed_OLD"
            ),
        )
        self.assertAlmostEqual(
            segment_b.duration, expected_duration, places=6,
            msg=(
                f"action_get_directions() left segment_b.duration={segment_b.duration} "
                f"inconsistent with distance/configured-speed ({expected_duration}) - "
                f"it silently landed on distance/{segment_b.ave_speed} (the STALE "
                "45.0 cache-row override cached on the segment before the action "
                "ran) instead of the duration the manual provider actually reported "
                "for this fresh distance, because route.mixin."
                "_process_direction_data_for_update writes 'duration' alongside "
                "'distance' on a field with no inverse to protect it"
            ),
        )

    # ------------------------------------------------------------------
    # T1 (round-2 design, Amendment "N2 rulings" § A-1.3/A-1.6, ruling 1) - a
    # degenerate provider pair at site 2 (route_section_transport_mode.py,
    # `_update_missing_distances_ave_speed`) must leave the row ELIGIBLE FOR
    # REPAIR, not permanently poisoned outside the self-heal domain. NOTE (design
    # Amendment "cont." § A-8): there is no cron and no production caller of this
    # method - "eligible for repair" is the honest guarantee, never "will be
    # repaired".
    # ------------------------------------------------------------------
    def test_site2_degenerate_provider_pair_leaves_row_eligible_for_repair(self):
        """Business rule (design Amendment § A-1.3/A-1.4, R2a/R2b): a provider
        reporting a positive distance in zero time is a MALFORMED observation, not
        a real answer of "zero speed". `_update_missing_distances_ave_speed`
        (route_section_transport_mode.py) must not write the degenerate
        `ave_speed = 0.0` it receives (`ms_to_kmh(record_data['ave_speed'])` where
        the provider already collapsed a zero-duration leg to zero, design
        Amendment N2 - all 5 providers do this: geo_direction_google.py,
        _mapbox.py, _searoutes.py, _vietmap.py, _here.py) - because
        that write would move the row's `distance` OUT of the self-heal search
        domain `('distance', 'in', [False, 0.0])` (:215) while `ave_speed` stays
        unrecoverably zero. The row must stay INSIDE that domain - eligible for
        repair - and its prior speed must be retained. A second call with a
        USABLE observation must then actually resolve it, proving the heal
        completes rather than merely remains possible."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T1 - site 2 degenerate pair')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")

        configured = self._configured_speed(self.manual_driving_provider)
        manual_driving.invalidate_recordset(['distance', 'ave_speed'])
        self.assertGreater(
            manual_driving.distance, 0.0,
            "precondition failed: expected the create-time cascade to have already resolved a real distance",
        )
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg="precondition failed: could not establish the cache row's configured speed",
        )

        # GIVEN the row falls back into the self-heal domain - the same shape the
        # module's own `missing_records.write({'distance': False})`
        # (route_section_transport_mode.py) and `remain.distance = False`
        # (:198) already produce: distance blanked, prior speed left untouched.
        manual_driving.write({'distance': False})
        self.assertIn(
            manual_driving.id,
            self.env['route.section.transport.mode'].search([('distance', 'in', [False, 0.0])]).ids,
            "precondition failed: the row must start inside the self-heal domain",
        )

        # WHEN the self-heal routine resolves it against a DEGENERATE observation -
        # positive distance, zero speed (site 2's actual write shape,
        # `ms_to_kmh(record_data['ave_speed'])`, route_section_transport_mode.py)
        # - while every OTHER row in the same DB-wide batch gets a USABLE one, so the
        # sweep has to single this row out rather than fail wholesale (see
        # `_stub_distance_matrix`).
        usable_ave_speed_ms = self.to_base.kmh_to_ms(configured)
        with patch.object(
            type(manual_driving), '_get_distance_matrix',
            self._stub_distance_matrix(manual_driving, usable_ave_speed_ms),
        ), self._assert_degenerate_speed_reported(1):
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        # THEN the row must still be returned by the self-heal search - eligible
        # for repair, not permanently poisoned outside it - and its prior speed
        # must not have been replaced by the degenerate observation's fabricated
        # zero.
        manual_driving.invalidate_recordset(['distance', 'ave_speed'])
        self.assertIn(
            manual_driving.id,
            self.env['route.section.transport.mode'].search([('distance', 'in', [False, 0.0])]).ids,
            (
                f"the row's distance is now {manual_driving.distance!r} - a degenerate "
                "provider pair (duration==0) moved it OUT of the self-heal domain, the "
                "exact poisoning ruling 1 exists to prevent"
            ),
        )
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg=(
                f"a degenerate observation zeroed the row's speed (now "
                f"{manual_driving.ave_speed}) instead of retaining its prior value "
                f"({configured}) - ruling 1 forbids fabricating a zero speed"
            ),
        )

        # AND a second call, this time with a USABLE observation, must actually
        # resolve the row - proving the heal completes, not merely remains
        # possible (design Amendment § A-1.6, second half).
        # No row is degenerate this time, so the sweep must resolve everything it
        # selected and report NOTHING - a silent repair pass is the observable of a
        # completed heal, and `assertNoLogs` makes a regression that starts warning
        # here fail loudly instead of quietly reddening the build.
        with patch.object(
            type(manual_driving), '_get_distance_matrix',
            self._stub_distance_matrix(
                self.env['route.section.transport.mode'], usable_ave_speed_ms),
        ), self.assertNoLogs(TRANSPORT_MODE_LOGGER, level='WARNING'):
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        manual_driving.invalidate_recordset(['distance', 'ave_speed'])
        self.assertNotIn(
            manual_driving.id,
            self.env['route.section.transport.mode'].search([('distance', 'in', [False, 0.0])]).ids,
            "a usable observation on the second call must resolve the row out of the self-heal domain",
        )
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg=f"a usable observation must persist its speed (now {manual_driving.ave_speed})",
        )

    # ------------------------------------------------------------------
    # T2 (round-2 design, Amendment "N2 rulings" § A-4, N2-4) - the SAME
    # defect one method above site 2: `_update_distance_ave_speed`
    # (route_section_transport_mode.py) guards `distance` (:190) and
    # `ave_speed` (:193) INDEPENDENTLY. This is the module's row-CREATION path
    # (`route_section.py`), busier than site 2 - every row is born at
    # distance=0.0, ave_speed=0.0 (`distance.mixin` field defaults of `distance`/`ave_speed`).
    # ------------------------------------------------------------------
    def test_update_distance_ave_speed_degenerate_pair_must_not_poison_a_freshly_created_row(self):
        """Business rule: same as T1 (R2a/R2b), applied to the twin write site.
        `_update_distance_ave_speed` must not let its independent `if distance:`
        guard (:190) write a positive distance while its sibling `if ave_speed:`
        guard (:193) correctly skips a degenerate (zero) speed - that combination
        reproduces the site-2 poisoning one method away, on the row-creation path
        every new `route.section.transport.mode` row goes through."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T2 - twin bug')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)
        self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")

        # GIVEN a row reset to the state EVERY row starts at when
        # `_generate_route_section_transport_modes_if_not_exists` first creates it
        # - distance=0.0, ave_speed=0.0 (`distance.mixin` field defaults of
        # `distance`/`ave_speed`) -
        # before its FIRST `_update_distance_ave_speed()` call ever resolves
        # anything.
        manual_driving.write({'distance': False, 'ave_speed': 0.0})
        self.assertIn(
            manual_driving.id,
            self.env['route.section.transport.mode'].search([('distance', 'in', [False, 0.0])]).ids,
            "precondition failed: the row must start inside the self-heal domain",
        )

        # WHEN this exact row-creation path resolves it against a DEGENERATE
        # provider observation (positive distance, zero speed - the shape every
        # provider emits for a zero-duration leg, design Amendment N2).
        degenerate_response = {manual_driving: {'distance': 5000.0, 'ave_speed': 0.0}}
        with patch.object(
            type(manual_driving), '_get_distance_matrix',
            lambda records, res_model=None, res_id=None: degenerate_response,
        ):
            manual_driving._update_distance_ave_speed()

        # THEN the row must still be inside the self-heal domain - `if distance:`
        # must not have fired alone and left `distance` positive while `ave_speed`
        # stayed at its untouched 0.0, which is exactly the site-2 poisoning one
        # method away.
        manual_driving.invalidate_recordset(['distance', 'ave_speed'])
        self.assertIn(
            manual_driving.id,
            self.env['route.section.transport.mode'].search([('distance', 'in', [False, 0.0])]).ids,
            (
                f"the row's distance is now {manual_driving.distance!r} - the "
                "independent `if distance:` guard at "
                "route_section_transport_mode.py wrote a positive distance from "
                "a degenerate (duration==0) observation while the sibling "
                "`if ave_speed:` guard at :193 correctly skipped, leaving "
                f"ave_speed={manual_driving.ave_speed!r} unresolved and the row "
                "permanently outside the self-heal domain"
            ),
        )

    # ------------------------------------------------------------------
    # T3 (round-2 design, Amendment "N2 rulings" § A-3, ruling A3, "CENSUS keyed
    # on MECHANISM, not marker") - the census predicate `'duration' in vals` is
    # the WRONG key: `duration` has no inverse post-R1 (distance_mixin.py),
    # so its presence in a vals dict is inert whenever `distance`/`ave_speed` is
    # ALSO written in the same call (it just gets recomputed away) - the site-4
    # fix can satisfy a `'duration' not in vals` census for the WRONG reason (by
    # adding `ave_speed`, not by removing `duration`). The MECHANISM that
    # actually matters: any write to a LEAF-tier `distance.mixin` descendant that
    # resolves `distance` to a real value as part of a provider-fetch action must
    # carry `ave_speed` in that SAME vals dict.
    # ------------------------------------------------------------------
    def test_census_provider_fetch_write_that_resolves_distance_must_carry_ave_speed(self):
        """Instruments `write` on the LEAF-tier `distance.mixin` descendants that
        `action_get_directions()` can reach (`route.segment`,
        `route.section.transport.mode`) - not one named line, so a FUTURE write
        site anywhere in the tree is caught the same way (design § D2.2's
        method) - and drives the real user action on the exact reproduction the
        RED test `test_action_get_directions_must_not_desync_segment_duration_from_the_providers_response`
        already proves reachable: two routes sharing one leg's `route.section`."""
        # route A is built only for its side effect - it creates the shared
        # Da Nang -> Hue route.section that route B's first leg reuses.
        self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T3 census - owner')
        route_b = self._build_route_through_waypoints(
            [self.address_danang, self.address_hue, self.address_dongha],
            'T3 census - sharer',
        )

        captured = []

        def make_instrumented(model_name, original):
            def instrumented_write(records, vals):
                if 'distance' in vals:
                    captured.append((model_name, dict(vals)))
                return original(records, vals)
            return instrumented_write

        segment_cls = self.env.registry['route.segment']
        cache_row_cls = self.env.registry['route.section.transport.mode']
        original_segment_write = segment_cls.write
        original_cache_row_write = cache_row_cls.write

        with patch.object(segment_cls, 'write', make_instrumented('route.segment', original_segment_write)), \
             patch.object(cache_row_cls, 'write', make_instrumented('route.section.transport.mode', original_cache_row_write)):
            route_b.action_get_directions()

        violations = [
            (model_name, vals)
            for model_name, vals in captured
            # "resolves distance to a real value" - a blanking write
            # (`distance: False`/`0.0`, R2b's "leave it healable" disposition) is
            # deliberately exempt; only a write that MOVES distance to something
            # real must carry the speed that justifies it.
            if vals.get('distance') and 'ave_speed' not in vals
        ]
        self.assertFalse(
            violations,
            (
                "a write resolved 'distance' to a real value during "
                "action_get_directions() without carrying 'ave_speed' in the SAME "
                "vals dict - keyed on the MECHANISM (ave_speed presence), not the "
                "marker ('duration' absence) the old census used and that a "
                f"site-4-shaped fix can satisfy for the wrong reason: {violations}"
            ),
        )

    # ------------------------------------------------------------------
    # S5 (design § 6 sequencing item "census test + fail-on-warning wiring -
    # Census asserts on R3's warning channel", reaffirmed unchanged by
    # amendment § A-9's sequencing table) - the mechanism census above is
    # scoped to the two LEAF models `action_get_directions()` can reach
    # (`route.segment`, `route.section.transport.mode`); it cannot see a
    # violation on `route.mixin`/`route.route`/`route.section`. R3
    # (`distance.mixin._normalize_derived_duration_vals`, LOG-ONLY per
    # amendment A2) is the ONE guard method every one of the 7 models in the
    # tree shares - it pops `'duration'` out of any write/create vals dict
    # and only LOGS a warning, so a future write site that violates the
    # Derived-Duration Rule (design § 2.1 R1) would otherwise degrade
    # silently into a log line nobody reads. This test turns that warning
    # channel into a hard CI failure - the "keep the old assert - it is
    # cheap and correct, just not sufficient" marker-based census (amendment
    # A3), reborn as an active fail-on-warning gate instead of a passive log,
    # and tree-wide rather than two-model-scoped.
    # ------------------------------------------------------------------
    def test_r3_warning_channel_must_never_fire_across_the_driven_action_surface(self):
        """Drives the module's provider-fetch action surface -
        `action_get_directions()` (sites 1 and 4), `_update_distance_ave_speed()`
        (the row-creation path and the migration's own repair call) and
        `_update_missing_distances_ave_speed()` (the self-heal routine) - with
        R3's guard instrumented, and asserts it never had to pop `'duration'`
        out of a vals dict on ANY model in the `distance.mixin` tree.

        This is a REGRESSION GUARD, not a red-before-green pin for new
        behaviour: on the current tree (S1-S3 already landed) no write site
        supplies `duration`, so the guard already never fires and this test
        is green on arrival - it exists to fail loudly, naming the offending
        model/ids/vals, the day that stops being true."""
        interceptions = []
        distance_mixin_cls = self.env.registry['distance.mixin']
        original_normalize = distance_mixin_cls._normalize_derived_duration_vals

        def spying_normalize(records, vals):
            if 'duration' in vals:
                interceptions.append((records._name, list(records.ids), dict(vals)))
            return original_normalize(records, vals)

        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'S5 - warning channel')
        cache_row = self._manual_driving_row(route.segment_ids[:1].route_section_id)
        self.assertTrue(cache_row, "expected a manual/driving cache row for a brand-new section")

        with patch.object(distance_mixin_cls, '_normalize_derived_duration_vals', spying_normalize):
            # action_get_directions(): exercises route.route/route.mixin's own
            # write (via `_update_from_directions_data`) plus site 1
            # (route.section.mixin) and site 4 (route.mixin's segment loop).
            route.action_get_directions()
            # _update_distance_ave_speed(): the row-creation path
            # (route_section.py) and the migration's own repair call
            # (post-migration.py) both go through this method.
            cache_row._update_distance_ave_speed()
            # _update_missing_distances_ave_speed(): force the row back into
            # its search domain first so the call actually resolves
            # something, rather than finding zero eligible rows.
            cache_row.write({'distance': False})
            # This one surface is an `@api.model` sweep over the WHOLE database, so
            # it also picks up - and files an operator-facing data-quality report
            # about - every unresolvable row the demo dataset happens to contain
            # (route sections between `viin_logistics`/`viin_freight_management`
            # demo locations that ship with no lat/long, so their haversine distance
            # is 0). That report is TRUE and stays at WARNING for real callers; it is
            # muted for the duration of THIS call only because those rows are neither
            # created nor asserted on here - this test's sole oracle is `interceptions`
            # below - and because letting one module's test print another module's
            # data-quality report reddens the build for something no code change here
            # can fix. The sweep still exercises R3 through its real write path via
            # `cache_row`, the row this test does own.
            with mute_logger(TRANSPORT_MODE_LOGGER):
                self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        self.assertFalse(
            interceptions,
            (
                "distance.mixin's R3 guard intercepted 'duration' in a "
                "write/create vals dict during the driven action surface - a "
                "write site is violating the Derived-Duration Rule (design § "
                f"2.1 R1) and only logging it, silently: {interceptions}"
            ),
        )

    # ------------------------------------------------------------------
    # T4 (round-2 design, Amendment "cont." § A-6.4, ruling 4 / R4 "mirror-refresh
    # rule") - wherever provider data lands in the cache
    # (`_update_distance_ave_speed`, `_update_missing_distances_ave_speed`), every
    # `route.segment` mirroring that cache row must be refreshed in the SAME
    # operation, because NO `@api.depends` edge exists (deliberately refused -
    # see T5) from the cache row into `route.segment.ave_speed`/`.distance`
    # (`route.segment._compute_distance` and `_compute_ave_speed` depend only
    # on `route_section_id`, `route_id.transport_mode`).
    # ------------------------------------------------------------------
    def test_provider_fetch_refresh_must_push_to_every_segment_mirroring_the_cache_row(self):
        """Business rule (design Amendment "cont." § 9 criterion 1, as rewritten
        by ruling 3 / § A-3.2): after a successful provider fetch, the fetch's
        speed must be visible on EVERY segment sharing that leg - not just the one
        that triggered the fetch. Proves the gap with a REAL speed change (30.0,
        distinct from the configured 60ish) so a stale mirror is unmistakable."""
        route_a = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T4 - owner')
        route_b = self._build_route_through_waypoints(
            [self.address_danang, self.address_hue, self.address_dongha],
            'T4 - sharer',
        )
        segment_a = route_a.segment_ids[:1]
        section = segment_a.route_section_id
        segment_b = route_b.segment_ids.filtered(lambda s: s.route_section_id == section)
        self.assertEqual(len(segment_b), 1, "precondition failed: expected route B to share route A's section")
        manual_driving = self._manual_driving_row(section)

        configured = self._configured_speed(self.manual_driving_provider)
        segment_a.invalidate_recordset(['ave_speed'])
        segment_b.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment_a.ave_speed, configured, places=6,
            msg="precondition failed: could not establish route A's segment speed",
        )
        self.assertAlmostEqual(
            segment_b.ave_speed, configured, places=6,
            msg="precondition failed: could not establish route B's segment speed",
        )

        # WHEN a fresh provider fetch resolves the shared cache row to a NEW
        # speed - distinct from the configured ~60, so a stale mirror is
        # unmistakable. Usable observation (both operands positive), so T1/T2's
        # degenerate-pair guard is not in play here - this test is orthogonal.
        fresh_response = {
            manual_driving: {
                'distance': manual_driving.distance * 1000,
                'ave_speed': self.to_base.kmh_to_ms(30.0),
            }
        }
        with patch.object(
            type(manual_driving), '_get_distance_matrix',
            lambda records, res_model=None, res_id=None: fresh_response,
        ):
            manual_driving._update_distance_ave_speed()
        manual_driving.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, 30.0, places=6,
            msg="precondition failed: the cache row itself must show the fresh speed",
        )

        # THEN BOTH segments mirroring this section - route A's own AND route
        # B's, which merely shares the leg - must reflect the fresh speed.
        # Neither segment's own write ever touched them; only a push-refresh can.
        segment_a.invalidate_recordset(['ave_speed'])
        segment_b.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment_a.ave_speed, 30.0, places=6,
            msg=(
                f"route A's segment still shows {segment_a.ave_speed} after the "
                "cache row refreshed to 30.0 - no @api.depends edge exists from the "
                "cache row into route.segment.ave_speed (by design, ruling 4), so "
                "nothing but a push-refresh (R4) can update it"
            ),
        )
        self.assertAlmostEqual(
            segment_b.ave_speed, 30.0, places=6,
            msg=(
                f"route B's segment (sharing the same section) still shows "
                f"{segment_b.ave_speed} after the refresh"
            ),
        )

    # ------------------------------------------------------------------
    # T5a (round-2 design, Amendment "cont." § A-6.3/A-6.6, ruling 4, test "B2")
    # - RECURSION CANARY, regression pin half. The architect PROVED recursion is
    # structurally impossible (§ A-6.2) AND separately REFUSED the
    # `@api.depends` edge for a DIFFERENT reason: row-selection asymmetry
    # (the inverse writes the manual/forced row; the compute reads the FIRST row
    # by sequence with a truthy speed - different rows whenever an API row sorts
    # before manual). This test is a GUARD AGAINST A FUTURE CHANGE: it is GREEN
    # NOW BY DESIGN (there is no edge to break it) and exists to go RED if anyone
    # later adds that edge.
    # ------------------------------------------------------------------
    def test_hand_entered_segment_speed_is_not_reverted_by_a_non_fetch_cache_write(self):
        """Business rule (design Amendment "cont." § A-6.3): a user's hand-typed
        segment speed (doc/index.rst:26, the same capability the CONTROL test
        `test_user_edited_segment_speed_still_propagates_to_manual_cache_row`
        pins) must not be silently reverted the next time ANY code writes the
        shared cache row - only R4's targeted push (scoped to the two
        provider-fetch methods) may ever refresh a segment mirror. GREEN NOW BY
        DESIGN: on the current tree there is no `@api.depends` edge from the
        cache row into `route.segment.ave_speed`, so nothing propagates a plain
        cache-row write into the segment at all - this assertion is not
        contrived to pass, it documents the ACTUAL current behavior and will
        catch the ONE future change (adding that edge) that would break it."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T5a - hand-typed pin')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        manual_driving = self._manual_driving_row(section)

        # GIVEN the documented manual-override capability (doc/index.rst:26, B5's
        # own capability) is exercised - the segment now carries a real,
        # positive, administrator-typed speed.
        with Form(route) as route_form:
            with route_form.segment_ids.edit(0) as segment_line:
                segment_line.ave_speed = 45.0
            route_form.save()
        segment.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment.ave_speed, 45.0, places=6,
            msg="precondition failed: could not establish the hand-typed segment speed",
        )

        # WHEN some OTHER, non-fetch write touches the shared cache row directly
        # - not via action_get_directions()/_update_distance_ave_speed() - the
        # shape R4 deliberately does NOT push-refresh for (design Amendment
        # "cont." § A-6.4, "deliberate non-change").
        manual_driving.write({'ave_speed': 99.0})

        # THEN the segment must still show the administrator's 45.0 - untouched,
        # because nothing but R4's targeted push refreshes a segment mirror, and
        # this write is neither of R4's two call sites.
        segment.invalidate_recordset(['ave_speed'])
        self.assertAlmostEqual(
            segment.ave_speed, 45.0, places=6,
            msg=(
                f"segment.ave_speed is now {segment.ave_speed} after a non-fetch "
                "write to the shared cache row - if this ever starts failing "
                "because it now reads 99.0, someone added an @api.depends edge "
                "from the cache row into route.segment.ave_speed/.distance, which "
                "ruling 4 (design Amendment \"cont.\" § A-6.3) explicitly refuses: "
                "it would silently revert a user's hand-typed override the next "
                "time ANY code touches the shared cache row"
            ),
        )

    # ------------------------------------------------------------------
    # T5b (round-2 design, Amendment "cont." § A-6.6, ruling 4, test "B3") -
    # RECURSION CANARY, bounded-invocation-count half: a DETECTOR, not an
    # avoider. Bounds the number of times the segment-mirror compute/inverse
    # methods fire as a function of the fixture's OWN recordset size, so an
    # unbounded/recursive cycle fails AT THE BOUND and NAMES the offending
    # method, instead of dying at Python's recursion depth limit with a stack
    # trace pointing nowhere useful.
    # ------------------------------------------------------------------
    def test_provider_fetch_refresh_call_count_stays_bounded_no_recursion(self):
        """R4 IS implemented (T4 above proves the live push). But
        `_push_mirroring_segment_recompute` (route_section_transport_mode.py:
        181-205) is deliberately LAZY: it only calls
        `env.add_to_compute(field, mirroring_segments)`, marking the mirroring
        segments' `distance`/`ave_speed` fields PENDING - it never itself invokes
        `_compute_ave_speed`/`_compute_distance`. Those only fire the next time a
        marked field is actually READ: `Field.__get__` unconditionally calls
        `self.recompute(record)` before returning a value whenever the field is
        both `compute` and `store` (odoo/fields.py), which drains
        `env.transaction.tocompute` for that record regardless of what is
        already cached. This test forces that pending recompute to run, inside
        the patch window, by reading every mirroring segment's `ave_speed`/
        `distance` right after the push - without that, `call_counts` stays
        empty and the bound assertion below runs zero times, protecting
        nothing. It is the safety net specified alongside R4 (design Amendment
        "cont." § A-6.2 proves recursion structurally impossible; this test is
        the executable detector in place of trusting that proof alone)."""
        route_a = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T5b - owner')
        # route B is built only for its side effect - it adds a second segment on
        # the shared Da Nang -> Hue section, so segments_on_section below has >1 row.
        self._build_route_through_waypoints(
            [self.address_danang, self.address_hue, self.address_dongha],
            'T5b - sharer',
        )
        section = route_a.segment_ids[:1].route_section_id
        manual_driving = self._manual_driving_row(section)
        segments_on_section = self.env['route.segment'].search([('route_section_id', '=', section.id)])
        self.assertGreaterEqual(
            len(segments_on_section), 2,
            "precondition failed: expected at least 2 segments (route A's and route B's) sharing the section",
        )

        call_counts = defaultdict(int)
        segment_cls = self.env.registry['route.segment']
        watched_methods = ('_compute_ave_speed', '_compute_distance', '_inverse_distance_ave_speed')
        originals = {name: getattr(segment_cls, name) for name in watched_methods}

        def make_counter(method_name, original):
            def counted(records, *args, **kwargs):
                call_counts[method_name] += 1
                return original(records, *args, **kwargs)
            return counted

        patchers = [
            patch.object(segment_cls, name, make_counter(name, original))
            for name, original in originals.items()
        ]
        for patcher in patchers:
            patcher.start()
        try:
            fresh_response = {
                manual_driving: {
                    'distance': manual_driving.distance * 1000,
                    'ave_speed': self.to_base.kmh_to_ms(30.0),
                }
            }
            with patch.object(
                type(manual_driving), '_get_distance_matrix',
                lambda records, res_model=None, res_id=None: fresh_response,
            ):
                manual_driving._update_distance_ave_speed()

            # `_push_mirroring_segment_recompute` only marks `ave_speed`/
            # `distance` via `env.add_to_compute` on `segments_on_section` - a
            # LAZY marker, not an eager write. Force that pending recompute to
            # actually run, still inside the patch window, so the watched
            # methods fire for real and `call_counts` observes them instead of
            # staying empty.
            segments_on_section.invalidate_recordset(['ave_speed', 'distance'])
            # read() forces the lazy add_to_compute recompute to run for real
            # (a plain attribute read would too, but a bare `seg.ave_speed`
            # expression trips pylint W0104 / flake8 B018); read() is an effectful
            # call the linters accept and it recomputes the whole recordset at once.
            segments_on_section.read(['ave_speed', 'distance'])
        finally:
            for patcher in patchers:
                patcher.stop()

        # BOUND: a small constant multiple of the recordset size - never
        # unbounded/recursive growth. Generous on purpose (this test's job is to
        # catch a RUNAWAY cycle, not to pin an exact call count).
        bound = len(segments_on_section) * 3
        for method_name, count in call_counts.items():
            self.assertLessEqual(
                count, bound,
                (
                    f"{method_name} fired {count} times for {len(segments_on_section)} "
                    f"segments (bound={bound}) - this names an unbounded/recursive "
                    "write-back cycle instead of failing silently at Python's "
                    "recursion limit"
                ),
            )

    # ------------------------------------------------------------------
    # T6a (round-2 design, Amendment "cont." § A-7.3/A-7.4, ruling 5) -
    # `route.section.duration` has NO override at all (route_section.py,
    # store=False) and therefore NO `@api.depends_context` - it inherits
    # `distance.mixin._compute_duration` (:48-51) unchanged, which declares
    # neither `transport_mode` nor `force_geo_direction_provider` as a context
    # key, though the two operands it reads (`distance`, `ave_speed`) both vary
    # by both keys.
    # ------------------------------------------------------------------
    def test_section_duration_read_under_two_transport_modes_must_not_share_one_cached_value(self):
        """A section's Est. Time read under one transport mode must not leak into
        a read under a DIFFERENT mode in the same env. Every `route.section` gets
        one `route.section.transport.mode` row per configured provider
        regardless of the route's own mode
        (`_generate_route_section_transport_modes_if_not_exists`,
        route_section.py, iterates ALL `transport.mode.provider` records)
        - so a plain two-waypoint 'driving' route's section already carries a
        real, resolved 'cycling' row too (transport_mode_provider_data.xml: 60
        km/h driving, 14.8 km/h cycling), with no extra fixture needed."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T6a - duration context')
        section = route.segment_ids[:1].route_section_id
        driving_row = section.transport_mode_ids.filtered(lambda m: m.mode == 'driving' and m.provider == 'manual')
        cycling_row = section.transport_mode_ids.filtered(lambda m: m.mode == 'cycling' and m.provider == 'manual')
        self.assertTrue(driving_row, "expected a manual/driving row (seeded on every section)")
        self.assertTrue(cycling_row, "expected a manual/cycling row (seeded on every section, transport_mode_provider_data.xml)")
        self.assertGreater(driving_row.ave_speed, 0.0, "precondition failed: driving row must carry a real speed")
        self.assertGreater(cycling_row.ave_speed, 0.0, "precondition failed: cycling row must carry a real speed")

        expected_driving_duration = driving_row.distance / driving_row.ave_speed
        expected_cycling_duration = cycling_row.distance / cycling_row.ave_speed
        self.assertNotAlmostEqual(
            expected_driving_duration, expected_cycling_duration, places=3,
            msg="precondition failed: driving (60 km/h) and cycling (14.8 km/h) must yield visibly different durations for this test to mean anything",
        )

        # WHEN read in ONE env, driving FIRST, then cycling.
        duration_driving = section.with_context(transport_mode='driving', force_geo_direction_provider='manual').duration
        duration_cycling = section.with_context(transport_mode='cycling', force_geo_direction_provider='manual').duration

        self.assertAlmostEqual(
            duration_driving, expected_driving_duration, places=6,
            msg=f"section.with_context(transport_mode='driving').duration is {duration_driving}, expected {expected_driving_duration}",
        )
        self.assertAlmostEqual(
            duration_cycling, expected_cycling_duration, places=6,
            msg=(
                f"section.with_context(transport_mode='cycling').duration is "
                f"{duration_cycling}, expected {expected_cycling_duration} - it "
                "instead returned the DRIVING mode's cached value because "
                "'duration' (route_section.py) declares NO @api.depends_context "
                "at all, so Odoo's non-stored-field cache does not distinguish the "
                "transport_mode context and reuses the FIRST context's result"
            ),
        )

    # ------------------------------------------------------------------
    # T6b (round-2 design, Amendment "cont." § A-7.3/A-7.4, ruling 5) -
    # `distance_in_miles` (route_section.py) and `ave_speed_in_miles` (:58)
    # each carry `@api.depends_context('transport_mode')` only - missing
    # `force_geo_direction_provider`, though the value each reads (`distance` /
    # `ave_speed`) varies by BOTH keys. Isolated by holding `transport_mode`
    # CONSTANT and varying only `force_geo_direction_provider`, using a REAL
    # provider value vs one that resolves to NO candidate at all
    # (route_section.py) - this needs no second provider module installed.
    # ------------------------------------------------------------------
    def test_section_miles_pair_read_under_two_forced_providers_must_not_share_one_cached_value(self):
        """A section's *_in_miles read under one forced provider must not leak
        into a read under a DIFFERENT forced provider in the same env, holding
        the transport mode fixed throughout so only the missing
        `force_geo_direction_provider` key is exercised."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'T6b - miles context')
        section = route.segment_ids[:1].route_section_id
        driving_row = section.transport_mode_ids.filtered(lambda m: m.mode == 'driving' and m.provider == 'manual')
        self.assertTrue(driving_row, "expected a manual/driving row")
        self.assertGreater(driving_row.distance, 0.0, "precondition failed: expected a real positive distance")
        self.assertGreater(driving_row.ave_speed, 0.0, "precondition failed: expected a real positive speed")

        km2mile = self.to_base.km2mile
        section_real_provider = section.with_context(transport_mode='driving', force_geo_direction_provider='manual')
        section_unresolved_provider = section.with_context(transport_mode='driving', force_geo_direction_provider='no-such-provider')

        for field_name, source_field in (('distance_in_miles', 'distance'), ('ave_speed_in_miles', 'ave_speed')):
            with self.subTest(field=field_name):
                expected_real = km2mile(driving_row[source_field])
                expected_unresolved = km2mile(0.0)
                self.assertNotAlmostEqual(
                    expected_real, expected_unresolved, places=3,
                    msg=f"precondition failed: a resolved vs. unresolved candidate must differ for {field_name} to mean anything",
                )

                # Confirm the km-side field ITSELF is correctly context-keyed (its
                # `route.section._compute_<source_field>` depends_context already
                # declares both keys) - the unresolved-provider context must
                # genuinely fall back to 0.0, not accidentally still resolve to the
                # manual row.
                self.assertAlmostEqual(
                    section_unresolved_provider[source_field], 0.0, places=6,
                    msg=f"precondition failed: {source_field} must resolve to 0.0 under an unmatched forced provider",
                )

                value_real = section_real_provider[field_name]
                value_unresolved = section_unresolved_provider[field_name]

                self.assertAlmostEqual(
                    value_real, expected_real, places=6,
                    msg=f"section.with_context(force_geo_direction_provider='manual').{field_name} is {value_real}, expected {expected_real}",
                )
                self.assertAlmostEqual(
                    value_unresolved, expected_unresolved, places=6,
                    msg=(
                        f"section.with_context(force_geo_direction_provider='no-such-provider')."
                        f"{field_name} is {value_unresolved}, expected {expected_unresolved} "
                        "(no candidate resolves) - it instead returned the manual "
                        f"provider's cached value because route.section._compute_{field_name} "
                        "declares "
                        "@api.depends_context('transport_mode') only, missing "
                        "'force_geo_direction_provider'"
                    ),
                )

    # ------------------------------------------------------------------
    # Site 6 (Viindoo/tvtmaaddons#14279, live recon this run, RUN-DAG node
    # N21) - the SIXTH zero-write site, in `route.mixin._reorder_waypoints`
    # (route_mixin.py), reached from the REAL user action
    # `action_optimize_route()` (route_mixin.py) via `_optimize_route`
    # (:304-323). Twice in that method - once in the "update existing
    # section" branch (:386-390), once in "create a new section" (:393-400) -
    # the literal `'ave_speed': distance / duration if duration else 0.0` is
    # written DIRECTLY into `route.segment.ave_speed` via
    # `Command.update`/`Command.create` inside `self.write({'segment_ids': ...})`.
    # Unlike every OTHER write site in this file, this one is NOT routed
    # through the boundary converter `_prepare_distance_vals_from_provider`
    # (route_section_mixin.py) - it re-derives the exact unsafe
    # expression that converter exists to replace.
    #
    # Because `route.segment.ave_speed` carries
    # `inverse='_inverse_distance_ave_speed'` (route_segment.py), a
    # direct `Command.update(..., {'ave_speed': 0.0})` is a PROTECTED write
    # (odoo/models.py) - it survives, permanently freezing the
    # segment at a fabricated 0.0 regardless of what the route's own
    # section/cache says, until something else disturbs one of
    # `_compute_ave_speed`'s declared dependencies (`route_section_id`,
    # `route_id.transport_mode`) - neither of which this write touches.
    #
    # REACHABILITY of `duration == 0` with `distance > 0` for the manual
    # provider (the only one this module ships): `_compute_haversine_matrix`
    # (its branch reusing a matched cache row) fills this exact shape from EXISTING
    # `route.section.transport.mode` rows, filtered ONLY by
    # `('distance', 'not in', [0, False])` - never `duration`, never
    # `ave_speed` - so a row whose `ave_speed` has been zeroed (collapsing
    # `duration` to 0 via `_compute_duration`, distance_mixin.py, since
    # this model has no override) while `distance` stays positive yields
    # exactly this degenerate matrix cell. The "fresh" (uncached) Haversine
    # branch can never produce this shape (a positive distance divided by a
    # positive configured speed is always a positive duration) - confirming
    # the poisoned-cache-row path is the ONLY realistic seam for this
    # provider, not a fabricated one.
    #
    # FIXTURE: two routes sharing one leg's `route.section` (Da Nang -> Hue),
    # the same shape N3b/T3 above already use - `route.route._check_waypoint_ids`
    # (route_route.py) forbids a second route with the IDENTICAL
    # waypoint tuple, so route B is the strictly longer, DIFFERENT chain
    # Da Nang -> Hue -> Dong Ha (shares the first leg's section without
    # colliding). The degenerate leg is captured ONCE from the REAL
    # `geo.direction.manual._get_distance_matrix` (the exact method
    # `_solve_tsp` calls, geo_direction.py) against the shared row after
    # zeroing its `ave_speed` directly - a plain, non-computed field on this
    # model (never a raw write to a COMPUTED field) - then the row is
    # restored to its administrator-configured speed BEFORE
    # `_reorder_waypoints` runs, so the segment `_prepare_segment_vals_list`
    # recreates when route B's waypoint-sequence write triggers
    # `_compute_segment_ids` (`waypoint_mixin.py`,
    # `route_mixin.py`) starts from a REAL, positive baseline - never
    # from the row's own transient corruption. This isolates the ONE thing
    # under test: `_reorder_waypoints`'s own unconditional write, given ONE
    # degenerate leg observation, discarding a healthy baseline for a
    # fabricated 0 - not the already-covered cache-corruption mirroring B1-B9
    # protect elsewhere in this file.
    #
    # Driven via `_reorder_waypoints` DIRECTLY, not the full
    # `action_optimize_route()`: the OR-Tools TSP solve `_solve_tsp` performs
    # for that full path is a genuine 3-point Hamiltonian-cycle search whose
    # total cost TIES for every permutation once distances are symmetric (as
    # real geographic distances always are) - which permutation
    # `PATH_CHEAPEST_ARC` reports is not predictable without executing it,
    # and this node is AUTHOR-ONLY (no live run permitted). `_reorder_waypoints`
    # is the exact method site 6 lives in; the `optimized_route` supplied here
    # (Da Nang, Hue, Dong Ha - unchanged order) is itself a legitimate
    # optimizer answer (the identity permutation is always a valid TSP
    # solution candidate).
    # ------------------------------------------------------------------
    def test_reorder_waypoints_degenerate_leg_must_not_zero_a_healthy_segments_ave_speed(self):
        """Business rule: `_reorder_waypoints` must never let a single degenerate
        (duration == 0, distance > 0) leg observation permanently zero a
        `route.segment.ave_speed` that currently reflects a real, positive speed -
        the fix must derive `ave_speed` the same guarded way every other write
        site in this file already does (`_prepare_distance_vals_from_provider`),
        retaining the segment's already-resolved speed instead of writing a
        fabricated 0."""
        route_a = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'Site6 - shared section owner')
        route_section = route_a.segment_ids[:1].route_section_id
        manual_driving = self._manual_driving_row(route_section)
        self.assertTrue(manual_driving, "expected a manual/driving cache row for a brand-new section")

        configured = self._configured_speed(self.manual_driving_provider)
        manual_driving.invalidate_recordset(['distance', 'ave_speed'])
        self.assertGreater(
            manual_driving.distance, 0.0,
            "precondition failed: expected the create-time cascade to have already resolved a real distance",
        )
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg="precondition failed: could not establish the cache row's configured speed",
        )

        route_b = self._build_route_through_waypoints(
            [self.address_danang, self.address_hue, self.address_dongha],
            'Site6 - sharer, longer chain',
        )
        segment_b_ab_before = route_b.segment_ids.filtered(lambda s: s.route_section_id == route_section)
        self.assertEqual(
            len(segment_b_ab_before), 1,
            "precondition failed: expected route B's Da Nang -> Hue leg to reuse route A's route.section",
        )

        # WHEN the shared cache row's speed gets zeroed - a plain write to
        # `ave_speed`, a NON-computed field on this model (never a raw write to a
        # computed field), the same shape B9a already proves reachable via a
        # documented Form edit on the Transportation Modes tab. `duration` (a
        # pure compute of distance/ave_speed on this model, distance_mixin.py:
        # 52-55) collapses to 0 while `distance` stays untouched and positive -
        # exactly the degenerate shape site 6 needs.
        manual_driving.write({'ave_speed': 0.0})
        manual_driving.invalidate_recordset(['ave_speed', 'duration'])
        self.assertEqual(
            manual_driving.ave_speed, 0.0,
            "precondition failed: could not zero the shared cache row's speed",
        )
        self.assertEqual(
            manual_driving.duration, 0.0,
            "precondition failed: expected the zeroed speed to collapse duration to 0 too",
        )
        self.assertGreater(
            manual_driving.distance, 0.0,
            "precondition failed: distance must stay positive - this is the degenerate shape, not a blanked row",
        )

        # AND the REAL method the optimizer calls (`_solve_tsp`,
        # geo_direction.py) is asked for this exact pair - grounding that the
        # poisoned row genuinely produces a degenerate matrix cell, not a
        # fabricated one (`geo.direction._compute_haversine_matrix`: the search domain
        # filters only `distance`, never `duration`/`ave_speed`).
        matrix_response = self.env['geo.direction.manual']._get_distance_matrix(
            self.address_danang + self.address_hue, mode='driving',
        )
        leg_ab = matrix_response['processed'][(self.address_danang, self.address_hue)]
        self.assertGreater(
            leg_ab['distance'], 0.0,
            "precondition failed: expected the real haversine matcher to still report a positive distance",
        )
        self.assertEqual(
            leg_ab['duration'], 0.0,
            (
                "precondition failed: expected the real haversine matcher to reproduce the "
                "degenerate (duration == 0) cell from the poisoned cache row - if this "
                "fails, the reproduction mechanism itself is not grounded"
            ),
        )

        # AND the cache row is restored to health BEFORE `_reorder_waypoints`
        # runs - isolating the ONE thing this test protects: `_reorder_waypoints`'s
        # own write, given ONE degenerate leg observation captured above,
        # discarding a HEALTHY baseline for a fabricated 0. Without this
        # restore, route B's Da Nang -> Hue segment would start ALREADY at 0
        # when `_compute_segment_ids` recreates it below (it would mirror the
        # same poisoned row), making the bug's own write indistinguishable from
        # the already-covered cache-corruption mirroring B1-B9 protect
        # elsewhere in this file.
        manual_driving.write({'ave_speed': configured})
        manual_driving.invalidate_recordset(['ave_speed', 'duration'])
        self.assertAlmostEqual(
            manual_driving.ave_speed, configured, places=6,
            msg="precondition failed: could not restore the shared cache row's configured speed",
        )

        # The second leg (Hue -> Dong Ha) is fetched fresh and healthy - it is
        # never poisoned, and only exists to make the fixture a realistic
        # multi-leg `_reorder_waypoints` call, matching the real
        # `action_optimize_route()` shape.
        matrix_response_bc = self.env['geo.direction.manual']._get_distance_matrix(
            self.address_hue + self.address_dongha, mode='driving',
        )
        leg_bc = matrix_response_bc['processed'][(self.address_hue, self.address_dongha)]
        self.assertGreater(
            leg_bc['duration'], 0.0,
            "precondition failed: expected the Hue -> Dong Ha leg to stay healthy (control, never poisoned)",
        )

        # WHEN route B's optimizer path reorders its waypoints with the
        # captured legs - the exact method site 6 lives in
        # (route_mixin.py `_reorder_waypoints`), driven directly (per this
        # node's brief) rather than through the full `action_optimize_route()`
        # -> OR-Tools TSP solve, whose 3-point solution is a genuine cost TIE
        # for symmetric real-world distances and therefore not predictable
        # without executing it (this node is AUTHOR-ONLY). The supplied
        # `optimized_route` (unchanged order) is itself a legitimate optimizer
        # answer.
        route_b.with_context(
            transport_mode=route_b.transport_mode,
            force_geo_direction_provider='manual',
        )._reorder_waypoints(
            [self.address_danang, self.address_hue, self.address_dongha],
            [
                {'from': self.address_danang, 'to': self.address_hue, 'distance': leg_ab['distance'], 'duration': leg_ab['duration']},
                {'from': self.address_hue, 'to': self.address_dongha, 'distance': leg_bc['distance'], 'duration': leg_bc['duration']},
            ],
        )

        segment_b_ab = route_b.segment_ids.filtered(
            lambda s: s.address_from_id == self.address_danang and s.address_to_id == self.address_hue
        )
        self.assertEqual(
            len(segment_b_ab), 1,
            "expected exactly one of route B's segments to cover the Da Nang -> Hue leg after reordering",
        )

        # THEN the segment's ave_speed must not be the fabricated 0.0
        # `_reorder_waypoints` currently writes for a degenerate leg - it must
        # derive/retain the real, positive speed instead (the same
        # `_prepare_distance_vals_from_provider` boundary converter guard every
        # other write site in this file already uses). Pin the EXACT retained
        # value, not merely "not 0.0" - a future regression that derives a
        # different, WRONG non-zero speed (instead of retaining the healthy
        # `configured` baseline established above) must fail here too.
        self.assertAlmostEqual(
            segment_b_ab.ave_speed, configured, places=6,
            msg=(
                f"route B's Da Nang -> Hue segment.ave_speed is "
                f"{segment_b_ab.ave_speed} after _reorder_waypoints() processed a "
                "degenerate (duration == 0, distance > 0) leg, expected the "
                f"healthy pre-write speed ({configured}) to be retained - "
                "route_mixin.py writes 'ave_speed': distance / duration "
                "if duration else 0.0' directly via Command.update, and because "
                "route.segment.ave_speed carries "
                "inverse='_inverse_distance_ave_speed' a fabricated or wrong "
                "value would be PROTECTED (odoo/models.py) and survive"
            ),
        )

    # ------------------------------------------------------------------
    # PATH-7 REGRESSION LOCK (code review, defect 3, MED) - `_prepare_segment_vals_list`
    # (waypoint_mixin.py) seeds the miles fields (`distance_in_miles`,
    # `ave_speed_in_miles`) alongside their km counterparts in the SAME create()
    # vals dict, so the create-time miles inverses never see the field default
    # 0.0 (path 7's original bug - a zeroed `ave_speed`, #14279). That fix is
    # safe only because of a load-bearing key ORDER in that dict (see the
    # comment at waypoint_mixin.py): `distance`/`ave_speed` are seeded
    # BEFORE `distance_in_miles`/`ave_speed_in_miles`, so `route.segment.create()`'s
    # own inverse-firing order (`odoo/models.py` - `determine_inverses` groups
    # fields by inverse METHOD NAME, keyed by first-encountered field per
    # method in `vals.items()` insertion order) runs `_inverse_distance_ave_speed`
    # (the segment -> cache-row push) BEFORE `_inverse_distance_in_miles`/
    # `_inverse_ave_speed_in_miles` (the miles round-trip) - so the push
    # carries the TRUE, non-round-tripped value into the shared cache row,
    # and only the segment's OWN `distance`/`ave_speed` are left round-tripped
    # afterwards. `mile2km(km2mile(x)) == x` is NOT bit-identical for every x
    # (e.g. 123.456789 drifts ~1.4e-14, confirmed standalone with this
    # module's own 1.609344 constant) - it only happens to hold for a given
    # x by floating-point luck, never guaranteed by the design itself.
    # ------------------------------------------------------------------
    def test_segment_mirrors_section_distance_and_speed_bit_for_bit_no_unit_roundtrip_drift(self):
        """REGRESSION LOCK, not a red-before-green pin: expected GREEN on
        arrival (verified standalone below and via source trace - see this
        run's report). It exists to fail loudly the day either the seed-order
        fix (waypoint_mixin.py) or this particular value's round-trip
        luck stops holding.

        Business rule: a freshly created `route.segment` must mirror its
        `route.section`'s `distance`/`ave_speed` with NO drift introduced by
        the segment's own create-time miles round-trip
        (`_inverse_distance_in_miles`/`_inverse_ave_speed_in_miles`) -
        bit-for-bit, not merely "close enough": `assertAlmostEqual` would hide
        exactly the ULP drift this test exists to catch. Uses the Da Nang ->
        Hue pair's real haversine distance (~79.558 km, an irrational,
        non-round value - never a bare literal like 60/45/100, none of which
        exercise the round-trip's failure mode) so a future regression that
        reorders `_prepare_segment_vals_list`'s vals dict, or otherwise stops
        protecting the shared cache row from the miles round-trip, has
        somewhere to fail."""
        route = self._build_two_waypoint_route(self.address_danang, self.address_hue, 'Path-7 lock - bit-exact mirror')
        segment = route.segment_ids[:1]
        section = segment.route_section_id
        self.assertTrue(section, "the segment creation cascade must attach a route.section")

        self.assertGreater(
            section.distance, 0.0,
            "precondition failed: expected a real positive distance from the haversine cascade",
        )
        # Precondition: this is a genuinely NON-ROUND value (never a bare
        # fixture literal like 60/45/100) - the whole point of this test is to
        # exercise the round-trip on a value that is not a floating-point-luck
        # coincidence by construction.
        self.assertNotEqual(
            section.distance, round(section.distance, 3),
            "precondition failed: expected a non-round haversine distance for "
            "this test to mean anything - got a suspiciously round value",
        )

        configured = self._configured_speed(self.manual_driving_provider)
        section.invalidate_recordset(['distance', 'ave_speed'])
        segment.invalidate_recordset(['distance', 'ave_speed'])

        # THEN - bit-for-bit, plain `==`, never assertAlmostEqual: the whole
        # point is to catch ULP drift, which assertAlmostEqual(places=6) would
        # silently absorb.
        self.assertEqual(
            segment.distance, section.distance,
            (
                f"segment.distance ({segment.distance!r}) is not bit-identical "
                f"to section.distance ({section.distance!r}) - the create-time "
                "miles round-trip (_inverse_distance_in_miles) drifted the "
                "segment's own mirror away from the section's true value"
            ),
        )
        self.assertEqual(
            segment.ave_speed, configured,
            (
                f"segment.ave_speed ({segment.ave_speed!r}) is not bit-identical "
                f"to the configured speed ({configured!r}) - the create-time "
                "miles round-trip (_inverse_ave_speed_in_miles) drifted the "
                "segment's own mirror away from the administrator-configured "
                "speed"
            ),
        )
