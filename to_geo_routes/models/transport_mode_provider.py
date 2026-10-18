from collections import defaultdict

from odoo import api, models, fields, _
from odoo.tools import ormcache
from odoo.exceptions import ValidationError

TRANSPORT_MODES = [
    ('driving', 'Driving'),
    ('cycling', 'Cycling'),
    ('transit', 'Transit (Public Transport)'),
    ('truck', 'Truck (Freight)'),
    ('water', 'Waterway (Boat / Ship / Ferry)'),
    ('air', 'Air (Flight)'),
    ('rail', 'Rail (Train)'),
    ]


class TransportModeProvider(models.Model):
    _name = 'transport.mode.provider'
    _order = 'sequence, id'
    _description = 'Transport Mode and Provider Mapping'

    sequence = fields.Integer('Sequence', default=1)
    name = fields.Char(compute='_compute_name', translate=True)
    provider = fields.Selection(
        [
            ('manual', 'Manual Input'),
        ],
        string='Provider',
        required=True,
        readonly=True,
        default='manual',
        help="Select the geographic routing provider for this transport mode and provider mapping:\n"
        "- HERE Maps: Provides comprehensive routing solutions, including driving, cycling, trucking and public transit.\n"
        "- Google Maps: Offers global mapping and routing services, including driving, cycling, and public transit (Directions and Distance Matrix APIs).\n"
        "- Mapbox: A versatile platform for customized maps and navigation, ideal for driving and cycling.\n"
        "- VietMap: A local provider optimized for transportation in Vietnam, supporting driving and cycling.\n"
        "- SeaRoutes: A specialized provider for maritime routing, ideal for sea transport legs.\n"
        "- Manual Input: Use this option to input distances and durations manually without relying on a provider.\n"
        "\n"
        "Note:\n"
        "To use external routing providers, the corresponding add-on modules must be installed and enabled "
        "in Settings (e.g., to_geo_routes_google, to_geo_routes_here, to_geo_routes_mapbox, "
        "to_geo_routes_vietmap, to_geo_routes_searoutes). Some providers may require API subscriptions "
        "or additional purchases."
    )

    transport_mode = fields.Selection(
        TRANSPORT_MODES, string='Transport Mode', required=True, readonly=True
        )
    provider_specific_key = fields.Char(
        string='Provider Specific Key',
        required=True, readonly=True,
        help="Key specific to the provider for profile of this transport mode"
    )
    active = fields.Boolean(string='Active', default=True, help="Is this mode active?")
    map_api_enabled = fields.Boolean(
        string='Map API Enabled',
        compute='_compute_map_api_enabled', compute_sudo=True,
        help="This is to indicate of the related provider's API is enabled for the current company"
        )
    ave_speed = fields.Float(
        string='Ave. Speed (km/h)',
        aggregator="avg",
        help="The average speed in kilometers per hour used for the Manual Input option, where no map provider is needed."
        )
    ave_speed_in_miles = fields.Float(
        string='Ave. Speed (mph)',
        compute='_compute_ave_speed_in_miles', store=True,
        inverse='_inverse_ave_speed_in_miles',
        aggregator="avg",
        help="The average speed in miles per hour used for the Manual Input option, where no map provider is needed."
        )
    show_speed_in_miles = fields.Boolean(compute='_compute_show_speed_in_miles')

    @api.model
    @ormcache('transport_mode', 'self.provider', 'self.transport_mode', 'self.ave_speed', 'self.ave_speed_in_miles')
    def _get_default_manual_speed(self, transport_mode):
        """
        Fetch the default manual speeds (km/h and mph) for the manual transport mode.

        :param str transport_mode: The transport mode (e.g., 'driving', 'cycling', 'transit', 'truck').
        :return: A tuple containing the average speed in km/h and mph.
        :rtype: tuple
        :raises ValidationError: If the given transport mode is invalid
        """
        # Validate transport_mode
        transport_modes = dict(self._fields['transport_mode']._description_selection(self.env))
        valid_modes = transport_modes.keys()
        if transport_mode not in valid_modes:
            raise ValidationError(
                _(
                    "Programming Error! The provided transport mode must be one of: %s.",
                    ", ".join(transport_modes.values())
                    )
                )

        manual_mode = self.env['transport.mode.provider'].with_context(active_test=False).search([
            ('provider', '=', 'manual'),
            ('transport_mode', '=', transport_mode)
            ], limit=1
        )
        return manual_mode.ave_speed or 0.0, manual_mode.ave_speed_in_miles or 0.0

    @api.depends('ave_speed')
    def _compute_ave_speed_in_miles(self):
        km2mile = self.env['to.base'].km2mile
        for r in self:
            r.ave_speed_in_miles = km2mile(r.ave_speed)

    @api.onchange('ave_speed_in_miles')
    def _inverse_ave_speed_in_miles(self):
        mile2km = self.env['to.base'].mile2km
        for r in self:
            r.ave_speed = mile2km(r.ave_speed_in_miles)

    @api.depends_context('company')
    def _compute_show_speed_in_miles(self):
        for r in self:
            r.show_speed_in_miles = self.env.company.distance_unit == 'miles'

    @api.depends_context('company')
    def _compute_map_api_enabled(self):
        grp = defaultdict(lambda: self.env['transport.mode.provider'])
        for r in self:
            grp[r.provider] |= r

        map_keys = self.env.company._get_map_routing_key_fields()
        for provider_key, providers in grp.items():
            field = map_keys.get(provider_key)
            if field and field in self.env.company._fields:
                providers.map_api_enabled = bool(self.env.company[field])
            else:
                providers.map_api_enabled = False

    @api.depends('transport_mode', 'provider')
    def _compute_name(self):
        transport_modes = dict(self._fields['transport_mode']._description_selection(self.env))
        providers = dict(self._fields['provider']._description_selection(self.env))
        for r in self:
            if r.transport_mode and r.provider:
                r.name = "%s [%s]" % (transport_modes[r.transport_mode], providers[r.provider])
            elif r.transport_mode:
                r.name = "%s" % transport_modes[r.transport_mode]
            elif r.provider:
                r.name = "%s" % providers[r.provider]
            else:
                r.name = False

    def _get_directions(self, origin, destination, waypoints=None, res_model=None, res_id=None):
        """
        Fetch detailed directions using the configured provider for this transport mode.
        Automatically extracts and logs error messages from the API response if applicable.

        :param recordset origin: A `res.partner` record representing the origin location.
        :param recordset destination: A `res.partner` record representing the destination location.
        :param recordset waypoints: An optional recordset of `res.partner` representing intermediate waypoints.
        :return: A dictionary containing processed API response data from the provider.
            - `processed`:
                - `distance` (int): Total distance in meters.
                - `duration` (int): Total duration in seconds.
                - `geometry` (str): Encoded polyline string representing the route.
        :rtype: dict

        Example:

        >>> provider = env['transport.mode.provider'].browse(1)  # Example provider record
        >>> origin = env['res.partner'].browse(1)  # Example origin
        >>> destination = env['res.partner'].browse(2)  # Example destination
        >>> waypoints = env['res.partner'].browse([3, 4])  # Example waypoints
        >>> result = provider._get_directions(origin, destination, waypoints)
        >>> print(result)
        {
            'processed': {
                'distance': 150000,  # Total distance in meters
                'duration': 5400,    # Total duration in seconds
                'geometry': 'encoded_polyline_string',  # Encoded polyline
            },
        }
        """
        # Determine the model for the provider
        model = f'geo.direction.{self.provider}'

        # Fetch directions using the provider's model
        res = self.env[model]._get_directions(
            origin,
            destination,
            self.provider_specific_key,
            waypoints,
            res_model=res_model,
            res_id=res_id,
            )

        return res

    def _get_optimized_route(self, origin, destination, waypoints):
        return self.env[f'geo.direction.{self.provider}']._solve_tsp(origin, destination, waypoints, self.provider_specific_key)

    def _get_distance_matrix(self, partners, res_model=None, res_id=None):
        """
        Compute a distance and duration matrix using the configured provider for this transport mode.
        Automatically extracts and logs error messages from the API response if applicable.

        :param recordset partners: A recordset of `res.partner` representing the locations to include in the matrix.
        :return: A dictionary mapping pairs of `res.partner` to their distance, duration, and normalized coordinates:
            - `distance` (int): Distance in meters between the pair.
            - `duration` (int): Duration in seconds between the pair.
            - `normalized` (tuple): Adjusted coordinates of the pair.
        :rtype: dict

        Example:

        >>> provider = env['transport.mode.provider'].browse(1)  # Example provider record
        >>> partners = env['res.partner'].browse([1, 2, 3])  # Example partner IDs
        >>> result = provider._get_distance_matrix(partners)
        >>> print(result)
        {
            (partnerA, partnerB): {
                'distance': 100000,    # Total distance in meters
                'duration': 3600,      # Total duration in seconds
                'ave_speed': 27.7778,  # Average speed in m/s
                'normalized': ((21.0285, 105.8542), (20.9367, 106.3333))  # Adjusted coordinates
            },
            ...
        }
        """
        model = f'geo.direction.{self.provider}'
        data = self.env[model]._get_distance_matrix(
            partners,
            self.provider_specific_key,
            res_model,
            res_id
            )
        return data.get('processed') or {}

    def _group_by_transport_mode(self):
        empty_provider = self.env['transport.mode.provider']
        grouped = defaultdict(lambda: empty_provider)
        for r in self:
            grouped[r.transport_mode] |= r
        return grouped
