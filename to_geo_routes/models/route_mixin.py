import logging

from collections import defaultdict

from odoo import api, Command, fields, models, _
from odoo.exceptions import ValidationError
from odoo.fields import Domain

_logger = logging.getLogger(__name__)


class RouteMixin(models.AbstractModel):
    _name = 'route.mixin'
    _description = 'Route Mixin'
    _inherit = ['mail.thread', 'route.section.mixin', 'transport.mode.mixin']

    name = fields.Char(
        string="Route name",
        compute='_compute_name', store=True,
        required=True, readonly=False,
        # No `index=`: this field merges with `route.section.mixin.name`, which is
        # `translate=True`, and a translated column only accepts a trigram index -
        # core drops any other index attribute and logs a schema WARNING for it
        # (odoo/orm/registry.py, `_check_indexes`). The attribute never produced an
        # index here, so stating it only bought the warning.
        # precompute to have value for required field
        precompute=True,
        )

    waypoint_ids = fields.One2many(
        comodel_name='_unknown',  # To be defined by inheriting models
        inverse_name=None,  # To be defined by inheriting models
        string="Waypoints"
    )
    segment_ids = fields.One2many(
        comodel_name='_unknown',  # To be defined by inheriting models
        inverse_name=None,  # To be defined by inheriting models
        string="Segments",
        compute='_compute_segment_ids',
        store=False
    )
    address_ids = fields.Many2many(
        'res.partner',
        string="Addresses", compute='_compute_address_ids', store=True, index=True,
        )

    waypoint_ids_count = fields.Integer(
        string='Waypoints Count',
        compute='_compute_waypoints_count'
        )
    company_id = fields.Many2one(
        'res.company', string='Company',
        )
    mapping_address = fields.Text(compute='_compute_mapping_address')

    # override
    address_from_id = fields.Many2one(
        string="Departure",
        compute='_compute_dept_dest', store=True, precompute=True,
        tracking=True, index=True, required=False
        )
    address_to_id = fields.Many2one(
        string="Destination",
        compute='_compute_dept_dest', store=True, precompute=True,
        tracking=True, index=True, required=False
        )
    distance = fields.Float(
        compute='_compute_distance', store=True, tracking=True,
        help="Total Distance (in kilometers) of the route, which is automatically computed from the route section lines."
        )
    distance_in_miles = fields.Float(
        readonly=True,
        help="Total Distance (in miles) of the route, which is automatically computed from the route section lines."
        )
    duration = fields.Float(
        string='Est. Time', tracking=True,
        compute='_compute_duration', store=True, readonly=True,
        help='The estimated time in hours spent to go through the route (without any stop consideration)'
        )
    ave_speed = fields.Float(
        tracking=True,
        compute='_compute_ave_speed', store=True, readonly=True,
        help="The average speed in kilometers per hour achievable on this this route, which is computed based on the average speed on every section of the route."
        )
    ave_speed_in_miles = fields.Float(
        readonly=True,
        help="The average speed in miles per hour achievable on this section."
        )

    @api.depends('company_id')
    def _compute_show_distance_in_miles(self):
        super()._compute_show_distance_in_miles()

    @api.depends('segment_ids.duration')
    def _compute_duration(self):
        """Override to compute duration base on section lines' duration"""
        for r in self:
            r.duration = sum(r.segment_ids.mapped('duration'))

    @api.depends('segment_ids.distance')
    def _compute_distance(self):
        for r in self:
            r.distance = sum(r.segment_ids.mapped('distance'))

    @api.depends('distance', 'duration')
    def _compute_ave_speed(self):
        for r in self:
            r.ave_speed = r.distance / r.duration if r.duration > 0.0 else 0.0

    def _inverse_distance_in_miles(self):
        """Override to ignore inverse"""
        pass

    def _inverse_ave_speed_in_miles(self):
        """Override to ignore inverse"""
        pass

    @api.depends('company_id')
    def _compute_transport_mode_provider_ids(self):
        super()._compute_transport_mode_provider_ids()

    @api.depends('waypoint_ids.sequence', 'waypoint_ids.address_id')
    def _compute_dept_dest(self):
        for r in self:
            waypoints = r.waypoint_ids.sorted('sequence')
            r.address_from_id = waypoints[:1].address_id
            r.address_to_id = waypoints[-1:].address_id

    @api.depends('address_from_id', 'address_to_id', 'transport_mode', 'waypoint_ids.sequence', 'waypoint_ids.address_id')
    def _compute_name(self):
        transport_modes = dict(self._fields['transport_mode']._description_selection(self.env))
        for r in self:
            from_address = r.address_from_id
            to_address = r.address_to_id
            transport_mode_value = r.transport_mode

            if not (from_address and to_address and transport_mode_value):
                r.name = '/'
                continue

            transport_mode = transport_modes.get(transport_mode_value, _("Unknown"))

            midpoint_name = False
            if r.waypoint_ids:
                excluded_address_ids = {from_address.id, to_address.id}
                intermediate_waypoints = [
                    waypoint
                    for waypoint in r.waypoint_ids.sorted('sequence')
                    if waypoint.address_id
                    and waypoint.address_id.id not in excluded_address_ids
                ]
                if intermediate_waypoints:
                    midpoint_index = len(intermediate_waypoints) // 2
                    midpoint_waypoint = intermediate_waypoints[midpoint_index]
                    if midpoint_waypoint.address_id:
                        midpoint_name = midpoint_waypoint.address_id.name

            if midpoint_name:
                r.name = _(
                    "%(transport_mode)s From %(from_address_name)s to %(to_address_name)s via %(midpoint_name)s",
                    transport_mode=transport_mode,
                    from_address_name=from_address.name,
                    to_address_name=to_address.name,
                    midpoint_name=midpoint_name,
                )
            else:
                r.name = _(
                    "%(transport_mode)s From %(from_address_name)s to %(to_address_name)s",
                    transport_mode=transport_mode,
                    from_address_name=from_address.name,
                    to_address_name=to_address.name,
                )

    @api.depends('waypoint_ids', 'waypoint_ids.address_id', 'waypoint_ids.sequence')
    def _compute_address_ids(self):
        for r in self:
            r.address_ids = [(6, 0, r.waypoint_ids.sorted('sequence').address_id.ids)]

    @api.depends('waypoint_ids.address_id')
    def _compute_mapping_address(self):
        for r in self:
            r.mapping_address = " -> ".join(
                wp.address_id.name or wp.address_id.display_name for wp in r.waypoint_ids
            )

    @api.depends('waypoint_ids.sequence', 'waypoint_ids.address_id', 'transport_mode')
    def _compute_segment_ids(self):
        for r in self:
            cmd = [Command.delete(seg.id) for seg in r.segment_ids]
            cmd += [
                Command.create(vals)
                # during on-fly computation, waypoint_ids may not be sorted in a correct order yet.
                # Hence, call sorted here is needed
                for vals in r.waypoint_ids.sorted('sequence').with_context(
                    transport_mode=r.transport_mode
                    )._prepare_segment_vals_list()
                ]
            r.segment_ids = cmd

    @api.depends('waypoint_ids')
    def _compute_waypoints_count(self):
        total_wp_data = self.env['route.waypoint']._read_group(
            [('route_id', 'in', self.ids)],
            ['route_id'],
            ['__count']
            )
        mapped_data = dict([(tup[0], tup[1]) for tup in total_wp_data])
        for r in self:
            r.waypoint_ids_count = mapped_data.get(r, 0)

    @api.constrains('waypoint_ids')
    def _check_waypoint_ids(self):
        for r in self:
            last_waypoint = False
            for waypoint in r.waypoint_ids:
                if last_waypoint:
                    if waypoint.address_id == last_waypoint.address_id:
                        raise ValidationError(
                            _(
                                "The waypoint %s seems to be as the same as the preceding one. You cannot have"
                                " same waypoints those are adjacent to each other. I.e. A->B->B->C must be either"
                                " A->B->C or A->B->C->B.",
                                waypoint.address_id.name
                                )
                            )
                last_waypoint = waypoint

    @api.constrains('address_from_id', 'address_to_id')
    def _check_address_from_id_address_to_id(self):
        # Allow round-trip routes where departure equals destination.
        return

    # new methods
    def _get_transport_mode_providers(self):
        return self.transport_mode_provider_ids

    def _get_intermediate_waypoint_addresses(self):
        self.ensure_one()
        waypoints = self.waypoint_ids - self.waypoint_ids[:1] - self.waypoint_ids[-1:]
        addresses = self.env['res.partner']
        for wp in waypoints:
            addresses += wp.address_id
        return addresses

    def _get_distance_matrix(self):
        self.ensure_one()
        if not self.transport_mode_provider_ids:
            raise ValidationError(_("No Geo-Direction Provider enabled..."))
        return self.waypoint_ids._get_distance_matrix(self.transport_mode_provider_ids, stop_at_first_success=True, ignore_failure_provider=True)

    def _process_direction_data_for_update(self, data):

        def convert(field_name, value):
            if field_name == 'distance':
                return value / 1000
            return value

        def get_effective_vals(original_vals, record):
            field_map = {'lat': 'latitude', 'lng': 'longitude'}
            return {
                (field_map.get(f) or f): convert(f, v)
                for f, v in original_vals.items()
                if (field_map.get(f) or f) in record._fields
                }

        self.ensure_one()
        update_vals = super()._process_direction_data_for_update(data)

        waypoint_update_cmd = []
        for waypoint, vals in zip(self.waypoint_ids, data.get('waypoints') or []):
            effective_vals = get_effective_vals(vals, waypoint)
            if effective_vals:
                waypoint_update_cmd.append(Command.update(waypoint.id, effective_vals))
        if waypoint_update_cmd:
            update_vals['waypoint_ids'] = waypoint_update_cmd

        segment_update_cmd = []
        for segment, vals in zip(self.segment_ids, data.get('sections') or []):
            # R1/R2 (design § 2.1 "Derived-Duration Rule", § D3.2 S3): the
            # distance/duration/ave_speed triangle is NEVER taken from the
            # generic `_fields`-membership passthrough below - `duration` is
            # a derived output, never a write input, and letting it through
            # here is exactly how it silently desynced from a fresh provider
            # response once `distance` lost its inverse (Viindoo/tvtmaaddons
            # #14279, site 4). The single boundary converter
            # `_prepare_distance_vals_from_provider` (route.section.mixin,
            # the same one site 1 already uses) is the ONLY place a provider
            # `(distance, duration)` pair becomes vals here - it never emits
            # 'duration'. Only the remaining, non-triangle keys (e.g.
            # 'polyline') still go through the generic passthrough.
            distance_triangle_keys = ('distance', 'duration', 'ave_speed')
            passthrough_vals = {k: v for k, v in vals.items() if k not in distance_triangle_keys}
            effective_vals = get_effective_vals(passthrough_vals, segment)
            if 'distance' in vals and 'duration' in vals:
                effective_vals.update(segment._prepare_distance_vals_from_provider(
                    vals['distance'] / 1000, duration_s=vals['duration'] / 3600
                ))
            if effective_vals:
                segment_update_cmd.append(Command.update(segment.id, effective_vals))
        if segment_update_cmd:
            update_vals['segment_ids'] = segment_update_cmd

        return update_vals

    def action_get_distance_matrix(self):
        self.ensure_one()
        self._get_distance_matrix()

    def _optimize_route(self):
        for r in self:
            if len(r.waypoint_ids) < 2:
                # Nothing to optimize with fewer than two waypoints (need at least an
                # origin and a destination); skip gracefully instead of crashing.
                # Without this guard, waypoint_ids._get_optimized_route() does self[0]
                # on an empty recordset (IndexError) for 0 waypoints, or feeds a
                # degenerate origin==destination/empty-waypoints input to the provider
                # for 1 waypoint, whose "Origin and destination cannot be the same"
                # error it then raises at the user for a route with nothing to optimize.
                continue
            if not r.transport_mode_provider_ids:
                raise ValidationError(_("No Geo-Direction Provider enabled for optimizing the route %s", r.display_name))
            response = r.waypoint_ids._get_optimized_route(r.transport_mode_provider_ids)
            optimized_route = response.get('processed', {}).get('optimized_route')
            legs = response.get('processed', {}).get('legs')
            if optimized_route:
                r.with_context(
                    transport_mode=r.transport_mode,
                    force_geo_direction_provider=response['provider']
                    )._reorder_waypoints(optimized_route, legs)

    def action_optimize_route(self):
        self._optimize_route()

    def _reorder_waypoints(self, optimized_route, legs):
        """
        Reorder the waypoints of a route.route based on the optimized_route and update route sections.

        A partner may occur more than once in optimized_route: a stop the route visits twice,
        or the origin of a round trip (origin == destination), which starts and ends the route.
        Each occurrence takes the next unused waypoint of that partner, in their current
        sequence order, so every waypoint keeps its own visit; a new waypoint is created only
        when the partner has no waypoint left.

        :param list optimized_route: A list of `res.partner` records representing the optimized route.
        :param list legs: A list of dictionaries containing 'from', 'to', 'distance', and 'duration'.
        """
        self.ensure_one()
        reordered_waypoints = []
        # partner id -> its waypoints not yet reordered, in their current sequence order
        available_waypoints = defaultdict(list)
        for waypoint in self.waypoint_ids.sorted('sequence'):
            available_waypoints[waypoint.address_id.id].append(waypoint)

        for current_sequence, partner in enumerate(optimized_route, start=1):
            if available_waypoints[partner.id]:
                waypoint = available_waypoints[partner.id].pop(0)
                reordered_waypoints.append((1, waypoint.id, {'sequence': current_sequence}))
            else:
                reordered_waypoints.append((0, 0, {
                    'address_id': partner.id,
                    'sequence': current_sequence,
                }))

        # Write reordered waypoints back to the route
        self.write({'waypoint_ids': reordered_waypoints})

        # Update route sections based on legs
        updated_sections = []
        existing_sections = {
            (line.address_from_id.id, line.address_to_id.id): line
            for line in self.segment_ids
        }

        for idx, leg in enumerate(legs):
            from_partner = leg['from']
            to_partner = leg['to']
            distance = leg['distance'] / 1000  # Convert meters to kilometers
            duration = leg['duration'] / 3600  # Convert seconds to hours

            if (from_partner.id, to_partner.id) in existing_sections:
                # Update existing section
                # R2 (design § 2.1 "Derived-Duration Rule", § D3.2 S3, Viindoo/tvtmaaddons
                # #14279 site 6): route through the same boundary converter every other
                # write site in this file uses instead of a raw division - a degenerate
                # (duration == 0, distance > 0) leg must leave the segment's existing,
                # already-resolved `ave_speed` untouched rather than overwrite it with a
                # fabricated 0.0 (the converter simply omits the key when unusable).
                section = existing_sections[(from_partner.id, to_partner.id)]
                updated_sections.append((1, section.id, {
                    'sequence': idx + 1,
                    **section._prepare_distance_vals_from_provider(distance, duration_s=duration),
                }))
            else:
                # Create a new section - same converter, so a degenerate leg leaves
                # 'ave_speed' out of vals and the new segment's own `_compute_ave_speed`
                # derives the real speed from the route.section cache instead of a
                # fabricated 0.0 (route.mixin also inherits route.section.mixin, so the
                # converter is available on `self` here).
                updated_sections.append((0, 0, {
                    'route_id': self.id,
                    'sequence': idx + 1,
                    'address_from_id': from_partner.id,
                    'address_to_id': to_partner.id,
                    **self._prepare_distance_vals_from_provider(distance, duration_s=duration),
                }))

        # Write updated sections back to the route
        self.write({'segment_ids': updated_sections})

    def _get_addresses(self):
        """Get recordset of the addresses of the current routes in self (could be duplicated and is expected)"""
        return self.waypoint_ids._get_addresses()

    def _group_by_transport_mode_and_addresses(self):
        grouped = defaultdict(lambda: self.env[self._name])
        for r in self:
            grouped[(r.transport_mode, tuple(r.waypoint_ids._get_addresses().ids))] |= r
        return grouped

    def _compute_display_name(self):
        super()._compute_display_name()
        for r in self:
            if r.code and r.name:
                r.display_name = '[%s] %s' % (r.code, r.name)

    @api.model
    def _search_display_name(self, operator, value):
        domain = super()._search_display_name(operator, value)
        if value:
            domain = Domain.OR([domain, [('code', operator, value)]])
        return domain
