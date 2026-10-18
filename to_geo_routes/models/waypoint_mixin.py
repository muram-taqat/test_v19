from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.fields import Domain


class WaypointMixin(models.AbstractModel):
    _name = 'waypoint.mixin'
    _order = 'sequence, id'
    _rec_name = 'address_id'
    _description = 'Waypoint Mixin'

    sequence = fields.Integer(
        string="Sequence", default=10,
        required=True, index=True,
        help="Gives the sequence order when displaying a list of waypoints."
        )
    address_id = fields.Many2one('res.partner', string="Address", required=True, index=True)
    latitude = fields.Float(string='Latitude', related='address_id.partner_latitude')
    longitude = fields.Float(string='Logitude', related='address_id.partner_longitude')
    street = fields.Char(string='Street', related='address_id.street')
    street2 = fields.Char(string='Street 2', related='address_id.street2')
    city = fields.Char(string='City', related='address_id.city')
    state_id = fields.Many2one('res.country.state', string="State", related='address_id.state_id')
    country_id = fields.Many2one('res.country', string="Country", related='address_id.country_id')
    mapping_address = fields.Char(
        string='Mapping Address',
        compute='_compute_mapping_address', compute_sudo=True,
        help="The complete address of the waypoint which will be used for map view"
    )

    @api.depends('address_id')
    def _compute_mapping_address(self):
        for r in self:
            address = r.address_id
            mapping_address = ''
            if address.street:
                mapping_address += address.street + ','
            if address.street2:
                mapping_address += address.street2 + ','
            if address.zip:
                mapping_address += address.zip + ' '
            if address.city:
                mapping_address += address.city + ','
            if address.country_id:
                mapping_address += address.country_id.name
            r.mapping_address = mapping_address or False

    def _get_optimized_route(self, transport_mode_providers):
        addresses = self.env['res.partner']
        for waypoint in self.sorted('sequence'):
            addresses += waypoint.address_id
        return addresses._get_optimized_route(transport_mode_providers)

    def _get_directions(self, transport_mode_providers, stop_at_first_success=True, ignore_failure_provider=True):
        if len(self) < 2:
            raise ValidationError(_("At least two locations are required for directions calculation."))
        origin = self[0]
        destination = self[-1]
        waypoints = self.env['res.partner']
        for wp in self - origin - destination:
            waypoints += wp.address_id
        res = {}
        for provider in transport_mode_providers:
            try:
                directions = provider._get_directions(origin.address_id, destination.address_id, waypoints, res_model=self._name)
                res[provider] = directions
                if stop_at_first_success:
                    return res
            except Exception:
                res[provider] = {'processed': {}}
        return res

    def _get_distance_matrix(self, transport_mode_providers, stop_at_first_success=True, ignore_failure_provider=True):
        return self.address_id._get_distance_matrix(transport_mode_providers, stop_at_first_success, ignore_failure_provider)

    def _get_addresses(self):
        """Get recordset of the addresses of the waypoints in self (could be duplicated and is expected)"""
        addresses = self.env['res.partner']
        # during on-fly computation, self may not be ordered correctly yet.
        # Hence, call sorted here is needed
        for waypoint in self.sorted('sequence'):
            addresses += waypoint.address_id
        return addresses

    def _search_route_sections(self):
        """Search all route.section matching consecutive waypoint pairs."""
        if len(self) < 2:
            return self.env["route.section"]
        key_values = set()
        starts = self[:-1]
        ends = self[1:]
        for wp1, wp2 in zip(starts, ends):
            key_values.add((wp1.address_id.id, wp2.address_id.id))

        candidate_domain = [
            ('address_from_id', 'in', starts.address_id.ids),
            ('address_to_id', 'in', ends.address_id.ids),
        ]
        transport_mode = self.env.context.get('transport_mode')
        if transport_mode:
            candidate_domain = Domain.AND([
                candidate_domain,
                [('transport_mode_ids.mode', '=', transport_mode)]
                ])
        force_geo_direction_provider = self.env.context.get('force_geo_direction_provider')
        if force_geo_direction_provider:
            candidate_domain = Domain.AND([
                candidate_domain,
                [('transport_mode_ids.provider', '=', force_geo_direction_provider)]
                ])

        candidate_sections = self.env['route.section'].search(candidate_domain)
        candidate_sections = candidate_sections.filtered(
            lambda s: any(
                transport_mode.map_api_enabled or transport_mode.provider == 'manual'
                for transport_mode in s.transport_mode_ids
            )
        )

        matched_sections = self.env['route.section']
        for section in candidate_sections:
            key = (section.address_from_id.id, section.address_to_id.id)
            if key in key_values:
                matched_sections |= section
        return matched_sections

    def _prepare_segment_vals_list(self):
        sections = self._search_route_sections()
        km2mile = self.env['to.base'].km2mile

        # Build a fast lookup: (from_addr_id, to_addr_id) -> section
        section_by_pair = {
            (section.address_from_id.id, section.address_to_id.id): section
            for section in sections
        }

        vals_list = []
        previous_waypoint = False
        for r in self:
            if not previous_waypoint:
                previous_waypoint = r
                continue

            from_address_id = previous_waypoint.address_id.id
            to_address_id = r.address_id.id
            # if from_address_id == to_address_id:
            #     raise ValidationError(_(
            #         "Could not build a segment from two waypoints with the same address: %s",
            #         r.address_id.display_name
            #     ))
            section = section_by_pair.get((from_address_id, to_address_id))

            vals = {
                "address_from_id": from_address_id,
                "address_to_id": to_address_id,
                "sequence": previous_waypoint.sequence,
            }
            if section:
                # Seed the miles fields alongside their km counterparts so the
                # create-time miles inverses (_inverse_distance_in_miles /
                # _inverse_ave_speed_in_miles) read the real value, not the
                # field default 0.0 (which would zero ave_speed - #14279 path 7).
                # The km value is authoritative: seed it FIRST so the
                # distance/ave_speed inverse runs on the true value before any
                # miles inverse can round-trip it - mile2km(km2mile(x)) is not
                # bit-identical for every x, so key order here is load-bearing.
                vals.update({
                    "route_section_id": section.id,
                    "distance": section.distance,
                    "distance_in_miles": km2mile(section.distance),
                    "ave_speed": section.ave_speed,
                    "ave_speed_in_miles": km2mile(section.ave_speed),
                })

            vals_list.append(vals)
            previous_waypoint = r

        return vals_list
