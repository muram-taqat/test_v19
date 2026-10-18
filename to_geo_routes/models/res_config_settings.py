from odoo import api, models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # HERE
    is_geo_routes_here_available = fields.Boolean(
        string="HERE Geo Routing API Available",
        compute='_compute_geo_routing_modules_available',
        help="Technical field to check if the module `to_geo_routes_here` is available in the system."
        )
    module_to_geo_routes_here = fields.Boolean(
        string="HERE Routing API Integration",
        help="Enable HERE Routing API integration for advanced routing."
    )
    # Google
    is_geo_routes_google_available = fields.Boolean(
        string="Google Geo Routing API Available",
        compute='_compute_geo_routing_modules_available',
        help="Technical field to check if the module `to_geo_routes_google` is available in the system."
        )
    module_to_geo_routes_google = fields.Boolean(
        string="Google Maps Directions & Distance Matrix API Integration",
        help="Enable Google Maps Directions and Distance Matrix API integration for advanced routing."
    )
    # Mapbox
    is_geo_routes_mapbox_available = fields.Boolean(
        string="Mapbox Matrix API Available",
        compute='_compute_geo_routing_modules_available',
        help="Technical field to check if the module `to_geo_routes_mapbox` is available in the system."
        )
    module_to_geo_routes_mapbox = fields.Boolean(
        string="Mapbox Directions & Matrix API Integration",
        help="Enable Mapbox Directions and Matrix API integration for advanced routing."
    )

    # Vietmap
    is_geo_routes_vietmap_available = fields.Boolean(
        string="Vietmap Matrix & Routing API Available",
        compute='_compute_geo_routing_modules_available',
        help="Technical field to check if the module `to_geo_routes_vietmap` is available in the system."
        )
    module_to_geo_routes_vietmap = fields.Boolean(
        string="Vietmap Matrix & Routing API Integration",
        help="Enable Vietmap Matrix & Routing API integration for advanced routing."
    )

    # SeaRoutes
    is_geo_routes_searoutes_available = fields.Boolean(
        string="SeaRoutes Routing API Available",
        compute='_compute_geo_routing_modules_available',
        help="Technical field to check if the module `to_geo_routes_searoutes` is available in the system."
        )
    module_to_geo_routes_searoutes = fields.Boolean(
        string="SeaRoutes Routing API Integration",
        help="Enable integration with SeaRoutes sea routing APIs for maritime transport legs."
    )

    distance_unit = fields.Selection(
        related='company_id.distance_unit',
        readonly=False
    )

    @api.depends('company_id')
    def _compute_geo_routing_modules_available(self):
        self.is_geo_routes_google_available = bool(self.env.ref('base.module_to_geo_routes_google', raise_if_not_found=False))
        self.is_geo_routes_here_available = bool(self.env.ref('base.module_to_geo_routes_here', raise_if_not_found=False))
        self.is_geo_routes_mapbox_available = bool(self.env.ref('base.module_to_geo_routes_mapbox', raise_if_not_found=False))
        self.is_geo_routes_vietmap_available = bool(self.env.ref('base.module_to_geo_routes_vietmap', raise_if_not_found=False))
        self.is_geo_routes_searoutes_available = bool(self.env.ref('base.module_to_geo_routes_searoutes', raise_if_not_found=False))
