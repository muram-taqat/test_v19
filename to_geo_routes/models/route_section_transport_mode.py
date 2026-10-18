import logging

from collections import defaultdict

from odoo import fields, models, api

from .route_section_mixin import DETERMINED_DISTANCE_STATUS_CAUSES, DISTANCE_STATUS_SELECTION

_logger = logging.getLogger(__name__)


class RouteSectionTransportMode(models.Model):
    """
    This model represents the transport mode (e.g., driving, cycling) for a specific route section. It stores
    the distance, duration, average speed, and other relevant information specific to the transport mode selected
    for a particular route section.

    The transport mode is linked to a `transport.mode.provider` which defines how the distance and duration
    are calculated—either manually or by querying external map providers like Google, HERE, etc.

    The goal of this model is to store transportation-related data for each route section and provider, ensuring
    that repeated API calls for the same data are avoided, improving speed and saving costs.
    """
    _name = 'route.section.transport.mode'
    _inherit = 'route.section.mixin'
    _order = 'sequence, id'
    _description = 'Route Section Mode'

    # new fields
    sequence = fields.Integer(
        related='transport_mode_provider_id.sequence', store=True,
        index=True
        )
    route_section_id = fields.Many2one(
        'route.section', string='Route Section',
        required=True, ondelete='cascade',
        index=True,
        bypass_search_access=True,
        )
    transport_mode_provider_id = fields.Many2one(
        'transport.mode.provider', string="Transport Mode Provider",
        required=True,
        ondelete='cascade',  # cascade is required when uninstalling `to_geo_routes_{provider} modules`
        help="Selected transport mode and provider for this route"
        )
    provider = fields.Selection(
        related='transport_mode_provider_id.provider', store=True, precompute=True,
        index=True
        )
    mode = fields.Selection(
        related='transport_mode_provider_id.transport_mode', store=True,
        index=True
        )
    map_api_enabled = fields.Boolean(
        string='Map API Enabled',
        compute='_compute_map_api_enabled', compute_sudo=True,
        help="This is to indicate of the related provider's API is enabled for the current company"
        )
    distance_status = fields.Selection(
        DISTANCE_STATUS_SELECTION,
        string="Distance Status",
        compute='_compute_distance_status', store=True,
        index=True,
        help="Why this route section holds the distance it holds. A distance of 0 km "
             "means two different things - the route was measured and the answer was "
             "zero, or it was never measured at all - and those have opposite remedies. "
             "This field is what tells them apart."
        )
    # override
    address_from_id = fields.Many2one(
        related='route_section_id.address_from_id', store=True, index=True,
        # as this is a required field, precompute before saving to database
        precompute=True
        )
    address_to_id = fields.Many2one(
        related='route_section_id.address_to_id', store=True, index=True,
        # as this is a required field, precompute before saving to database
        precompute=True
        )
    distance = fields.Float(
        help="The distance in kilometers for the specified route section based on the selected transport mode."
        )
    distance_in_miles = fields.Float(
        help="TThe distance in miles for the specified route section based on the selected transport mode."
        )
    duration = fields.Float(
        help="The duration in hours required to traverse the specified route section based on the selected transport mode."
        )
    ave_speed = fields.Float(
        help="The average speed in kilometers per hour attainable on the specified route section based on the selected transport mode."
        )
    ave_speed_in_miles = fields.Float(
        help="The average speed in miles per hour attainable on the specified route section based on the selected transport mode."
        )
    active = fields.Boolean(
        related='transport_mode_provider_id.active', store=True, index=True
        )

    _route_id_mode_unique = models.Constraint(
        'UNIQUE(route_section_id, transport_mode_provider_id)',
        "Route section and mode must be unique!",
    )
    _check_positive_ave_speed = models.Constraint(
        'CHECK(ave_speed >= 0)',
        "The average speed must not be negative!",
    )
    _check_positive_ave_speed_in_miles = models.Constraint(
        'CHECK(ave_speed_in_miles >= 0)',
        "The average speed in miles per hour must not be negative!",
    )
    _check_positive_distance = models.Constraint(
        'CHECK(distance >= 0)',
        "The distance must not be negative!",
    )
    _check_positive_distance_in_miles = models.Constraint(
        'CHECK(distance_in_miles >= 0)',
        "The distance in miles must not be negative!",
    )

    @api.depends('route_section_id', 'mode', 'provider')
    def _compute_name(self):
        providers = dict(self._fields['provider']._description_selection(self.env))
        modes = dict(self._fields['mode']._description_selection(self.env))
        for r in self:
            # Both lookups fall back to the RAW stored code. A selection value contributed by a
            # sibling module (`to_geo_routes_google` and friends each `selection_add` their own
            # provider) is NOT present in the selection during a staged registry load: installing
            # any new module reloads `to_geo_routes` and re-executes its data files BEFORE the
            # provider addons re-register. Measured: the crash below happened while loading
            # `to_geo_routes/data/data_demo.xml`, with zero mentions of `to_geo_routes_google` in
            # the log up to that point, on a database whose rows already carried
            # `provider='google'`.
            #
            #     KeyError: 'google'   (was: `providers[r.provider]`)
            #
            # A bare `providers[...]` therefore aborts the WHOLE install with an unhandled
            # KeyError - for a customer, installing any app on a database that uses Google routing.
            # `name` is a display string; rendering the stored code is always better than raising.
            r.name = "%s [%s] [%s]" % (
                r.route_section_id.display_name,
                modes.get(r.mode, r.mode),
                providers.get(r.provider, r.provider),
            )

    @api.depends_context('company')
    @api.depends('transport_mode_provider_id.map_api_enabled')
    def _compute_map_api_enabled(self):
        for r in self:
            r.map_api_enabled = r.transport_mode_provider_id.map_api_enabled

    @api.depends(
        'address_from_id.partner_latitude', 'address_from_id.partner_longitude',
        'address_to_id.partner_latitude', 'address_to_id.partner_longitude')
    def _compute_distance_status(self):
        """Give `distance_status` EXACTLY the lifecycle `distance` has - this is the
        whole point of the field being computed rather than plain-stored.

        `distance` on this model is a STORED COMPUTED field: `distance.mixin` declares
        it stored, `route.section.mixin` adds `compute='_compute_distance'` and the
        coordinate `depends`, and the redeclaration above sets only `help=`, so compute
        and depends carry down the `_inherit` chain unchanged. Its profile is compute +
        readonly + NO inverse, the profile `distance_mixin.py` already documents for
        `duration`: a value written alone survives only until the compute's dependencies
        move, and is reverted the moment they do.

        Every provider-written distance in this module therefore stands on that, and it
        collapses the instant an endpoint partner's coordinates change. A plain stored
        `distance_status`, written only at the write paths, would stay put while
        `distance` moved - leaving a row reporting a genuine zero-length measurement for
        a section whose endpoint has no coordinates at all. That is a user-facing lie of
        exactly the kind this field exists to remove, so the two fields are declared with
        the IDENTICAL compute/store/depends profile (compute + `store=True`, readonly,
        no inverse, and the IDENTICAL `depends`). One dependency change now moves both,
        or neither. `index=True` is the one deliberate addition on `distance_status`
        alone, for searchability - it is not part of the shared profile.

        The two outcomes reachable from a measurement are the only two this compute can
        know. R1: when the compute is the last writer and both endpoints carry valid
        coordinates, the row IS measured - even when the great-circle answer is exactly
        `0.0`, because two distinct records at the same coordinates (two berths of one
        terminal) genuinely are zero kilometres apart. A provider observation of
        `distance == 0` is a different statement made by a different writer, and is
        classified separately; the LAST writer wins.
        """
        for r in self:
            r.distance_status = 'measured' if r._has_measurable_endpoints() else 'no_coordinates'

    def _mark_generic_distance_status(self):
        """R2 - record the generic default ONLY on rows whose failure has no
        determined cause; never downgrade one that has.

        Both blanking sites (`_update_distance_ave_speed`'s `remain.distance = False`
        and `_update_missing_distances_ave_speed`'s
        `missing_records.write({'distance': False})`) run AFTER the step that knows
        WHY each row failed. Applying `no_provider_data` blindly there would erase
        `no_coordinates`/`no_distance`/`no_time` - the only part of the record an
        operator can act on - and replace it with the one value that says nothing.

        A stale `measured` IS overwritten here, and deliberately: the distance that
        justified it has just been blanked, so leaving it would be the drift this
        field exists to remove.
        """
        undetermined = self.filtered(
            lambda mode_record: mode_record.distance_status not in DETERMINED_DISTANCE_STATUS_CAUSES
            )
        if undetermined:
            undetermined.distance_status = 'no_provider_data'

    @api.depends_context('company')
    def _compute_show_distance_in_miles(self):
        super()._compute_show_distance_in_miles()

    def _apply_bare_distance_status(self, vals):
        """THE single spelling of the bare-positive-distance rule, applied by BOTH
        `create` and `write`. Returns the vals to hand to `super()`.

        It is one method rather than one copy per door on purpose: two copies drift,
        and the first divergence between them is a row that obeys one door and not
        the other, with nothing in the suite noticing. `distance.mixin` already uses
        this exact two-door shape for its own rule
        (`_normalize_derived_duration_vals`, applied by both `write` and `create`).

        ## The rule

        Keep a bare positive `distance` from leaving a stale/absent
        `distance_status` behind it.

        Every OTHER writer in this module already records the status next to
        the distance: the provider paths write
        `dict(update_vals, distance_status='measured')`
        (`_update_distance_ave_speed`, `_update_missing_distances_ave_speed`),
        the human-entry path (`route_segment.py` rule R3) writes
        `update_data['distance_status'] = 'measured'`, and the blanking paths
        write a falsy distance and then a determined cause (rule R2, see
        `_mark_generic_distance_status`). A BARE `write({'distance': 123.0})`
        with no status is the one path none of them cover: `distance_status`
        is computed from the four endpoint-coordinate fields alone
        (`_compute_distance_status`) and never depends on `distance`, so it
        is left untouched - and the row can show a real distance beside a
        status that denies any measurement ever happened (observed:
        `distance=123.0 status=no_coordinates`).

        Both halves of the guard are load-bearing:

        - TRUTHY `distance` only. A falsy write (`False`/`0.0`) must leave
          the status alone - that is what preserves rule R2, because
          `_update_distance_ave_speed`'s `remain.distance = False` and
          `_update_missing_distances_ave_speed`'s
          `missing_records.write({'distance': False})` blank a distance
          AFTER an earlier step already recorded WHY the row failed, and
          those determined causes (`DETERMINED_DISTANCE_STATUS_CAUSES`,
          `route_section_mixin.py`) must survive the blank.
        - only when the caller did NOT also supply `distance_status`. An
          explicit status always wins - that is how every provider path and
          the R3 path already behave; this override must not second-guess
          them.

        Why `measured`: rule R3 already applies `measured` to a hand-entered
        distance regardless of endpoint coordinates, on the ground that a
        human-supplied distance is a genuine measurement and leaving a
        failure status beside it is exactly the drift `distance_status`
        exists to prevent. A bare positive write is likewise an affirmative
        act asserting a distance exists, so it gets the same answer.

        This deliberately reads the opposite of the 18.0.1.0.4 migration,
        which classifies a PRE-EXISTING stale positive distance on unlocated
        endpoints as `no_coordinates`: a value already sitting in the column
        carries no evidence of intent, whereas an active write does. Do not
        "harmonise" the two - they answer different questions.

        Out of scope: `_compute_distance` never reaches this rule at all -
        inside a compute, `Field.__set__` (`odoo/fields.py`) takes the
        protected branch and calls `Field.write` directly, bypassing
        `Model.write`. Outside a compute, `record.distance = x` DOES route
        through `records.write({...})`, which is exactly why the falsy guard
        above protects the blanking paths.

        ## What the CREATE door does and does not buy today - MEASURED

        On this model the rule is currently a no-op at create, and NOT because
        of any guard. `distance` and `distance_status` are both stored computes
        that are `readonly` with no `inverse=`, so the
        create-time protection predicate in `BaseModel.create` (`field.compute and (not
        field.readonly or field.precompute)`) is False for both: neither is
        protected, `_create`'s `records.modified(self._fields, create=True)`
        marks both for recompute, and a caller-supplied `distance` is discarded
        before the transaction ever sees it. Measured on this tree:
        `create({'distance': 123.0, 'distance_status': 'measured'})` lands
        `distance=0.0, distance_status='no_coordinates'`.

        The door is wired anyway, because that protection is incidental to a
        FIELD PROFILE rather than to any rule: `route.segment` in this same
        module already declares its `distance` with
        `inverse='_inverse_distance_ave_speed'`, which makes the field
        protected at create. The day this model's `distance` gains an inverse
        or is redeclared `readonly=False`, a caller-supplied distance starts
        surviving create - and this is then the only thing standing between
        that caller and the exact `distance=123.0 status=no_coordinates` lie
        the write door already refuses.
        """
        if vals.get('distance') and 'distance_status' not in vals:
            return dict(vals, distance_status='measured')
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        """Apply the shared bare-positive-distance rule at the create door.

        See :meth:`_apply_bare_distance_status` for the rule, and for what this
        door does and does not buy on this model today.
        """
        return super().create([self._apply_bare_distance_status(vals) for vals in vals_list])

    def write(self, vals):
        """Apply the shared bare-positive-distance rule at the write door.

        See :meth:`_apply_bare_distance_status` for the rule.
        """
        return super().write(self._apply_bare_distance_status(vals))

    def _get_transport_mode_providers(self):
        return self.transport_mode_provider_id

    def _get_distance_matrix(self, res_model=None, res_id=None):
        res = {}
        # Group by transport_mode_provider_id
        grouped_modes = defaultdict(lambda: self.env[self._name])
        for r in self:
            grouped_modes[r.transport_mode_provider_id] += r
        # Iterate over each provider and process its route sections
        for provider, section_transport_modes in grouped_modes.items():
            # A row whose OWN endpoint carries an invalid coordinate can
            # never be resolved - and, worse, feeding it into the batched
            # provider call below would poison the WHOLE group: for the
            # manual provider, geo_direction_manual._get_distance_matrix
            # validates the combined address set with raise_if_not_valid=True
            # (the default), so one bad partner raises for every row sharing
            # this provider, the `except Exception` below swallows it, and
            # every row - including ones whose own addresses were always
            # valid - loses its result (Viindoo/tvtmaaddons#14279). Isolate
            # invalid rows here, per row, before the batched call, so one bad
            # partner can no longer corrupt its unrelated neighbours.
            valid_modes = section_transport_modes.filtered(
                lambda mode_obj: mode_obj._has_measurable_endpoints()
                )
            if not valid_modes:
                continue
            try:
                # Get the distance matrix from transport_mode_provider
                distance_matrix_data = provider._get_distance_matrix(
                    valid_modes.address_from_id | valid_modes.address_to_id,
                    res_model=res_model,
                    res_id=res_id
                    )

                # Update distances in route.section.transport.mode
                for mode_obj in valid_modes:
                    from_address = mode_obj.address_from_id
                    to_address = mode_obj.address_to_id
                    distance_info = distance_matrix_data.get((from_address, to_address))
                    if distance_info:
                        res[mode_obj] = distance_info
            except Exception as e:
                _logger.error("Error while processing API response: %s", str(e))
        return res

    def _push_mirroring_segment_recompute(self):
        """R4 - Mirror-refresh rule (design amendment "cont." § A-6.4): after a
        provider fetch resolves fresh distance/ave_speed on `self`, every
        `route.segment` mirroring that cache row must reflect it. Deliberately
        NOT an `@api.depends` edge - ruling 4 (§ A-6.3) refuses one: the
        inverse (`route.segment._inverse_distance_ave_speed`) writes the
        manual/forced-provider row while the compute
        (`route.segment._compute_ave_speed`/`_compute_distance`) reads the
        FIRST row by `sequence, id` with a truthy speed - a DIFFERENT row
        whenever an API row sorts before the manual one. A declarative edge
        would fire on every cache write and silently revert a hand-typed
        segment override the next time any code touches the shared row; this
        push fires ONLY on the provider-fetch path, where a fresh value is
        supposed to win. Call ONLY from `_update_distance_ave_speed`/
        `_update_missing_distances_ave_speed` - never from a plain write.
        """
        if not self:
            return
        mirroring_segments = self.env['route.segment'].search([
            ('route_section_id', 'in', self.route_section_id.ids),
        ])
        if not mirroring_segments:
            return
        for field_name in ('distance', 'ave_speed'):
            self.env.add_to_compute(mirroring_segments._fields[field_name], mirroring_segments)

    def _update_distance_ave_speed(self):
        to_process = self.filtered(lambda x: x.map_api_enabled or x.provider == 'manual')
        distance_matrix = to_process._get_distance_matrix()
        processed = self.env[self._name]
        no_distance_records = self.env[self._name]
        no_time_records = self.env[self._name]
        ms_to_kmh = self.env['to.base'].ms_to_kmh
        for r in to_process:
            data = distance_matrix.get(r)
            if not data:
                continue
            distance_km = data.get('distance', 0.0) / 1000
            ave_speed_kmh = ms_to_kmh(data.get('ave_speed', 0.0))
            update_vals = r._prepare_distance_vals_from_provider(
                distance_km, ave_speed=ave_speed_kmh)
            if 'ave_speed' not in update_vals:
                # R2a/R2b (design amendment "N2 rulings" § A-4, N2-4): the SAME
                # defect as `_update_missing_distances_ave_speed`, one method
                # above this one - and this is the module's row-CREATION path
                # (`RouteSection.create`), busier than that one: every fresh
                # row starts at distance=0.0, ave_speed=0.0
                # (the `distance.mixin` field declarations). A degenerate (zero-time)
                # observation must not write a positive distance while
                # `ave_speed` is left unresolved - that combination is the
                # exact poisoning ruling 1 exists to prevent. Write nothing;
                # the `remain` tail below keeps the row inside the self-heal
                # domain (`('distance', 'in', [False, 0.0])`).
                #
                # W5: the row still RECORDS which of the two degenerate causes
                # this was, so the failure survives on the record rather than
                # only in a log line that ages out of the build. The status is
                # set WITHOUT the distance, below - writing the converter's
                # vals dict to land it would carry `distance` along and cause
                # the very poisoning this branch exists to prevent.
                if r._classify_provider_observation(distance_km, ave_speed_kmh) == 'no_distance':
                    no_distance_records |= r
                else:
                    no_time_records |= r
                continue
            r.write(dict(update_vals, distance_status='measured'))
            processed |= r
        processed._push_mirroring_segment_recompute()
        # Bulk per cause - one write each, never one per record.
        if no_distance_records:
            no_distance_records.distance_status = 'no_distance'
        if no_time_records:
            no_time_records.distance_status = 'no_time'
        remain = self - processed
        if remain:
            remain.distance = False
            # R2 - the rows just classified above keep their cause; only the ones
            # nothing could explain fall back to the generic default. This method
            # stays SILENT by design: it runs on every `route.section` creation
            # (`RouteSection.create`), so warning here would turn ordinary record
            # creation into build-log noise. `_update_missing_distances_ave_speed`
            # is the sweep that reports.
            remain._mark_generic_distance_status()

    @api.model
    def _update_missing_distances_ave_speed(self, transport_mode_providers=None, res_model=None, res_id=None):
        """
        Update missing distances and durations in route.section.transport.mode.

        Ensures that all route sections have valid distance and duration values.

        :param transport_mode_providers: A recordset of `transport.mode.provider` (optional).
        :param str res_model: The model name related to the distance matrix (optional).
        :param int res_id: The record ID related to the distance matrix (optional).
        """
        transport_mode_providers = transport_mode_providers or self.env['transport.mode.provider']
        transport_mode_providers |= self.env.ref('to_geo_routes.manual_input_driving')

        route_section_modes = self.search([
            ('distance', 'in', [False, 0.0]),
            ('address_from_id', '!=', False),
            ('address_to_id', '!=', False),
            ('transport_mode_provider_id', 'in', transport_mode_providers.ids),
        ]).filtered(lambda x: x.map_api_enabled or x.provider == 'manual')

        if not route_section_modes:
            _logger.debug("No missing distances to update.")
            return
        _logger.debug("Found %s route_section_transport_mode records to update.", len(route_section_modes))
        # fill coordinates for addresses if missing
        missing_coordinate_addresses = (route_section_modes.address_from_id | route_section_modes.address_to_id).filtered(
            lambda p: p.partner_latitude is False
                or p.partner_longitude is False
                or (p.partner_latitude == 0.0 and p.partner_longitude == 0.0)
        )
        if missing_coordinate_addresses:
            _logger.debug("Found %s res.partner records that need to fill coordinates.", len(missing_coordinate_addresses))
            missing_coordinate_addresses.geo_localize()

        distance_matrix = route_section_modes._get_distance_matrix(res_model=res_model, res_id=res_id)

        # Update distances and durations in batch if possible
        # using splittor to process each and every batch of 1000 records for performance
        updated_records = self.env['route.section.transport.mode']
        degenerate_records = self.env['route.section.transport.mode']
        no_distance_records = self.env['route.section.transport.mode']
        ms_to_kmh = self.env['to.base'].ms_to_kmh
        for batch in self.env['to.base'].splittor(route_section_modes):
            for record in batch:
                record_data = distance_matrix.get(record)
                if not record_data:
                    continue
                distance_km = record_data['distance'] / 1000  # Convert to km
                ave_speed_kmh = ms_to_kmh(record_data['ave_speed'])  # Convert to km/h
                update_vals = record._prepare_distance_vals_from_provider(
                    distance_km, ave_speed=ave_speed_kmh)
                if 'ave_speed' not in update_vals:
                    # R2a/R2b (design amendment "N2 rulings" § A-1.3/A-1.4,
                    # Ruling 1): a positive distance with a degenerate
                    # (zero-time) speed is a MALFORMED observation, never a
                    # real "zero speed" answer. This row was selected BY the
                    # self-heal domain (`('distance', 'in', [False, 0.0])`,
                    # :215) - writing the converter's 'distance' here would
                    # move it permanently OUT of that domain while
                    # `ave_speed` stayed unrecoverably zero, the exact
                    # poisoning this ruling exists to prevent. Leave the row
                    # untouched: it falls into `missing_records` below and
                    # stays ELIGIBLE FOR REPAIR (no cron currently re-runs
                    # this method - staying eligible, not "will be
                    # self-healed", is the honest guarantee).
                    #
                    # W5 (design § 2.5): this branch used to collapse TWO
                    # causes, and everything downstream then reported both as
                    # a zero-time observation - a claim that is factually
                    # FALSE when what came back was a distance of zero, and
                    # one that sends the operator to chase the provider's
                    # timing instead of its distance. Split them here, on the
                    # classifier, and give each its own message and its own
                    # recorded status below. The status is set WITHOUT the
                    # distance: writing the converter's vals dict to land it
                    # would move the row permanently OUT of the self-heal
                    # domain, which is the exact poisoning this branch exists
                    # to prevent.
                    if record._classify_provider_observation(distance_km, ave_speed_kmh) == 'no_distance':
                        no_distance_records |= record
                    else:
                        degenerate_records |= record
                    continue
                record.write(dict(update_vals, distance_status='measured'))
                updated_records |= record

        _logger.debug("Updated %s records with distance and duration.", len(updated_records))
        updated_records._push_mirroring_segment_recompute()

        # Bulk per cause - one write each, never one per record.
        if no_distance_records:
            no_distance_records.distance_status = 'no_distance'
        if degenerate_records:
            degenerate_records.distance_status = 'no_time'

        missing_records = route_section_modes - updated_records
        if missing_records:
            missing_records.write({'distance': False})
            # W5 / review F5 - the THIRD cause, and the one the coordinate-presence
            # fix itself manufactures. `_get_distance_matrix` drops a row whose own
            # endpoint carries no coordinates BEFORE any provider is called, so the
            # row never appears in `distance_matrix`, never joins `degenerate_records`
            # and used to land in `no_data_records` - reported as "No distance data
            # found ... provider Y", naming a provider that never saw it. The
            # endpoint is the party at fault, so it gets its own message naming the
            # PARTNER and its own status.
            #
            # Scoped to `missing_records`, NOT to everything the sweep selected: a
            # coordinate-less row that some caller resolved anyway (a stub, or a
            # provider reached by another path) has not failed, and warning about it
            # would turn a fully successful sweep into a noisy one.
            no_coordinate_records = missing_records.filtered(
                lambda mode_record: not mode_record._has_measurable_endpoints()
                )
            if no_coordinate_records:
                no_coordinate_records.distance_status = 'no_coordinates'
            # R2 - runs last, so every cause determined above is already on its row
            # and only the genuinely unexplained rows take the generic default.
            missing_records._mark_generic_distance_status()

            no_data_records = (
                missing_records - degenerate_records - no_distance_records - no_coordinate_records
            )
            if no_data_records:
                _logger.warning(
                    "No distance data found for %s records in mode %s, provider %s.",
                    len(no_data_records),
                    no_data_records[0].transport_mode_provider_id.transport_mode,
                    no_data_records[0].transport_mode_provider_id.name,
                )
            if degenerate_records:
                # Tightening 2 (design amendment § A-1.3): distance data WAS
                # found for these records - only the speed could not be
                # determined (a zero-time provider observation) - so the
                # "No distance data found" message above would be factually
                # false for them. A distinct message keeps an operator from
                # chasing the wrong failure.
                _logger.warning(
                    "Distance data found but no usable speed (zero-time "
                    "observation) for %s records in mode %s, provider %s - "
                    "the speed was not written; these records remain "
                    "eligible for repair.",
                    len(degenerate_records),
                    degenerate_records[0].transport_mode_provider_id.transport_mode,
                    degenerate_records[0].transport_mode_provider_id.name,
                )
            if no_distance_records:
                # W5 (design § 2.5): the provider ANSWERED these rows - it answered
                # with no distance at all - so neither of the two messages above is
                # true of them. This wording deliberately avoids the literal
                # "No distance data found": that string belongs to the rows the
                # provider never answered, and reusing it here would make the two
                # indistinguishable again (it is also asserted ABSENT for an
                # answered row by `_assert_degenerate_speed_reported`).
                _logger.warning(
                    "Provider returned no distance (a zero-length answer) for %s "
                    "records in mode %s, provider %s - nothing was written; these "
                    "records remain eligible for repair.",
                    len(no_distance_records),
                    no_distance_records[0].transport_mode_provider_id.transport_mode,
                    no_distance_records[0].transport_mode_provider_id.name,
                )
            if no_coordinate_records:
                # W5 / review F5: no provider was ever called for these, so naming
                # one would be a misattribution. Name the ADDRESSES instead - an
                # operator reading a provider name and a count has no way to find
                # which address to fix, which is the whole failure this split
                # exists to end.
                unlocated_addresses = (
                    no_coordinate_records.address_from_id | no_coordinate_records.address_to_id
                ).filtered(
                    lambda partner: not partner._validate_coordinates(raise_if_not_valid=False)
                )
                _logger.warning(
                    "Cannot measure %s route section(s): their endpoints carry no "
                    "usable coordinates, so no provider was called for them. Fill in "
                    "the coordinates of these addresses: %s.",
                    len(no_coordinate_records),
                    ', '.join(sorted(unlocated_addresses.mapped('display_name'))),
                )
