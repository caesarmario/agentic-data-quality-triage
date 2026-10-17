"""Durable sanitized audit and bot delivery; an uncertain POST is never repeated."""
from __future__ import annotations

import os

from agent.tools.incident_resolution_store import control_transaction, decoded


def flush(limit: int = 30) -> dict:
    from agent.tools.audit_log import write_agent_audit_event, build_audit_idempotency_key
    from apps.discord_bot.delivery import build_retry_session, build_bot_headers, channel_messages_url, post_bot_message, validate_bot_message_response
    from pipelines.common.clickhouse import build_clickhouse_client
    with control_transaction() as cursor:
        cursor.execute("SELECT event_id,aggregate_id,event_type,payload,audit_done,delivery_done,delivery_state "
                       "FROM dq_control.recovery_outbox WHERE NOT (audit_done AND delivery_done) ORDER BY created_at LIMIT %s", (limit,))
        events = cursor.fetchall()
    counts = {"audited": 0, "delivered": 0, "pending": 0}
    for eid, aggregate_id, kind, payload, audited, delivered, delivery_state in events:
        payload = decoded(payload)
        try:
            if not audited:
                write_agent_audit_event(build_clickhouse_client(), action=kind,
                    status=str(payload.get("status", "recorded")), actor=str(payload.get("actor", "system")),
                    alert_key=str(payload.get("alert_key", "")), tool_name="recovery_control",
                    output_payload=payload, idempotency_key=build_audit_idempotency_key("recovery", str(eid)))
                with control_transaction() as cursor:
                    cursor.execute("UPDATE dq_control.recovery_outbox SET audit_done=true WHERE event_id=%s", (eid,))
                counts["audited"] += 1
            if delivered:
                continue
            if kind == "recovery_execution_started":
                from agent.recovery.service import get_execution
                current = get_execution(aggregate_id)
                if current["status"] in {"succeeded", "failed"}:
                    with control_transaction() as cursor:
                        cursor.execute("UPDATE dq_control.recovery_outbox SET delivery_done=true,delivery_state='done',error_code='' WHERE event_id=%s", (eid,))
                    continue
            if kind in {"approved_execution_reconciled", "recovery_execution_requested"}:
                with control_transaction() as cursor:
                    cursor.execute("UPDATE dq_control.recovery_outbox SET delivery_done=true,delivery_state='done',error_code='' WHERE event_id=%s", (eid,))
                continue
            channel = os.getenv("DISCORD_OPS_CHANNEL_ID", "")
            token = os.getenv("DISCORD_BOT_TOKEN", "")
            if not channel or not token:
                counts["pending"] += 1
                continue
            marker = "Recovery event · " + str(eid)
            with build_retry_session() as session:
                headers = build_bot_headers(token)
                identity = session.get("https://discord.com/api/v10/users/@me", headers=headers, timeout=5)
                identity.raise_for_status()
                author_id = str(identity.json()["id"])
                if delivery_state != "pending":
                    response = session.get(channel_messages_url(channel), headers=headers, params={"limit": 100}, timeout=5)
                    response.raise_for_status()
                    matches = [message for message in response.json() if str(message.get("author", {}).get("id")) == author_id
                        and any(embed.get("footer", {}).get("text") == marker for embed in message.get("embeds", []))]
                    if not matches:
                        counts["pending"] += 1
                        continue
                    message = matches[0]
                else:
                    with control_transaction() as cursor:
                        cursor.execute("UPDATE dq_control.recovery_outbox SET delivery_state='claimed' WHERE event_id=%s "
                                       "AND delivery_state='pending' RETURNING event_id", (eid,))
                        if not cursor.fetchone():
                            counts["pending"] += 1
                            continue
                    if kind == "recovery_plan_created":
                        from agent.recovery.service import get_plan
                        from apps.discord_bot.recovery import plan_embed, plan_components
                        bundle = get_plan(payload["plan_id"])
                        embed = plan_embed(bundle).to_dict()
                        components = plan_components(bundle)
                    else:
                        embed = {"title": "Recovery update · " + str(payload.get("alert_ref", "")),
                            "description": "Execution: **" + str(payload.get("status", "running")).upper()
                                + "**\nData verification: **" + str(payload.get("validation_status", "not_checked")).upper()
                                + "**\nThe incident is not closed automatically.", "color": 0x13946B}
                        components = []
                    embed["footer"] = {"text": marker}
                    _, message = post_bot_message(session, token, channel,
                        {"embeds": [embed], "components": components, "allowed_mentions": {"parse": []}}, 8)
                mid, _, returned_author = validate_bot_message_response(message, channel)
                if returned_author != author_id:
                    raise ValueError("UnexpectedBotAuthor")
                with control_transaction() as cursor:
                    cursor.execute("UPDATE dq_control.recovery_outbox SET delivery_done=true,delivery_state='done',message_id=%s,author_id=%s,error_code='' WHERE event_id=%s",
                                   (mid, author_id, eid))
                counts["delivered"] += 1
        except Exception as exc:
            with control_transaction() as cursor:
                cursor.execute("UPDATE dq_control.recovery_outbox SET error_code=%s,delivery_state=CASE WHEN delivery_state='claimed' THEN 'unknown' ELSE delivery_state END WHERE event_id=%s",
                               (type(exc).__name__, eid))
            counts["pending"] += 1
    return counts
