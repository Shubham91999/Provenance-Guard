"""Provenance Guard — Flask API.

Core endpoints (planning.md §2, §6):
  POST /submit            classify text (or image metadata) -> attribution, confidence, label
  POST /appeal            contest a classification -> status becomes under_review
  GET  /log               recent audit-log entries (documentation/grading visibility)
  GET  /health            liveness check

Stretch endpoints:
  POST /verify-human          earn a Verified-Human certificate (S2)
  GET  /certificate/<creator> check a creator's certificate (S2)
  GET  /analytics             platform metrics as JSON (S3)
  GET  /dashboard             minimal HTML analytics view (S3)
"""

import uuid

from flask import Flask, jsonify, render_template_string, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import config
import detection
import scoring
import storage

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

storage.init_db()

# The exact attestation a creator must sign to earn a Verified-Human cert (S2).
ATTESTATION_TEXT = (
    "I certify this account's work is my own original human writing."
)
MIN_SAMPLE_WORDS = 20


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "llm_configured": bool(config.GROQ_API_KEY)})


@app.route("/submit", methods=["POST"])
@limiter.limit(config.RATE_LIMIT)
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = data.get("creator_id")
    content_type = (data.get("content_type") or "text").strip()
    metadata = data.get("metadata")  # only used for image_metadata (S4)

    if not text:
        return jsonify({"error": "Field 'text' is required and must be non-empty."}), 400
    if not creator_id:
        return jsonify({"error": "Field 'creator_id' is required."}), 400
    if content_type not in ("text", "image_metadata"):
        return jsonify({
            "error": "content_type must be 'text' or 'image_metadata'."
        }), 400

    content_id = str(uuid.uuid4())

    # --- Three ensemble signals over the text/caption (S1) ---
    llm_score, llm_detail = detection.llm_signal(text)              # semantic
    stylometric_score, stylo_detail = detection.stylometric_signal(text)  # structural
    repetition_score, rep_detail = detection.repetition_signal(text)      # redundancy
    word_count = stylo_detail["word_count"]

    result = scoring.combine_scores(
        llm_score, stylometric_score, word_count, repetition_score
    )
    confidence = result["confidence"]
    attribution = result["attribution"]

    # --- Multi-modal: metadata provenance overrides soft inference (S4) ---
    metadata_check = None
    if content_type == "image_metadata":
        metadata_check = detection.metadata_provenance_check(metadata)
        if metadata_check["declared_ai"]:
            attribution = config.LIKELY_AI
            confidence = 0.99
            result["reason"] = (
                "image metadata explicitly declares AI generation -> "
                "authoritative override to likely_ai"
            )

    # --- Verified-Human badge (S2) ---
    verified_human = storage.is_verified_human(creator_id)

    # --- Transparency label ---
    label = scoring.generate_label(attribution, confidence, verified_human)
    if content_type == "image_metadata" and metadata_check and metadata_check["declared_ai"]:
        label["provenance_note"] = (
            "This verdict is based on the image's own metadata, which declares "
            "AI generation."
        )

    # --- Audit log ---
    details = {
        "content_type": content_type,
        "signals": {
            "llm": llm_detail,
            "stylometric": stylo_detail,
            "repetition": rep_detail,
        },
        "votes": result["votes"],
        "scoring": {
            "degraded": result["degraded"],
            "forced_uncertain": result["forced_uncertain"],
            "reason": result["reason"],
        },
        "verified_human": verified_human,
    }
    if metadata_check is not None:
        details["metadata_check"] = metadata_check

    storage.record_classification(
        content_id=content_id,
        creator_id=creator_id,
        text=text,
        attribution=attribution,
        confidence=confidence,
        llm_score=llm_score,
        stylometric_score=round(stylometric_score, 3),
        details=details,
    )

    return jsonify({
        "content_id": content_id,
        "content_type": content_type,
        "attribution": attribution,
        "confidence": confidence,
        "signals": {
            "llm_score": llm_score,
            "stylometric_score": round(stylometric_score, 3),
            "repetition_score": round(repetition_score, 3),
        },
        "votes": result["votes"],
        "label": label,
        "metadata_check": metadata_check,
        "notes": {
            "degraded": result["degraded"],
            "forced_uncertain": result["forced_uncertain"],
            "reason": result["reason"],
            "verified_human": verified_human,
        },
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    creator_reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "Field 'content_id' is required."}), 400
    if not creator_reasoning:
        return jsonify({"error": "Field 'creator_reasoning' is required."}), 400

    updated = storage.record_appeal(content_id, creator_reasoning)
    if updated is None:
        return jsonify({"error": f"Unknown content_id: {content_id}"}), 404

    return jsonify({
        "content_id": content_id,
        "status": updated["status"],
        "message": (
            "Appeal received. This content is now under review by a human "
            "moderator. The original classification and your reasoning have "
            "been logged."
        ),
        "original_attribution": updated["attribution"],
        "original_confidence": updated["confidence"],
    })


