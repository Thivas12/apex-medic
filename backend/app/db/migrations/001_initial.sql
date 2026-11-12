CREATE TABLE apexmedic.cases (
    case_id text PRIMARY KEY,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE apexmedic.case_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id text NOT NULL UNIQUE,
    case_id text NOT NULL REFERENCES apexmedic.cases(case_id),
    event_type text NOT NULL CHECK (event_type IN ('observation', 'test', 'answer')),
    recorded_at timestamptz NOT NULL,
    payload text NOT NULL
);
CREATE INDEX events_by_case ON apexmedic.case_events(case_id, sequence);

CREATE TABLE apexmedic.visitor_sessions (
    token_hash text PRIMARY KEY,
    created_at bigint NOT NULL,
    expires_at bigint NOT NULL
);
CREATE TABLE apexmedic.case_owners (
    case_id text PRIMARY KEY REFERENCES apexmedic.cases(case_id),
    session_hash text NOT NULL REFERENCES apexmedic.visitor_sessions(token_hash)
);
CREATE INDEX owners_by_session ON apexmedic.case_owners(session_hash);

CREATE TABLE apexmedic.storage_totals (
    id integer PRIMARY KEY CHECK (id = 1),
    case_count bigint NOT NULL CHECK (case_count >= 0),
    payload_bytes bigint NOT NULL CHECK (payload_bytes >= 0)
);
INSERT INTO apexmedic.storage_totals VALUES (1, 0, 0);

CREATE FUNCTION apexmedic.account_payload() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    count_delta bigint := 0;
    byte_delta bigint := 0;
BEGIN
    IF TG_OP = 'INSERT' THEN
        byte_delta := octet_length(NEW.payload);
        IF TG_TABLE_NAME = 'cases' THEN count_delta := 1; END IF;
    ELSIF TG_OP = 'DELETE' THEN
        byte_delta := -octet_length(OLD.payload);
        IF TG_TABLE_NAME = 'cases' THEN count_delta := -1; END IF;
    ELSE
        byte_delta := octet_length(NEW.payload) - octet_length(OLD.payload);
    END IF;
    UPDATE apexmedic.storage_totals
    SET case_count = case_count + count_delta, payload_bytes = payload_bytes + byte_delta
    WHERE id = 1;
    IF NOT FOUND THEN RAISE EXCEPTION 'Storage accounting is unavailable'; END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER cases_budget AFTER INSERT OR UPDATE OR DELETE ON apexmedic.cases
FOR EACH ROW EXECUTE FUNCTION apexmedic.account_payload();
CREATE TRIGGER events_budget AFTER INSERT OR UPDATE OR DELETE ON apexmedic.case_events
FOR EACH ROW EXECUTE FUNCTION apexmedic.account_payload();

CREATE TABLE apexmedic.request_counters (
    bucket text PRIMARY KEY,
    hits bigint NOT NULL CHECK (hits > 0),
    expires_at double precision NOT NULL
);
CREATE INDEX request_counter_expiry ON apexmedic.request_counters(expires_at);
