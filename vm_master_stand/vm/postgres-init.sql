CREATE TABLE reports (
    id text PRIMARY KEY, title text NOT NULL, body text NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'published'))
);
INSERT INTO reports VALUES ('REL-204', 'Staging readiness',
    'Twelve sensor modules reconciled. Alex must review the warehouse export. Next review: 2026-11-02.', 'draft');
CREATE TABLE profiles (id text PRIMARY KEY, name text NOT NULL);
INSERT INTO profiles VALUES ('alex', 'Alex Morgan');
CREATE TABLE subscriptions (
    profile_id text REFERENCES profiles(id), channel text NOT NULL,
    event text NOT NULL, enabled boolean NOT NULL DEFAULT true,
    PRIMARY KEY (profile_id, channel, event)
);
CREATE TABLE tickets (id text PRIMARY KEY, owner text NOT NULL, status text NOT NULL);
INSERT INTO tickets VALUES ('OPS-318', 'Maya Chen', 'awaiting_review');
CREATE TABLE messages (
    id bigserial PRIMARY KEY, ticket_id text NOT NULL REFERENCES tickets(id),
    recipient text NOT NULL, body text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE inventory (sku text PRIMARY KEY, available integer NOT NULL CHECK (available >= 0));
INSERT INTO inventory VALUES ('sensor-module', 12);
CREATE TABLE reservations (
    order_id text PRIMARY KEY, sku text NOT NULL REFERENCES inventory(sku),
    quantity integer NOT NULL CHECK (quantity > 0)
);
CREATE TABLE service_routes (id text PRIMARY KEY, destination text NOT NULL);
INSERT INTO service_routes VALUES ('inventory-export', 'staging');
CREATE TABLE audit_events (
    id bigserial PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
    action text NOT NULL, payload jsonb NOT NULL, outcome text NOT NULL
);
