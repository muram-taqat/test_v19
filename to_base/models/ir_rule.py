from odoo import models


class IrRule(models.Model):
    _inherit = 'ir.rule'

    def _get_rules(self, model_name, mode='read'):
        """Perf short-circuit for permissive users.

        When the current user has a *permissive* group rule ([(1, '=', 1)] or an
        empty domain) for ``model_name``, the OR of all its group rules is
        trivially TRUE. Every OTHER group rule is therefore redundant - including
        the deep-relational access rules (multi-warehouse / sales-team /
        affiliate...) whose 3-4 hop o2m/m2m domains compile into heavy SQL
        subqueries and dominate search/reconcile-heavy operations.

        We drop those redundant group rules HERE - before ``_compute_domain``
        evaluates them and ``_where_calc`` compiles them - so they are never
        turned into SQL. Global (AND) rules are kept untouched: access is
        unchanged, only wasted work is removed. Big win for privileged users
        (admin / managers), i.e. most test runs and power users.

        Restricted users without a permissive rule keep the full behaviour.
        """
        rules = super()._get_rules(model_name, mode)
        if not rules:
            return rules

        # Read rule fields with sudo (the querying user is usually not allowed
        # to read ir.rule), exactly like ir.rule._compute_domain does.
        rules_su = rules.sudo()
        group_rules = rules_su.filtered('groups')
        if not group_rules:
            return rules

        def _is_permissive(rule):
            # An empty domain_force is match-all: _compute_domain feeds it as []
            # to expression.normalize_domain(), which returns [TRUE_LEAF] i.e.
            # [(1, '=', 1)] -> the rule imposes no restriction, so it is permissive
            # exactly like an explicit [(1, '=', 1)].
            if not rule.domain_force:
                return True
            dom = rule.domain_force.replace(' ', '').replace('"', "'")
            return dom == "[(1,'=',1)]"

        # Stop at the first permissive group rule (no need to scan them all).
        # Iterate with a prefetch covering the WHOLE rule set: the domain_force
        # read in _is_permissive() then fetches every rule (global ones
        # included) in ONE query, so _compute_domain's later reads are all
        # cache hits. filtered() narrows _prefetch_ids to the group rules only,
        # which would split the fetch in two (+1 query per _compute_domain
        # cache miss) and break core's query-count perf tests
        # (test_mail_full TestRatingPerformance: 1615 > 1614).
        group_rules = group_rules.with_prefetch(rules_su._ids)
        permissive = next((r for r in group_rules if _is_permissive(r)), None)
        if permissive:
            # keep all global rules + that single permissive group rule (enough
            # to keep the group-OR TRUE); drop the redundant deep group rules.
            keep = (rules_su - group_rules) | permissive
            return rules.browse(keep.ids)
        return rules
