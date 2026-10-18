from odoo import api, models, _
from odoo.exceptions import ValidationError


class GeoDirectionManual(models.AbstractModel):
    _name = 'geo.direction.manual'
    _inherit = 'geo.direction'
    _description = 'Geo Direction ManualInput'

    @api.model
    def _get_directions(self, origin, destination, mode, waypoints=None, res_model=None, res_id=None, timeout=30):
        """
        Calculate detailed directions between multiple points using the Haversine formula.

        :param recordset origin: A `res.partner` record representing the origin. Must have latitude and longitude.
        :param recordset destination: A `res.partner` record representing the destination. Must have latitude and longitude.
        :param str mode: Transport mode (e.g., 'driving', 'cycling').
        :param recordset waypoints: An optional recordset of `res.partner` representing waypoints. Must have latitude and longitude.
        :return: A dictionary containing the distance, duration, and detailed legs for each segment.
        :rtype: dict
        :raises ValidationError: If any of the partners do not have valid latitude and longitude.

        Example:

        >>> origin = env['res.partner'].browse(1)  # Example origin
        >>> destination = env['res.partner'].browse(2)  # Example destination
        >>> waypoints = env['res.partner'].browse([3, 4])  # Example waypoints
        >>> mode = 'driving'
        >>> result = obj._get_directions(origin, destination, mode, waypoints)
        >>> print(result)
        {
            'provider': 'manual',
            'processed': {
                'distance': 150000,
                'duration': 5400,
                'geometry': 'encoded_polyline_for_entire_route',
                'sections': [
                    {
                        'distance': 50000,
                        'duration': 1800,
                        'summary': 'From Point A to Point B',
                        'polyline': 'encoded_polyline_for_section',
                        'transport_mode': 'car',
                        'steps': [
                            {'distance': 50000, 'duration': 1800, 'summary': 'From Point A to Point B', 'polyline': 'encoded_polyline_for_section'}
                        ]
                    }
                ],
                'waypoints': [
                    {'lat': 21.0285, 'lng': 105.8542, 'name': 'Hanoi'},
                    {'lat': 20.9500, 'lng': 106.2000, 'name': 'Waypoint'},
                    {'lat': 20.9367, 'lng': 106.3333, 'name': 'Hai Phong'}
                ]
            }
        }
        """
        # Validate coordinates
        waypoints = waypoints or self.env['res.partner']
        all_points = origin + waypoints + destination
        all_points._validate_coordinates()

        # Get average speed for the mode
        ave_speed, dummy = self.env['transport.mode.provider']._get_default_manual_speed(mode)
        ave_speed_m_s = ave_speed * 1000 / 3600  # Convert to m/s

        # Initialize variables
        legs = []
        total_distance = 0.0
        total_duration = 0.0
        route_coordinates = []  # Collect coordinates for the entire route
        waypoints_data = []
        haversine_distance = self.env['res.partner'].haversine_distance

        # Prepare waypoints data
        for partner in all_points:
            waypoints_data.append({
                'lat': partner.partner_latitude,
                'lng': partner.partner_longitude,
                'name': partner.display_name,
            })

        last_point = None
        section_coordinates = []  # Coordinates for the current section
        for current_point in all_points:
            section_coordinates.append((current_point.partner_latitude, current_point.partner_longitude))
            if last_point:
                # Calculate distance and duration
                distance = haversine_distance(last_point, current_point)
                duration = distance / ave_speed_m_s if ave_speed_m_s > 0 else 0.0
                summary = _("From %(last_point_display_name)s to %(current_point_display_name)s", last_point_display_name=last_point.display_name, current_point_display_name=current_point.display_name)

                # Append leg data
                leg_polyline = self.encode_polyline(section_coordinates)
                legs.append({
                    'distance': distance,
                    'duration': duration,
                    'summary': summary,
                    'polyline': leg_polyline,
                    'transport_mode': mode,
                    'steps': [
                        {
                            'distance': distance,
                            'duration': duration,
                            'summary': summary,
                            'polyline': leg_polyline,
                        }
                    ]
                })

                # Update totals
                total_distance += distance
                total_duration += duration

                # Append section coordinates to the route
                route_coordinates.extend(section_coordinates)
                section_coordinates = [(current_point.partner_latitude, current_point.partner_longitude)]  # Start new section

            last_point = current_point

        # Encode the entire route as a single polyline
        route_polyline = self.encode_polyline(route_coordinates)

        # Prepare processed data
        processed_data = {
            'distance': total_distance,
            'duration': total_duration,
            'geometry': route_polyline,  # Encoded polyline for the entire route
            'sections': legs,
            'waypoints': waypoints_data,
        }
        self._validate_normalized_directions_data(processed_data)

        # Build response
        return self._build_response(processed_data=processed_data)

    @api.model
    def _get_distance_matrix(self, partners, mode, res_model=None, res_id=None, timeout=30):
        """
        Compute a distance and duration matrix using Manual Mode with the Haversine formula.

        :param recordset partners: A recordset of res.partner.
        :param str mode: Transport mode (e.g., 'driving', 'cycling').
        :return: A dictionary containing both processed API response data.
        :rtype: dict

        Example:

        >>> partners = env['res.partner'].browse([1, 2, 3])  # Example partner IDs
        >>> mode = 'driving'
        >>> result = obj._get_distance_matrix(partners, mode=mode)
        >>> print(result)
        {
            'provider': 'manual',
            'processed': {
                (partnerA, partnerB): {
                    'distance': 100000,  # Total distance in meters
                    'duration': 3600,    # Total duration in seconds
                    'ave_speed': 27.7778 # Average speed in m/s
                    'normalized': ((21.0285, 105.8542), (20.9367, 106.3333))  # Original coordinates
                },
                ...
            },
        }
        """
        # Validate input
        if len(partners) < 2:
            raise ValidationError(_("You need at least two address to compute a matrix."))

        # Validate coordinates
        partners._validate_coordinates()

        # Get average speed for the mode
        ave_speed, dummy = self.env['transport.mode.provider']._get_default_manual_speed(mode)

        # Compute the haversine matrix
        matrix_result = self._compute_haversine_matrix(partners, mode, ave_speed=ave_speed)

        # Return processed data
        return self._build_response(processed_data=matrix_result)
