"""Neon (Postgres) storage for submissions and running success-rate stats.

Tables:
  submissions      - recent individual submissions, pruned to MAX_ROWS
  submission_stats - one row per run_label with lifetime counters that are
                     never pruned, so the success rate survives cleanup

The DB is best-effort: if a write fails, we reconnect once and otherwise
drop that record rather than crash the submit loop. That's why the stats
are "a good indication" rather than exact.
"""
import os
import psycopg2

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_label   TEXT        NOT NULL,
    email       TEXT,
    first_name  TEXT,
    last_name   TEXT,
    success     BOOLEAN     NOT NULL,
    status_code INTEGER,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS submission_stats (
    run_label    TEXT PRIMARY KEY,
    total        BIGINT      NOT NULL DEFAULT 0,
    successes    BIGINT      NOT NULL DEFAULT 0,
    failures     BIGINT      NOT NULL DEFAULT 0,
    rate_limited BIGINT      NOT NULL DEFAULT 0,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW success_rate AS
SELECT run_label, total, successes, failures, rate_limited,
       ROUND(100.0 * successes / NULLIF(total, 0), 2) AS success_pct,
       first_seen, last_seen
FROM submission_stats;
"""

INSERT_SUBMISSION = """
INSERT INTO submissions (run_label, email, first_name, last_name, success, status_code, message)
VALUES (%s, %s, %s, %s, %s, %s, %s)
"""

UPSERT_STATS = """
INSERT INTO submission_stats (run_label, total, successes, failures, rate_limited)
VALUES (%(label)s, 1, %(s)s, %(f)s, %(r)s)
ON CONFLICT (run_label) DO UPDATE SET
    total        = submission_stats.total + 1,
    successes    = submission_stats.successes + EXCLUDED.successes,
    failures     = submission_stats.failures + EXCLUDED.failures,
    rate_limited = submission_stats.rate_limited + EXCLUDED.rate_limited,
    last_seen    = now()
"""

# Keep only the newest MAX_ROWS submissions. Deleting by id cutoff is cheap
# because id is the primary key.
PRUNE = """
DELETE FROM submissions
WHERE id <= (
    SELECT id FROM submissions ORDER BY id DESC OFFSET %s LIMIT 1
)
"""


class SubmissionDB:
    def __init__(self, dsn=None, run_label="default", max_rows=10_000, prune_every=100):
        # strip() guards against a trailing newline/space pasted into the env var
        self.dsn = (dsn or os.environ.get("DATABASE_URL") or "").strip().strip('"\'')
        if not self.dsn:
            raise ValueError("DATABASE_URL is not set (put your Neon connection string in .env).")
        self.run_label = run_label
        self.max_rows = max_rows
        self.prune_every = prune_every
        self._writes = 0
        self.conn = None
        self._connect()
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)

    def _connect(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        # keepalives help with Neon dropping idle connections
        self.conn = psycopg2.connect(
            self.dsn, connect_timeout=10,
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        )
        self.conn.autocommit = True

    def _execute(self, fn):
        """Run fn(cursor); on a connection error, reconnect and retry once."""
        for attempt in (1, 2):
            try:
                with self.conn.cursor() as cur:
                    return fn(cur)
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                if attempt == 2:
                    raise
                print(f"   ⚠️ DB connection lost ({e.__class__.__name__}), reconnecting...")
                self._connect()

    def record(self, email, first_name, last_name, success, status_code, message):
        """Store one submission and bump the counters. Never raises."""
        rate_limited = status_code == 429

        def write(cur):
            cur.execute(INSERT_SUBMISSION, (
                self.run_label, email, first_name, last_name,
                success, status_code, (message or "")[:500],
            ))
            cur.execute(UPSERT_STATS, {
                "label": self.run_label,
                "s": int(success), "f": int(not success), "r": int(rate_limited),
            })

        try:
            self._execute(write)
            self._writes += 1
            if self._writes % self.prune_every == 0:
                self.prune()
        except Exception as e:
            print(f"   ⚠️ DB write skipped: {e}")

    def prune(self):
        try:
            deleted = self._execute(lambda cur: (cur.execute(PRUNE, (self.max_rows,)), cur.rowcount)[1])
            if deleted:
                print(f"   🧹 Pruned {deleted} old submissions (keeping newest {self.max_rows}).")
        except Exception as e:
            print(f"   ⚠️ DB prune skipped: {e}")

    def stats(self):
        try:
            def q(cur):
                cur.execute(
                    "SELECT total, successes, failures, rate_limited, success_pct "
                    "FROM success_rate WHERE run_label = %s",
                    (self.run_label,),
                )
                return cur.fetchone()
            return self._execute(q)
        except Exception as e:
            print(f"   ⚠️ DB stats read failed: {e}")
            return None

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
