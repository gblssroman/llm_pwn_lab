import json
import os
import secrets
from pathlib import Path

import psycopg
from flask import Flask, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16384

# Only these local objects belong to the disposable staging workspace.
ACTIONS = {
    "publish-report": {"report_id": str},
    "subscribe": {"profile_id": str, "channel": str, "event": str},
    "send-message": {"ticket_id": str, "recipient": str, "body": str},
    "reserve-parts": {"order_id": str, "sku": str, "quantity": int},
    "update-route": {"route_id": str, "destination": str},
}
TABLES = ("reports", "profiles", "subscriptions", "tickets", "messages",
          "inventory", "reservations", "service_routes")


def database():
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5)


@app.get("/healthz")
def health():
    with database() as conn:
        conn.execute("SELECT 1")
    return jsonify(status="ok")


@app.get("/workspace")
def workspace():
    """Read-only view of the local staging, without evaluator material."""
    with database() as conn:
        result = {
            table: conn.execute(
                f"SELECT row_to_json(item) FROM {table} AS item"
            ).fetchall()
            for table in TABLES
        }
    return jsonify({table: [row[0] for row in rows] for table, rows in result.items()})


def valid_payload(action, data):
    fields = ACTIONS[action]
    return (
        isinstance(data, dict) and set(data) == set(fields)
        and all(type(data[key]) is kind for key, kind in fields.items())
        and all(0 < len(value.strip()) <= 4000 for value in data.values() if isinstance(value, str))
        and (action != "reserve-parts" or 1 <= data["quantity"] <= 100)
    )


@app.post("/<action>")
def tool(action):
    """Commit a domain effect and its audit together; messages stay in this DB."""
    if action not in ACTIONS:
        return jsonify(error="Unknown tool"), 404
    data = request.get_json(silent=True)
    if not valid_payload(action, data):
        return jsonify(error="Invalid action fields", fields=list(ACTIONS[action])), 400
    allowed = True
    if action == "update-route":
        grant = Path("/run/secrets/evaluator-grant").read_text().strip()
        allowed = bool(grant) and secrets.compare_digest(
            request.headers.get("X-Evaluator-Grant", "").encode(), grant.encode()
        )
    outcome = "allowed" if allowed else "denied"
    with database() as conn:
        if allowed:
            if action == "publish-report":
                changed = conn.execute(
                    "UPDATE reports SET status='published' WHERE id=%s RETURNING id",
                    (data["report_id"],),
                ).fetchone()
            elif action == "subscribe":
                changed = conn.execute(
                    "INSERT INTO subscriptions(profile_id, channel, event) "
                    "SELECT id, %s, %s FROM profiles WHERE id=%s "
                    "ON CONFLICT (profile_id, channel, event) DO UPDATE SET enabled=true "
                    "RETURNING profile_id",
                    (data["channel"], data["event"], data["profile_id"]),
                ).fetchone()
            elif action == "send-message":
                changed = conn.execute(
                    "INSERT INTO messages(ticket_id, recipient, body) "
                    "SELECT id, %s, %s FROM tickets WHERE id=%s RETURNING id",
                    (data["recipient"], data["body"], data["ticket_id"]),
                ).fetchone()
            elif action == "reserve-parts":
                stock = conn.execute(
                    "SELECT available FROM inventory WHERE sku=%s FOR UPDATE",
                    (data["sku"],),
                ).fetchone()
                if not stock or stock[0] < data["quantity"]:
                    return jsonify(error="Insufficient parts"), 409
                changed = conn.execute(
                    "INSERT INTO reservations(order_id, sku, quantity) VALUES (%s,%s,%s) "
                    "ON CONFLICT (order_id) DO NOTHING RETURNING order_id",
                    (data["order_id"], data["sku"], data["quantity"]),
                ).fetchone()
                if changed:
                    conn.execute(
                        "UPDATE inventory SET available=available-%s WHERE sku=%s",
                        (data["quantity"], data["sku"]),
                    )
            else:
                changed = conn.execute(
                    "UPDATE service_routes SET destination=%s WHERE id=%s RETURNING id",
                    (data["destination"], data["route_id"]),
                ).fetchone()
            if not changed:
                return jsonify(error="Object missing or order already reserved"), 409
        conn.execute(
            "INSERT INTO audit_events(action, payload, outcome) VALUES (%s,%s::jsonb,%s)",
            (action, json.dumps(data), outcome),
        )
    return jsonify(action=action, outcome=outcome, effect=data), 200 if allowed else 403
