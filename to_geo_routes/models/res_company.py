from odoo import api, models, fields
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    distance_unit = fields.Selection(
        selection=[('kilometers', 'Kilometers'), ('miles', 'Miles')],
        string="Distance Unit",
        default='kilometers',
        help="Select the unit of measurement for distances. It can be Kilometers or Miles.",
    )

    @api.model
    def _get_map_routing_key_fields(self):
        return {
            # provider_name: company field that store the provider's API.
            # for example
            # 'google': 'google_routes_api_key',
            }

    def _get_map_routing_api_key(self, provider):
        """Return the routing API key of ``provider`` for this company.

        The key is read with ``sudo()`` so a non-system user can run a routing
        request while the key field itself stays restricted to system users.

        :param str provider: provider code, a key of :meth:`_get_map_routing_key_fields`.
        :return: the API key.
        :rtype: str
        :raises ValidationError: when the provider has no key field or the key is empty.
        """
        self.ensure_one()
        field_name = self._get_map_routing_key_fields().get(provider)
        api_key = self.sudo()[field_name] if field_name else False
        if not api_key:
            provider_field = self.env['transport.mode.provider']._fields['provider']
            provider_label = dict(provider_field._description_selection(self.env)).get(provider, provider)
            raise ValidationError(self.env._(
                "The %(provider)s API key is not configured for the company %(company)s. Please ask an administrator to set it in Settings.",
                provider=provider_label,
                company=self.display_name,
            ))
        return api_key