@app.route("/log", methods=["GET"])
def log():
    limit = request.args.get("limit", default=50, type=int)
    status = request.args.get("status", default=None, type=str)
    return jsonify({"entries": storage.get_log(limit=limit, status=status)})


# --------------------------------------------------------------------------- #
# STRETCH S2 — Verified-Human certificate
# --------------------------------------------------------------------------- #

@app.route("/verify-human", methods=["POST"])
def verify_human():
    data = request.get_json(silent=True) or {}
    creator_id = data.get("creator_id")
    attestation = (data.get("attestation") or "").strip()
    sample = (data.get("writing_sample") or "").strip()

    if not creator_id:
        return jsonify({"error": "Field 'creator_id' is required."}), 400
    if attestation != ATTESTATION_TEXT:
        return jsonify({
            "error": "Attestation text does not match.",
            "required_attestation": ATTESTATION_TEXT,
        }), 400
    if len(sample.split()) < MIN_SAMPLE_WORDS:
        return jsonify({
            "error": f"writing_sample must be at least {MIN_SAMPLE_WORDS} words."
        }), 400

    cert_id = str(uuid.uuid4())
    cert = storage.issue_certificate(cert_id, creator_id, method="attestation+sample")
    return jsonify({
        "message": "Verified-Human certificate issued.",
        "certificate": cert,
        "badge": {"text": "✔ Verified Human Creator"},
    })


@app.route("/certificate/<creator_id>", methods=["GET"])
def certificate(creator_id):
    cert = storage.get_certificate(creator_id)
    if cert is None:
        return jsonify({"verified_human": False, "creator_id": creator_id}), 404
    return jsonify({"verified_human": True, "certificate": cert})


# --------------------------------------------------------------------------- #
# STRETCH S3 — Analytics dashboard
# --------------------------------------------------------------------------- #

@app.route("/analytics", methods=["GET"])
def analytics():
    return jsonify(storage.get_analytics())


_DASHBOARD_HTML = """
<!doctype html><html><head><title>Provenance Guard — Analytics</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;color:#222}
 h1{font-size:1.4rem} .card{border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0}
 .big{font-size:2rem;font-weight:700} .row{display:flex;gap:16px;flex-wrap:wrap}
 .row .card{flex:1;min-width:160px} .muted{color:#777;font-size:.85rem}
 table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:4px 8px;border-bottom:1px solid #eee}
</style></head><body>
<h1>Provenance Guard — Analytics</h1>
<div class="row">
  <div class="card"><div class="muted">Total classifications</div><div class="big">{{ a.total_classifications }}</div></div>
  <div class="card"><div class="muted">Appeal rate</div><div class="big">{{ a.appeals.appeal_rate_percent }}%</div><div class="muted">{{ a.appeals.count }} appeals</div></div>
  <div class="card"><div class="muted">Avg confidence</div><div class="big">{{ a.average_confidence.overall }}</div></div>
  <div class="card"><div class="muted">Verified humans</div><div class="big">{{ a.verified_human_creators }}</div></div>
</div>
<div class="card">
  <h3>Detection patterns</h3>
  <table><tr><th>Attribution</th><th>Count</th><th>%</th><th>Avg confidence</th></tr>
  {% for attr, d in a.detection_patterns.items() %}
    <tr><td>{{ attr }}</td><td>{{ d.count }}</td><td>{{ d.percent }}%</td>
    <td>{{ a.average_confidence.by_attribution[attr] }}</td></tr>
  {% endfor %}
  </table>
  <div class="muted">Degraded (no-LLM) rate: {{ a.degraded_rate_percent }}%</div>
</div>
</body></html>
"""


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template_string(_DASHBOARD_HTML, a=storage.get_analytics())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
