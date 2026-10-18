from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.fields import Command
from odoo.tests import TransactionCase, tagged

PROVIDER = 'manual'
KEY_FIELDS_METHOD = 'odoo.addons.to_geo_routes.models.res_company.ResCompany._get_map_routing_key_fields'


@tagged('post_install', '-at_install')
class TestRoutingApiKey(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_company = cls.env['res.company'].create({'name': 'Hidden Key Company'})
        cls.geo_user = cls.env['res.users'].create({
            'name': 'Geo Key User',
            'login': 'geo_key_user',
            'email': 'geo.key.user@example.viindoo.com',
            'group_ids': [Command.set([cls.env.ref('to_geo_routes.group_user').id])],
        })

    def test_key_is_returned_to_a_user_who_cannot_read_the_key_field_directly(self):
        # The company record is outside the user's allowed companies, so reading its
        # field as that user is refused, exactly like a system-only key field.
        company_as_user = self.other_company.with_user(self.geo_user)
        with self.assertRaises(AccessError):
            company_as_user.read(['name'])
        with patch(KEY_FIELDS_METHOD, return_value={PROVIDER: 'name'}):
            self.assertEqual(
                company_as_user._get_map_routing_api_key(PROVIDER),
                'Hidden Key Company',
            )

    def test_missing_key_names_the_provider_and_the_company(self):
        self.other_company.street = False
        provider_label = dict(
            self.env['transport.mode.provider']._fields['provider']._description_selection(self.env)
        )[PROVIDER]
        with patch(KEY_FIELDS_METHOD, return_value={PROVIDER: 'street'}):
            with self.assertRaises(ValidationError) as caught:
                self.other_company._get_map_routing_api_key(PROVIDER)
        message = str(caught.exception)
        self.assertIn(provider_label, message)
        self.assertIn(self.other_company.display_name, message)

    def test_provider_without_a_key_field_is_refused(self):
        with self.assertRaises(ValidationError):
            self.other_company._get_map_routing_api_key('provider_without_key_field')


@tagged('post_install', '-at_install')
class TestRouteFailureReportMixin(TransactionCase):

    def _failures(self, count):
        partners = self.env['res.partner'].create([
            {'name': 'Failed partner %s' % index} for index in range(count)
        ])
        return [(partner, 'boom %s' % partner.id) for partner in partners]

    def test_the_cap_and_renderer_are_reachable_on_every_consumer_with_identical_output(self):
        consumers = ('route.route', 'route.section', 'route.segment', 'geo.direction')
        cap = self.env['route.route'].MAX_REPORTED_FAILURES
        failures = self._failures(cap + 2)
        expected_head = [
            '- %s: %s' % (record.display_name, error) for record, error in failures[:cap]
        ]
        outputs = set()
        for model_name in consumers:
            model = self.env[model_name]
            self.assertEqual(model.MAX_REPORTED_FAILURES, cap, model_name)
            rendered = model._render_failure_lines(failures)
            self.assertEqual(rendered.splitlines()[:cap], expected_head, model_name)
            self.assertEqual(len(rendered.splitlines()), cap + 1, model_name)
            outputs.add(rendered)
        self.assertEqual(len(outputs), 1, "consumers render the failure report differently")

    def test_no_truncation_line_when_failures_fit_the_cap(self):
        failures = self._failures(2)
        rendered = self.env['geo.direction']._render_failure_lines(failures)
        self.assertEqual(len(rendered.splitlines()), 2)
