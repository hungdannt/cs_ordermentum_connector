import logging
from datetime import timedelta
from urllib.parse import urlencode

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..ordermentum_client import OrdermentumClient, parse_ordermentum_datetime


_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    _sql_constraints = [
        (
            "sale_order_ordermentum_order_id_uniq",
            "unique(ordermentum_order_id)",
            "Ordermentum Order ID must be unique.",
        ),
    ]

    ordermentum_order_id = fields.Char(string="Ordermentum Order ID", copy=False, index=True)
    ordermentum_order_status = fields.Char(string="Ordermentum Order Status", copy=False, readonly=True)
    ordermentum_last_sync_date = fields.Datetime(string="Ordermentum Last Sync", copy=False, readonly=True)
    ordermentum_delivered_applied = fields.Boolean(default=False)
    is_ordermentum = fields.Boolean(default=False)
    po_number = fields.Char(string="PO Number")

    ordermentum_fulfilment_type = fields.Selection(
        [("van", "Van"), ("3pl", "3PL (CartonCloud)")],
        string="Fulfilment",
        default="van",
        copy=False,
        tracking=True,
    )

    def _ordermentum_get_param(self, key: str, default=None):
        return self.env["ir.config_parameter"].sudo().get_param(key, default=default)

    @api.model
    def _ordermentum_fetch_order_detail(self, order_id: str):
        client = OrdermentumClient(self.env)
        path = f"/v1/orders/{order_id}"
        response = client.request("GET", path)
        try:
            flow = self.env["cs.ordermentum.log"]._flow_from_context()
            if flow:
                flow.add_step(
                    name=f"GET {path}",
                    state="done" if isinstance(response, dict) else "error",
                    event="api_get_order",
                    request_payload={"path": path},
                    response_payload=response,
                )
            else:
                self.env["cs.ordermentum.log"].create_log(
                    name=f"GET {path}",
                    log_type="sync",
                    state="done" if isinstance(response, dict) else "error",
                    event="api_get_order",
                    reference=order_id,
                    request_payload={"path": path},
                    response_payload=response,
                )
        except Exception:
            pass
        return response

    def _ordermentum_bool_param(self, key: str, default: bool = False) -> bool:
        raw = self._ordermentum_get_param(key)
        if raw is None:
            return default
        return str(raw).lower() in ("1", "true", "yes", "y")

    def _ordermentum_parse_dt(self, value):
        return parse_ordermentum_datetime(value)

    @classmethod
    def _ordermentum_build_orders_url_v2(cls, supplier_id: str, page_size: int, page_no: int, updated_at_gte: str | None):
        params = {
            "supplierId": supplier_id,
            "pageSize": page_size,
            "pageNo": page_no,
        }
        if updated_at_gte:
            params["updatedAt[gte]"] = updated_at_gte
        return f"/v2/orders?{urlencode(params)}"

    def _ordermentum_get_or_create_delivery_partner(self, parent_partner, retailer_address):
        """Create or find a delivery partner from retailer address"""
        if not retailer_address:
            return False
            
        # Search for existing delivery partner with same address
        delivery_partners = self.env["res.partner"].search([
            ("parent_id", "=", parent_partner.id),
            ("type", "=", "delivery"),
            ("street", "=", retailer_address.get("street1") or ""),
            ("city", "=", retailer_address.get("suburb") or ""),
            ("zip", "=", retailer_address.get("postcode") or ""),
        ])
        
        if delivery_partners:
            return delivery_partners[0]
            
        # Create new delivery partner
        vals = {
            "parent_id": parent_partner.id,
            "type": "delivery",
            "name": f"{parent_partner.name} (Delivery)",
            "street": retailer_address.get("street1") or "",
            "street2": retailer_address.get("street2") or "",
            "city": retailer_address.get("suburb") or "",
            "state_id": self._ordermentum_get_state(retailer_address.get("state"), retailer_address.get("country")),
            "zip": retailer_address.get("postcode") or "",
            "country_id": self._ordermentum_get_country(retailer_address.get("country")),
            "is_company": False,  # Delivery addresses are typically contacts, not companies
        }
        
        return self.env["res.partner"].sudo().create(vals)

    def _ordermentum_get_state(self, state_name, country_name):
        """Convert state name/code to Odoo state_id"""
        if not state_name:
            return False
        country_id = self._ordermentum_get_country(country_name)
        
        # First try exact match on code (for Australian states like NSW, VIC)
        if country_id:
            state = self.env["res.country.state"].search([
                ("code", "=", state_name.upper()),
                ("country_id", "=", country_id)
            ], limit=1)
            if state:
                return state.id
            
            # Then try name match
            state = self.env["res.country.state"].search([
                ("name", "=ilike", state_name),
                ("country_id", "=", country_id)
            ], limit=1)
        else:
            # Try code first without country filter
            state = self.env["res.country.state"].search([("code", "=", state_name.upper())], limit=1)
            if state:
                return state.id
            
            # Then try name
            state = self.env["res.country.state"].search([("name", "=ilike", state_name)], limit=1)
        
        return state.id if state else False

    def _ordermentum_get_country(self, country_name):
        """Convert country name to Odoo country_id"""
        if not country_name:
            return False
        country = self.env["res.country"].search([("name", "=ilike", country_name)], limit=1)
        return country.id if country else False

    def _ordermentum_get_default_payment_term(self):
        term_id = self._ordermentum_get_param("cs_ordermentum_connector.default_payment_term_id")
        if not term_id:
            return False
        try:
            term_id = int(term_id)
        except Exception:
            return False
        term = self.env["account.payment.term"].browse(term_id)
        return term if term.exists() else False

    def _ordermentum_get_or_create_product(self, code: str, name: str | None = None):
        Product = self.env["product.product"].sudo()
        code = (code or "").strip()
        if not code:
            raise ValidationError("Missing product code/SKU from Ordermentum")

        product = Product.search([("default_code", "=", code)], limit=1)
        if product:
            return product

        return Product.create(
            {
                "name": name or code,
                "default_code": code,
                "detailed_type": "product",
            }
        )

    def _ordermentum_get_or_create_partner_from_purchaser(self, purchaser_id: str):
        Partner = self.env["res.partner"].sudo()
        purchaser_id = (purchaser_id or "").strip()
        if not purchaser_id:
            return False

        partner = Partner.search([("ordermentum_purchaser_id", "=", purchaser_id)], limit=1)
        if partner:
            return partner

        client = OrdermentumClient(self.env)
        payload = client.request("GET", f"/v1/purchasers/{purchaser_id}")
        if not isinstance(payload, dict):
            raise ValidationError("Unexpected purchaser payload from Ordermentum")

        # Use billing address for the partner (customer)
        billing_address = payload.get("billingAddress") or {}
        retailer_abn = payload.get("retailerAbn").replace(" ", "") if payload.get("retailerAbn") else False
        retailer_email = payload.get("retailerBillingEmail") or False
        retailer_name = payload.get("retailerName") or False
        
        # Search for existing partner by VAT or email
        search_domain = []
        if retailer_abn:
            search_domain.append(("vat", "=", retailer_abn))
        if retailer_email:
            search_domain.append(("email", "=", retailer_email))
        if retailer_name:
            search_domain.append(("name", "=", retailer_name))
        
        if search_domain:
            existing_partner = Partner.search(search_domain, limit=1)
            if existing_partner:
                # Update the existing partner with the purchaser_id
                existing_partner.ordermentum_purchaser_id = purchaser_id
                _logger.info("Updated existing partner %s with Ordermentum purchaser ID %s", existing_partner.name, purchaser_id)
                return existing_partner
        
        vals = {
            "name": payload.get("retailerName"),
            "phone": payload.get("retailerPhone") or False,
            "vat": retailer_abn,
            "email": retailer_email,
            "street": billing_address.get("street1") or "",
            "street2": billing_address.get("street2") or "",
            "city": billing_address.get("suburb") or "",
            "state_id": self._ordermentum_get_state(billing_address.get("state"), billing_address.get("country")),
            "zip": billing_address.get("postcode") or "",
            "country_id": self._ordermentum_get_country(billing_address.get("country")),
            "ordermentum_purchaser_id": purchaser_id,
            "is_company": True,
        }
        return Partner.create(vals)

    def _ordermentum_prepare_lines(self, detail: dict):
        lines = []
        # Get GST tax (10% for Australia)
        gst_tax = self.env["account.tax"].search([("name", "ilike", "GST"), ("type_tax_use", "=", "sale")], limit=1)
        if not gst_tax:
            _logger.warning("GST tax (10%) not found. Orders will be created without tax.")
        
        for item in (detail.get("lineItems") or []):
            if not isinstance(item, dict):
                continue
            sku = item.get("SKU") or item.get("sku") or item.get("productCode") or item.get("code")
            name = item.get("name")
            qty = item.get("quantity") or 0
            price = item.get("price") or 0
            if not sku:
                continue
            product = self._ordermentum_get_or_create_product(str(sku), name=name)
            line_vals = {
                "product_id": product.id, 
                "product_uom_qty": float(qty), 
                "price_unit": float(price)
            }
            if gst_tax:
                line_vals["tax_id"] = [(6, 0, [gst_tax.id])]
            lines.append((0, 0, line_vals))
        return lines

    def _ordermentum_lines_signature(self, line_commands):
        signature = []
        for cmd in (line_commands or []):
            if not isinstance(cmd, (list, tuple)) or len(cmd) < 3:
                continue
            if cmd[0] != 0:
                continue
            vals = cmd[2] if isinstance(cmd[2], dict) else {}
            signature.append(
                (
                    int(vals.get("product_id") or 0),
                    float(vals.get("product_uom_qty") or 0.0),
                )
            )
        return sorted(signature)

    def _ordermentum_lock_order_id(self, order_id: str):
        """Prevent concurrent creation of the same Ordermentum order.

        Webhooks may arrive concurrently (e.g. order_* and invoice_updated), and
        both can trigger an upsert. We use a transaction-level advisory lock to
        serialize upserts per ordermentum_order_id.
        """
        order_id = (order_id or "").strip()
        if not order_id:
            return
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (order_id,))

    def _ordermentum_build_update_vals(self, detail: dict):
        vals = {}

        delivery_date = self._ordermentum_parse_dt(detail.get("deliveryDate"))
        if delivery_date:
            vals["commitment_date"] = delivery_date

        if detail.get("comment"):
            vals["note"] = detail.get("comment")

        if detail.get("reference"):
            vals["po_number"] = detail.get("reference")

        return vals

    def _ordermentum_needs_rollback(self, order, detail: dict, desired_vals: dict):
        desired_lines = self._ordermentum_lines_signature(self._ordermentum_prepare_lines(detail))
        current_lines = sorted(
            [
                (
                    l.product_id.id,
                    float(l.product_uom_qty or 0.0),
                )
                for l in order.order_line
                if not l.display_type
            ]
        )
        if desired_lines != current_lines:
            return True

        return False

    def _ordermentum_rollback_and_reconfirm(self, order, detail: dict, desired_vals: dict):
        flow = self.env["cs.ordermentum.log"]._flow_from_context()

        if order.picking_ids.filtered(lambda p: p.state == "done"):
            if flow:
                flow.add_step(
                    name="Rollback skipped: picking done",
                    state="error",
                    event="rollback_skipped",
                    request_payload={"sale_order": order.name, "reason": "picking_done"},
                    sale_order=order,
                )
            return False

        invoices = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice" and m.state != "cancel")
        if invoices.filtered(lambda m: m.payment_state == "paid"):
            if flow:
                flow.add_step(
                    name="Rollback skipped: invoice paid",
                    state="error",
                    event="rollback_skipped",
                    request_payload={"sale_order": order.name, "reason": "invoice_paid"},
                    sale_order=order,
                )
            return False

        for inv in invoices:
            try:
                if inv.state == "posted" and hasattr(inv, "button_draft"):
                    inv.button_draft()
                if hasattr(inv, "button_cancel"):
                    inv.button_cancel()
                elif hasattr(inv, "action_cancel"):
                    inv.action_cancel()
            except Exception as e:
                if flow:
                    flow.add_step(
                        name=f"Invoice rollback failed: {inv.name}",
                        state="error",
                        event="invoice_rollback_failed",
                        request_payload={"invoice": inv.name, "error": str(e)},
                        sale_order=order,
                        invoice=inv,
                    )
                raise

        pickings = order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        if pickings:
            pickings.action_cancel()
            pickings.unlink()

        order.with_context(disable_cancel_warning=True).action_cancel()
        order.action_draft()
        update_vals = dict(desired_vals)
        desired_line_cmds = [(5, 0, 0)] + (self._ordermentum_prepare_lines(detail) or [])
        update_vals["order_line"] = desired_line_cmds
        update_vals["ordermentum_order_status"] = detail.get("status") or ""
        update_vals["ordermentum_last_sync_date"] = fields.Datetime.now()

        order.write(update_vals)

        order.with_context(skip_ordermentum_confirm_email=True).action_confirm()

        auto_invoice = self._ordermentum_bool_param("cs_ordermentum_connector.auto_create_invoice", default=True)
        if auto_invoice and order.invoice_status in ("to invoice",):
            new_invoices = order._create_invoices()
            auto_post = self._ordermentum_bool_param("cs_ordermentum_connector.auto_post_invoice", default=True)
            if auto_post:
                for inv in new_invoices:
                    inv.action_post()
                    template = self.env.ref('cs_ordermentum_connector.mail_template_ordermentum_invoice_created', raise_if_not_found=False)
                    if template:
                        template.send_mail(inv.id, force_send=True)


        if flow:
            flow.add_step(
                name=f"Rollback applied and reconfirmed: {order.name}",
                event="rollback_applied",
                sale_order=order,
            )
        return True

    def _ordermentum_upsert_from_detail(self, detail: dict):
        order_id = detail.get("id")
        if not order_id:
            return False

        # Serialize processing per Ordermentum order ID to avoid duplicates.
        self._ordermentum_lock_order_id(str(order_id))

        salesperson_name = (detail.get("properties") or {}).get("account_manager")
        missing_salesperson = not salesperson_name or salesperson_name == '–'
        if missing_salesperson:
            flow = self.env["cs.ordermentum.log"]._flow_from_context()
            if flow:
                flow.add_step(
                    name=f"Salesperson not found, skipped",
                    event="order_created",
                    request_payload={"ordermentum_order_id": order_id},
                )
            return False
        existing = self.search([("ordermentum_order_id", "=", order_id)], limit=1)

        order_number = detail.get("orderNumber")
        if not existing:
            existing = self.search([("client_order_ref", "=", order_number)], limit=1)
            if existing:
                existing.write(
                    {
                        "ordermentum_order_id": order_id,
                        "ordermentum_order_status": detail.get("status") or "",
                        "ordermentum_last_sync_date": fields.Datetime.now(),
                    }
                )

        # Re-check under lock (another concurrent webhook may have created it).
        if not existing:
            purchaser_id = detail.get("purchaserId")
            partner = self._ordermentum_get_or_create_partner_from_purchaser(purchaser_id) if purchaser_id else False
            if not partner:
                partner = self.env.company.partner_id

            salesperson = self.env["res.users"].sudo().search([("name", "ilike", salesperson_name)], limit=1)
            company_id = int(
                self._ordermentum_get_param("cs_ordermentum_connector.default_company_id")
            )
            company = self.env["res.company"].browse(company_id)
            salesperson_ctx = salesperson.with_company(company)
            warehouse = salesperson_ctx._get_default_warehouse_id()
            vals = {
                "partner_id": partner.id,
                "company_id": company_id,
                "partner_invoice_id": partner.id,
                "ordermentum_order_id": order_id,
                "ordermentum_order_status": detail.get("status") or "",
                "ordermentum_last_sync_date": fields.Datetime.now(),
                "user_id": salesperson.id,
                "warehouse_id": warehouse.id,
            }

            if order_number:
                vals["name"] = order_number
                vals["client_order_ref"] = order_number
            
            # Handle delivery address from retailerAddress in the order detail
            retailer_address = detail.get("retailerAddress") or {}
            if retailer_address:
                # Check if retailer address is different from billing address
                billing_addr = f"{partner.street or ''} {partner.street2 or ''} {partner.city or ''} {partner.zip or ''}".strip()
                retailer_addr = f"{retailer_address.get('street1', '')} {retailer_address.get('street2', '')} {retailer_address.get('suburb', '')} {retailer_address.get('postcode', '')}".strip()
                
                if retailer_addr and retailer_addr != billing_addr:
                    # Create or find delivery partner
                    delivery_partner = self._ordermentum_get_or_create_delivery_partner(partner, retailer_address)
                    if delivery_partner:
                        vals["partner_shipping_id"] = delivery_partner.id
                    else:
                        vals["partner_shipping_id"] = partner.id
                else:
                    vals["partner_shipping_id"] = partner.id
                
                # Store comment and PO number
                if detail.get("comment"):
                    vals["note"] = detail.get("comment")
                if detail.get("reference"):
                    vals["po_number"] = detail.get("reference")
            else:
                vals["partner_shipping_id"] = partner.id

            created_at = self._ordermentum_parse_dt(detail.get("createdAt"))
            if created_at:
                vals["date_order"] = created_at

            delivery_date = self._ordermentum_parse_dt(detail.get("deliveryDate"))
            if delivery_date:
                vals["commitment_date"] = delivery_date

            payment_term = self._ordermentum_get_default_payment_term()
            if payment_term:
                vals["payment_term_id"] = payment_term.id

            vals["order_line"] = self._ordermentum_prepare_lines(detail)
            vals["is_ordermentum"] = True
            order = self.create(vals)
            _logger.info("Ordermentum order created: %s (ordermentum_id=%s)", order.name, order_id)
            try:
                flow = self.env["cs.ordermentum.log"]._flow_from_context()
                if flow:
                    flow.add_step(
                        name=f"Order Created: {order.name}",
                        event="order_created",
                        request_payload={"ordermentum_order_id": order_id},
                        sale_order=order,
                    )
                else:
                    self.env["cs.ordermentum.log"].create_log(
                        name=f"Order Created: {order.name}",
                        log_type="sync",
                        state="done",
                        event="order_created",
                        reference=order_id,
                        request_payload={"ordermentum_order_id": order_id},
                    )
            except Exception:
                pass

            auto_confirm = self._ordermentum_bool_param("cs_ordermentum_connector.auto_confirm_orders", default=True)
            if auto_confirm and order.state in ("draft", "sent"):
                order.action_confirm()
                _logger.info("Ordermentum order confirmed: %s", order.name)
                try:
                    flow = self.env["cs.ordermentum.log"]._flow_from_context()
                    if flow:
                        flow.add_step(name=f"Order Confirmed: {order.name}", event="order_confirmed", sale_order=order)
                    else:
                        self.env["cs.ordermentum.log"].create_log(
                            name=f"Order Confirmed: {order.name}",
                            log_type="sync",
                            state="done",
                            event="order_confirmed",
                            reference=order_id,
                        )
                except Exception:
                    pass

            auto_invoice = self._ordermentum_bool_param("cs_ordermentum_connector.auto_create_invoice", default=True)
            if auto_invoice and order.invoice_status in ("to invoice",):
                invoices = order._create_invoices()
                _logger.info("Ordermentum invoices created for %s: %s", order.name, ",".join(invoices.mapped("name")))
                auto_post = self._ordermentum_bool_param("cs_ordermentum_connector.auto_post_invoice", default=True)
                if auto_post:
                    for inv in invoices:
                        try:
                            inv.action_post()
                            _logger.info("Ordermentum invoice posted: %s (origin=%s)", inv.name, order.name)
                            try:
                                flow = self.env["cs.ordermentum.log"]._flow_from_context()
                                if flow:
                                    flow.add_step(
                                        name=f"Invoice Posted: {inv.name}",
                                        event="invoice_posted",
                                        request_payload={"invoice": inv.name, "origin": order.name},
                                        sale_order=order,
                                        invoice=inv,
                                    )
                                else:
                                    self.env["cs.ordermentum.log"].create_log(
                                        name=f"Invoice Posted: {inv.name}",
                                        log_type="sync",
                                        state="done",
                                        event="invoice_posted",
                                        reference=order.name,
                                        request_payload={"invoice": inv.name, "origin": order.name},
                                    )
                            except Exception:
                                pass
                        except Exception:
                            _logger.exception("Failed to post invoice for %s", order.name)

        else:
            order = existing

            desired_vals = self._ordermentum_build_update_vals(detail)
            needs_rollback = self._ordermentum_needs_rollback(order, detail, desired_vals)

            if needs_rollback:
                applied = self._ordermentum_rollback_and_reconfirm(order, detail, desired_vals)
                if not applied:
                    return order
            else:
                changed_vals = {}
                for k, v in desired_vals.items():
                    if order[k] != v:
                        changed_vals[k] = v
                if changed_vals:
                    changed_vals.update(
                        {
                            "ordermentum_order_status": detail.get("status") or "",
                            "ordermentum_last_sync_date": fields.Datetime.now(),
                        }
                    )
                    order.write(changed_vals)

            status = (detail.get("status") or "").strip().lower()
            if status == "cancelled":
                order.with_context(disable_cancel_warning=True).action_cancel()

            _logger.info("Ordermentum order updated: %s (ordermentum_id=%s)", order.name, order_id)
            try:
                flow = self.env["cs.ordermentum.log"]._flow_from_context()
                if flow:
                    flow.add_step(
                        name=f"Order Updated: {order.name}",
                        event="order_updated",
                        request_payload={"ordermentum_order_id": order_id},
                        sale_order=order,
                    )
                else:
                    self.env["cs.ordermentum.log"].create_log(
                        name=f"Order Updated: {order.name}",
                        log_type="sync",
                        state="done",
                        event="order_updated",
                        reference=order_id,
                        request_payload={"ordermentum_order_id": order_id},
                    )
            except Exception:
                pass

        return order

    def _ordermentum_apply_delivered(self):
        for order in self:
            if order.ordermentum_fulfilment_type != "van":
                continue

            pickings = order.picking_ids.filtered(
                lambda p: p.picking_type_id.code == "outgoing" and p.state not in ("done", "cancel")
            )
            for picking in pickings:
                for move in picking.move_ids:
                    move.quantity = move.product_uom_qty
                picking.action_assign()
                picking.button_validate()

            template = self.env.ref("cs_ordermentum_connector.mail_template_ordermentum_delivered", raise_if_not_found=False)
            template.send_mail(picking.id, force_send=True)

    def action_confirm(self):
        res = super().action_confirm()
        if not self.ordermentum_order_id or self.ordermentum_fulfilment_type == '3pl':
            return res
        return res

    def _prepare_invoice(self):
        """This method used set a flag if the invoice is created from Ordermentum order.
        """
        inv_val = super()._prepare_invoice()
        if self.ordermentum_order_id:
            inv_val.update(
                {
                    "ordermentum_order_id": self.ordermentum_order_id,
                }
            )
        return inv_val
    
    def write(self, vals):
        res = super().write(vals)
        if "ordermentum_fulfilment_type" in vals and vals["ordermentum_fulfilment_type"] == "3pl":
            for order in self:
                try:
                    order.picking_ids.action_cancel()
                    order.picking_ids.unlink()
                    order.with_context(disable_cancel_warning=True).action_cancel()
                    order.action_draft()
                    order.action_confirm()
                    order._cartoncloud_push_outbound_immediate()
                except Exception:
                    _logger.exception("CartonCloud push outbound failed for %s", order.name)
        return res

    @api.model
    def cron_ordermentum_sync_orders(self):
        supplier_id = (self.env["ir.config_parameter"].sudo().get_param("cs_ordermentum_connector.supplier_id") or "").strip()
        if not supplier_id:
            raise ValidationError("Missing Ordermentum supplier_id in Settings")

        page_size = int(
            self.env["ir.config_parameter"].sudo().get_param("cs_ordermentum_connector.page_size", default="50") or 50
        )

        cursor = self.env["ir.config_parameter"].sudo().get_param("cs_ordermentum_connector.last_orders_poll_updated_at")

        if not cursor:
            cursor_dt = fields.Datetime.now() - timedelta(days=7)
            cursor = cursor_dt.isoformat(timespec="seconds") + "Z"

        client = OrdermentumClient(self.env)

        page_no = 1
        max_updated_dt = None
        total_summaries = 0
        total_delivered = 0

        while True:
            url = self._ordermentum_build_orders_url_v2(supplier_id, page_size, page_no, cursor)
            payload = client.request("GET", url)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list) or not data:
                break

            total_summaries += len(data)

            for summary in data:
                if not isinstance(summary, dict) or not summary.get("id"):
                    continue

                updated_dt = self._ordermentum_parse_dt(summary.get("updatedAt"))
                if updated_dt and (not max_updated_dt or updated_dt > max_updated_dt):
                    max_updated_dt = updated_dt

                if not summary.get("deliveredAt"):
                    continue

                order = self.env["sale.order"].sudo().search(
                    [("ordermentum_order_id", "=", summary["id"])],
                    limit=1,
                )

                if not order:
                    try:
                        detail = self.env["sale.order"].sudo()._ordermentum_fetch_order_detail(summary["id"])
                        if isinstance(detail, dict):
                            order = self.env["sale.order"].sudo()._ordermentum_upsert_from_detail(detail)
                        else:
                            order = False
                    except Exception:
                        _logger.exception(
                            "Ordermentum poll fetch/upsert failed for ordermentum_id=%s",
                            summary.get("id"),
                        )
                        order = False

                # If the order exists already, we can apply delivered
                if order:
                    if order.ordermentum_delivered_applied:
                        continue
                    try:
                        order._ordermentum_apply_delivered()
                        order.ordermentum_delivered_applied = True
                        total_delivered += 1
                    except Exception:
                        _logger.exception(
                            "Ordermentum poll apply delivered failed for order %s (ordermentum_id=%s)",
                            order.name,
                            summary.get("id"),
                        )
                    continue

            links = payload.get("links") if isinstance(payload, dict) else None
            next_link = links.get("next") if isinstance(links, dict) else None
            if next_link:
                page_no += 1
                continue
            break

        if max_updated_dt:
            max_updated_dt = max_updated_dt - timedelta(minutes=2)
            self.env["ir.config_parameter"].sudo().set_param(
                "cs_ordermentum_connector.last_orders_poll_updated_at",
                max_updated_dt.isoformat(timespec="milliseconds") + "Z",
            )

        _logger.info(
            "Ordermentum poll orders done: summaries=%s delivered_processed=%s cursor=%s",
            total_summaries,
            total_delivered,
            cursor,
        )
