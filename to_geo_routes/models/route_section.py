from odoo import fields, models, api

from .route_section_mixin import DISTANCE_STATUS_SELECTION


class RouteSection(models.Model):
    """
    This model represents a route section between two addresses (`address_from_id` and `address_to_id`).
    It stores the details of the section including distance, duration, average speed, and polyline for the route.

    The `transport_mode_ids` field holds the mapping between each transport mode (e.g., driving, cycling) and
    the corresponding distance and duration, either from manually inputted data or fetched from external providers.

    The goal of this model is to provide a structured approach to store route sections and optimize the usage
    of external APIs by caching transport mode distances and durations in `route.section.transport.mode`.
    """
    _name = 'route.section'
    _inherit = ['mail.thread', 'route.section.mixin']
    _description = 'Route Section'

    transport_mode_ids = fields.One2many(
        'route.section.transport.mode', 'route_section_id',
        string='Transport Modes', bypass_search_access=True
        )
    segment_ids = fields.One2many(
        'route.segment', 'route_section_id',
        string='Route segment'
        )
    # override
    address_from_id = fields.Many2one(tracking=True)
    address_to_id = fields.Many2one(tracking=True)
    distance = fields.Float(store=False)
    distance_in_miles = fields.Float(store=False)
    duration = fields.Float(store=False)
    ave_speed = fields.Float(
        compute='_compute_ave_speed', store=False
        )
    ave_speed_in_miles = fields.Float(store=False)
    polyline = fields.Text(compute='_compute_polyline', store=False)
    # new field - the section-level mirror of the cache row's status. `store=False`
    # mirrors `distance` above it, for the same reason: a section has no distance of
    # its own, it PROJECTS one transport mode's, and which one depends on the reading
    # context. The status has to follow the same projection or it explains the wrong
    # row - see `_compute_distance_status`.
    distance_status = fields.Selection(
        DISTANCE_STATUS_SELECTION,
        string="Distance Status",
        compute='_compute_distance_status', store=False,
        help="Why this route section shows the distance it shows. It always describes "
             "the same transport mode the distance itself was taken from."
        )

    _address_from_address_to_unique = models.Constraint(
        'UNIQUE(address_from_id, address_to_id)',
        "Section must be unique!",
    )

    # override
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._generate_route_section_transport_modes_if_not_exists()
        return records

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    def _compute_duration(self):
        super()._compute_duration()

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    def _compute_distance_in_miles(self):
        super()._compute_distance_in_miles()

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    def _compute_ave_speed_in_miles(self):
        super()._compute_ave_speed_in_miles()

    def _inverse_distance_in_miles(self):
        """Override to ignore inverse"""
        pass

    def _inverse_ave_speed_in_miles(self):
        """Override to ignore inverse"""
        pass

    # new methods
    def _get_active_route_section_transport_mode_candidates(self):
        """Return transport mode records ordered by preference for current context."""
        self.ensure_one()
        transport_mode = self.env.context.get('transport_mode') or 'driving'
        forced_provider = self.env.context.get('force_geo_direction_provider')

        # First, narrow down by mode once.
        mode_candidates = self.transport_mode_ids.filtered(
            lambda mode_record: mode_record.mode == transport_mode
        )
        if not mode_candidates:
            return mode_candidates

        if forced_provider:
            return mode_candidates.filtered(
                lambda mode_record: mode_record.provider == forced_provider
            )

        api_candidates = mode_candidates.filtered(
            lambda mode_record: mode_record.map_api_enabled
        )
        if api_candidates:
            return api_candidates

        return mode_candidates.filtered(
            lambda mode_record: mode_record.provider == 'manual'
        )

    def _get_selected_route_section_transport_mode(self):
        """Return THE one transport mode row this section is currently speaking for.

        SSOT for the projection: the distance a section shows and the status
        explaining it must describe the SAME underlying measurement, and a section
        that reports a real distance beside a status explaining a different row's
        failure is worse than one with no status at all.

        Selection, not ordering. `transport_mode_ids[0]` is the LOWEST `sequence`
        across every provider, which is frequently a different row entirely - so a
        `related='transport_mode_ids.distance_status'` would silently describe the
        wrong measurement. The rule here is the module's own: narrow to the
        candidates for the reading context first
        (:meth:`_get_active_route_section_transport_mode_candidates`), then take the
        first one that actually carries a distance; failing that, the
        highest-priority candidate, because when nothing resolved a distance the
        section still owes the operator the reason the preferred provider gives.

        :return: a single-record recordset, or an empty one when the section has no
                 candidate at all for the current context.
        """
        self.ensure_one()
        candidates = self._get_active_route_section_transport_mode_candidates()
        return next(
            (
                mode_record
                for mode_record in candidates
                if mode_record.distance
            ),
            candidates[:1],
        )

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    @api.depends('transport_mode_ids.distance')
    def _compute_distance(self):
        super()._compute_distance()
        for r in self:
            selected = r._get_selected_route_section_transport_mode()
            r.distance = selected.distance if selected else 0.0

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    @api.depends('transport_mode_ids.distance', 'transport_mode_ids.distance_status')
    def _compute_distance_status(self):
        """Mirror the status OF THE ROW THE DISTANCE CAME FROM.

        Depends on `transport_mode_ids.distance` as well as on their status,
        because which row is being spoken for is itself a function of the
        distances - see :meth:`_get_selected_route_section_transport_mode`.
        """
        for r in self:
            selected = r._get_selected_route_section_transport_mode()
            r.distance_status = selected.distance_status if selected else 'no_provider_data'

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    @api.depends('transport_mode_ids.ave_speed')
    def _compute_ave_speed(self):
        for r in self:
            candidates = r._get_active_route_section_transport_mode_candidates()
            selected = next(
                (
                    mode_record
                    for mode_record in candidates
                    if mode_record.ave_speed
                ),
                False,
            )
            r.ave_speed = selected.ave_speed if selected else 0.0

    @api.depends_context('transport_mode', 'force_geo_direction_provider')
    @api.depends('transport_mode_ids.polyline')
    def _compute_polyline(self):
        for r in self:
            candidates = r._get_active_route_section_transport_mode_candidates()
            selected = next(
                (
                    mode_record
                    for mode_record in candidates
                    if mode_record.polyline
                ),
                False,
            )
            r.polyline = selected.polyline if selected else False

    def action_generate_route_section_transport_modes_if_not_exists(self):
        self._generate_route_section_transport_modes_if_not_exists()

    def _generate_route_section_transport_modes_if_not_exists(self):
        """Ensure there is one route.section.transport.mode per section/provider pair."""
        route_section_transport_modes = self.env['route.section.transport.mode']
        if not self:
            return route_section_transport_modes

        mode_providers = self.env['transport.mode.provider'].search([])
        if not mode_providers:
            return route_section_transport_modes

        # Build a set of existing (section_id, provider_id) pairs for O(1) lookup.
        existing_pairs = {
            (mode.route_section_id.id, mode.transport_mode_provider_id.id)
            for mode in self.transport_mode_ids
        }

        vals_list = []
        for r in self:
            for provider in mode_providers:
                pair_key = (r.id, provider.id)
                if pair_key in existing_pairs:
                    continue
                vals_list.append(
                    {
                        'route_section_id': r.id,
                        'transport_mode_provider_id': provider.id,
                    }
                )
                existing_pairs.add(pair_key)

        if not vals_list:
            return route_section_transport_modes

        route_section_transport_modes = route_section_transport_modes.create(vals_list)
        route_section_transport_modes._update_distance_ave_speed()
        return route_section_transport_modes

    def _group_by_address_pair(self):
        default_section = self.env['route.section']
        sections_map = {}
        for r in self:
            key = (r.address_from_id.id, r.address_to_id.id)
            sections_map.setdefault(key, default_section)
            sections_map[key] |= r
        return sections_map
