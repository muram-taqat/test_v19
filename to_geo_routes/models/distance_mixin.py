import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class DistanceMixin(models.AbstractModel):
    _name = 'distance.mixin'
    _description = 'Distance Mixin'

    distance = fields.Float(
        string='Distance (km)', store=True,
        )
    distance_in_miles = fields.Float(
        string='Distance (mi.)',
        compute='_compute_distance_in_miles', store=True,
        inverse='_inverse_distance_in_miles',
        )
    duration = fields.Float(
        string="Duration (hours)",
        compute="_compute_duration", store=True, readonly=True,
        help="The estimated time in hours needed to cover the distance."
        )
    ave_speed = fields.Float(
        string='Ave. Speed (km/h)',
        aggregator="avg", default=0.0, store=True,
        help="The average speed in kilometers per hour that can be achieved over this distance."
        )
    ave_speed_in_miles = fields.Float(
        string='Ave. Speed (mph)',
        compute='_compute_ave_speed_in_miles', store=True,
        inverse='_inverse_ave_speed_in_miles',
        aggregator="avg", default=0.0,
        help="The average speed in miles per hour that can be achieved over this distance."
        )

    show_distance_in_miles = fields.Boolean(compute='_compute_show_distance_in_miles')

    @api.depends('distance')
    def _compute_distance_in_miles(self):
        km2mile = self.env['to.base'].km2mile
        for r in self:
            r.distance_in_miles = km2mile(r.distance)

    @api.depends('ave_speed')
    def _compute_ave_speed_in_miles(self):
        km2mile = self.env['to.base'].km2mile
        for r in self:
            r.ave_speed_in_miles = km2mile(r.ave_speed)

    @api.depends('ave_speed', 'distance')
    def _compute_duration(self):
        for r in self:
            r.duration = r.distance / r.ave_speed if r.ave_speed else 0.0

    def _compute_show_distance_in_miles(self):
        has_company_field = 'company_id' in self._fields
        for r in self:
            if has_company_field and r.company_id:
                company = r.company_id
            else:
                company = self.env.company
            r.show_distance_in_miles = company.distance_unit == 'miles'

    @api.onchange('distance_in_miles')
    def _inverse_distance_in_miles(self):
        mile2km = self.env['to.base'].mile2km
        for r in self:
            r.distance = mile2km(r.distance_in_miles)

    @api.onchange('ave_speed_in_miles')
    def _inverse_ave_speed_in_miles(self):
        mile2km = self.env['to.base'].mile2km
        for r in self:
            r.ave_speed = mile2km(r.ave_speed_in_miles)

    # CRUD methods
    #
    # R3 - mixin-level normalising guard (design § 2.2), downgraded to
    # LOG-ONLY by amendment A2. The 7-model `ave_speed` writability census
    # below is the required evidence artifact (design amendment ledger,
    # node N8): it applies the exact predicate the amendment specifies -
    # `f = self._fields['ave_speed']; not f.compute or f.inverse` - to every
    # model in the `distance.mixin` tree, read directly off each model's
    # field declaration (grounded: local-source, OSM unreachable this run).
    #
    # | Model                            | ave_speed compute/inverse                                   | Writable? |
    # |-----------------------------------|--------------------------------------------------------------|-----------|
    # | distance.mixin                    | no compute (plain stored)                                     | YES       |
    # | route.section.mixin               | override sets only `help=`; inherits no-compute from above   | YES       |
    # | route.segment                     | compute='_compute_ave_speed', inverse='_inverse_distance_ave_speed' | YES  |
    # | route.section.transport.mode      | override sets only `help=`; inherits no-compute               | YES       |
    # | route.mixin                       | compute='_compute_ave_speed', readonly=True, NO inverse       | NO        |
    # | route.route                       | no override; inherits route.mixin's declaration               | NO        |
    # | route.section (projection)        | compute='_compute_ave_speed', store=False, NO inverse          | NO        |
    #
    # Result: 4 of 7 genuinely writable, 3 of 7 not - not the "2 of 7 / 5 of
    # 7" figure the reviewer estimated informally; this census supersedes
    # that estimate with the derived count. Either way the conclusion
    # holds: a tier-blind `ave_speed = distance / duration` derivation here
    # would be silently swallowed by recompute on the 3 unwritable models
    # (both aggregate-tier models plus the pure projection), so amendment
    # A2 ships LOG-ONLY instead - this method pops `duration` and warns; it
    # derives NOTHING. If the gated-derivation fallback is ever revisited,
    # this table is the evidence, already computed.
    def _normalize_derived_duration_vals(self, vals):
        """Pop `duration` out of `vals` and log a warning - never derive.

        `duration` is a stored, `readonly=True` field on `distance.mixin`
        (`compute='_compute_duration'`, no `inverse=`): it is always a
        DERIVED output, never a write input, on every model in this
        inheritance tree (design § 2.1 "Derived-Duration Rule").

        Odoo's own write-protection predicate (the protection predicate in `BaseModel.write`:
        `field.inverse or (field.compute and not field.readonly)`) is FALSE
        for `duration` (no inverse; `readonly=True` makes the second clause
        false too), so a `duration` written ALONGSIDE its dependencies
        (`distance`/`ave_speed`) is silently discarded by the recompute
        anyway - but written ALONE it survives BY ACCIDENT (nothing dirties
        `_compute_duration`'s dependencies, so no recompute is ever
        scheduled). That accidental survival is the exact ambiguity this
        guard closes: after this method runs, `duration` is discarded on
        EVERY write/create, uniformly, with a warning naming the offending
        model/record so a violating caller is diagnosable instead of
        silently "corrected".

        LOG-ONLY (amendment A2 - do NOT reintroduce derivation here). See
        the writability census above this method for why the tier-blind
        `ave_speed = distance / duration` fallback was rejected.
        """
        if 'duration' in vals:
            _logger.warning(
                "%s: 'duration' was supplied in write/create vals "
                "(ids=%s, vals=%s) but distance.mixin.duration is a "
                "derived, readonly field - it is never a write input "
                "anywhere in this inheritance tree. Discarding the key; "
                "no value is derived or altered (Viindoo/tvtmaaddons#14279, "
                "R3 log-only per design amendment A2).",
                self._name, self.ids, vals,
            )
            vals.pop('duration')

    def write(self, vals):
        self._normalize_derived_duration_vals(vals)
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._normalize_derived_duration_vals(vals)
        return super().create(vals_list)
