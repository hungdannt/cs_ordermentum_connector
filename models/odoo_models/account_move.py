import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from ..ordermentum_client import OrdermentumClient


_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    ordermentum_paid_synced = fields.Boolean(string="Ordermentum Paid Synced", default=False, copy=False)
    ordermentum_order_id = fields.Char(string="Ordermentum Order ID", copy=False, index=True)


    @api.depends('amount_residual', 'move_type', 'state', 'company_id')
    def _compute_payment_state(self):
        super()._compute_payment_state()
        for invoice in self:
            if invoice.move_type == 'out_invoice' and invoice.ordermentum_order_id and invoice.payment_state == 'paid':
                template = self.env.ref(
                    'cs_ordermentum_connector.mail_template_ordermentum_payment_done',
                    raise_if_not_found=False
                )
                if template:
                    template.send_mail(invoice.id, force_send=True)


    def _ordermentum_register_payment(self):
        self.ensure_one()

        if self.move_type != "out_invoice":
            return
        if self.state != "posted":
            return

        invoice = self
        if invoice.company_id:
            invoice = invoice.with_company(invoice.company_id)

        try:
            flow = self.env["cs.ordermentum.log"]._flow_from_context()
            payload = {
                "invoice": self.name,
                "origin": self.invoice_origin,
                "payment_state": self.payment_state,
                "amount_residual": self.amount_residual,
            }
            if flow:
                flow.add_step(
                    name=f"Register Payment Attempt: {self.name}",
                    event="register_payment_attempt",
                    request_payload=payload,
                    invoice=self,
                )
            else:
                self.env["cs.ordermentum.log"].create_log(
                    name=f"Register Payment Attempt: {self.name}",
                    log_type="payment",
                    state="done",
                    event="register_payment_attempt",
                    reference=self.invoice_origin or self.name,
                    request_payload=payload,
                )
        except Exception:
            pass
        if self.payment_state == "paid":
            self.ordermentum_paid_synced = True
            return

        journal_id = self.env["ir.config_parameter"].sudo().get_param("cs_ordermentum_connector.payment_journal_id")
        if not journal_id:
            raise ValidationError("Missing Ordermentum Payment Journal in Settings")
        try:
            journal_id = int(journal_id)
        except Exception as e:
            raise ValidationError("Invalid Ordermentum Payment Journal in Settings") from e

        journal = invoice.env["account.journal"].browse(journal_id)
        if not journal.exists():
            raise ValidationError("Ordermentum Payment Journal not found")

        receivable_lines = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled
        )
        if not receivable_lines:
            return

        wizard_ctx = {
            "active_model": "account.move.line",
            "active_ids": receivable_lines.ids,
            "dont_redirect_to_payments": True,
            "company_id": invoice.company_id.id,
            "allowed_company_ids": [invoice.company_id.id],
        }

        wizard = (
            invoice.env["account.payment.register"]
            .with_context(**wizard_ctx)
            .create(
                {
                    "journal_id": journal.id,
                    "amount": invoice.amount_residual,
                    "payment_date": fields.Date.context_today(invoice),
                }
            )
        )

        wizard.action_create_payments()
        invoice.ordermentum_paid_synced = True
        _logger.info("Ordermentum payment registered: invoice=%s", invoice.name)
        try:
            flow = invoice.env["cs.ordermentum.log"]._flow_from_context()
            payload = {"invoice": invoice.name, "origin": invoice.invoice_origin}
            if flow:
                flow.add_step(
                    name=f"Payment Registered: {invoice.name}",
                    event="payment_registered",
                    response_payload=payload,
                    invoice=invoice,
                )
            else:
                invoice.env["cs.ordermentum.log"].create_log(
                    name=f"Payment Registered: {invoice.name}",
                    log_type="payment",
                    state="done",
                    event="payment_registered",
                    reference=invoice.invoice_origin or invoice.name,
                    response_payload=payload,
                )
        except Exception:
            pass

    @api.model
    def _ordermentum_fetch_invoice_detail(self, invoice_id: str):
        client = OrdermentumClient(self.env)
        path = f"/v1/invoices/{invoice_id}"
        response = client.request("GET", path)
        try:
            flow = self.env["cs.ordermentum.log"]._flow_from_context()
            if flow:
                flow.add_step(
                    name=f"GET {path}",
                    state="done" if isinstance(response, dict) else "error",
                    event="api_get_invoice",
                    request_payload={"path": path},
                    response_payload=response,
                )
            else:
                self.env["cs.ordermentum.log"].create_log(
                    name=f"GET {path}",
                    log_type="sync",
                    state="done" if isinstance(response, dict) else "error",
                    event="api_get_invoice",
                    reference=invoice_id,
                    request_payload={"path": path},
                    response_payload=response,
                )
        except Exception:
            pass
        return response

    @api.model
    def _ordermentum_apply_invoice_detail(self, detail: dict):
        status = (detail.get("status") or "").strip().lower()
        if status != "paid":
            return

        _logger.info("Ordermentum invoice detail paid received")
        try:
            flow = self.env["cs.ordermentum.log"]._flow_from_context()
            if flow:
                flow.add_step(name="Invoice Detail Paid", event="invoice_paid_detail", request_payload=detail)
            else:
                self.env["cs.ordermentum.log"].create_log(
                    name="Invoice Detail Paid",
                    log_type="payment",
                    state="done",
                    event="invoice_paid_detail",
                    request_payload=detail,
                )
        except Exception:
            pass

        SaleOrder = self.env["sale.order"].sudo()
        ordermentum_order_ids = detail.get("orderIds") or detail.get("order_ids") or []
        if isinstance(ordermentum_order_ids, str):
            ordermentum_order_ids = [ordermentum_order_ids]
        if not isinstance(ordermentum_order_ids, list) or not ordermentum_order_ids:
            return

        orders = SaleOrder.search([("ordermentum_order_id", "in", ordermentum_order_ids)])

        missing_ids = list(set([oid for oid in ordermentum_order_ids if oid]) - set(orders.mapped("ordermentum_order_id")))
        if missing_ids:
            for order_id in missing_ids:
                try:
                    detail_payload = SaleOrder._ordermentum_fetch_order_detail(order_id)
                    if isinstance(detail_payload, dict):
                        SaleOrder._ordermentum_upsert_from_detail(detail_payload)
                except Exception:
                    _logger.exception("Failed to fetch/upsert Ordermentum order %s while applying invoice detail", order_id)

            orders = SaleOrder.search([("ordermentum_order_id", "in", ordermentum_order_ids)])
        if not orders:
            return

        invoices = self.search(
            [
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("payment_state", "!=", "paid"),
                ("invoice_origin", "in", orders.mapped("name")),
                ("ordermentum_paid_synced", "=", False),
            ]
        )
        for inv in invoices:
            inv._ordermentum_register_payment()

        _logger.info(
            "Ordermentum invoice detail applied: orders=%s invoices=%s",
            len(orders),
            len(invoices),
        )

    def _cron_send_invoice_reminders(self):
        today = fields.Date.today()

        invoices = self.search([
            ('ordermentum_order_id', '=', True),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('amount_residual', '>', 0),
            ('invoice_date_due', '!=', False),
            ('payment_state', '=', 'not_paid'),
        ])

        for inv in invoices:
            days_overdue = (today - inv.invoice_date_due).days

            if days_overdue == 0:
                inv._send_reminder_email('cs_ordermentum_connector.mail_template_ordermentum_invoice_due')

            elif days_overdue == 7:
                inv._send_reminder_email('cs_ordermentum_connector.mail_template_ordermentum_invoice_overdue_7')

            elif days_overdue == 14:
                inv._send_reminder_email('cs_ordermentum_connector.mail_template_ordermentum_invoice_overdue_14')

            elif days_overdue == 30:
                inv._send_reminder_email('cs_ordermentum_connector.mail_template_ordermentum_invoice_overdue_30')