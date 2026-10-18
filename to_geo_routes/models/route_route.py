from odoo import api, fields, models, _
from odoo.exceptions import UserError


class RouteRoute(models.Model):
    """Route supports single transport mode only (e.g. either car or truck or sea or air, etc), not mixed.
    Future development may add journey (e.g. transportation.journey, transportation.journey.leg) to chain
    multiple routes for a mixed transportation mode in a single journal to support door to door
    transportation / forwarding
    """
    _name = 'route.route'
    _description = 'Route'
    _inherit = 'route.mixin'

    code = fields.Char(string='Code', help="The automatic generated unique code", default='/', required=True, readonly=True, copy=False)
    # override
    waypoint_ids = fields.One2many(
        'route.waypoint', 'route_id', readonly=False
        )
    segment_ids = fields.One2many(
        'route.segment', 'route_id', readonly=False, store=True
    )
    address_ids = fields.Many2many(
        help='The addresses that the route crosses'
        )
    company_id = fields.Many2one(
        help="If not specified, the route is shared for all the companies."
        )

    # override
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('code', '/') == '/':
                company_id = vals.get('company_id', self.default_get(['company_id']).get('company_id'))
                # Ensures things are taken from the right company.
                vals['code'] = self.with_company(company_id).env['ir.sequence'].next_by_code('geo.route.sequence')
        return super().create(vals_list)

    def copy(self, default=None):  # pylint: disable=method-required-super
        """
        Override copy method to prevent duplication of a route.
        The parent class's `copy` method is intentionally not called because
        duplication of routes is not allowed by design.
        """
        raise UserError(_("Duplicate a route is not allowed!"))

    @api.constrains('waypoint_ids', 'transport_mode')
    def _check_waypoint_ids(self):
        super()._check_waypoint_ids()
        similar_routes = self.env['route.route'].search(
            [('waypoint_ids.address_id', 'in', self.waypoint_ids.address_id.ids)]
            )
        grouped = similar_routes._group_by_transport_mode_and_addresses()
        empty_route = self.env['route.route']
        for r in self:
            addresses = r.waypoint_ids._get_addresses()
            existing_routes = grouped.get((r.transport_mode, tuple(addresses.ids)), empty_route)
            overlap = (existing_routes - r)[:1]
            if overlap:
                raise UserError(
                    _(
                        "You are about to create or update a route that includes the same waypoints "
                        "as the existing route, '%(overlap_name)s', which also passes through the following locations:\n%(join)s",
                        overlap_name=overlap.name,
                        join="\n".join(f"{idx}. {address.name}" for idx, address in enumerate(addresses))
                        )
                    )
