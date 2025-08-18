from __future__ import annotations
from tomcat.config import settings
from tomcat.utils.sender import safe_send
from tomcat.models.dues_store import Payment, insert_payment

async def handle_dues_notice(intent, ctx):
    channel = ctx["channel"]
    author = ctx.get("author")
    if settings.ch_dues_portal and channel.id != settings.ch_dues_portal:
        return
    payment = Payment(
        provider="discord",
        txn_id=f"disc-{ctx['message'].id}",
        amount_cents=0,
        currency=settings.dues_currency,
        payer_name=author.display_name if author else None,
        payer_handle=None,
        payer_email=None,
        memo=ctx["message"].content,
        ts_epoch=int(ctx["message"].created_at.timestamp()),
        raw_source=f"discord:{ctx['message'].id}",
    )
    insert_payment(payment)
    await safe_send(channel, "Dues notice recorded.")
