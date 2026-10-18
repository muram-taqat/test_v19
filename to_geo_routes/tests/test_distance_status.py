from unittest.mock import patch

from odoo.tests import Form, TransactionCase, tagged

# The logger whose WARNINGs this file deliberately provokes.
#
# Viindoo Runbot grades a build by the HIGHEST log level present in it, not by the
# test tally, so every deliberate provocation here is bracketed by `assertLogs` at
# the point it is provoked. That both ASSERTS the diagnostic actually fired (these
# messages ARE the observable contract of the three-cause split) and, as a side
# effect of `propagate = False`, keeps it off the build log.
TRANSPORT_MODE_LOGGER = 'odoo.addons.to_geo_routes.models.route_section_transport_mode'

# The module's own self-heal domain (`route_section_transport_mode.py:255`). The
# authors listed BOTH `False` and `0.0` because, on a `fields.Float`, they are the
# same stored value - see the class docstring.
SELF_HEAL_DOMAIN = [('distance', 'in', [False, 0.0])]

# The literal the no-DISTANCE message must never contain. `_assert_degenerate_speed_reported`
# (test_route_section_cache.py:140-145, used at :1412) asserts its absence for the
# zero-time cause; the design carries the same constraint onto the new no-distance
# cause, so a coder who reuses the old wording reds an existing CONTROL test as well
# as this file.
NO_DATA_FOUND_LITERAL = 'No distance data found'


