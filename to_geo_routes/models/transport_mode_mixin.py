from odoo import api, models, fields

from .transport_mode_provider import TRANSPORT_MODES


class TransportModeProvider(models.AbstractModel):
    _name = 'transport.mode.mixin'
    _description = 'Transport Mode Mixin'

    transport_mode = fields.Selection(
        TRANSPORT_MODES, string="Transport Mode", required=True,
        )
    transport_mode_provider_ids = fields.Many2many(
        'transport.mode.provider',
        string='Available Transport Mode Providers',
        compute='_compute_transport_mode_provider_ids'
        )

    @api.depends_context('company')
    @api.depends('transport_mode')
    def _compute_transport_mode_provider_ids(self):
        transport_modes = list(set(self.mapped('transport_mode')))
        provider_model = self.env['transport.mode.provider']
        all_providers = provider_model.search([('transport_mode', 'in', transport_modes)])
        empty_provider = provider_model

        company_records_map = self._group_by_company()
        default_company = self.env.company

        for company, records in company_records_map.items():
            current_company = company or default_company

            # Pre-filter providers once per company
            company_providers = all_providers.with_company(current_company).filtered(
                lambda provider: provider.map_api_enabled or provider.provider == 'manual'
            )
            providers_by_mode = company_providers._group_by_transport_mode()

            for record in records:
                record.transport_mode_provider_ids = providers_by_mode.get(
                    record.transport_mode, empty_provider
                )
