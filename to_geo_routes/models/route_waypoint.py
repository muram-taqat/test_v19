from odoo import fields, models


class RouteWaypoint(models.Model):
    _name = 'route.waypoint'
    _inherit = 'waypoint.mixin'
    _description = 'Route Waypoint'
    _order = 'route_id, sequence, id'
    _rec_name = 'address_id'

    # override
    address_id = fields.Many2one(string="Waypoint")
    # new fields
    route_id = fields.Many2one(
        'route.route', string="Route",
        required=True, ondelete='cascade',
        index=True,
        copy=False
    )
    waypoint_area_id = fields.Many2one(
        'route.waypoint.area', related='address_id.waypoint_area_id', readonly=True,
        string='Waypoint Area',
        help='This field is to group waypoints into an area'
    )
