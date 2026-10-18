from odoo import fields, models, api


class RouteSegment(models.Model):
    _name = 'route.segment'
    _inherit = 'route.section.mixin'
    _description = 'Route Segment'
    _order = 'route_id, sequence, id'
    _rec_name = 'route_section_id'

    route_id = fields.Many2one(
        'route.route',
        string='Route',
        required=True,
        ondelete='cascade',
        index=True,
        copy=False,
        readonly=True,
    )
    sequence = fields.Integer(
        string="Sequence",
        default=10,
        help="Defines the order of the segment inside the route.",
    )
    route_section_id = fields.Many2one(
        'route.section',
        string='Section',
        compute='_compute_route_section_id', store=True,
        ondelete='restrict',
        index='btree_not_null',
    )

    # Override
    address_from_id = fields.Many2one(readonly=True)
    address_to_id = fields.Many2one(readonly=True)

    distance = fields.Float(
        store=True,
        inverse='_inverse_distance_ave_speed',
    )
    ave_speed = fields.Float(
        compute='_compute_ave_speed', store=True,
        inverse='_inverse_distance_ave_speed',
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered(lambda r: not r.route_section_id)._attach_section()
        return records

    def write(self, vals):
        res = super().write(vals)
        if 'address_from_id' in vals or 'address_to_id' in vals:
            self._attach_section()
        return res

    @api.depends('route_id.company_id')
    def _compute_show_distance_in_miles(self):
        super()._compute_show_distance_in_miles()

    @api.depends('route_section_id', 'route_id.transport_mode')
    def _compute_distance(self):
        """
        Compute distance for segments using context-based values from route.section.
        Segments without a matching provider fallback to the mixin's compute method.
        """
        segments_for_super = self.env['route.segment']
        for segment in self:
            route_section = segment.route_section_id
            route = segment.route_id
            if not route_section or not route:
                segments_for_super |= segment
                continue

            transport_mode_name = route.transport_mode
            mode_record = next(
                (
                    mode_record
                    for mode_record in route_section.transport_mode_ids
                    if mode_record.mode == transport_mode_name and mode_record.distance
                ),
                False,
            )
            if mode_record:
                section_with_context = route_section.with_context(
                    transport_mode=transport_mode_name,
                    force_geo_direction_provider=mode_record.provider,
                )
                segment.distance = section_with_context.distance
            else:
                segments_for_super |= segment

        if segments_for_super:
            super(RouteSegment, segments_for_super)._compute_distance()

    @api.depends('address_from_id', 'address_to_id')
    def _compute_route_section_id(self):
        """
        Compute the route.section record that matches the from/to address pair.
        """
        sections = self._search_route_sections()
        default_section = self.env['route.section']
        grouped = sections._group_by_address_pair()

        for segment in self:
            segment.route_section_id = grouped.get(
                (segment.address_from_id.id, segment.address_to_id.id),
                default_section,
            )[:1]

    @api.depends('route_section_id', 'route_id.transport_mode')
    def _compute_ave_speed(self):
        """
        Compute the average speed based on the matching transport mode/provider.
        """
        for r in self:
            route_section = r.route_section_id
            route = r.route_id
            if not route_section or not route:
                r.ave_speed = 0.0
                continue

            transport_mode_name = route.transport_mode
            mode_record = next(
                (
                    mode_record
                    for mode_record in route_section.transport_mode_ids
                    if mode_record.mode == transport_mode_name and mode_record.ave_speed
                ),
                False,
            )
            provider_name = mode_record.provider if mode_record else False
            section_with_context = route_section.with_context(
                transport_mode=transport_mode_name,
                force_geo_direction_provider=provider_name,
            )
            r.ave_speed = section_with_context.ave_speed

    def _inverse_distance_ave_speed(self):
        """
        Synchronize distance and ave_speed from the segment back into
        the route.section transport modes (manual or forced provider).
        """
        provider_names = {'manual'}
        forced = self.env.context.get('force_geo_direction_provider')
        if forced:
            provider_names.add(forced)

        for r in self:
            route_section = r.route_section_id
            route = r.route_id
            if not route_section or not route:
                continue

            transport_mode_name = route.transport_mode
            manual_mode_records = route_section.transport_mode_ids.filtered(
                lambda mode_record: mode_record.provider in provider_names
                and mode_record.mode == transport_mode_name
            )
            if not manual_mode_records:
                continue

            section_manual = route_section.with_context(
                transport_mode=transport_mode_name,
                force_geo_direction_provider='manual',
            )

            update_data = {}
            if r.distance and r.distance != section_manual.distance:
                update_data['distance'] = r.distance
            if r.ave_speed and r.ave_speed != section_manual.ave_speed:
                update_data['ave_speed'] = r.ave_speed

            if update_data:
                if 'distance' in update_data:
                    # R3 - the FOURTH path that writes a cache row's `distance`, and
                    # the one the design's three-path enumeration misses. A user
                    # hand-editing a segment on the Route form's Segments tab is a
                    # documented, supported override (`doc/index.rst:26`), and it
                    # reaches the cache row through neither the provider converter
                    # nor the compute. A distance a human typed in IS a genuine
                    # measurement: leaving whatever stale failure status the row kept
                    # from its last provider fetch beside a real, human-supplied
                    # distance is exactly the drift that makes a status worse than no
                    # status. `ave_speed`-only edits are untouched - they change no
                    # distance, so they explain none.
                    update_data['distance_status'] = 'measured'
                manual_mode_records.write(update_data)
