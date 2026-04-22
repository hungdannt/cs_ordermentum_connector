from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    ordermentum_purchaser_id = fields.Char(string="Ordermentum Purchaser ID", copy=False)
