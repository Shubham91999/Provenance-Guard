"""SQLite-backed storage: content status + append-only audit log.

Two tables (planning.md §2, §6):
  - content:    current state of each submission (used for status lookups/appeals)
  - audit_log:  append-only event log (classification + appeal events)

Timestamps are UTC ISO-8601 strings.
"""

import json
import sqlite3
from datetime import datetime, timezone

import config


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content (
                content_id          TEXT PRIMARY KEY,
                creator_id          TEXT,
                text                TEXT,
                attribution         TEXT,
                confidence          REAL,
                llm_score           REAL,
                stylometric_score   REAL,
                status              TEXT,
                created_at          TEXT,
                appeal_reasoning    TEXT,
                appealed_at         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp           TEXT,
                event_type          TEXT,   -- 'classification' | 'appeal'
                content_id          TEXT,
                creator_id          TEXT,
                attribution         TEXT,
                confidence          REAL,
                llm_score           REAL,
                stylometric_score   REAL,
                status              TEXT,
                appeal_reasoning    TEXT,
                details             TEXT     -- JSON blob for extra context
            )
            """
        )
        # STRETCH S2: Verified-Human certificates.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS certificates (
                cert_id     TEXT PRIMARY KEY,
                creator_id  TEXT UNIQUE,
                issued_at   TEXT,
                method      TEXT,
                status      TEXT
            )
            """
        )


def record_classification(
    content_id,
    creator_id,
    text,
    attribution,
    confidence,
    llm_score,
    stylometric_score,
    details,
):
    """Insert a new content row and a classification event in the audit log."""
    ts = _now()
    status = "classified"
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO content (content_id, creator_id, text, attribution,
                confidence, llm_score, stylometric_score, status, created_at,
                appeal_reasoning, appealed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (content_id, creator_id, text, attribution, confidence, llm_score,
             stylometric_score, status, ts),
        )
        conn.execute(
            """
            INSERT INTO audit_log (timestamp, event_type, content_id, creator_id,
                attribution, confidence, llm_score, stylometric_score, status,
                appeal_reasoning, details)
            VALUES (?, 'classification', ?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (ts, content_id, creator_id, attribution, confidence, llm_score,
             stylometric_score, status, json.dumps(details)),
        )
    return ts


def get_content(content_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM content WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def record_appeal(content_id, creator_reasoning):
    """Flip status to under_review and log the appeal alongside the original decision.

    Returns the updated content dict, or None if the content_id is unknown.
    """
    content = get_content(content_id)
    if content is None:
        return None

    ts = _now()
    new_status = "under_review"
    with _connect() as conn:
        conn.execute(
            """
            UPDATE content
               SET status = ?, appeal_reasoning = ?, appealed_at = ?
             WHERE content_id = ?
            """,
            (new_status, creator_reasoning, ts, content_id),
        )
        # Append the appeal event alongside the original classification data.
        conn.execute(
            """
            INSERT INTO audit_log (timestamp, event_type, content_id, creator_id,
                attribution, confidence, llm_score, stylometric_score, status,
                appeal_reasoning, details)
            VALUES (?, 'appeal', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, content_id, content["creator_id"], content["attribution"],
             content["confidence"], content["llm_score"],
             content["stylometric_score"], new_status, creator_reasoning,
             json.dumps({"original_status": content["status"]})),
        )
    return get_content(content_id)


# --------------------------------------------------------------------------- #
# STRETCH S2 — Verified-Human certificates
# --------------------------------------------------------------------------- #

def issue_certificate(cert_id, creator_id, method):
    """Issue (or refresh) a Verified-Human certificate for a creator."""
    ts = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO certificates (cert_id, creator_id, issued_at, method, status)
            VALUES (?, ?, ?, ?, 'active')
            ON CONFLICT(creator_id) DO UPDATE SET
                cert_id=excluded.cert_id,
                issued_at=excluded.issued_at,
                method=excluded.method,
                status='active'
            """,
            (cert_id, creator_id, ts, method),
        )
    return get_certificate(creator_id)


def get_certificate(creator_id):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM certificates WHERE creator_id = ? AND status = 'active'",
            (creator_id,),
        ).fetchone()
    return dict(row) if row else None


def is_verified_human(creator_id):
    return get_certificate(creator_id) is not None


# --------------------------------------------------------------------------- #
# STRETCH S3 — Analytics
# --------------------------------------------------------------------------- #

def get_analytics():
    """Aggregate metrics from the content store + audit log (planning.md §S3)."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM content").fetchall()
        appeal_count = conn.execute(
            "SELECT COUNT(*) AS c FROM audit_log WHERE event_type = 'appeal'"
        ).fetchone()["c"]
        verified_creators = conn.execute(
            "SELECT COUNT(*) AS c FROM certificates WHERE status = 'active'"
        ).fetchone()["c"]

    contents = [dict(r) for r in rows]
    total = len(contents)

    by_attr = {"likely_ai": 0, "uncertain": 0, "likely_human": 0}
    conf_by_attr = {"likely_ai": [], "uncertain": [], "likely_human": []}
    degraded = 0
    all_conf = []
    for c in contents:
        attr = c["attribution"]
        by_attr[attr] = by_attr.get(attr, 0) + 1
        conf_by_attr.setdefault(attr, []).append(c["confidence"])
        all_conf.append(c["confidence"])
        if c["llm_score"] is None:
            degraded += 1

    def _avg(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    def _pct(n):
        return round(100 * n / total, 1) if total else 0.0

    return {
        "total_classifications": total,
        "detection_patterns": {
            attr: {"count": by_attr[attr], "percent": _pct(by_attr[attr])}
            for attr in by_attr
        },
        "appeals": {
            "count": appeal_count,
            "appeal_rate_percent": _pct(appeal_count),
        },
        "average_confidence": {
            "overall": _avg(all_conf),
            "by_attribution": {a: _avg(v) for a, v in conf_by_attr.items()},
        },
        "degraded_rate_percent": _pct(degraded),
        "verified_human_creators": verified_creators,
    }


def get_log(limit=50, status=None):
    """Return the most recent audit-log entries as a list of dicts."""
    query = "SELECT * FROM audit_log"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    entries = []
    for row in rows:
        entry = dict(row)
        if entry.get("details"):
            try:
                entry["details"] = json.loads(entry["details"])
            except (ValueError, TypeError):
                pass
        entries.append(entry)
    return entries
