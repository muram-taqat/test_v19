from unittest.mock import patch

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import tagged, TransactionCase


@tagged('post_install', '-at_install')
class TestSolveTsp(TransactionCase):
    """Route optimization (`geo.direction._solve_tsp`) on the manual (haversine) provider."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env['res.partner']
        # North to south: Ha Noi, Ninh Binh, Thanh Hoa, Vinh
        cls.hanoi = Partner.create({'name': 'TSP Ha Noi', 'partner_latitude': 21.0285, 'partner_longitude': 105.8542})
        cls.ninh_binh = Partner.create({'name': 'TSP Ninh Binh', 'partner_latitude': 20.2506, 'partner_longitude': 105.9745})
        cls.thanh_hoa = Partner.create({'name': 'TSP Thanh Hoa', 'partner_latitude': 19.8067, 'partner_longitude': 105.7852})
        cls.vinh = Partner.create({'name': 'TSP Vinh', 'partner_latitude': 18.6796, 'partner_longitude': 105.6813})
        # Between Ha Noi and Ninh Binh
        cls.phu_ly = Partner.create({'name': 'TSP Phu Ly', 'partner_latitude': 20.5411, 'partner_longitude': 105.9139})
        cls.manual = cls.env['geo.direction.manual']

    def _solve(self, origin, destination, waypoints):
        return self.manual._solve_tsp(origin, destination, waypoints, 'driving')['processed']

    def _matrix_without(self, *missing_pairs):
        original = type(self.manual)._get_distance_matrix

        def get_distance_matrix(model, partners, mode, *args, **kwargs):
            response = original(model, partners, mode, *args, **kwargs)
            for pair in missing_pairs:
                response['processed'].pop(pair, None)
            return response
        return patch.object(type(self.manual), '_get_distance_matrix', get_distance_matrix)

    def _four_city_route(self, name):
        return self.env['route.route'].create({
            'name': name,
            'transport_mode': 'driving',
            'waypoint_ids': [
                Command.create({'address_id': partner.id})
                for partner in (self.hanoi, self.ninh_binh, self.thanh_hoa, self.vinh)
            ],
        })

    def test_destination_stays_last_even_when_it_is_closer_than_the_waypoints(self):
        # Ninh Binh lies on the way to the waypoints: a tour free to end anywhere would
        # visit it in the middle, but it is the destination and must come last.
        result = self._solve(self.hanoi, self.ninh_binh, self.thanh_hoa + self.vinh)

        route = result['optimized_route']
        self.assertEqual(route[0], self.hanoi)
        self.assertEqual(route[-1], self.ninh_binh)
        self.assertEqual(set(route), {self.hanoi, self.ninh_binh, self.thanh_hoa, self.vinh})
        self.assertEqual(len(route), 4)
        self.assertEqual([(leg['from'], leg['to']) for leg in result['legs']], list(zip(route, route[1:])))
        self.assertAlmostEqual(result['total_distance'], sum(leg['distance'] for leg in result['legs']))

    def test_round_trip_returns_to_the_origin(self):
        result = self._solve(self.hanoi, self.hanoi, self.ninh_binh + self.thanh_hoa)

        route = result['optimized_route']
        self.assertEqual(route[0], self.hanoi)
        self.assertEqual(route[-1], self.hanoi)
        self.assertEqual(set(route[1:-1]), {self.ninh_binh, self.thanh_hoa})
        self.assertEqual(len(result['legs']), 3)
        self.assertGreater(result['total_distance'], 0)

    def test_missing_pair_is_avoided_when_another_order_exists(self):
        # The shortest order (Ha Noi, Ninh Binh, Thanh Hoa, Vinh) needs Ninh Binh -> Thanh Hoa.
        with self._matrix_without((self.ninh_binh, self.thanh_hoa)):
            result = self._solve(self.hanoi, self.vinh, self.ninh_binh + self.thanh_hoa)

        route = result['optimized_route']
        self.assertEqual(route[0], self.hanoi)
        self.assertEqual(route[-1], self.vinh)
        self.assertEqual(set(route), {self.hanoi, self.ninh_binh, self.thanh_hoa, self.vinh})
        self.assertNotIn((self.ninh_binh, self.thanh_hoa), list(zip(route, route[1:])))
        self.assertEqual(len(result['legs']), 3)

    def _assert_names_unknown_pair_from_hanoi(self, message):
        """The error names both locations of the unknown pair: Ha Noi and the stop it cannot reach."""
        self.assertIn(self.hanoi.display_name, message)
        self.assertTrue(
            any(partner.display_name in message for partner in (self.ninh_binh, self.thanh_hoa, self.vinh)),
            "the error must name the location Ha Noi has no distance to: %s" % message,
        )

    def test_unavoidable_missing_pair_raises_a_clear_error(self):
        # Without any distance from Ha Noi, no route can leave the origin.
        missing = [(self.hanoi, partner) for partner in (self.ninh_binh, self.thanh_hoa, self.vinh)]
        with self._matrix_without(*missing), self.assertRaises(ValidationError) as caught:
            self._solve(self.hanoi, self.vinh, self.ninh_binh + self.thanh_hoa)
        self._assert_names_unknown_pair_from_hanoi(str(caught.exception))

    def test_stop_listed_twice_is_visited_twice_never_back_to_back(self):
        # Thanh Hoa is listed twice: the only order that does not visit it twice in a row
        # puts Ninh Binh between both visits, although Ha Noi -> Ninh Binh -> Thanh Hoa
        # -> Thanh Hoa would be shorter.
        result = self._solve(self.hanoi, self.vinh, self.thanh_hoa + self.ninh_binh + self.thanh_hoa)

        route = result['optimized_route']
        self.assertEqual(route, [self.hanoi, self.thanh_hoa, self.ninh_binh, self.thanh_hoa, self.vinh])
        self.assertEqual([(leg['from'], leg['to']) for leg in result['legs']], list(zip(route, route[1:])))
        self.assertTrue(all(leg['from'] != leg['to'] for leg in result['legs']))

    def test_stop_that_can_only_be_visited_twice_in_a_row_is_refused(self):
        # Nothing can separate both visits of Thanh Hoa: no route may get a leg from a
        # location to itself.
        with self.assertRaises(ValidationError) as caught:
            self._solve(self.hanoi, self.vinh, self.thanh_hoa + self.thanh_hoa)
        self.assertIn(self.thanh_hoa.display_name, str(caught.exception))

    def test_optimizing_a_route_tells_the_user_which_distance_is_unknown(self):
        route = self._four_city_route('TSP unknown distance route')
        missing = [(self.hanoi, partner) for partner in (self.ninh_binh, self.thanh_hoa, self.vinh)]
        with self._matrix_without(*missing), \
                self.assertLogs('odoo.addons.to_geo_routes.models.res_partner', level='WARNING'), \
                self.assertRaises(ValidationError) as caught:
            route.action_optimize_route()
        self._assert_names_unknown_pair_from_hanoi(str(caught.exception))

    def test_optimizing_a_route_keeps_both_visits_of_a_stop_listed_twice(self):
        # Ha Noi, Thanh Hoa, Ninh Binh, Phu Ly, Ninh Binh, Vinh: Ninh Binh is visited twice,
        # never back to back, which a route allows. The stops are listed out of their
        # north-to-south order, so optimizing moves both visits of Ninh Binh.
        route = self.env['route.route'].create({
            'name': 'TSP stop visited twice route',
            'transport_mode': 'driving',
            'waypoint_ids': [
                Command.create({'address_id': partner.id, 'sequence': sequence})
                for sequence, partner in enumerate(
                    (self.hanoi, self.thanh_hoa, self.ninh_binh, self.phu_ly, self.ninh_binh, self.vinh), start=1
                )
            ],
        })
        ninh_binh_waypoints = route.waypoint_ids.filtered(lambda wp: wp.address_id == self.ninh_binh)
        self.assertEqual(len(ninh_binh_waypoints), 2)

        route.action_optimize_route()

        waypoints = route.waypoint_ids.sorted('sequence')
        self.assertEqual(len(waypoints), 6)
        self.assertLessEqual(ninh_binh_waypoints, waypoints, "both waypoints of the stop visited twice must be kept")
        self.assertEqual(len(set(waypoints.mapped('sequence'))), 6, "every waypoint must get its own position")
        addresses = [wp.address_id for wp in waypoints]
        self.assertEqual(addresses[0], self.hanoi)
        self.assertEqual(addresses[-1], self.vinh)
        for from_address, to_address in zip(addresses, addresses[1:]):
            self.assertNotEqual(from_address, to_address, "a stop must never be visited twice in a row")
        self.assertEqual(len(route.segment_ids), 5)
        for segment in route.segment_ids:
            self.assertNotEqual(segment.address_from_id, segment.address_to_id)

    def test_optimizing_a_route_keeps_a_middle_visit_of_the_origin(self):
        # Ha Noi, Ninh Binh, Ha Noi, Vinh: the route comes back through its origin before
        # heading to the destination - that middle visit is a stop like any other.
        route = self.env['route.route'].create({
            'name': 'TSP origin visited again route',
            'transport_mode': 'driving',
            'waypoint_ids': [
                Command.create({'address_id': partner.id, 'sequence': sequence})
                for sequence, partner in enumerate((self.hanoi, self.ninh_binh, self.hanoi, self.vinh), start=1)
            ],
        })
        original_waypoints = route.waypoint_ids

        route.action_optimize_route()

        waypoints = route.waypoint_ids.sorted('sequence')
        self.assertEqual(set(waypoints.ids), set(original_waypoints.ids), "no waypoint may be lost or added")
        self.assertEqual(
            [wp.address_id for wp in waypoints], [self.hanoi, self.ninh_binh, self.hanoi, self.vinh],
        )
        self.assertEqual(
            [(segment.address_from_id, segment.address_to_id) for segment in route.segment_ids.sorted('sequence')],
            [(self.hanoi, self.ninh_binh), (self.ninh_binh, self.hanoi), (self.hanoi, self.vinh)],
        )

    def test_unexpected_provider_failure_is_logged_with_its_traceback(self):
        route = self._four_city_route('TSP crashing provider route')
        Provider = type(self.env['transport.mode.provider'])

        def crash(provider, origin, destination, waypoints):
            raise KeyError('unexpected provider payload')

        with patch.object(Provider, '_get_optimized_route', crash), \
                self.assertLogs('odoo.addons.to_geo_routes.models.res_partner', level='WARNING') as logs, \
                self.assertRaises(ValidationError):
            route.action_optimize_route()
        self.assertTrue(logs.records)
        self.assertTrue(
            all(record.exc_info and record.exc_info[0] is KeyError for record in logs.records),
            "an unexpected provider failure must be logged with its traceback",
        )

    def test_expected_provider_error_is_logged_without_a_traceback(self):
        route = self._four_city_route('TSP unknown distance route without traceback')
        missing = [(self.hanoi, partner) for partner in (self.ninh_binh, self.thanh_hoa, self.vinh)]
        with self._matrix_without(*missing), \
                self.assertLogs('odoo.addons.to_geo_routes.models.res_partner', level='WARNING') as logs, \
                self.assertRaises(ValidationError):
            route.action_optimize_route()
        self.assertTrue(logs.records)
        self.assertFalse(any(record.exc_info for record in logs.records))

    def test_route_name_follows_the_optimized_order_of_its_stops(self):
        # The name shows the middle intermediate stop: Thanh Hoa before Ninh Binh puts
        # Ninh Binh in the middle; once optimized, Thanh Hoa is the middle stop.
        route = self.env['route.route'].create({
            'transport_mode': 'driving',
            'waypoint_ids': [
                Command.create({'address_id': partner.id, 'sequence': sequence})
                for sequence, partner in enumerate((self.hanoi, self.thanh_hoa, self.ninh_binh, self.vinh), 1)
            ],
        })
        self.assertIn(self.ninh_binh.name, route.name)

        route.action_optimize_route()

        self.assertEqual(route.waypoint_ids.sorted('sequence').address_id[1], self.ninh_binh)
        self.assertIn(self.thanh_hoa.name, route.name)
        self.assertNotIn(self.ninh_binh.name, route.name)

    def test_directions_keep_a_middle_visit_of_the_origin(self):
        Provider = type(self.env['transport.mode.provider'])
        requested_waypoints = []

        def get_directions(provider, origin, destination, waypoints, res_model=None):
            requested_waypoints.append(waypoints)
            return {'processed': {}}

        partners = self.hanoi + self.ninh_binh + self.hanoi + self.vinh
        providers = self.env['transport.mode.provider'].search([], limit=1)
        with patch.object(Provider, '_get_directions', get_directions):
            partners._get_directions(providers)
        self.assertEqual([waypoints.ids for waypoints in requested_waypoints], [[self.ninh_binh.id, self.hanoi.id]])
