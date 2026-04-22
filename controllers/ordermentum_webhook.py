import json
import logging
from odoo import http
from odoo.http import request


_logger = logging.getLogger(__name__)


class OrdermentumWebhookController(http.Controller):
    @http.route("/ordermentum/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def ordermentum_webhook(self, **kwargs):
        try:
            raw = request.httprequest.data
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else (raw or "{}"))
        except Exception:
            return request.make_response("Bad Request", headers=[("Content-Type", "text/plain")], status=400)

        if not isinstance(payload, dict):
            return request.make_response("Bad Request", headers=[("Content-Type", "text/plain")], status=400)

        event = (payload.get("event") or "").strip()
        entity_id = payload.get("id") if isinstance(payload, dict) else None

        _logger.info("Ordermentum webhook received: event=%s id=%s", event, entity_id)

        flow = False
        try:
            flow = request.env["cs.ordermentum.log"].start_flow(
                name=f"Webhook: {event}",
                log_type="webhook",
                event=event,
                reference=entity_id,
                request_payload=payload,
            )
        except Exception:
            flow = False

        env = request.env
        context = dict(env.context)
        if flow:
            context['ordermentum_log_id'] = flow.id
        
        try:
            if event.startswith("order"):
                env["sale.order"].with_context(context).sudo()._ordermentum_upsert_from_detail(payload)
                _logger.info("Ordermentum webhook processed order: id=%s", entity_id)
            elif event.startswith("invoice_updated"):
                env["account.move"].with_context(context).sudo()._ordermentum_apply_invoice_detail(payload)
                _logger.info("Ordermentum webhook processed invoice: id=%s", entity_id)
            else:
                _logger.info("Ordermentum webhook ignored (unhandled event): %s", event)
        except Exception:
            _logger.exception("Ordermentum webhook processing failed: event=%s id=%s", event, entity_id)
            if flow:
                try:
                    flow.add_step(name="Webhook processing failed", state="error", event="exception")
                    flow.state = "error"
                except Exception:
                    pass

            return request.make_response("Error", headers=[("Content-Type", "text/plain")], status=500)

        return request.make_response("OK", headers=[("Content-Type", "text/plain")], status=200)
