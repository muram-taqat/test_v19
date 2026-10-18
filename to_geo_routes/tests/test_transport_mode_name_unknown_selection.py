from unittest.mock import patch

from odoo.fields import Selection
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestTransportModeNameUnknownSelection(TransactionCase):
    """`route.section.transport.mode.name` must render even while a provider code is missing
    from the field's selection.

    THE MEASURED FAILURE THIS PINS. Each provider addon (`to_geo_routes_google`,
    `..._here`, `..._mapbox`, `..._vietmap`, `..._searoutes`) contributes its own code to
    `provider` via `selection_add`. During a STAGED registry load those contributions are not
    in place yet: installing any new module reloads `to_geo_routes` and re-executes its data
    files BEFORE the provider addons re-register. On a database whose rows already carry
    `provider='google'`, the `name` recompute then looked the code up in a selection that did
    not have it.

    Observed on 2026-08-06 while installing modules onto a database that already had Google
    routing rows - the traceback aborted the ENTIRE install:

        File ".../to_geo_routes/models/route_section_transport_mode.py", line 132, in _compute_name
          r.name = "%s [%s] [%s]" % (..., modes.get(r.mode), providers[r.provider])
        KeyError: 'google'

    at the moment `to_geo_routes/data/data_demo.xml` was loading, with `to_geo_routes_google`
    not yet mentioned anywhere in that run's log. For a customer this is: installing any app on
    a database that uses Google routing fails outright.

    `name` is a display string. Rendering the stored code is always preferable to raising, so
    both halves of the format fall back to the raw value.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider_record = cls.env.ref('to_geo_routes.manual_input_driving')

    def test_name_renders_a_provider_code_the_selection_does_not_yet_carry(self):
        """RED before the fix: `providers[r.provider]` raises KeyError and the read below
        propagates it. Green after: the raw stored code is rendered instead.
        """
        row = self.env['route.section.transport.mode'].new({
            'transport_mode_provider_id': self.provider_record.id,
        })
        provider_code = row.provider
        self.assertTrue(
            provider_code,
            "arrange guard: the fixture must actually carry a provider code, otherwise the"
            " missing-from-selection condition below is never exercised",
        )

        # Reproduce the staged-load state: the selection carries nothing yet, exactly as it
        # does before the provider addons re-register their `selection_add` contributions.
        with patch.object(Selection, '_description_selection', lambda self, env: []):
            row.invalidate_recordset(['name'])
            rendered = row.name

        self.assertIn(
            provider_code, rendered,
            "with the provider absent from the selection the name must still name the provider"
            " by its stored code - an operator reading the row needs to know which provider it"
            " belongs to, and a display string is never worth an exception",
        )

    def test_name_still_renders_the_human_label_when_the_selection_is_complete(self):
        """NON-REGRESSION GUARD - green before and after. The fallback added for the crash above
        must not cost the normal case its readable label: with the selection fully registered,
        `name` must still show the translated label, never the raw code.
        """
        row = self.env['route.section.transport.mode'].new({
            'transport_mode_provider_id': self.provider_record.id,
        })
        selection = dict(
            self.env['route.section.transport.mode']._fields['provider']._description_selection(self.env)
        )
        expected_label = selection[row.provider]

        self.assertIn(
            expected_label, row.name,
            "with the selection complete the name must carry the human label, not the raw code -"
            " otherwise the crash fix silently degraded every row's display",
        )
