from odoo import models


class RouteFailureReportMixin(models.AbstractModel):
    _name = 'route.failure.report.mixin'
    _description = 'Route Failure Report Mixin'

    # How many failed records :meth:`_get_directions` RENDERS in its aggregate report.
    # The reported COUNT is never capped - only the per-line detail is.
    #
    # That method's batch branch is scoped by its own docstring to "hundreds of
    # thousands of coordinate-bearing records", and it embeds one `display_name` plus
    # one FULL exception string per failure TWICE: into an `ir.logging` row and into a
    # `UserError` that is JSON-serialised to the browser. A 20% failure rate over
    # 200,000 records is a multi-megabyte string in each. Fifty named records is
    # already more than an operator acts on in one pass, and the prose keeps the exact
    # arithmetic, so nothing needed to size the problem is lost. A carrier that
    # genuinely needs a different budget overrides the attribute.
    MAX_REPORTED_FAILURES = 50

    def _render_failure_lines(self, failures):
        """Render at most :attr:`MAX_REPORTED_FAILURES` of ``failures`` as text.

        Split out of :meth:`_get_directions` so the rendered list and the reported
        COUNT cannot drift: the count is computed by the caller from ``failures``
        itself and is never capped, while everything that grows with the batch lives
        here. See :attr:`MAX_REPORTED_FAILURES` for why the cap exists at all.

        :param failures: a list of ``(record, exception)`` pairs, in batch order.
        :return: the rendered block, ending in a truncation notice when capped.
        """
        lines = [
            "- %s: %s" % (failed_record.display_name, error)
            for failed_record, error in failures[:self.MAX_REPORTED_FAILURES]
        ]
        if len(failures) > len(lines):
            lines.append(self.env._("... and %(remaining_count)s more.", remaining_count=len(failures) - len(lines)))
        return "\n".join(lines)
