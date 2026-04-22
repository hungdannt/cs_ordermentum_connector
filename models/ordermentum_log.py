import json

from odoo import api, fields, models


class OrdermentumLog(models.Model):
    _name = "cs.ordermentum.log"
    _description = "Ordermentum Log"
    _order = "create_date desc, id desc"

    name = fields.Char(required=True)
    log_type = fields.Selection(
        [
            ("webhook", "Webhook"),
            ("sync", "Sync"),
            ("payment", "Payment"),
            ("email", "Email"),
            ("settings", "Settings"),
        ],
        required=True,
        default="sync",
    )
    state = fields.Selection([("done", "Done"), ("error", "Error")], required=True, default="done")

    event = fields.Char()
    reference = fields.Char()

    sale_order_id = fields.Many2one("sale.order", ondelete="set null")
    invoice_id = fields.Many2one("account.move", ondelete="set null")

    line_ids = fields.One2many("cs.ordermentum.log.line", "log_id")

    request_payload = fields.Text()
    response_payload = fields.Text()

    def _as_json_text(self, payload):
        if payload is None:
            return False
        try:
            if isinstance(payload, str):
                return payload
            return json.dumps(payload, default=str, ensure_ascii=False, indent=2)
        except Exception:
            return str(payload)

    @api.model
    def _flow_from_context(self):
        log_id = self.env.context.get("ordermentum_log_id")
        if not log_id:
            return False
        log = self.sudo().browse(int(log_id))
        return log if log.exists() else False

    def add_step(
        self,
        *,
        name: str,
        state: str = "done",
        event: str | None = None,
        request_payload=None,
        response_payload=None,
        sale_order=None,
        invoice=None,
    ):
        self.ensure_one()
        seq = (self.line_ids and (max(self.line_ids.mapped("sequence")) + 1)) or 1
        line = self.env["cs.ordermentum.log.line"].sudo().create(
            {
                "log_id": self.id,
                "sequence": seq,
                "name": name,
                "state": state,
                "event": event or False,
                "request_payload": self._as_json_text(request_payload),
                "response_payload": self._as_json_text(response_payload),
            }
        )

        if sale_order and not self.sale_order_id:
            self.sale_order_id = sale_order.id
        if invoice and not self.invoice_id:
            self.invoice_id = invoice.id

        if state == "error" and self.state != "error":
            self.state = "error"
        return line

    @api.model
    def start_flow(
        self,
        *,
        name: str,
        log_type: str = "webhook",
        event: str | None = None,
        reference: str | None = None,
        request_payload=None,
    ):
        return self.sudo().create(
            {
                "name": name,
                "log_type": log_type,
                "state": "done",
                "event": event or False,
                "reference": reference or False,
                "request_payload": self._as_json_text(request_payload),
            }
        )

    @api.model
    def create_log(
        self,
        *,
        name: str,
        log_type: str = "sync",
        state: str = "done",
        event: str | None = None,
        reference: str | None = None,
        request_payload=None,
        response_payload=None,
    ):
        return self.sudo().create(
            {
                "name": name,
                "log_type": log_type,
                "state": state,
                "event": event or False,
                "reference": reference or False,
                "request_payload": self._as_json_text(request_payload),
                "response_payload": self._as_json_text(response_payload),
            }
        )


class OrdermentumLogLine(models.Model):
    _name = "cs.ordermentum.log.line"
    _description = "Ordermentum Log Step"
    _order = "sequence asc, id asc"

    log_id = fields.Many2one("cs.ordermentum.log", required=True, ondelete="cascade")
    sequence = fields.Integer(default=1)

    name = fields.Char(required=True)
    state = fields.Selection([("done", "Done"), ("error", "Error")], required=True, default="done")
    event = fields.Char()

    request_payload = fields.Text()
    response_payload = fields.Text()
