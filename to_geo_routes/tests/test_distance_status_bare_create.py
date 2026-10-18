from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDistanceStatusBareCreate(TransactionCase):
    """Protects the CREATE door of the bare-positive-distance rule on
    ``route.section.transport.mode`` (Viindoo/tvtmaaddons#14409 close-out).

    ## What this file adds to ``test_distance_status_bare_write.py``

    That file owns the WRITE door: a bare ``write({'distance': 123.0})`` with no
    ``distance_status`` key must record ``measured``, because ``distance_status`` is
    a stored compute whose ``@api.depends`` lists only the four endpoint-coordinate
    fields and never ``distance`` - so the bare write triggers no recompute and can
    leave a row showing a real, positive distance beside a status that DENIES any
    measurement was ever made.

    ``create()`` had no counterpart. This file adds one, and pins the rule to a
    SINGLE spelling shared by both doors - the same shape ``distance.mixin`` already
    uses for its own two-door rule (``_normalize_derived_duration_vals``, applied by
    both ``write()`` and ``create()``).

    ## MEASURED, and it supersedes the assumption this file was scoped from

    The create-door lie is **not representable on this model today**, and NOT because
    of any guard. Measured on the target tree:

        create({'distance': 123.0, 'distance_status': 'measured'})
          ->  distance = 0.0,  distance_status = 'no_coordinates'

    BOTH keys are discarded. On ``route.section.transport.mode`` ``distance`` and
    ``distance_status`` are stored computes that are ``readonly`` with NO
    ``inverse=``, so the ORM's create-time protection predicate
    (``field.compute and (not field.readonly or field.precompute)``) is False for
    both: neither is protected, ``_create``'s ``records.modified(self._fields,
    create=True)`` marks both for recompute, and the caller's values are replaced
    before the transaction ever sees them. A caller-supplied distance never lands at
    create at all - which is why the failing state the create guard exists to prevent
    cannot currently be reached through it, and why the invariant test below is
    honestly named a premise guard rather than presented as delivery evidence.

    The guard is added anyway, and it is not decoration. The protection above is
    incidental to a FIELD PROFILE, not to any rule: ``route.segment`` in this very
    module already declares its ``distance`` with ``inverse='_inverse_distance_ave_
    speed'``, which makes ``readonly`` False and the field protected at create. The
    day this model's ``distance`` gains an inverse - or is redeclared
    ``readonly=False`` - a caller-supplied distance starts surviving create, and the
    guard below is the only thing standing between that caller and the exact
    ``distance=123.0 status=no_coordinates`` lie the write door already refuses.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.manual_driving_provider = cls.env.ref('to_geo_routes.manual_input_driving')

    # ------------------------------------------------------------------
    # fixtures - same shape test_distance_status_bare_write.py uses
    # ------------------------------------------------------------------
    def _make_partner(self, name, latitude=None, longitude=None):
        """Create a partner carrying ONLY the coordinate keys explicitly asked for.
        See `test_distance_status_bare_write.py` for why no address field is ever
        passed and why an omitted coordinate reads back as `0.0`.
        """
        vals = {'name': name}
        if latitude is not None:
            vals['partner_latitude'] = latitude
        if longitude is not None:
            vals['partner_longitude'] = longitude
        return self.env['res.partner'].create(vals)

    def _section_without_its_manual_cache_row(self, tag):
        """A `route.section` whose auto-generated manual/driving cache row has been
        removed, so this test can drive `create()` on that row itself rather than the
        creation cascade.

        `route.section.create` cascades into
        `_generate_route_section_transport_modes_if_not_exists`, which already makes
        one row per provider - and `route_id_mode_unique` forbids a second row for
        the same (section, provider) pair.
        """
        origin = self._make_partner(f'{tag} origin')
        destination = self._make_partner(f'{tag} destination')
        section = self.env['route.section'].create({
            'address_from_id': origin.id,
            'address_to_id': destination.id,
        })
        section.transport_mode_ids.filtered(
            lambda mode_record: mode_record.transport_mode_provider_id == self.manual_driving_provider
        ).unlink()
        self.env.flush_all()
        return section

    # ------------------------------------------------------------------
    # A - the invariant the create guard exists to hold
    # ------------------------------------------------------------------
    def test_create_never_persists_a_positive_distance_beside_no_coordinates_premise_guard(self):
        """PREMISE GUARD - GREEN ON ARRIVAL, by the measured mechanism documented in
        the class docstring. This is NOT evidence that anything was fixed.

        It asserts the invariant itself - a row may never come out of `create()`
        carrying a positive distance beside a status that denies any measurement -
        rather than the mechanism that currently delivers it, so it keeps holding
        (and keeps being meaningful) whichever of the two mechanisms is in force:
        the create-time recompute that owns the value today, or the shared guard that
        owns it the day this model's `distance` gains an `inverse=` and starts
        surviving create the way `route.segment`'s already does.
        """
        section = self._section_without_its_manual_cache_row('bare-create A')

        cache_row = self.env['route.section.transport.mode'].create({
            'route_section_id': section.id,
            'transport_mode_provider_id': self.manual_driving_provider.id,
            'distance': 123.0,
        })
        self.env.flush_all()
        cache_row.invalidate_recordset(['distance', 'distance_status'])

        self.assertFalse(
            cache_row.distance > 0 and cache_row.distance_status == 'no_coordinates',
            f"the row came out of create() reading distance={cache_row.distance} beside "
            "distance_status='no_coordinates' - a real, positive distance next to a "
            "status that denies any measurement was ever made. An operator reading that "
            "row has no way to learn from the record alone that a value was just "
            "written to it",
        )

        # MECHANISM PIN, deliberately separate from the invariant above. Today the
        # create-time recompute discards the caller's distance outright, so the
        # invariant holds for a reason that has nothing to do with the guard. If this
        # line ever fails, create-supplied distances have started surviving on this
        # model and the shared guard is now the ONLY thing holding the invariant -
        # which is exactly the state it was added for, and worth failing loudly to
        # announce.
        self.assertEqual(
            cache_row.distance, 0.0,
            "a caller-supplied distance survived create() on route.section.transport."
            "mode. That is a legitimate change (route.segment's distance already "
            "carries an inverse and survives), but it moves this model from 'the lie "
            "is unreachable through create' to 'the lie is prevented only by the "
            "shared bare-distance guard' - re-read that guard before relaxing this pin",
        )

    # ------------------------------------------------------------------
    # B - ONE spelling of the rule, shared by both doors
    # ------------------------------------------------------------------
    def test_a_bare_positive_distance_records_measured_while_a_blank_one_preserves_a_recorded_cause(self):
        """The rule itself, asserted directly on the single method both doors call.

        Every clause here is load-bearing and each is already relied on by an
        existing WRITE-door test in `test_distance_status_bare_write.py`:

        - a TRUTHY distance with no status is stamped `measured` (scenarios A/B);
        - a FALSY distance is left ALONE, so the R2 blanking paths
          (`missing_records.write({'distance': False})`,
          `remain.distance = False`) cannot stomp an already-recorded cause
          (scenarios C/D);
        - `0.0` is treated identically to `False`, because on a `fields.Float` with
          no `digits=` they are ONE stored value (scenario D);
        - an explicitly supplied `distance_status` always wins, because every
          provider and sweep path already passes its own (scenario E).

        RED mechanism on the unmodified tree: `_apply_bare_distance_status` does not
        exist - the rule is spelled inline inside `write()` and nowhere else - so this
        raises AttributeError.
        """
        model = self.env['route.section.transport.mode']

        self.assertEqual(
            model._apply_bare_distance_status({'distance': 123.0}),
            {'distance': 123.0, 'distance_status': 'measured'},
            "a bare positive distance is an affirmative act asserting a distance "
            "exists, and must record that it was measured",
        )
        self.assertEqual(
            model._apply_bare_distance_status({'distance': False}),
            {'distance': False},
            "a FALSY distance is the R2 blanking shape - it runs AFTER an earlier "
            "step recorded WHY the row failed, and that determined cause must survive",
        )
        self.assertEqual(
            model._apply_bare_distance_status({'distance': 0.0}),
            {'distance': 0.0},
            "0.0 stores the identical value False does on a Float with no digits=, "
            "and must be treated identically or the R2 poisoning returns on whichever "
            "of the two spellings was missed",
        )
        self.assertEqual(
            model._apply_bare_distance_status({'distance': 5.0, 'distance_status': 'no_time'}),
            {'distance': 5.0, 'distance_status': 'no_time'},
            "an explicit status always wins - every provider and sweep path passes "
            "its own, and overriding them would silently corrupt all of them",
        )

    def _spy_on_the_shared_rule(self):
        """Wrap the shared rule so a door can be proven to route through it, and the
        vals it produced can be inspected."""
        model_cls = type(self.env['route.section.transport.mode'])
        original_rule = model_cls._apply_bare_distance_status
        applications = []

        def spy(record_self, vals):
            result = original_rule(record_self, vals)
            applications.append(result)
            return result

        return patch.object(model_cls, '_apply_bare_distance_status', spy), applications

    def test_a_row_created_with_a_positive_distance_cannot_escape_the_rule_the_write_door_enforces(self):
        """`create()` must apply the SAME rule object `write()` applies, not a second
        copy of it. Two copies drift; the first divergence between them is a row that
        obeys one door and not the other, and nothing in the suite would notice.

        This is asserted structurally - by proving the door calls the shared rule and
        by inspecting the vals that rule produced - because on this model the create
        door's runtime effect is currently masked by the create-time recompute (see
        the class docstring). Structural is what remains observable; it is also what
        makes one mutation of the shared rule redden BOTH doors' tests.

        RED mechanism on the unmodified tree: `create()` is not overridden here at
        all, so the shared rule is never called and `applications` stays empty.
        """
        section = self._section_without_its_manual_cache_row('bare-create B')
        rule_patch, applications = self._spy_on_the_shared_rule()

        with rule_patch:
            self.env['route.section.transport.mode'].create({
                'route_section_id': section.id,
                'transport_mode_provider_id': self.manual_driving_provider.id,
                'distance': 123.0,
            })

        self.assertTrue(
            applications,
            "create() never applied the shared bare-distance rule. The write door "
            "refuses to leave a positive distance beside a stale status and the "
            "create door does not - one rule with two doors is what keeps them from "
            "drifting apart",
        )
        # `assertIn`, not equality: creating a cache row cascades into the module's
        # own bookkeeping (`route.section` regenerates the provider rows it is
        # missing), so the shared rule legitimately runs more than once here. What
        # matters is that the vals THIS test supplied came out stamped.
        self.assertIn(
            'measured', [vals.get('distance_status') for vals in applications],
            "create() applied the shared rule but it did not stamp 'measured' onto a "
            "bare positive distance",
        )

    def test_a_row_written_with_a_positive_distance_obeys_the_same_rule_the_create_door_enforces(self):
        """The other half of the same contract: `write()` must go through the shared
        rule too, so that mutating that one rule reddens BOTH doors rather than
        leaving one of them quietly enforcing an older version of it.

        RED mechanism on the unmodified tree: `_apply_bare_distance_status` does not
        exist, so patching it raises AttributeError.
        """
        section = self._section_without_its_manual_cache_row('bare-create C')
        cache_row = self.env['route.section.transport.mode'].create({
            'route_section_id': section.id,
            'transport_mode_provider_id': self.manual_driving_provider.id,
        })
        self.env.flush_all()

        rule_patch, applications = self._spy_on_the_shared_rule()
        with rule_patch:
            cache_row.write({'distance': 123.0})

        self.assertTrue(
            applications,
            "write() never applied the shared bare-distance rule - the rule has "
            "drifted back into an inline copy and the create door no longer shares it",
        )
        self.assertIn(
            'measured', [vals.get('distance_status') for vals in applications],
            "write() applied the shared rule but it did not stamp 'measured' onto a "
            "bare positive distance",
        )
