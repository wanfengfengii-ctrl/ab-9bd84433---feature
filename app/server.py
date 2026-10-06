"""Radiotherapy QC platform API.

Records per-channel fraction doses for treatment courses with strong
consistency guarantees: concurrent submissions, client retries and service
restarts never cause duplicate accumulation or prescription overflow.

Consistency model
-----------------
* All state lives in a single SQLite database (persisted on a volume).
* Every mutating request is handled inside one ``BEGIN IMMEDIATE``
  transaction guarded by a process-wide lock, so concurrent requests are
  fully serialized and each submission is all-or-nothing.
* ``deliveryId`` uniqueness makes client/网络 retries safe: an accepted
  delivery replays its first response, a conflicting reuse is rejected.
* ``expectedRevision`` implements optimistic concurrency: exactly one of a
  set of competing submissions is accepted and each acceptance increments
  the revision by exactly one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

MAX_CHANNELS = 16
MAX_CHANNEL_NAME_LEN = 64
MAX_COURSE_ID_LEN = 128
MAX_DELIVERY_ID_LEN = 128
MAX_BODY_BYTES = 64 * 1024

# Canonical positive decimal: no leading zeros, optional fraction.
DECIMAL_RE = re.compile(r"^(0|[1-9]\d*)(\.\d{1,6})?$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    course_id    TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active', 'complete')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS channels (
    course_id    TEXT NOT NULL REFERENCES courses(course_id),
    channel      TEXT NOT NULL,
    prescription TEXT NOT NULL,
    cumulative   TEXT NOT NULL,
    PRIMARY KEY (course_id, channel)
);
CREATE TABLE IF NOT EXISTS deliveries (
    course_id    TEXT NOT NULL REFERENCES courses(course_id),
    delivery_id  TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (course_id, delivery_id)
);
"""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def parse_positive_decimal(value):
    """Return a Decimal for a canonical positive decimal string, else None."""
    if not isinstance(value, str) or not DECIMAL_RE.match(value):
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        return None
    return d if d > 0 else None


def canonical_decimal(d: Decimal) -> str:
    """Render a Decimal as a normalized fixed-point string (1.50 -> 1.5)."""
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _reject_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key: {key!r}")
        obj[key] = value
    return obj


def strict_loads(raw: bytes):
    """Parse JSON, rejecting objects with duplicate keys (e.g. channels)."""
    return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)


def validate_course_payload(payload):
    """Validate a PUT body. Returns (channels: dict[str, Decimal], error)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return None, "'channels' must be an object mapping channel names to prescription doses"
    if not 1 <= len(channels) <= MAX_CHANNELS:
        return None, f"course must define between 1 and {MAX_CHANNELS} unique channels"
    parsed = {}
    for name, dose in channels.items():
        if not name or len(name) > MAX_CHANNEL_NAME_LEN:
            return None, "channel names must be non-empty strings of at most 64 characters"
        d = parse_positive_decimal(dose)
        if d is None:
            return None, f"invalid prescription dose for channel {name!r}: must be a positive decimal string"
        parsed[name] = d
    return parsed, None


def validate_delivery_payload(payload):
    """Validate a POST body. Returns ((delivery_id, expected, increments), error)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    delivery_id = payload.get("deliveryId")
    if not isinstance(delivery_id, str) or not delivery_id or len(delivery_id) > MAX_DELIVERY_ID_LEN:
        return None, "'deliveryId' must be a non-empty string of at most 128 characters"
    expected = payload.get("expectedRevision")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return None, "'expectedRevision' must be a non-negative integer"
    increments = payload.get("increments")
    if not isinstance(increments, dict) or not increments:
        return None, "'increments' must name at least one channel with a positive dose"
    parsed = {}
    for name, dose in increments.items():
        d = parse_positive_decimal(dose)
        if d is None:
            return None, f"invalid dose increment for channel {name!r}: must be a positive decimal string"
        parsed[name] = d
    return (delivery_id, expected, parsed), None


def _hash_payload(obj) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Storage / core logic
# ---------------------------------------------------------------------------

