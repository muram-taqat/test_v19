from unittest.mock import patch

from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestFetchDirectionsDataResTargetPairing(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        origin = cls.env['res.partner'].create({
            'name': 'Origin',
            'partner_latitude': 20.861135,
            'partner_longitude': 106.672549,
        })
        destination = cls.env['res.partner'].create({
            'name': 'Destination',
            'partner_latitude': 20.9522972,
            'partner_longitude': 107.0781387,
        })
        route_section = cls.env['route.section'].create({
            'address_from_id': origin.id,
            'address_to_id': destination.id,
        })
        cls.route_section_transport_mode = route_section.transport_mode_ids.filtered(
            lambda mode: mode.transport_mode_provider_id == cls.env.ref('to_geo_routes.manual_input_driving')
        )

    def _fetch_directions_data_call_kwargs(self, **kwargs):
        transport_mode_provider_model = type(self.env['transport.mode.provider'])
        with patch.object(
            transport_mode_provider_model,
            '_get_directions',
            return_value={'processed': {'distance': 1000.0, 'duration': 60.0, 'geometry': 'poly'}},
        ) as mocked_get_directions:
            self.route_section_transport_mode._fetch_directions_data(**kwargs)
        return mocked_get_directions.call_args.kwargs

    def test_fetch_directions_data_with_only_res_model_falls_back_to_own_pair(self):
        call_kwargs = self._fetch_directions_data_call_kwargs(res_model='unrelated.model')
        self.assertEqual(
            (call_kwargs['res_model'], call_kwargs['res_id']),
            (self.route_section_transport_mode._name, self.route_section_transport_mode.id),
        )

    def test_fetch_directions_data_with_only_res_id_falls_back_to_own_pair(self):
        unrelated_id = self.route_section_transport_mode.id + 999999
        call_kwargs = self._fetch_directions_data_call_kwargs(res_id=unrelated_id)
        self.assertEqual(
            (call_kwargs['res_model'], call_kwargs['res_id']),
            (self.route_section_transport_mode._name, self.route_section_transport_mode.id),
        )
