import logging
import polyline

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from odoo import api, models, _
from odoo.fields import Domain
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Cost OR-Tools gives a pair of locations missing from the distance matrix (an integer, so it
# cannot overflow like float('inf') does, yet higher than any real road distance in meters).
MISSING_ARC_COST = 2 ** 40
# Time a route optimization may spend searching for a route that avoids unknown pairs.
TSP_LOCAL_SEARCH_SECONDS = 5


class GeoDirection(models.AbstractModel):
    _name = 'geo.direction'
    _inherit = ['route.failure.report.mixin']
    _description = 'Base for GeoDirection Implementations'

    @api.model
    def _compute_haversine_matrix(self, partners, mode, ave_speed=50.0):
        """
        Compute a distance and duration matrix using Haversine formula.

        :param recordset partners: A recordset of res.partner representing all points.
        :param str mode: Transport mode (e.g., 'driving', 'cycling').
        :param float ave_speed: Assume an average speed in km/h (default is 50 km/h).
        :return: A dictionary mapping partner pairs to distance, duration, and normalized coordinates.
        :rtype: dict

        Example:

        >>> partners = self.env['res.partner'].browse([1, 2, 3, 4])
        >>> result = self._compute_haversine_matrix(partners, ave_speed=60.0)
        >>> print(result)
        {
            (partnerA, partnerB): {
                'distance': 100000,  # Total distance in meters
                'duration': 7200,    # Total duration in seconds
                'ave_speed': 27.7778 # Average speed in m/s
                'normalized': ((21.0285, 105.8542), (20.9367, 106.3333))  # Adjusted coordinates
            },
            ...
        }
        """
        # Validate coordinates for all partners
        partners._validate_coordinates()

        if ave_speed <= 0:
            raise ValidationError(_("Average speed must be greater than 0 to compute duration."))

        # Initialize the result matrix
        matrix_result = {}
        domain_conditions = []

        # Prepare domain for searching existing route sections
        for origin in partners:
            for destination in partners:
                if origin == destination:
                    continue
                matrix_result[(origin, destination)] = {}
                domain_conditions.append([
                    ('address_from_id', '=', origin.id),
                    ('address_to_id', '=', destination.id),
                    ('distance', 'not in', [0, False]),
                    ('mode', '=', mode)
                ])

        # Flatten the domain conditions
        route_section_domain = Domain.OR(domain_conditions) if domain_conditions else []

        # Search for existing route section data
        existing_data = self.env['route.section.transport.mode'].search_read(
            domain=route_section_domain,
            fields=['address_from_id', 'address_to_id', 'distance', 'duration']
        )

        # Prepare existing data as a dictionary for quick lookup
        existing_data_map = {
            (rec['address_from_id'][0], rec['address_to_id'][0]): rec
            for rec in existing_data
        }

        # Haversine distance calculator
        haversine_distance = self.env['res.partner'].haversine_distance
        ave_speed_ms = self.env['to.base'].kmh_to_ms(ave_speed)

        # Calculate distances and durations
        for (origin, destination), data in matrix_result.items():
            # Get coordinates
            origin_coords = (origin.partner_latitude, origin.partner_longitude)
            destination_coords = (destination.partner_latitude, destination.partner_longitude)
            data['normalized'] = (origin_coords, destination_coords)

            # Check if pre-existing data matches
            matched = existing_data_map.get((origin.id, destination.id))
            if matched:
                data.update({
                    'distance': matched['distance'] * 1000,  # Distance in meters
                    'duration': matched['duration'] * 3600,  # Duration in seconds
                    'ave_speed': ave_speed_ms,  # Average speed in m/s
                })
            else:
                # Calculate distance and duration using Haversine
                distance = haversine_distance(origin, destination)  # Distance in meters
                duration = distance / ave_speed_ms  # duration in seconds
                data.update({
                    'distance': distance,
                    'duration': duration,
                    'ave_speed': ave_speed_ms,  # Average speed in m/s
                })

        return matrix_result

    def _validate_normalized_directions_data(self, data):
        """
        Validate if the input data conforms to the required normalized structure for directions.

        :param dict data: The data to validate.
        :raises ValidationError: If the data is invalid.
        :return: True if validation passes.
        :rtype: bool

        **Expected Structure:**
        The data should follow the structure:
        {
            "distance": 7847,  # Total distance in meters (non-negative number)
            "duration": 1620,  # Total duration in seconds (non-negative number)
            "waypoints": [
                {"lat": 52.5208, "lng": 13.4093, "name": "Waypoint"},
                {"lat": 52.521415, "lng": 13.413411, "name": "S+U Alexanderplatz Bhf"}
            ],
            "geometry": "Encoded polyline string representing the entire route",
            "sections": [
                {
                    "distance": 50000,  # Distance of the section in meters
                    "duration": 1800,   # Duration of the section in seconds
                    "summary": "From Point A to Point B",
                    "polyline": "Encoded polyline string for the section",
                    "transport_mode": "car",
                    "steps": [
                        {
                            "distance": 25000,  # Distance of the step in meters
                            "duration": 900,    # Duration of the step in seconds
                            "summary": "From Point A to Point A1",
                            "polyline": "Encoded polyline string for the step"
                        }
                    ]
                }
            ]
        }

        **Validation Rules:**
        - `distance` and `duration` must be non-negative numbers.
        - `geometry` must be a non-empty string.
        - `waypoints` must be a list of dictionaries with keys: `lat`, `lng`, and `name`.
        - Each `section` must have `distance`, `duration`, `summary`, `polyline`, `transport_mode`, and `steps`.
        - Each `step` must include `distance`, `duration`, `summary`, and `polyline`.

        **Example:**
        >>> data = {
        >>>     "distance": 7847,
        >>>     "duration": 1620,
        >>>     "waypoints": [{"lat": 52.5208, "lng": 13.4093, "name": "Waypoint"}],
        >>>     "geometry": "BGikzlkDq...",
        >>>     "sections": [
        >>>         {
        >>>             "distance": 5000,
        >>>             "duration": 600,
        >>>             "summary": "From Point A to Point B",
        >>>             "polyline": "BHw2-...",
        >>>             "transport_mode": "car",
        >>>             "steps": [
        >>>                 {"distance": 2500, "duration": 300, "summary": "From Point A to Point A1", "polyline": "BGox..."}
        >>>             ]
        >>>         }
        >>>     ]
        >>> }
        >>> self._validate_normalized_directions_data(data)
        True
        """
        # Validate the root keys
        required_keys = {"distance", "duration", "waypoints", "geometry", "sections"}
        if not isinstance(data, dict):
            raise ValidationError(_("Data must be a dictionary."))

        missing_keys = required_keys - data.keys()
        if missing_keys:
            raise ValidationError(_("Missing required keys: %s") % ", ".join(missing_keys))

        # Validate distance and duration
        if not isinstance(data["distance"], (int, float)) or data["distance"] < 0:
            raise ValidationError(_("`distance` must be a non-negative number."))
        if not isinstance(data["duration"], (int, float)) or data["duration"] < 0:
            raise ValidationError(_("`duration` must be a non-negative number."))

        # Validate geometry
        if not isinstance(data["geometry"], str) or not data["geometry"]:
            raise ValidationError(_("`geometry` must be a non-empty string."))

        # Validate waypoints
        if not isinstance(data["waypoints"], list):
            raise ValidationError(_("`waypoints` must be a list."))
        for waypoint in data["waypoints"]:
            if not isinstance(waypoint, dict):
                raise ValidationError(_("Each waypoint must be a dictionary."))
            if not {"lat", "lng"}.issubset(waypoint.keys()):
                raise ValidationError(_("Each waypoint must have `lat` and `lng` keys."))
            if not isinstance(waypoint["lat"], (int, float)):
                raise ValidationError(_("`lat` in waypoints must be a number."))
            if not isinstance(waypoint["lng"], (int, float)):
                raise ValidationError(_("`lng` in waypoints must be a number."))

        # Validate sections
        if not isinstance(data["sections"], list):
            raise ValidationError(_("`sections` must be a list."))
        for section in data["sections"]:
            if not isinstance(section, dict):
                raise ValidationError(_("Each section must be a dictionary."))
            required_section_keys = {"distance", "duration", "summary", "polyline", "transport_mode", "steps"}
            missing_section_keys = required_section_keys - section.keys()
            if missing_section_keys:
                raise ValidationError(_("Missing required keys in section: %s") % ", ".join(missing_section_keys))

            # Validate distance and duration in section
            if not isinstance(section["distance"], (int, float)) or section["distance"] < 0:
                raise ValidationError(_("`distance` in sections must be a non-negative number."))
            if not isinstance(section["duration"], (int, float)) or section["duration"] < 0:
                raise ValidationError(_("`duration` in sections must be a non-negative number."))

            # Validate polyline in section
            if not isinstance(section["polyline"], str) or not section["polyline"]:
                raise ValidationError(_("`polyline` in sections must be a non-empty string."))

            # Validate transport_mode
            if not isinstance(section["transport_mode"], str) or not section["transport_mode"]:
                raise ValidationError(_("`transport_mode` in sections must be a non-empty string."))

            # Validate steps
            if not isinstance(section["steps"], list):
                raise ValidationError(_("`steps` in sections must be a list."))
            for step in section["steps"]:
                if not isinstance(step, dict):
                    raise ValidationError(_("Each step must be a dictionary."))
                required_step_keys = {"distance", "duration", "summary", "polyline"}
                missing_step_keys = required_step_keys - step.keys()
                if missing_step_keys:
                    raise ValidationError(_("Missing required keys in step: %s") % ", ".join(missing_step_keys))

                # Validate distance and duration in step
                if not isinstance(step["distance"], (int, float)) or step["distance"] < 0:
                    raise ValidationError(_("`distance` in steps must be a non-negative number."))
                if not isinstance(step["duration"], (int, float)) or step["duration"] < 0:
                    raise ValidationError(_("`duration` in steps must be a non-negative number."))

                # Validate polyline in step
                if not isinstance(step["polyline"], str) or not step["polyline"]:
                    raise ValidationError(_("`polyline` in steps must be a non-empty string."))

                # Validate summary
                if not isinstance(step["summary"], str) or not step["summary"]:
                    raise ValidationError(_("`summary` in steps must be a non-empty string."))

        # If all checks pass
        return True

    def _build_response(self, processed_data):
        """
        Construct a unified response structure and automatically include the provider's name based on
        the model's `_name`.

        :param dict processed_data: Parsed and structured data for application usage.
        :return: Combined response structure, including the provider name and processed data from API response.
        :rtype: dict

        Example:

        >>> processed_data = {
        ...     'optimized_route': [partner_1, partner_2, partner_3],
        ...     'total_distance': 12000,
        ...     'total_duration': 1800,
        ...     'legs': [
        ...         {'from': partner_1, 'to': partner_2, 'distance': 6000, 'duration': 900},
        ...         {'from': partner_2, 'to': partner_3, 'distance': 6000, 'duration': 900},
        ...     ],
        ... }
        >>> result = self._build_response(processed_data)
        >>> print(result)
        {
            'provider': 'google',
            'processed': {
                'optimized_route': [partner_1, partner_2, partner_3],
                'total_distance': 12000,
                'total_duration': 1800,
                'legs': [
                    {'from': partner_1, 'to': partner_2, 'distance': 6000, 'duration': 900},
                    {'from': partner_2, 'to': partner_3, 'distance': 6000, 'duration': 900},
                ],
            },
        }
        """
        return {
            'provider': self._get_geo_direction_provider(),
            'processed': processed_data,
        }

    @api.model
    def _get_directions(self, origin, destination, mode, waypoints=None, res_model=None, res_id=None, timeout=30):
        """
        Fetch detailed directions between two points.

        :param recordset origin: A `res.partner` record representing the origin. Must have latitude and longitude.
        :param recordset destination: A `res.partner` record representing the destination. Must have latitude and longitude.
        :param str mode: Transport mode (e.g., 'driving', 'cycling').
        :param recordset waypoints: An optional recordset of `res.partner` representing waypoints.
                                     Must have latitude and longitude.
        :param str res_model: Optional model name the call is made on behalf of, logged with the request.
        :param int res_id: Optional record id the call is made on behalf of, logged with the request.
        :param float timeout: Request timeout in seconds, honoured by the providers that issue HTTP calls.
        :return: A dictionary containing the distance, duration, and geometry.
        :rtype: dict
        :raises ValidationError: If any of the partners do not have valid latitude and longitude.
        :raises NotImplementedError: If the method is not implemented in a derived class.

        Example:

        >>> origin = env['res.partner'].browse(1)  # Example origin
        >>> destination = env['res.partner'].browse(2)  # Example destination
        >>> waypoints = env['res.partner'].browse([3, 4])  # Example waypoints
        >>> mode = 'driving'
        >>> result = obj._get_directions(origin, destination, mode, waypoints)
        >>> print(result)
        {
            'processed': {
                'distance': 150000,  # Total distance in meters
                'duration': 5400,    # Total duration in seconds
                'geometry': 'encoded_polyline_string'  # Encoded polyline
            }
        }
        """
        raise NotImplementedError(
            _("Method `_get_directions` must be implemented in the provider class.")
        )

    @api.model
    def _get_distance_matrix(self, partners, mode, res_model=None, res_id=None, timeout=30):
        """
        Compute a distance and duration matrix for a set of points.

        :param recordset partners: A recordset of res.partner.
        :param str mode: Transport mode (e.g., 'driving', 'cycling').
        :return: A dictionary mapping partner pairs to distance, duration, and normalized coordinates.
        :rtype: dict
        :raises NotImplementedError: If the method is not implemented in a derived class.

        Example:

        >>> result = obj._get_distance_matrix(partners, mode='driving')
        >>> print(result)
        {
            (partnerA, partnerB): {
                'distance': 100000,    # Total distance in meters
                'duration': 3600,      # Total duration in seconds
                'ave_speed': 27.7778,  # Average speed in m/s
                'normalized': ((21.0285, 105.8542), (20.9367, 106.3333))
            },
            ...
        }
        """
        raise NotImplementedError(
            _("Method `_get_distance_matrix` must be implemented in the provider class.")
        )

    @api.model
    def encode_polyline(self, coordinates):
        """
        Encode a list of coordinates into an encoded polyline

        :param list coordinates: the list of coordinates to encode into a polyline
            Example:
            [
                (21.0285, 105.8542),
                (20.9500, 106.2000),
                (20.9367, 106.3333)
            ]
        :return: encoded polyline
        :rtype: str
        """
        return polyline.encode(coordinates)

    @api.model
    def decode_polyline(self, encoded_polyline):
        """
        Decode an encoded polyline

        :param str encoded_polyline: The encoded polyline string.
        :return: list of points.
            Example:
            [
                (21.0285, 105.8542),
                (20.9500, 106.2000),
                (20.9367, 106.3333)
            ]
        :rtype: list[tuple]
        """
        return polyline.decode(encoded_polyline)

    @api.model
    def merge_polilines(self, encoded_polylines):
        all_coordinates = []
        for encoded_polyline in encoded_polylines:
            coordinates = self.decode_polyline(encoded_polyline)
            all_coordinates.extend(coordinates)
        return self.encode_polyline(all_coordinates)

    @api.model
    def _solve_tsp(self, origin, destination, waypoints, mode):
        """
        Solve the Traveling Salesman Problem (TSP) using OR-Tools based on the computed distance matrix.

        This method determines the optimal route that visits all waypoints starting from the origin
        and ending at the destination. The route is optimized for the given transportation mode.

        :param recordset origin: A `res.partner` record representing the starting point.
        :param recordset destination: A `res.partner` record representing the ending point.
        :param recordset waypoints: A recordset of `res.partner` representing intermediate stops.
        :param str mode: The transport mode for optimization (e.g., 'driving', 'cycling').
        :return: A dictionary containing the optimized route, total distance, total duration, and leg details.
            For a round trip (origin == destination), `optimized_route` and `legs` end back at the origin.
        :rtype: dict
        :raises ValidationError: If the method is called directly from the base class (`geo.direction`) instead of a derived provider class.
        :raises ValidationError: If the best route has to use a pair of locations the distance matrix has no distance for.
        :raises ValidationError: If every route would visit a stop listed more than once twice in a row.

        A stop may be listed more than once (e.g. origin A, waypoints B, C, B): it is then visited once per
        listing, and two visits of the same partner are never consecutive.

        Example:

        >>> origin = env['res.partner'].browse(1)  # Starting point (e.g., Partner ID 1)
        >>> destination = env['res.partner'].browse(4)  # Ending point (e.g., Partner ID 4)
        >>> waypoints = env['res.partner'].browse([2, 3])  # Intermediate stops (e.g., Partner IDs 2, 3)
        >>> mode = 'driving'  # Transport mode
        >>> result = obj._solve_tsp(origin, destination, waypoints, mode)
        >>> print(result)
        {
            'provider': 'google',
            'processed': {
                'optimized_route': [partner_1, partner_3, partner_2, partner_4],  # Optimal visit sequence
                'total_distance': 15000,  # Total distance in meters
                'total_duration': 1800,  # Total duration in seconds
                'legs': [  # Details of each segment in the route
                    {'from': partner_1, 'to': partner_3, 'distance': 5000, 'duration': 600},
                    {'from': partner_3, 'to': partner_2, 'distance': 7000, 'duration': 800},
                    {'from': partner_2, 'to': partner_4, 'distance': 3000, 'duration': 400},
                ],
            }
        }
        """

        if self._name == 'geo.direction':
            raise ValidationError(_(
                "Programming Error! Directly calling `_solve_tsp` from the base class `geo.direction` is not allowed. "
                "You must call this method from a derived provider class, such as `geo.direction.manual`, "
                "`geo.direction.google`, etc."
            ))

        if origin == destination and not waypoints:
            raise ValidationError(_("Origin and destination cannot be the same if there are no intermediate waypoints."))

        # A round trip starts and ends on the same node; otherwise the destination is its own end node.
        round_trip = origin == destination
        all_points = origin + waypoints if round_trip else origin + waypoints + destination
        num_locations = len(all_points)

        if num_locations < 2:
            raise ValidationError(_("At least two locations are required for route optimization."))

        # Fetch the distance matrix
        response = self._get_distance_matrix(all_points, mode)
        matrix_result = response['processed']

        def is_repeated_visit(from_node, to_node):
            """Whether the arc goes between two different nodes of the same partner (a stop listed twice)."""
            return from_node != to_node and all_points[from_node] == all_points[to_node]

        def get_arc(from_node, to_node):
            """Return the matrix cell between two nodes, a zero leg for the same node, or None when the pair
            is unknown or forbidden.

            Visiting a stop listed twice back to back is forbidden: the route would get a leg from a
            location to itself, which a route rejects as adjacent duplicate waypoints.
            """
            if from_node == to_node:
                return {'distance': 0, 'duration': 0}
            if is_repeated_visit(from_node, to_node):
                return None
            arc = matrix_result.get((all_points[from_node], all_points[to_node]))
            if not arc or arc.get('distance') is None:
                return None
            return arc

        # Create OR-Tools Routing Model with a fixed start (origin) and end (destination) node
        end_node = 0 if round_trip else num_locations - 1
        manager = pywrapcp.RoutingIndexManager(num_locations, 1, [0], [end_node])
        routing = pywrapcp.RoutingModel(manager)

        def distance_callback(from_index, to_index):
            arc = get_arc(manager.IndexToNode(from_index), manager.IndexToNode(to_index))
            # OR-Tools needs an integer cost: a pair the provider could not compute, or two visits
            # of the same stop in a row, gets a cost high enough to be used only when no route avoids it.
            return MISSING_ARC_COST if arc is None else int(round(arc['distance']))

        transit_callback_index = routing.RegisterTransitCallback(distance_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

        # Set search parameters
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC

        def solution_nodes(solution):
            """Visit sequence from origin to destination (back to the origin for a round trip)"""
            nodes = []
            index = routing.Start(0)
            while not routing.IsEnd(index):
                nodes.append(manager.IndexToNode(index))
                index = solution.Value(routing.NextVar(index))
            nodes.append(manager.IndexToNode(index))
            return nodes

        # Solve the routing problem
        solution = routing.SolveWithParameters(search_parameters)
        if not solution:
            raise ValidationError(_("Unable to optimize the route. Please check the data."))
        nodes = solution_nodes(solution)
        if any(get_arc(a, b) is None for a, b in zip(nodes, nodes[1:])):
            # The greedy first solution may take an unknown or forbidden pair although a route
            # without one exists: let a time-bounded local search look for it before giving up.
            search_parameters.local_search_metaheuristic = (
                routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
            )
            search_parameters.time_limit.FromSeconds(TSP_LOCAL_SEARCH_SECONDS)
            improved_solution = routing.SolveWithParameters(search_parameters)
            if improved_solution:
                nodes = solution_nodes(improved_solution)

        legs = []
        unknown_pairs = []
        repeated_stops = []
        total_distance = 0
        total_duration = 0
        for from_node, to_node in zip(nodes, nodes[1:]):
            if is_repeated_visit(from_node, to_node):
                repeated_stops.append(all_points[from_node].display_name)
                continue
            arc = get_arc(from_node, to_node)
            if arc is None:
                unknown_pairs.append(
                    f"{all_points[from_node].display_name} -> {all_points[to_node].display_name}"
                )
                continue
            leg_distance = arc.get('distance', 0)
            leg_duration = arc.get('duration') or 0
            total_distance += leg_distance
            total_duration += leg_duration
            legs.append({
                'from': all_points[from_node],
                'to': all_points[to_node],
                'distance': leg_distance,
                'duration': leg_duration,
            })
        if unknown_pairs:
            raise ValidationError(self.env._(
                "Unable to optimize the route: the distance between the following locations is unknown: %(pairs)s",
                pairs=", ".join(unknown_pairs),
            ))
        if repeated_stops:
            raise ValidationError(self.env._(
                "Unable to optimize the route: no order was found that avoids visiting the following locations"
                " twice in a row: %(locations)s",
                locations=", ".join(repeated_stops),
            ))
        optimized_route = [all_points[node] for node in nodes]

        processed_data = {
            'optimized_route': optimized_route,  # List of res.partner
            'total_distance': total_distance,
            'total_duration': total_duration,
            'legs': legs,  # Details for each leg with res.partner objects
        }
        return self._build_response(processed_data=processed_data)

    @api.model
    def _get_geo_direction_provider(self):
        """Extract provider from the model name"""
        provider = self.env.context.get('force_geo_direction_provider')
        if not provider:
            if self._name.startswith('geo.direction.'):
                provider = self._name.split('.')[-1]
        if not provider:
            raise ValidationError(_("Programming Error! Unknown Geo Direction Provider"))
        return provider

    @api.model
    def _response2json(self, response):
        provider = self._get_geo_direction_provider()
        if response is None:
            raise ValidationError(
                _(
                    "%s API request error. Please check API request log for more details!",
                    provider
                    )
                )
        try:
            return response.json()
        except Exception as e:
            raise ValidationError(
                _(
                    "Invalid response from %s API. Please check API request log for more details!",
                    provider
                    )
                ) from e