class CourseStore:
    """Thread-safe, transaction-backed store for courses and deliveries."""

    def __init__(self, db_path: str):
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # -- queries -----------------------------------------------------------

    def _course_body(self, course_id: str) -> dict:
        row = self._conn.execute(
            "SELECT revision, status, created_at FROM courses WHERE course_id=?",
            (course_id,),
        ).fetchone()
        rows = self._conn.execute(
            "SELECT channel, prescription, cumulative FROM channels "
            "WHERE course_id=? ORDER BY channel",
            (course_id,),
        ).fetchall()
        return {
            "courseId": course_id,
            "revision": row[0],
            "status": row[1],
            "createdAt": row[2],
            "channels": {
                r[0]: {"prescription": r[1], "cumulative": r[2]} for r in rows
            },
        }

    def get_course(self, course_id: str):
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM courses WHERE course_id=?", (course_id,)
            ).fetchone()
            if exists is None:
                return 404, {"error": "course_not_found",
                             "message": f"course {course_id!r} does not exist"}
            return 200, self._course_body(course_id)

    # -- commands ----------------------------------------------------------

    def create_course(self, course_id: str, channels: dict):
        """Idempotently create a course. Returns (http_status, body)."""
        request_hash = _hash_payload(
            {"channels": {k: canonical_decimal(v) for k, v in sorted(channels.items())}}
        )
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    "SELECT request_hash FROM courses WHERE course_id=?", (course_id,)
                ).fetchone()
                if row is not None:
                    cur.execute("ROLLBACK")
                    if row[0] == request_hash:
                        # Same-content retry: return the existing result.
                        return 200, self._course_body(course_id)
                    return 409, {
                        "error": "course_conflict",
                        "message": f"course {course_id!r} already exists with different content",
                    }
                cur.execute(
                    "INSERT INTO courses(course_id, request_hash, revision, status) "
                    "VALUES (?, ?, 0, 'active')",
                    (course_id, request_hash),
                )
                for name, dose in channels.items():
                    cur.execute(
                        "INSERT INTO channels(course_id, channel, prescription, cumulative) "
                        "VALUES (?, ?, ?, '0')",
                        (course_id, name, canonical_decimal(dose)),
                    )
                cur.execute("COMMIT")
                return 201, self._course_body(course_id)
            except Exception:
                self._conn.rollback()
                raise

    def submit_delivery(self, course_id: str, delivery_id: str,
                        expected_revision: int, increments: dict):
        """Atomically accept or reject a delivery. Returns (http_status, body)."""
        request_hash = _hash_payload({
            "deliveryId": delivery_id,
            "expectedRevision": expected_revision,
            "increments": {k: canonical_decimal(v) for k, v in sorted(increments.items())},
        })
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                course = cur.execute(
                    "SELECT revision, status FROM courses WHERE course_id=?", (course_id,)
                ).fetchone()
                if course is None:
                    cur.execute("ROLLBACK")
                    return 404, {"error": "course_not_found",
                                 "message": f"course {course_id!r} does not exist"}
                revision, status = course

                prior = cur.execute(
                    "SELECT request_hash, response FROM deliveries "
                    "WHERE course_id=? AND delivery_id=?",
                    (course_id, delivery_id),
                ).fetchone()
                if prior is not None:
                    cur.execute("ROLLBACK")
                    if prior[0] == request_hash:
                        # Same id, same content: replay the first result.
                        return 200, json.loads(prior[1])
                    return 409, {
                        "error": "delivery_conflict",
                        "message": f"deliveryId {delivery_id!r} already used with different content",
                    }

                if status == "complete":
                    cur.execute("ROLLBACK")
                    return 409, {
                        "error": "course_complete",
                        "message": "course is complete; no further deliveries are accepted",
                    }
                if expected_revision != revision:
                    cur.execute("ROLLBACK")
                    return 409, {
                        "error": "stale_revision",
                        "message": f"expected revision {expected_revision} but current revision is {revision}",
                        "revision": revision,
                    }

                rows = cur.execute(
                    "SELECT channel, prescription, cumulative FROM channels WHERE course_id=?",
                    (course_id,),
                ).fetchall()
                prescription = {r[0]: Decimal(r[1]) for r in rows}
                cumulative = {r[0]: Decimal(r[2]) for r in rows}

                unknown = sorted(n for n in increments if n not in prescription)
                if unknown:
                    cur.execute("ROLLBACK")
                    return 409, {
                        "error": "unknown_channel",
                        "message": f"channels not defined for this course: {unknown}",
                        "channels": unknown,
                    }

                new_cumulative = dict(cumulative)
                for name, dose in increments.items():
                    new_cumulative[name] = cumulative[name] + dose
                exceeded = sorted(
                    n for n in increments if new_cumulative[n] > prescription[n]
                )
                if exceeded:
                    cur.execute("ROLLBACK")
                    return 409, {
                        "error": "prescription_exceeded",
                        "message": f"cumulative dose would exceed prescription for channels: {exceeded}",
                        "channels": exceeded,
                    }

                # All checks passed: apply the whole submission as one write.
                for name in increments:
                    cur.execute(
                        "UPDATE channels SET cumulative=? WHERE course_id=? AND channel=?",
                        (canonical_decimal(new_cumulative[name]), course_id, name),
                    )
                new_revision = revision + 1
                new_status = (
                    "complete"
                    if all(new_cumulative[n] == prescription[n] for n in prescription)
                    else "active"
                )
                cur.execute(
                    "UPDATE courses SET revision=?, status=? WHERE course_id=?",
                    (new_revision, new_status, course_id),
                )
                body = {
                    "courseId": course_id,
                    "deliveryId": delivery_id,
                    "revision": new_revision,
                    "status": new_status,
                    "cumulative": {
                        n: canonical_decimal(new_cumulative[n])
                        for n in sorted(new_cumulative)
                    },
                }
                cur.execute(
                    "INSERT INTO deliveries(course_id, delivery_id, request_hash, response) "
                    "VALUES (?, ?, ?, ?)",
                    (course_id, delivery_id, request_hash,
                     json.dumps(body, sort_keys=True)),
                )
                cur.execute("COMMIT")
                return 200, body
            except Exception:
                self._conn.rollback()
                raise


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

