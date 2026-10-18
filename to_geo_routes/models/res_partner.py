import logging

from math import radians, sin, cos, sqrt, atan2, pi

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class Partner(models.Model):
    _inherit = 'res.partner'

    waypoint_area_id = fields.Many2one(
        'route.waypoint.area', string='Waypoint Area', ondelete='restrict',
        help='This field is to group partner addresses into an area',
        index='btree_not_null',
        )
    route_waypoint_ids = fields.One2many(
        'route.waypoint', 'address_id',
        string='Waypoints',
        help="The Waypoints on routes that base on this address"
        )
    route_ids = fields.Many2many('route.route', help="Routes that go through this address")
    routes_count = fields.Integer(string='Routes Count', compute='_compute_routes_count')

    @api.model
    def find_by_coordinates(self, lat, lng, partners=None):
        """
        Find a partner by its coordinates.

        If a subset of partners is provided, search within it first.
        Otherwise, search across the entire database.

        :param float lat: Latitude of the partner.
        :param float lng: Longitude of the partner.
        :param recordset partners: (Optional) Subset of `res.partner` to search within.
        :return: The matching `res.partner` record or None.
        """
        # Check within the provided subset if given
        if partners:
            for partner in partners:
                if partner.partner_latitude == lat and partner.partner_longitude == lng:
                    return partner

        # Fallback to search across the database
        return self.search([('partner_latitude', '=', lat), ('partner_longitude', '=', lng)], limit=1)

    @api.model
    def haversine_distance(self, partner1, partner2):
        """
        Calculate the great-circle distance between two partners using the Haversine formula.

        :param res.partner partner1: The first partner record.
        :param res.partner partner2: The second partner record.
        :return: Distance between the two partners in meters.
        :rtype: float
        :raises ValueError: If either partner does not have valid coordinates.

        Example:

        >>> partner1 = self.env['res.partner'].browse(1)
        >>> partner2 = self.env['res.partner'].browse(2)
        >>> distance = self.haversine_distance(partner1, partner2)
        >>> print(distance)  # Output: 120000.0 (approx.)
        """
        (partner1 + partner2)._validate_coordinates()

        R = 6371000  # Radius of Earth in meters
        lat1, lon1 = radians(partner1.partner_latitude), radians(partner1.partner_longitude)
        lat2, lon2 = radians(partner2.partner_latitude), radians(partner2.partner_longitude)

        delta_lat = lat2 - lat1
        delta_lon = lon2 - lon1

        a = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        c = 2 * atan2(sqrt(a), sqrt(1 - a))
        return R * c

    @api.model
    def rhumline_distance(self, partner1, partner2):
        """
        Calculate the rhumline (loxodrome) distance between two partners.

        :param res.partner partner1: The first partner record.
        :param res.partner partner2: The second partner record.
        :return: Distance between the two partners in meters.
        :rtype: float
        :raises ValueError: If either partner does not have valid coordinates.

        Example:

        >>> partner1 = self.env['res.partner'].browse(1)
        >>> partner2 = self.env['res.partner'].browse(2)
        >>> distance = self.rhumline_distance(partner1, partner2)
        >>> print(distance)  # Output: 130000.0 (approx.)
        """
        (partner1 + partner2)._validate_coordinates()

        R = 6371000  # Radius of Earth in meters
        lat1, lon1 = radians(partner1.partner_latitude), radians(partner1.partner_longitude)
        lat2, lon2 = radians(partner2.partner_latitude), radians(partner2.partner_longitude)

        delta_lon = lon2 - lon1
        delta_lat = lat2 - lat1

        # Adjust delta_lon to handle crossing the International Date Line
        if delta_lon > pi:
            delta_lon -= 2 * pi
        elif delta_lon < -pi:
            delta_lon += 2 * pi

        # Calculate Rhumline distance
        mean_lat = (lat1 + lat2) / 2
        x = delta_lon * cos(mean_lat)  # Horizontal distance
        y = delta_lat  # Vertical distance

        distance = sqrt(x ** 2 + y ** 2) * R
        return distance

    @api.depends('route_ids')
    def _compute_routes_count(self):
        for r in self:
            r.routes_count = len(r.route_ids)

    @api.constrains('waypoint_area_id', 'state_id', 'country_id')
    def _check_waypoint_area_id(self):
        self.waypoint_area_id._check_restricted_by()

    def _has_geo_coordinates(self):
        """Return whether every record in ``self`` is usable as a routing endpoint.

        This is THE single place the coordinate-presence rule is written. A partner
        counts as geolocated only when BOTH axes are non-zero:

        ============  ============  ========  ===========================================
        latitude      longitude     result    why
        ============  ============  ========  ===========================================
        ``0.0``       ``0.0``       False     core's own "not geolocated" marker
        ``0.0``       ``106.7``     False     half-filled data: the latitude is missing
        ``20.86``     ``0.0``       False     half-filled data: the longitude is missing
        ``20.86``     ``106.68``    True      both axes actually measured
        ============  ============  ========  ===========================================

        **Why zero rather than NULL.** ``partner_latitude``/``partner_longitude``
        are ``fields.Float`` declared in ``base``, and ``Float.convert_to_record``
        is ``return value or 0.0`` - a NULL column reads back as ``0.0`` and NEVER
        as ``False``. Presence is therefore not expressible as ``value is False``.
        ``base_geolocalize.write()`` also writes an explicit ``(0.0, 0.0)`` whenever
        an address field changes without both coordinates in the same vals, so core
        itself both manufactures and reads (0,0) as "unset".

        **Deliberately stricter than core's UI convention.** For a ROUTING ENDPOINT
        a single missing axis yields a position that is silently WRONG rather than
        absent. Accepted caveat: a place exactly on the Greenwich meridian or the
        equator would be classified unset - at the field's declared
        ``digits=(10, 7)`` (roughly 1 cm) a real place reads ``0.0012345``, never
        exactly ``0.0000000``, so an exact zero on one axis is a data artifact.

        This answers PRESENCE only. For the ``-90..90``/``-180..180`` test use
        :meth:`_check_coordinate_range`; for both at once use
        :meth:`_validate_coordinates`.

        :return: True only when ``self`` is non-empty and every record carries both axes.
        :rtype: bool
        """
        return bool(self) and all(
            bool(r.partner_latitude) and bool(r.partner_longitude) for r in self
            )

    def _check_coordinate_range(self, raise_if_not_valid=True):
        """Validate that latitude and longitude are within valid ranges.

        RANGE ONLY - this method must never test presence. A half-zero pair such as
        ``(0.0, 106.7)`` is two perfectly in-range numbers and is ACCEPTED here,
        even though :meth:`_has_geo_coordinates` rejects it. That separation is what
        lets the range-only call sites - notably the already-shipped ``18.0.1.0.3``
        migration, which calls with ``raise_if_not_valid=False`` and leaves a row
        untouched when the answer is False - keep their behaviour bit-for-bit while
        :meth:`_validate_coordinates` tightens around them.

        :param bool raise_if_not_valid: report the refusal instead of raising it.
        :return: True when every record is in range; False on the first record that
                 is not, when ``raise_if_not_valid`` is False.
        :rtype: bool
        :raises ValidationError: on an out-of-range coordinate, unless
            ``raise_if_not_valid`` is False.
        """
        for r in self:
            if not (-90 <= r.partner_latitude <= 90 and -180 <= r.partner_longitude <= 180):
                if raise_if_not_valid:
                    raise ValidationError(
                        _(
                            "Invalid coordinates for the %(display_name)s. Lat.: %(partner_latitude)s, Long.: %(partner_longitude)s",
                            display_name=r.display_name,
                            partner_latitude=r.partner_latitude,
                            partner_longitude=r.partner_longitude
                            )
                        )
                return False
        return True

    def _validate_coordinates(self, raise_if_not_valid=True):
        """Validate that latitude and longitude are present AND within valid ranges.

        Composed over :meth:`_has_geo_coordinates` and
        :meth:`_check_coordinate_range` so the presence rule keeps exactly one
        spelling. The signature and the ``ValidationError`` type are deliberately
        unchanged - the call sites in the provider modules depend on both.

        **Behaviour change, intended.** The presence branch this replaces tested
        ``partner_latitude is False``, which a Float can never be, so it had never
        fired: a partner at (0,0) sailed through and got routed from Null Island. It
        is now correctly reported as having no coordinates. A caller that only ever
        cared about range must call :meth:`_check_coordinate_range` instead, which
        preserves the old effective behaviour exactly.
        """
        for r in self:
            if not r._has_geo_coordinates():
                if raise_if_not_valid:
                    raise ValidationError(
                        _(
                            "Coordinate of the %s is not available. Please fill it first.",
                            r.display_name
                            )
                        )
                return False
            if not r._check_coordinate_range(raise_if_not_valid=raise_if_not_valid):
                return False
        return True

    def prepare_matrix_coordinates(self):
        """
        Prepare full origins and destinations without duplicate pairs (e.g., no (A, A)).

        :return: Tuple of origins, destinations, and mapping to res.partner.
        :rtype: tuple (list[tuple], list[tuple], dict)
        """
        if len(self) < 2:
            raise ValidationError(_("At least two partners are required for distance matrix calculations."))

        # Initialize results
        origins = []
        destinations = []
        location_mapping = {}

        # Iterate through all unique pairs (A, B) where A != B
        for origin_partner in self:
            for dest_partner in self:
                if origin_partner == dest_partner:
                    continue  # Skip if origin and destination are the same

                # Add to origins and destinations
                origin_coord = (origin_partner.partner_latitude, origin_partner.partner_longitude)
                dest_coord = (dest_partner.partner_latitude, dest_partner.partner_longitude)
                origins.append(origin_coord)
                destinations.append(dest_coord)

                # Map coordinates to res.partner for easier reference
                location_mapping[(origin_coord, dest_coord)] = (origin_partner, dest_partner)

        return origins, destinations, location_mapping

    def _get_directions(self, transport_mode_providers):
        """
        Compute detailed directions for a sequence of locations using multiple transport mode providers.

        :param recordset transport_mode_providers: A recordset of `transport.mode.provider` defining the providers and transport modes to be used.
        :return: A dictionary mapping each provider to its processed API response data or `None` if an error occurred.
        :rtype: dict

        Example:

        >>> partners = env['res.partner'].browse([1, 2, 3])  # Example locations
        >>> providers = env['transport.mode.provider'].search([('active', '=', True)])
        >>> result = partners._get_directions(providers)
        >>> print(result)
        {
            providerA: {
                'processed': {
                    'distance': 150000,  # Total distance in meters
                    'duration': 5400,    # Total duration in seconds
                    'geometry': 'encoded_polyline_string',  # Encoded polyline
                },
            },
            providerB: None,  # Failed to compute directions for this provider
            ...
        }

        :raises ValidationError: If fewer than two locations are provided in the recordset.

        Note:
            - The first partner in the recordset is treated as the origin.
            - The last partner is treated as the destination.
            - Any intermediate partners are treated as waypoints.
        """
        if len(self) < 2:
            raise ValidationError(_("At least two locations are required for directions calculation."))
        origin = self[0]
        destination = self[-1]
        # Positional slice: a set difference would also drop a middle visit of the origin or destination.
        waypoints = self[1:-1]
        res = {}
        for provider in transport_mode_providers:
            try:
                directions = provider._get_directions(origin, destination, waypoints, res_model=self._name)
                res[provider] = directions
            except Exception as e:
                res[provider] = {'processed': {}}
                _logger.warning(str(e))
        return res

    def _get_optimized_route(self, transport_mode_providers):
        """Return the optimized route of the first provider that can compute it.

        ``self`` is the ordered list of the route's addresses, which may list a stop more
        than once (never twice in a row): the first one is the origin, the last one the
        destination and every one in between is a stop to visit, even when it is the
        origin or destination partner again.

        :raise UserError: the error of the last provider when none of them succeeds, so the
            user learns why the route could not be optimized
        """
        origin, destination = self[0], self[-1]
        # Positional slice, not `self - origin - destination`: a set difference would
        # also drop a middle visit of the origin or destination partner.
        waypoints = self[1:-1]
        last_error = None
        for provider in transport_mode_providers:
            try:
                return provider._get_optimized_route(origin, destination, waypoints)
            except UserError as e:
                _logger.warning(str(e))
                last_error = e
            except Exception as e:
                # An unexpected failure: keep its traceback to diagnose it.
                _logger.warning(str(e), exc_info=True)
                last_error = e
        if isinstance(last_error, UserError):
            raise last_error
        raise ValidationError(self.env._("None of the enabled providers could optimize the route."))

    def _get_distance_matrix(self, transport_mode_providers, stop_at_first_success=True, ignore_failure_provider=True):
        """
        Compute a distance and duration matrix for a set of locations using multiple transport mode providers.

        :param recordset transport_mode_providers: A recordset of `transport.mode.provider` defining the providers and transport modes to be used.
        :param bool stop_at_first_success: Stop as soon as one provider returns a usable matrix.
        :param bool ignore_failure_provider: Keep going when a provider fails, recording `None` for it, instead of raising.
        :return: A dictionary mapping each provider to its processed API response data or `None` if an error occurred.
        :rtype: dict

        Example:

        >>> partners = env['res.partner'].browse([1, 2, 3])  # Example locations
        >>> providers = env['transport.mode.provider'].search([('active', '=', True)])
        >>> result = partners._get_distance_matrix(providers)
        >>> print(result)
        {
            providerA: {
                (partnerA, partnerB): {
                    'distance': 100000,    # Total distance in meters
                    'duration': 3600,      # Total duration in seconds
                    'ave_speed': 27.7778,  # Average speed in m/s
                    'normalized': ((21.0285, 105.8542), (20.9367, 106.3333))  # Adjusted coordinates
                },
                ...
                },
            providerB: None,  # Failed to compute matrix for this provider
            ...
        }

        Note:
            - Each provider is called separately, and results are stored per provider.
            - Errors during matrix computation are logged and the provider result is set to `None`.
        """
        res = {}
        for provider in transport_mode_providers:
            try:
                res[provider] = provider._get_distance_matrix(self)
                if stop_at_first_success:
                    return res
            except Exception as e:
                if not ignore_failure_provider:
                    res[provider] = {}
                _logger.warning(str(e))
        return res
