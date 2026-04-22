import logging

from odoo import fields, models


_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ordermentum_auth_base_url = fields.Char(
        string="Ordermentum Auth Base URL",
        config_parameter="cs_ordermentum_connector.auth_base_url",
        default="https://app.ordermentum.com",
    )
    ordermentum_api_base_url = fields.Char(
        string="Ordermentum API Base URL",
        config_parameter="cs_ordermentum_connector.api_base_url",
        default="https://app.ordermentum.com",
    )
    ordermentum_username = fields.Char(
        string="Ordermentum Username",
        config_parameter="cs_ordermentum_connector.username",
    )
    ordermentum_password = fields.Char(
        string="Ordermentum Password",
        config_parameter="cs_ordermentum_connector.password",
    )

    ordermentum_supplier_id = fields.Char(
        string="Ordermentum Supplier UUID",
        config_parameter="cs_ordermentum_connector.supplier_id",
    )

    ordermentum_page_size = fields.Integer(
        string="Ordermentum Page Size",
        config_parameter="cs_ordermentum_connector.page_size",
        default=50,
    )

    ordermentum_default_payment_term_id = fields.Many2one(
        "account.payment.term",
        string="Default Payment Term",
        config_parameter="cs_ordermentum_connector.default_payment_term_id",
    )

    ordermentum_auto_confirm_orders = fields.Boolean(
        string="Auto Confirm Imported Orders",
        config_parameter="cs_ordermentum_connector.auto_confirm_orders",
        default=True,
    )

    ordermentum_auto_create_invoice = fields.Boolean(
        string="Auto Create Invoice",
        config_parameter="cs_ordermentum_connector.auto_create_invoice",
        default=True,
    )

    ordermentum_auto_post_invoice = fields.Boolean(
        string="Auto Post Invoice",
        config_parameter="cs_ordermentum_connector.auto_post_invoice",
        default=True,
    )

    ordermentum_payment_journal_id = fields.Many2one(
        "account.journal",
        string="Payment Journal",
        domain=[("type", "in", ("bank", "cash"))],
        config_parameter="cs_ordermentum_connector.payment_journal_id",
    )

    ordermentum_default_company_id = fields.Many2one(
        "res.company",
        string="Default Company",
        config_parameter="cs_ordermentum_connector.default_company_id",
        help="Default company for sale orders",
    )
