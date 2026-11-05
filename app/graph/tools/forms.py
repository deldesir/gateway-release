import json
import os

from loguru import logger

# Business brand for chat-form notifications — set BUSINESS_NAME in the
# gitignored .env.local; the code stays generic.
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Shop")


def _notify_operator(subject: str, body: str) -> bool:
    """Deliver a form to the operator for real (email-first via the bridge).

    submit_form used to log and claim success — a fake integration: the
    user was told "ticket submitted" while nothing left the box. Route
    through the same admin-notify path the nightly jobs use.
    """
    try:
        from app.graph.tools.email import notify_admin
        if not notify_admin(subject, body):
            logger.error("[submit_form] operator notify not delivered")
            return False
        return True
    except Exception as e:
        logger.error(f"[submit_form] operator notify failed: {e}")
        return False


def submit_form(args: dict, **kwargs) -> str:
    """Submit a completed form from the RiveBot multi-turn flow."""
    form_type = args.get("form_type", "")
    data = args.get("data", "")
    user_id = args.get("user_id", "unknown")

    if not form_type or not data:
        return json.dumps({"error": "Missing form_type or data"})

    logger.info(f"[submit_form] type={form_type} user={user_id} data='{data}'")

    # ── Route by form type ──────────────────────────────────────
    if form_type == "support":
        delivered = _notify_operator(
            f"New {BUSINESS_NAME} support ticket (chat form)",
            f"From: {user_id}\nIssue: {data}",
        )
        if delivered:
            return json.dumps({
                "status": "success",
                "message": "✅ Tiket sipò ou an soumèt avèk siksè! Nou pral kontakte w nan lè ki pi vit posib. 📋"
            })
        return json.dumps({
            "status": "error",
            "message": "⚠️ Nou pa t ka voye tikè a kounye a. Tanpri eseye ankò pita."
        })

    elif form_type in ("upgrade", "request"):
        # "request" = a product request (the retail replacement for the
        # old fictional plan-upgrade form; "upgrade" kept for compatibility).
        delivered = _notify_operator(
            f"New {BUSINESS_NAME} product request (chat form)",
            f"From: {user_id}\nRequest: {data}",
        )
        if delivered:
            return json.dumps({
                "status": "success",
                "message": "✅ Demann ou an soumèt! N ap kontakte w ak disponiblite ak pri. 📞"
            })
        return json.dumps({
            "status": "error",
            "message": "⚠️ Nou pa t ka voye demann lan kounye a. Tanpri eseye ankò pita."
        })

    else:
        logger.warning(f"[submit_form] Unknown form type: {form_type}")
        delivered = _notify_operator(
            f"New {BUSINESS_NAME} form ({form_type})", f"From: {user_id}\n{data}")
        return json.dumps({
            "status": "success" if delivered else "error",
            "message": "✅ Fòmilè w la soumèt. Mèsi!" if delivered
            else "⚠️ Nou pa t ka voye fòmilè a. Tanpri eseye ankò pita."
        })
