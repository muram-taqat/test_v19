from odoo.tests import Form, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDistanceStatusBareWrite(TransactionCase):
    """Protects the ONE rule a bare ``write({'distance': ...})`` must obey on
    ``route.section.transport.mode`` (Viindoo/tvtmaaddons#14409 follow-up).

    ## The defect this file exists to catch

    Every writer this module already owns records ``distance_status`` alongside
    ``distance`` in the SAME call: the provider paths write
    ``dict(update_vals, distance_status='measured')``
    (``route_section_transport_mode.py:313,416``), the human-entry path writes
    ``update_data['distance_status'] = 'measured'`` (``route_segment.py`` R3), and
    the blanking paths write a falsy distance and then a determined cause (R2,
    ``_mark_generic_distance_status``). ``distance_status`` is a STORED COMPUTED
    field (``route_section_transport_mode.py:59-68``) whose ``@api.depends`` lists
    only the four endpoint-coordinate fields, NEVER ``distance`` - so a BARE write
    of ``distance`` (no ``distance_status`` key in the same vals) triggers no
    recompute at all and leaves the row's status exactly where it was.

    This is not hypothetical: a sibling module's migration restores
    provably-human distances with exactly this bare shape, on databases whose
    seaports sat at ``(0,0)`` before the restore:

    ```
    T0_created            distance=0.0      status=no_coordinates
    T1_after_coord_repair distance=0.0      status=no_coordinates
    T2_after_restore      distance=123.0    status=no_coordinates
    ```

    An operator reading that row afterwards sees a real, positive distance
    beside a status that DENIES any measurement was ever made.

    ## The rule under test

    A bare write of a POSITIVE distance must record ``measured`` - a distance
    the caller supplies without qualification is being asserted as real, exactly
    like every other writer in this module already treats it. A bare write of a
    FALSY distance (``False`` or ``0.0`` - the SAME stored value on a
    ``fields.Float`` with no ``digits=``, see ``test_distance_status.py``'s class
    docstring) must change NOTHING, because that is precisely the shape
    ``_update_missing_distances_ave_speed``'s ``missing_records.write({'distance':
    False})`` and ``_update_distance_ave_speed``'s ``remain.distance = False`` use
    to blank a row AFTER a determined cause (R2, ``DETERMINED_DISTANCE_STATUS_CAUSES``)
    has already been recorded on it - a fix that reacted to the falsy write would
    stomp that cause and re-break the three-cause split #14409 built.

    ## The trap this file shares with test_distance_status.py

    ``distance`` is a ``fields.Float`` with no ``digits=`` anywhere in this tree,
    so ``False`` and ``0.0`` are ONE stored value (``Float.convert_to_column`` is
    ``float(value or 0.0)``, ``Float.convert_to_record`` is ``value or 0.0``).
    Every "was this measured" assertion below goes through ``distance_status``,
    never through the falsiness of ``distance``.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

    # ------------------------------------------------------------------
    # fixtures - same shape test_distance_status.py uses
    # ------------------------------------------------------------------
    def _make_partner(self, name, latitude=None, longitude=None):
        """Create a partner carrying ONLY the coordinate keys explicitly asked
        for. No address field is ever passed - `to_geo_routes` depends on
        `base_geolocalize`, whose `write()` resets both coordinates to (0,0)
        whenever an address field is written without both coordinates in the
        same vals, which would silently overwrite the coordinates under test.
        Omitting both coordinates stores SQL NULL, which reads back as `0.0` -
        the same value core itself writes to mean "not geolocated".
        """
        vals = {'name': name}
        if latitude is not None:
            vals['partner_latitude'] = latitude
        if longitude is not None:
            vals['partner_longitude'] = longitude
        return self.env['res.partner'].create(vals)

    def _build_section(self, address_from, address_to):
        """Create a `route.section` directly - `create` cascades into
        `_generate_route_section_transport_modes_if_not_exists`, which creates
        one cache row per `transport.mode.provider` and runs
        `_update_distance_ave_speed` over them."""
        return self.env['route.section'].create({
            'address_from_id': address_from.id,
            'address_to_id': address_to.id,
        })

    def _build_two_waypoint_route(self, address_from, address_to, name):
        """A `route.route` with two waypoints, which materialises one
        `route.segment` mirroring one `route.section`. Needed only by the R3
        control, which exercises the hand-edit path a bare `route.section`
        write cannot reach."""
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

    # ------------------------------------------------------------------
    # scenario A - unlocated endpoints, bare positive-distance write
    # ------------------------------------------------------------------
    def test_bare_write_of_positive_distance_on_unlocated_endpoints_records_measured(self):
        """A bare `write({'distance': 123.0})` on a row whose endpoints have no
        coordinates - the exact shape the sibling migration's restore uses on a
        seaport that started at (0,0) - must record `measured`. Leaving the row
        at `no_coordinates` beside a real, positive distance tells the operator
        the row was never measured when in fact a value was just written to it,
        which is the same user-facing lie #14409 exists to end.

        EXPECTED RED on the unmodified tree: `distance_status` is a stored
        computed field whose depends never include `distance`, so this bare
        write triggers no recompute and the row keeps `no_coordinates`.
        """
        origin = self._make_partner('bare-write A origin')
        destination = self._make_partner('bare-write A destination')
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        cache_row.invalidate_recordset(['distance', 'distance_status'])
        self.assertEqual(
            cache_row.distance_status, 'no_coordinates',
            "precondition failed: a freshly created row with two unlocated endpoints "
            "must start unmeasured, or this test proves nothing about the write path",
        )

        # WHEN a caller bare-writes a positive distance, with no distance_status
        # key in the vals - the exact shape the sibling migration's restore uses.
        cache_row.write({'distance': 123.0})
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertEqual(
            cache_row.distance, 123.0,
            "precondition failed: the bare write did not persist the distance at all, "
            "so the status assertion below would be testing nothing",
        )
        self.assertEqual(
            cache_row.distance_status, 'measured',
            "the row now reads a real, positive distance of 123.0 km beside a status "
            "that denies any measurement was ever made - an operator has no way to "
            "learn from the record alone that this row was just written to",
        )

    # ------------------------------------------------------------------
    # scenario B - valid endpoints, stale cause overwritten by a bare write
    # ------------------------------------------------------------------
    def test_bare_write_of_positive_distance_overwrites_a_stale_recorded_cause(self):
        """A bare `write({'distance': 456.0})` on a row that currently carries a
        STALE failure cause (forced here exactly the way the provider sweeps
        record one - a direct `distance_status` write) must overwrite that cause
        with `measured`. Leaving the stale cause in place tells the operator the
        row failed when it in fact now carries a fresh, positive distance.

        EXPECTED RED on the unmodified tree: same mechanism as scenario A - the
        bare write never touches `distance_status`, so the row keeps whatever
        stale cause it was forced to.
        """
        origin = self._make_partner('bare-write B origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('bare-write B destination', latitude=16.4637, longitude=107.5909)
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        cache_row.invalidate_recordset(['distance', 'distance_status'])
        self.assertEqual(
            cache_row.distance_status, 'measured',
            "precondition failed: the create-time cascade should have measured this "
            "row over two valid, distinct endpoints",
        )

        # GIVEN the row is forced to a stale cause exactly the way the provider
        # sweeps record one - a direct field write, legitimate per those sweeps.
        cache_row.distance_status = 'no_distance'
        self.env.flush_all()

        # WHEN a caller bare-writes a fresh positive distance, with no
        # distance_status key in the vals.
        cache_row.write({'distance': 456.0})
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertEqual(
            cache_row.distance, 456.0,
            "precondition failed: the bare write did not persist the distance at all, "
            "so the status assertion below would be testing nothing",
        )
        self.assertEqual(
            cache_row.distance_status, 'measured',
            "the row now reads a real, positive distance of 456.0 km beside a stale "
            "'no_distance' cause left over from before the write - an operator would "
            "be told this row failed to measure when it plainly did not",
        )

    # ------------------------------------------------------------------
    # scenario C / D - R2 GUARD: a bare FALSY write must change nothing
    # ------------------------------------------------------------------
    def test_bare_write_of_false_distance_leaves_a_determined_cause_unchanged_r2_guard(self):
        """R2 GUARD. `_update_missing_distances_ave_speed`'s
        `missing_records.write({'distance': False})` (`route_section_transport_mode.py:315`)
        blanks a row's distance AFTER a determined cause is already recorded on
        it. A fix that reacts to a bare FALSY write by touching `distance_status`
        would stomp that already-recorded cause and re-break the three-cause
        split #14409 built - so this must stay green both today and after any
        fix for scenarios A/B.
        """
        origin = self._make_partner('bare-write C origin')
        destination = self._make_partner('bare-write C destination')
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        # GIVEN the row carries a determined cause - forced directly, exactly as
        # the sweeps that own DETERMINED_DISTANCE_STATUS_CAUSES record one.
        cache_row.distance_status = 'no_time'
        self.env.flush_all()

        # WHEN a caller bare-writes a FALSY distance - the R2 blanking shape.
        cache_row.write({'distance': False})
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertEqual(
            cache_row.distance_status, 'no_time',
            "a bare write of a FALSY distance must never touch distance_status - "
            "this is the exact shape _update_missing_distances_ave_speed uses to "
            "blank a row AFTER recording why it failed, and overwriting that "
            "recorded cause here would throw away the only thing an operator can "
            "act on and re-break the three-cause split #14409 built",
        )

    def test_bare_write_of_zero_distance_leaves_a_determined_cause_unchanged_r2_guard(self):
        """R2 GUARD, `0.0` variant. `_update_distance_ave_speed`'s
        `remain.distance = False` stores the SAME value as an explicit `0.0`
        write on a `fields.Float` with no `digits=` - see the class docstring.
        A fix keyed on "distance is falsy" must treat `0.0` identically to
        `False`, or it silently reintroduces the R2 poisoning on whichever of
        the two spellings it missed.
        """
        origin = self._make_partner('bare-write D origin')
        destination = self._make_partner('bare-write D destination')
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        cache_row.distance_status = 'no_distance'
        self.env.flush_all()

        # WHEN a caller bare-writes 0.0 - the literal spelling, not `False`.
        cache_row.write({'distance': 0.0})
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertEqual(
            cache_row.distance_status, 'no_distance',
            "a bare write of 0.0 stores the identical value `False` does on this "
            "field, and must be treated identically: touching distance_status here "
            "would overwrite an already-recorded cause with a status the caller "
            "never asked for, on a value indistinguishable from the R2 blanking "
            "shape this guard protects",
        )

    # ------------------------------------------------------------------
    # scenario E - an explicit distance_status in the SAME write wins
    # ------------------------------------------------------------------
    def test_explicit_distance_status_in_the_same_write_wins_over_inference_over_correction_guard(self):
        """Every provider path already passes its own `distance_status`
        alongside `distance` in one call (`dict(update_vals,
        distance_status='measured')`, and the two blanking sweeps' own
        `no_distance` / `no_time` writes). A fix for the bare-write defect must
        never override a value the caller explicitly supplied - inferring
        `measured` from a positive `distance` when the caller already said
        `no_time` would silently corrupt every one of those existing callers.
        """
        origin = self._make_partner('bare-write E origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('bare-write E destination', latitude=16.4637, longitude=107.5909)
        section = self._build_section(origin, destination)
        cache_row = self._cache_row(section)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        # WHEN a caller writes a POSITIVE distance and explicitly names a
        # non-'measured' cause in the very same call.
        cache_row.write({'distance': 5.0, 'distance_status': 'no_time'})
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertEqual(
            cache_row.distance, 5.0,
            "precondition failed: the write did not persist the distance at all, so "
            "the status assertion below would be testing nothing",
        )
        self.assertEqual(
            cache_row.distance_status, 'no_time',
            "the caller explicitly named 'no_time' in the same write that supplied "
            "the distance - an inference layer that overrides this to 'measured' "
            "because the distance happens to be positive would silently corrupt "
            "every existing provider and sweep caller, which all pass their own "
            "distance_status this same way",
        )

    # ------------------------------------------------------------------
    # R3 control - the existing human-entry path must still end at 'measured'
    # ------------------------------------------------------------------
    def test_hand_entered_segment_distance_still_ends_at_measured(self):
        """Control: `route.segment._inverse_distance_ave_speed` (R3) already
        writes `update_data['distance_status'] = 'measured'` alongside the
        distance whenever a user hand-edits a segment on the Route form's
        Segments tab - a documented, supported override (`doc/index.rst:26`).
        This drives that path, not a bare write, so a fix aimed at scenarios
        A/B must not regress it: R3 already writes both keys in one call and
        needs no inference to begin with.
        """
        origin = self._make_partner('bare-write R3 origin', latitude=16.0544, longitude=108.2022)
        destination = self._make_partner('bare-write R3 destination', latitude=16.4637, longitude=107.5909)
        route = self._build_two_waypoint_route(origin, destination, 'bare-write R3 - hand-entered distance')
        segment = route.segment_ids[:1]
        self.assertTrue(segment, "precondition failed: expected one segment for a two-waypoint route")
        cache_row = self._cache_row(segment.route_section_id)
        self.assertTrue(cache_row, "precondition failed: expected a manual/driving cache row")

        # WHEN an operator types a distance they measured themselves onto the
        # segment - the R3 path, which writes distance_status alongside
        # distance and is therefore untouched by any bare-write inference fix.
        segment.distance = 123.0
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertAlmostEqual(
            cache_row.distance, 123.0, places=6,
            msg="precondition failed: the hand-entered distance did not reach the "
                f"manual cache row (got {cache_row.distance}), so the R3 path this "
                "control is about did not run",
        )
        self.assertEqual(
            cache_row.distance_status, 'measured',
            "a hand-entered distance is a genuine measurement - the row must not "
            "read a real, human-supplied distance beside a status that denies it "
            "was ever measured. R3 already carries both keys in one write; a "
            "bare-write inference fix must leave this path exactly as it is",
        )