@tagged('post_install', '-at_install')
class TestDistanceStatus(TransactionCase):
    """Protects `route.section.transport.mode.distance_status` - the field that makes
    the user's question answerable.

    ## The question this file exists to answer

    Issue #14409: an operator sees a route section reading "0 km" and cannot tell
    whether the route WAS MEASURED AS ZERO or was NEVER MEASURED AT ALL. Those two
    states have opposite remedies (accept it / go fix the endpoint's coordinates)
    and, today, an identical observable.

    ## The trap every future editor of this file must know

    `distance` is `fields.Float` with NO `digits=` at all six declaration sites in
    this tree (`distance_mixin.py:12`, `route_section_mixin.py:18`,
    `route_section_transport_mode.py:68`, `route_mixin.py:62`, `route_segment.py:36`,
    `route_section.py:30`). Therefore:

      - `Float.convert_to_column` is `float(value or 0.0)` - writing `False` STORES `0.0`
      - `Float.convert_to_record` is `return value or 0.0` - a NULL column READS `0.0`

    *** ANY TEST ASSERTING `not row.distance` / `assertFalse(row.distance)` IS GREEN
        BEFORE AND AFTER THIS CHANGE AND PROVES NOTHING. ***

    An earlier draft of the design shipped exactly that test and an adversarial review
    killed it. The module's own code already admits the collapse: its self-heal domain
    is `('distance', 'in', [False, 0.0])` - the authors listed both values because they
    are the same value.

    This is the entire reason `distance_status` exists. Every assertion in this file
    about "was this measured" goes through `distance_status`, NEVER through the
    falsiness of `distance`. Where a test here needs two states to be different, it
    asserts they differ ON THE STATUS while asserting `distance` is EQUAL - that
    equality is the point, not an accident.

    **Do not "fix" a red in this file by asserting falsiness.** That converts a real
    failure into a test that cannot fail.

    ## The second fact this file rests on

    `route.section.transport.mode.distance` is a STORED COMPUTED field, driven by the
    haversine over its endpoints' coordinates. `distance.mixin` declares it plain,
    `route.section.mixin` adds `compute='_compute_distance'`, and the transport-mode
    redeclaration sets only `help=` - so compute, store and the coordinate `depends`
    carry down the `_inherit` chain (`Field._get_attrs` accumulates attributes across
    every definition in the MRO; verified against the live registry). Two consequences
    the tests below exploit and pin:

      - editing an endpoint partner's coordinates RE-MEASURES the row, which is how the
        headline test drives two rows to an identical `0.0` by different routes
      - a provider-written distance survives only until those dependencies move, so a
        `distance_status` that does not share `distance`'s lifecycle goes stale

    ## Relationship to the existing suite

    `test_route_section_cache.py` owns the CONTROL behaviours this work must not
    disturb (the zero-time message at its :1412, the degenerate-rows-stay-eligible
    oracle, and `test_user_edited_segment_speed_still_propagates_to_manual_cache_row`).
    This file neither imports nor subclasses that class - subclassing an Odoo
    `TestCase` re-runs its whole suite - and duplicates none of those tests. The two
    stub helpers below are deliberate re-implementations of the same idea, for the
    reason their originals document: `_update_missing_distances_ave_speed` is an
    `@api.model` sweep that SEARCHES the whole database, so a stub answering only the
    row under test makes the method report on the STUB's coverage instead of on the
    code under test. Every stub here answers the whole batch.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.to_base = cls.env['to.base']
        cls.manual_driving_provider = cls.env.ref('to_geo_routes.manual_input_driving')
        cls.manual_cycling_provider = cls.env.ref('to_geo_routes.manual_input_cycling')

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _make_partner(self, name, latitude=None, longitude=None):
        """Create a partner carrying ONLY the coordinate keys explicitly asked for.

        No address field is ever passed, and that is load-bearing: `to_geo_routes`
        depends on `base_geolocalize`, whose `write()` resets BOTH coordinates to
        (0,0) whenever `street`, `zip`, `city`, `state_id` or `country_id` is written
        without both coordinates in the same vals. Adding an address field here would
        let core silently overwrite the very coordinates under test.

        Omitting both coordinates stores SQL NULL - which reads back as `0.0`, the
        same value core itself writes to mean "not geolocated".
        """
        vals = {'name': name}
        if latitude is not None:
            vals['partner_latitude'] = latitude
        if longitude is not None:
            vals['partner_longitude'] = longitude
        return self.env['res.partner'].create(vals)

    def _build_section(self, address_from, address_to):
        """Create a `route.section` directly - the same shape `data/data_demo.xml`
        uses. `create` cascades into `_generate_route_section_transport_modes_if_not_exists`,
        which creates one cache row per `transport.mode.provider` and runs
        `_update_distance_ave_speed` over them."""
        return self.env['route.section'].create({
            'address_from_id': address_from.id,
            'address_to_id': address_to.id,
        })

    def _build_two_waypoint_route(self, address_from, address_to, name):
        """A `route.route` with two waypoints, which materialises one `route.segment`
        mirroring one `route.section`. Needed only by the segment-inverse test, which
        exercises the hand-edit path a `route.section` alone cannot reach."""
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

    def _cache_row(self, section, mode='driving'):
        return section.transport_mode_ids.filtered(
            lambda mode_record: mode_record.provider == 'manual' and mode_record.mode == mode
        )

    def _configured_speed(self, provider):
        """The administrator-configured speed round-tripped through the module's own
        km/h <-> m/s conversion - the exact value the normal creation path produces
        (60.00000000000001 for driving, never the bare literal 60)."""
        return self.to_base.ms_to_kmh(self.to_base.kmh_to_ms(provider.ave_speed))

    def _usable_speed_ms(self):
        return self.to_base.kmh_to_ms(self._configured_speed(self.manual_driving_provider))

    def _park_in_self_heal_domain(self, rows):
        """Put `rows` back into the self-heal domain using the module's OWN shape -
        `missing_records.write({'distance': False})`
        (`route_section_transport_mode.py:315`) and `remain.distance = False` (:238)
        both produce exactly this: distance blanked, prior speed untouched.

        Note what this does NOT assert: it does not `assertFalse(row.distance)`
        afterwards, because that assertion cannot fail. It asserts membership of the
        search domain instead, which is a real, falsifiable observable.
        """
        rows.write({'distance': False})
        parked = self.env['route.section.transport.mode'].search(SELF_HEAL_DOMAIN)
        for row in rows:
            self.assertIn(
                row.id, parked.ids,
                "precondition failed: the row must start inside the self-heal domain "
                "so the sweep actually selects it",
            )

    # ------------------------------------------------------------------
    # stubs - both answer the WHOLE batch, for the reason in the class docstring
    # ------------------------------------------------------------------
    def _stub_cache_row_matrix(self, rows_under_test, observation, usable_ave_speed_ms):
        """Stub `route.section.transport.mode._get_distance_matrix` - the MODEL-level
        collector - so `rows_under_test` receive `observation` and every other row the
        sweep hands over receives a usable one.

        Replacing the model-level method also replaces its coordinate PRE-FILTER
        (`route_section_transport_mode.py:154-159`), so every selected row is answered
        regardless of its endpoints. That is exactly what the no-distance and
        zero-time tests want: it removes any stray "no data" report coming from
        unrelated coordinate-less demo rows elsewhere in the database, leaving the
        emitted output attributable to the row under test alone.

        The coordinate test below needs the opposite and therefore stubs one layer
        deeper - see `_stub_provider_matrix`.
        """
        under_test_ids = set(rows_under_test.ids)

        def _matrix(records, res_model=None, res_id=None):
            return {
                row: dict(observation) if row.id in under_test_ids else {
                    'distance': 5000.0,
                    'ave_speed': usable_ave_speed_ms,
                }
                for row in records
            }

        return _matrix

    def _stub_provider_matrix(self, usable_ave_speed_ms):
        """Stub `transport.mode.provider._get_distance_matrix` - one layer BELOW the
        model-level collector - so that `route.section.transport.mode._get_distance_matrix`,
        including its coordinate pre-filter at `route_section_transport_mode.py:154-159`,
        genuinely executes.

        That pre-filter is the entire subject of the coordinate test: it drops a
        coordinate-less row BEFORE any provider is called, which is why attributing
        that row's failure to the provider is a misattribution. A stub that replaced
        the model-level method would replace the pre-filter too and the test would
        assert nothing about it.

        The provider contract is `{(from_partner, to_partner): {...}}`, so this
        answers every ordered pair of the partners it is handed - i.e. the whole
        batch, per the rule in the class docstring.
        """
        def _matrix(provider, partners, res_model=None, res_id=None):
            return {
                (origin, destination): {
                    'distance': 5000.0,
                    'duration': 5000.0 / usable_ave_speed_ms,
                    'ave_speed': usable_ave_speed_ms,
                }
                for origin in partners
                for destination in partners
                if origin != destination
            }

        return _matrix

    # ------------------------------------------------------------------
    # RED-B - a provider observation of distance == 0 is a no-DISTANCE cause,
    #         not a zero-time one
    # ------------------------------------------------------------------
    def test_provider_returning_zero_distance_is_not_reported_as_a_zero_time_observation(self):
        """Business rule: when a provider answers with NO DISTANCE, the operator must
        be told the provider returned no distance - not that it returned a distance in
        zero time. The two have different remedies, which is the whole reason the
        module already split "no data at all" from "zero-time"; this splits the third
        cause the existing split still collapses.

        Today `_is_usable_provider_observation` returns False for BOTH `distance == 0`
        and `duration/speed == 0`, and `_prepare_distance_vals_from_provider` omits
        `ave_speed` for both, so both callers collapse them into
        `'ave_speed' not in update_vals`. The row therefore joins `degenerate_records`
        and `route_section_transport_mode.py:331-339` reports it as "Distance data
        found but no usable speed (zero-time observation)" - a statement that is
        factually FALSE when the distance was zero.

        EXPECTED RED on the unmodified tree (not yet observed - this pass authors
        only): `assertNotIn('zero-time', ...)` fails on an ASSERTION FAILURE, because
        the zero-time message is emitted for a row whose distance was zero. The
        `distance_status` assertion below it would additionally raise AttributeError.

        Message constraints this test encodes, because CI reds otherwise:
          - the new no-distance message must NOT contain the literal
            "No distance data found" (that literal belongs to the no-data cause and
            `_assert_degenerate_speed_reported` asserts its absence for a row the
            provider DID answer)
          - it must NOT claim a zero-time observation
          - it must still SAY that no distance came back, so the operator learns the
            cause from the message rather than only from the field
        """
        origin = self._make_partner('RED-B origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('RED-B destination', latitude=16.4637, longitude=107.5909)
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        self._park_in_self_heal_domain(cache_row)

        # WHEN the provider answers this row with NO DISTANCE, while every sibling row
        # in the same database-wide batch gets a usable observation - so the sweep has
        # to single this row out rather than fail wholesale.
        with patch.object(
            type(cache_row), '_get_distance_matrix',
            self._stub_cache_row_matrix(
                cache_row, {'distance': 0.0, 'ave_speed': 0.0}, self._usable_speed_ms()),
        ), self.assertLogs(TRANSPORT_MODE_LOGGER, level='WARNING') as captured:
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        emitted = '\n'.join(captured.output)

        self.assertNotIn(
            NO_DATA_FOUND_LITERAL, emitted,
            "the provider DID answer this row - it answered with no distance - so the "
            "no-data-at-all wording must not be reused for it; that literal is also "
            "asserted absent by _assert_degenerate_speed_reported, so reusing it reds "
            f"an existing CONTROL test too. Got: {emitted}",
        )
        self.assertNotIn(
            'zero-time', emitted.lower(),
            "a provider observation whose DISTANCE was zero must not be reported as a "
            "zero-time observation - that claim is factually false and sends the "
            f"operator to chase the provider's timing instead of its distance. Got: {emitted}",
        )
        self.assertIn(
            'no distance', emitted.lower(),
            "the message must state the cause it found - that the provider returned no "
            f"distance - so the operator can act on the log alone. Got: {emitted}",
        )

        cache_row.invalidate_recordset(['distance', 'distance_status'])
        self.assertEqual(
            cache_row.distance_status, 'no_distance',
            "a provider observation of distance == 0 must be RECORDED as the "
            "no-distance cause, not left indistinguishable from every other failure",
        )

        # AND the row must stay eligible for repair. This is not a duplicate of the
        # existing eligibility CONTROL - it is the constraint that stops the obvious
        # implementation from breaking it. The design mitigates status drift by
        # emitting `distance_status` out of `_prepare_distance_vals_from_provider`,
        # but this caller DISCARDS that whole vals dict on the unusable branch
        # (`route_section_transport_mode.py:291-306`). A coder who "fixes" the status
        # by writing the returned vals dict would write `distance` along with it and
        # move the row permanently OUT of the self-heal domain - the exact poisoning
        # the existing ruling exists to prevent. The status must be recorded WITHOUT
        # the distance.
        self.assertIn(
            cache_row.id,
            self.env['route.section.transport.mode'].search(SELF_HEAL_DOMAIN).ids,
            f"the row's distance is now {cache_row.distance!r} - recording the "
            "no-distance status must not also persist the provider's distance, which "
            "would move the row out of the self-heal domain and end its eligibility "
            "for repair",
        )

    # ------------------------------------------------------------------
    # RED-C - a coordinate-less endpoint is an ENDPOINT problem, named by PARTNER
    # ------------------------------------------------------------------
    def test_coordinate_less_endpoint_is_reported_against_the_partner_not_the_provider(self):
        """Business rule: when a section cannot be measured because one of its
        endpoints has no coordinates, the operator must be sent to the ENDPOINT. The
        provider never saw this row and cannot be the party at fault.

        THE MECHANISM, and it is a regression the coordinate fix itself manufactures:
        `route.section.transport.mode._get_distance_matrix` pre-filters rows through
        `_validate_coordinates(raise_if_not_valid=False)` BEFORE any provider call
        (`route_section_transport_mode.py:154-159`). Once the presence predicate
        tightens, a coordinate-less row is dropped there, never reaches the provider,
        `record_data` is falsy at :284-286, it never joins `degenerate_records`, and it
        lands in `no_data_records` - reported as "No distance data found ... provider Y",
        naming the provider for a failure the provider never saw. That is the SAME
        misattribution the three-cause split exists to remove, newly created by the
        presence fix.

        This test stubs the PROVIDER, not the model-level collector, precisely so that
        pre-filter really runs - see `_stub_provider_matrix`.

        EXPECTED RED (not yet observed - this pass authors only). The presence
        predicate has ALREADY landed, so the live mechanism is the strong one:
        the coordinate-less row is dropped by the pre-filter, never reaches the
        provider, and is reported as "No distance data found ... provider Y" - naming
        the provider. `lines_naming_the_partner` is therefore EMPTY and
        `assertTrue(lines_naming_the_partner)` fails on a genuine ASSERTION FAILURE.
        That is the misattribution this test exists to forbid. The `distance_status`
        assertion below would additionally raise AttributeError.

        (Recorded for completeness: before the presence predicate landed, this row
        passed the pre-filter and resolved silently, so nothing was reported about it
        at all - the weaker red the design records as "it silently computes 0 km".
        That state no longer exists on this tree.)

        The assertions are scoped to THIS partner by name rather than to a message
        count, so unrelated coordinate-less demo rows elsewhere in the database cannot
        make the test pass or fail for the wrong reason.
        """
        origin = self._make_partner('RED-C origin', latitude=21.010458, longitude=105.787416)
        unlocated = self._make_partner('RED-C endpoint never geolocated')
        section = self._build_section(origin, unlocated)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        self._park_in_self_heal_domain(cache_row)

        with patch.object(
            type(self.manual_driving_provider), '_get_distance_matrix',
            self._stub_provider_matrix(self._usable_speed_ms()),
        ), self.assertLogs(TRANSPORT_MODE_LOGGER, level='WARNING') as captured:
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        emitted = '\n'.join(captured.output)
        lines_naming_the_partner = [
            line for line in captured.output if unlocated.display_name in line
        ]

        self.assertTrue(
            lines_naming_the_partner,
            "the endpoint is the party at fault, so the report must NAME THE PARTNER - "
            "an operator reading a message that names only a provider and a count has "
            "no way to find which address to fix. Got: %s" % emitted,
        )
        for line in lines_naming_the_partner:
            self.assertNotIn(
                NO_DATA_FOUND_LITERAL, line,
                "this row never reached the provider - the pre-filter dropped it - so "
                "its failure must not be reported with the provider's no-data wording. "
                f"Got: {line}",
            )

        cache_row.invalidate_recordset(['distance', 'distance_status'])
        self.assertEqual(
            cache_row.distance_status, 'no_coordinates',
            "a row dropped for a coordinate-less endpoint must RECORD that cause. Note "
            "the precedence this pins: a known, specific cause must never be "
            "downgraded to the generic default by a later write in the same sweep",
        )

    # ------------------------------------------------------------------
    # RED-D - THE HEADLINE. The user's contract, made observable.
    # ------------------------------------------------------------------
    def test_measured_as_zero_is_distinguishable_from_never_measured(self):
        """*** THE TEST THIS WORK-ITEM EXISTS FOR. ***

        Business rule, in the user's own words: "was this route measured as zero, or
        never measured at all?" Two sections both read a distance of exactly 0.0. One
        was genuinely measured - its two endpoints are distinct records at the SAME
        coordinates, two berths of one terminal, and the great-circle distance between
        them really is zero. The other was never measured at all - one endpoint has no
        coordinates. The operator must be able to tell them apart.

        WHY THIS CANNOT BE ASSERTED ON `distance`: both rows hold `0.0`, byte for byte.
        `distance` is a `fields.Float`, so `False` and `0.0` are one value and no
        attribute read and no search domain can separate them (see the class
        docstring). The withdrawn first draft of this work asserted
        `assertFalse(section.distance)` and was green before and after. THIS test makes
        the one assertion that draft could never make: the two rows carry an EQUAL
        distance and a DIFFERENT status.

        Both rows are driven to their final state by a coordinate edit on the endpoint
        partner, which dirties `_compute_distance`'s dependencies and makes the compute
        path - the path the design assigns the `measured` / `no_coordinates` decision
        to - unambiguously the LAST writer. Creating the sections in their final shape
        instead would leave the create-time provider cascade as a competing writer, and
        the two paths do not agree about a zero-length measurement (see the report
        accompanying these tests).

        EXPECTED RED on the unmodified tree (not yet observed - this pass authors
        only): `route.section.transport.mode.distance_status` DOES NOT EXIST, so the
        first read of it raises **AttributeError**, not an assertion failure. That is
        exactly the mechanism the design named for this test, and it is stated plainly
        rather than dressed up as an oracle mismatch. The status reads are placed
        FIRST, before the distance preconditions, so that this is the failure the
        transcript shows.
        """
        terminal = self._make_partner('RED-D terminal', latitude=20.861135, longitude=106.672549)
        berth = self._make_partner('RED-D berth of the same terminal', latitude=16.0544, longitude=108.2022)
        measured_section = self._build_section(terminal, berth)
        measured_row = self._cache_row(measured_section)

        origin = self._make_partner('RED-D origin', latitude=21.010458, longitude=105.787416)
        losing_coordinates = self._make_partner(
            'RED-D endpoint that loses its coordinates', latitude=20.9522972, longitude=107.0781387)
        unmeasured_section = self._build_section(origin, losing_coordinates)
        unmeasured_row = self._cache_row(unmeasured_section)

        self.assertTrue(measured_row and unmeasured_row, "precondition failed: expected both cache rows")
        for row in (measured_row, unmeasured_row):
            row.invalidate_recordset(['distance'])
            self.assertGreater(
                row.distance, 0.0,
                "precondition failed: expected the create-time cascade to have resolved "
                "a real distance before the endpoints move",
            )

        # WHEN the berth turns out to sit at the terminal's own coordinates - a
        # genuine measurement whose answer happens to be zero...
        berth.write({
            'partner_latitude': terminal.partner_latitude,
            'partner_longitude': terminal.partner_longitude,
        })
        # ...and the other section's endpoint turns out to have no coordinates at all.
        # (0,0) is not "the Atlantic" here: it is the marker core itself writes to mean
        # "not geolocated", and it is what a coordinate-less partner reads back as.
        losing_coordinates.write({'partner_latitude': 0.0, 'partner_longitude': 0.0})
        self.env.flush_all()
        measured_row.invalidate_recordset(['distance', 'distance_status'])
        unmeasured_row.invalidate_recordset(['distance', 'distance_status'])

        # Read the statuses FIRST - on the unmodified tree this is the AttributeError
        # that is this test's expected red.
        measured_status = measured_row.distance_status
        unmeasured_status = unmeasured_row.distance_status

        self.assertEqual(
            measured_row.distance, 0.0,
            "precondition failed: moving the endpoint onto the origin did not re-measure "
            "the cache row, so `route.section.transport.mode.distance` is not tracking "
            "its endpoints' coordinates and the compute path this test drives is not "
            "wired as assumed - fix that before reading anything into the status below",
        )
        self.assertEqual(
            unmeasured_row.distance, 0.0,
            "precondition failed: clearing the endpoint's coordinates did not blank the "
            "cache row's distance - the presence predicate is not being consulted on "
            "the compute path",
        )

        # THE ASSERTION THE WITHDRAWN DRAFT COULD NEVER MAKE.
        self.assertEqual(
            measured_row.distance, unmeasured_row.distance,
            "these two rows MUST hold the same distance - that is the premise of the "
            "whole work item. If they ever differ, this test has stopped exercising the "
            "ambiguity it exists to resolve and must be repaired, not relaxed",
        )
        self.assertNotEqual(
            measured_status, unmeasured_status,
            "two rows holding an identical distance of 0.0 - one measured as zero, one "
            "never measured - must be DISTINGUISHABLE. This single assertion is the "
            "user's contract from issue #14409; on `distance` alone it is unanswerable",
        )
        self.assertEqual(
            measured_status, 'measured',
            "two distinct endpoints at the same coordinates were genuinely measured, "
            "and the answer was zero - that is a measurement, not a failure",
        )
        self.assertEqual(
            unmeasured_status, 'no_coordinates',
            "an endpoint without coordinates was never measured at all, and the record "
            "must say so instead of presenting an unmeasured row as a zero-length one",
        )

    # ------------------------------------------------------------------
    # RED-E - the zero-time cause keeps its message AND gains its status
    # ------------------------------------------------------------------
    def test_positive_distance_with_zero_time_still_reports_the_zero_time_cause(self):
        """Business rule: splitting out the two new causes must not blur the one the
        module already reports correctly. A provider that answers with a POSITIVE
        distance in zero time is still a zero-time observation, must still say so in
        the same shape, and must now also record that cause on the row.

        The existing message shape is a hard constraint: `_assert_degenerate_speed_reported`
        (test_route_section_cache.py:121-144, used at :1412) asserts both the
        `"for %s records"` shape and the absence of the no-data literal. A coder who
        rewords this message to make room for the new causes reds that CONTROL.

        This does NOT duplicate that CONTROL. The CONTROL's subject is that the row
        stays eligible for repair and retains its prior speed; this test's subject is
        the new `distance_status` value, with the message shape asserted only as the
        preservation constraint, and with eligibility re-asserted for the specific
        reason given at the end.

        EXPECTED RED on the unmodified tree (not yet observed - this pass authors
        only): the message assertions PASS (this cause is already reported correctly);
        the `distance_status` read raises **AttributeError** because the field does not
        exist. This test is red purely on the new field, which is what makes it a clean
        specification of W6 rather than a restatement of W5.
        """
        origin = self._make_partner('RED-E origin', latitude=16.8163, longitude=107.1005)
        destination = self._make_partner('RED-E destination', latitude=16.4637, longitude=107.5909)
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        self._park_in_self_heal_domain(cache_row)

        with patch.object(
            type(cache_row), '_get_distance_matrix',
            self._stub_cache_row_matrix(
                cache_row, {'distance': 5000.0, 'ave_speed': 0.0}, self._usable_speed_ms()),
        ), self.assertLogs(TRANSPORT_MODE_LOGGER, level='WARNING') as captured:
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        emitted = '\n'.join(captured.output)

        self.assertIn(
            'zero-time', emitted.lower(),
            "a positive distance in zero time IS a zero-time observation and must keep "
            f"being reported as one. Got: {emitted}",
        )
        self.assertIn(
            "for 1 records", emitted,
            "the zero-time message must keep its `for %s records` shape - "
            "_assert_degenerate_speed_reported asserts exactly this and reds if the "
            f"wording is restructured. Got: {emitted}",
        )
        self.assertNotIn(
            NO_DATA_FOUND_LITERAL, emitted,
            "the provider DID return a distance for this row, so the no-data wording "
            f"must not appear. Got: {emitted}",
        )

        cache_row.invalidate_recordset(['distance', 'distance_status'])
        self.assertEqual(
            cache_row.distance_status, 'no_time',
            "the zero-time cause must now be RECORDED on the row, not only logged - a "
            "log line ages out of the build, the row does not",
        )
        self.assertIn(
            cache_row.id,
            self.env['route.section.transport.mode'].search(SELF_HEAL_DOMAIN).ids,
            f"the row's distance is now {cache_row.distance!r} - recording the zero-time "
            "status must not also persist the provider's distance; doing so would move "
            "the row out of the self-heal domain, which is exactly the poisoning the "
            "existing CONTROL forbids",
        )

    # ------------------------------------------------------------------
    # W6-3 - distance and status must share ONE lifecycle
    # ------------------------------------------------------------------
    def test_recomputing_distance_must_not_leave_a_stale_measured_status(self):
        """Business rule: a row must never claim to have been measured once the reason
        it holds its distance has gone away.

        THE REGISTRY FACT THIS TEST RESTS ON (verified against the live registry, and
        absent from the design): `route.section.transport.mode.distance` is a STORED
        COMPUTED field. `distance.mixin` declares it plain, `route.section.mixin` adds
        `compute='_compute_distance'`, and the transport-mode redeclaration
        (`route_section_transport_mode.py:68`) sets only `help=` - so compute, store and
        the coordinate `depends` all carry down the `_inherit` chain unchanged. Its
        profile is compute + readonly + NO inverse, the very profile this module's own
        `distance_mixin.py:115-118` documents for `duration`: a value written ALONE
        survives only BY ACCIDENT, and is reverted the moment the compute's dependencies
        move.

        CONSEQUENCE: every provider-written distance in this module is standing on that
        accident, and it collapses as soon as an endpoint partner's coordinates change -
        which the sibling data work is about to do to 56 seaports at once. If
        `distance_status` is a plain stored field written only at the write paths, the
        recompute moves `distance` while the status stays put, and the row ends up
        reporting a genuine zero-length measurement for a section whose endpoint has no
        coordinates at all. That is the "a status that drifts out of step with the
        distance is worse than no status" failure the design names, and it is a
        user-facing lie of exactly the kind this work item exists to remove.

        This test therefore pins the OUTCOME - the two fields describe the same reality
        after a recompute - and deliberately does not pin HOW. Giving them a shared
        lifecycle (one compute writing both) is the obvious way; any other way that
        keeps them in step is equally acceptable.

        EXPECTED RED on the unmodified tree (not yet observed - this pass authors
        only): **AttributeError** - the field does not exist. After a plain stored
        `distance_status` written only at the write paths, it reds on an ASSERTION
        FAILURE reporting a stale 'measured' against a blanked distance - which is the
        real defect this test is here to catch.
        """
        origin = self._make_partner('W6-3 origin', latitude=16.0544, longitude=108.2022)
        endpoint = self._make_partner('W6-3 endpoint', latitude=16.4637, longitude=107.5909)
        section = self._build_section(origin, endpoint)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        # GIVEN a stored, usable provider result on the row.
        self._park_in_self_heal_domain(cache_row)
        with patch.object(
            type(cache_row), '_get_distance_matrix',
            self._stub_cache_row_matrix(
                self.env['route.section.transport.mode'], {}, self._usable_speed_ms()),
        ), self.assertNoLogs(TRANSPORT_MODE_LOGGER, level='WARNING'):
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed()

        cache_row.invalidate_recordset(['distance', 'distance_status'])
        self.assertGreater(
            cache_row.distance, 0.0,
            "precondition failed: the sweep should have stored a usable provider distance",
        )
        self.assertEqual(
            cache_row.distance_status, 'measured',
            "precondition failed: a usable provider observation must record a measurement",
        )

        # WHEN the endpoint turns out to have no coordinates, which dirties
        # `_compute_distance`'s dependencies and reverts the stored provider distance.
        endpoint.write({'partner_latitude': 0.0, 'partner_longitude': 0.0})
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertEqual(
            cache_row.distance, 0.0,
            "precondition failed: clearing the endpoint's coordinates did not revert the "
            "stored provider distance - the stored-computed behaviour this test is "
            "built on is not present, so read nothing into the status below",
        )
        self.assertEqual(
            cache_row.distance_status, 'no_coordinates',
            "the recompute took the distance away, so the status must move with it. A "
            "row left reading 'measured' beside a blanked distance tells the operator "
            "this section was genuinely measured at zero length when in truth its "
            "endpoint has no coordinates - the precise confusion this field exists to "
            "end, reintroduced by a status that does not share the distance's lifecycle",
        )

    # ------------------------------------------------------------------
    # W6-4 - the FOURTH distance-write path (the design covers three)
    # ------------------------------------------------------------------
    def test_hand_entered_segment_distance_leaves_the_cache_row_measured(self):
        """Business rule: a distance a human typed in is a genuine measurement, and the
        row must say so.

        WHY THIS TEST EXISTS: the design enumerates three paths that write `distance`
        (`_compute_distance`, `_update_distance_ave_speed`,
        `_update_missing_distances_ave_speed`) and mitigates status drift by emitting
        the status from the single provider-side boundary converter. There is a
        FOURTH. `route.segment._inverse_distance_ave_speed` (`route_segment.py:139-175`)
        writes `{'distance': r.distance}` straight onto the manual cache row whenever a
        user hand-edits a segment on the Route form's Segments tab - a documented,
        supported override (`doc/index.rst:26`). It goes through neither the converter
        nor the compute.

        If that path does not set the status, the status drifts out of step with the
        distance on precisely the path the CONTROL test
        `test_user_edited_segment_speed_still_propagates_to_manual_cache_row` protects -
        and the design itself states that a status which drifts out of step with the
        distance is worse than no status at all.

        This does not duplicate that CONTROL: the CONTROL asserts the SPEED still
        propagates, this asserts the STATUS accompanies a propagated DISTANCE.

        EXPECTED RED on the unmodified tree (not yet observed - this pass authors
        only): **AttributeError** - the field does not exist. After the field exists but
        before this path is wired, it reds on an ASSERTION FAILURE showing whatever
        stale status the row kept from its last provider or compute write.
        """
        origin = self._make_partner('W6-4 origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('W6-4 destination', latitude=16.4637, longitude=107.5909)
        route = self._build_two_waypoint_route(origin, destination, 'W6-4 - hand-entered distance')
        segment = route.segment_ids[:1]
        self.assertTrue(segment, "precondition failed: expected one segment for a two-waypoint route")
        cache_row = self._cache_row(segment.route_section_id)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        # WHEN an operator types a distance they measured themselves onto the segment.
        segment.distance = 123.0
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertAlmostEqual(
            cache_row.distance, 123.0, places=6,
            msg="precondition failed: the hand-entered distance did not reach the manual "
                f"cache row (got {cache_row.distance}), so the write path this test is "
                "about did not run",
        )
        self.assertEqual(
            cache_row.distance_status, 'measured',
            "a hand-entered distance is a genuine measurement. Leaving a stale failure "
            "status on a row that now carries a real, human-supplied distance is the "
            "drift the design warns is worse than having no status at all",
        )

    # ------------------------------------------------------------------
    # W6-5 - the section-level mirror must follow SELECTION, not ORDERING
    # ------------------------------------------------------------------
    def test_section_distance_and_status_describe_the_same_transport_mode_row(self):
        """Business rule: the distance a section shows and the status explaining it must
        describe the SAME underlying measurement. A section that reports a real distance
        while its status explains a different row's failure is worse than no status.

        WHY A PLAIN `related=` CANNOT WORK: `route.section.distance` is not the first
        transport mode's distance. `_compute_distance` (`route_section.py:105-117`)
        narrows to the rows matching the requested transport mode, then selects the
        first of THOSE with a truthy distance
        (`_get_active_route_section_transport_mode_candidates`). A
        `related='transport_mode_ids.distance_status'` resolves to
        `transport_mode_ids[0]` - the lowest `sequence` - which is frequently a
        different row entirely.

        This test forces exactly that divergence: the cycling provider is re-sequenced
        so its row sorts FIRST, and it is given a zero-time observation while the
        driving row - the one the section's own selection logic picks, because the
        section is read in the default driving context - is given a usable one. A naive
        related would report the cycling row's failure alongside the driving row's
        distance.

        EXPECTED RED on the unmodified tree (not yet observed - this pass authors
        only): **AttributeError** - neither `route.section.distance_status` nor
        `route.section.transport.mode.distance_status` exists. After a naive
        `related=` is written it reds on an ASSERTION FAILURE, reporting the cycling
        row's status where the driving row's was required.
        """
        origin = self._make_partner('W6-5 origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('W6-5 destination', latitude=16.8163, longitude=107.1005)
        section = self._build_section(origin, destination)

        # GIVEN the cycling row sorts FIRST while the driving row is the one the
        # section's selection logic will pick. Without this the two coincide - driving
        # ships the lowest sequence - and the test would pass vacuously against a naive
        # related.
        self.manual_cycling_provider.sequence = 1
        self.env.flush_all()
        section.invalidate_recordset(['transport_mode_ids'])

        driving_row = self._cache_row(section, mode='driving')
        cycling_row = self._cache_row(section, mode='cycling')
        self.assertTrue(driving_row and cycling_row, "precondition failed: expected both cache rows")
        self.assertEqual(
            section.transport_mode_ids[:1], cycling_row,
            "precondition failed: the cycling row must sort FIRST for this test to "
            "distinguish a selection-aware mirror from a naive related - re-sequencing "
            "did not take effect",
        )

        self._park_in_self_heal_domain(driving_row | cycling_row)

        # WHEN the sweep resolves the driving row usably and the cycling row
        # degenerately, so the two rows genuinely end up in different states.
        with patch.object(
            type(driving_row), '_get_distance_matrix',
            self._stub_cache_row_matrix(
                cycling_row, {'distance': 5000.0, 'ave_speed': 0.0}, self._usable_speed_ms()),
        ), self.assertLogs(TRANSPORT_MODE_LOGGER, level='WARNING'):
            self.env['route.section.transport.mode']._update_missing_distances_ave_speed(
                transport_mode_providers=self.manual_cycling_provider,
            )

        section.invalidate_recordset(['distance', 'distance_status'])
        driving_row.invalidate_recordset(['distance', 'distance_status'])
        cycling_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertNotEqual(
            cycling_row.distance_status, driving_row.distance_status,
            "precondition failed: the two rows must end in DIFFERENT states, otherwise "
            "this test cannot tell a selection-aware mirror from a naive related and "
            "would pass vacuously",
        )
        self.assertGreater(
            driving_row.distance, 0.0,
            "precondition failed: the driving row should have resolved a real distance",
        )
        self.assertAlmostEqual(
            section.distance, driving_row.distance, places=6,
            msg="precondition failed: the section must be projecting the driving row's "
                "distance for the mirror assertion below to mean anything",
        )
        self.assertEqual(
            section.distance_status, driving_row.distance_status,
            "the section reports the DRIVING row's distance, so it must report the "
            "DRIVING row's status. Reporting the first-ordered row's status instead "
            "describes a measurement the section is not showing - a status that "
            "explains the wrong row is worse than no status",
        )