COURSE_RE = re.compile(r"^/api/courses/([^/]+)$")
DELIVERIES_RE = re.compile(r"^/api/courses/([^/]+)/deliveries$")


class Handler(BaseHTTPRequestHandler):
    store: CourseStore = None  # injected by run()
    server_version = "RTQC/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep container logs clean
        pass

    # -- helpers -----------------------------------------------------------

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, (400, {"error": "invalid_request", "message": "bad Content-Length"})
        if length > MAX_BODY_BYTES:
            return None, (413, {"error": "payload_too_large", "message": "request body too large"})
        raw = self.rfile.read(length) if length else b""
        try:
            return strict_loads(raw), None
        except ValueError as exc:
            return None, (400, {"error": "invalid_json", "message": str(exc)})

    @staticmethod
    def _valid_course_id(course_id: str) -> bool:
        return 0 < len(course_id) <= MAX_COURSE_ID_LEN

    # -- routes --------------------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        m = COURSE_RE.match(self.path)
        if m:
            course_id = unquote(m.group(1))
            status, body = self.store.get_course(course_id)
            return self._send(status, body)
        self._send(404, {"error": "not_found", "message": "unknown route"})

    def do_PUT(self):
        m = COURSE_RE.match(self.path)
        if not m:
            return self._send(404, {"error": "not_found", "message": "unknown route"})
        course_id = unquote(m.group(1))
        if not self._valid_course_id(course_id):
            return self._send(422, {"error": "invalid_course_id",
                                    "message": "course id must be 1-128 characters"})
        payload, err = self._read_json()
        if err:
            return self._send(*err)
        channels, error = validate_course_payload(payload)
        if error:
            return self._send(422, {"error": "invalid_course", "message": error})
        status, body = self.store.create_course(course_id, channels)
        self._send(status, body)

    def do_POST(self):
        m = DELIVERIES_RE.match(self.path)
        if not m:
            return self._send(404, {"error": "not_found", "message": "unknown route"})
        course_id = unquote(m.group(1))
        payload, err = self._read_json()
        if err:
            return self._send(*err)
        parsed, error = validate_delivery_payload(payload)
        if error:
            return self._send(422, {"error": "invalid_delivery", "message": error})
        delivery_id, expected, increments = parsed
        status, body = self.store.submit_delivery(
            course_id, delivery_id, expected, increments
        )
        self._send(status, body)


def run(port=None, db_path=None):
    port = int(port or os.environ.get("PORT", "8000"))
    db_path = db_path or os.environ.get("DB_PATH", "/data/app.db")
    Handler.store = CourseStore(db_path)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"rtqc-api listening on 0.0.0.0:{port}, db={db_path}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
