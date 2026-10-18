from odoo.tools.sql import table_exists


def migrate(cr, version):
    """Fix error when refreshing demo instances"""
    if table_exists(cr, 'mail_compose_message_res_partner_rel'):
        cr.execute("""
            DELETE FROM mail_compose_message_res_partner_rel r
            WHERE NOT EXISTS (SELECT 1 FROM mail_compose_message m WHERE m.id = r.wizard_id)
            OR NOT EXISTS (SELECT 1 FROM res_partner rp WHERE rp.id = r.partner_id)
        """)
    if table_exists(cr, 'mail_notification'):
        cr.execute("""
            DELETE FROM mail_notification m
            WHERE NOT EXISTS (SELECT 1 FROM mail_mail mm WHERE mm.id = m.mail_mail_id)
            AND m.mail_mail_id IS NOT NULL
        """)
    if table_exists(cr, 'mail_mail_res_partner_rel'):
        cr.execute("""
            DELETE FROM mail_mail_res_partner_rel r
            WHERE NOT EXISTS (SELECT 1 FROM mail_mail mm WHERE mm.id = r.mail_mail_id)
        """)
    if table_exists(cr, 'website_track'):
        cr.execute("""
            DELETE FROM website_track wt
            WHERE NOT EXISTS (SELECT 1 FROM website_visitor wv WHERE wv.id = wt.visitor_id)
        """)
