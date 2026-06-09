"""
Flask portal for the LinkedIn Job Automation dashboard.
Run with: python portal/app.py
"""
import sys
from pathlib import Path

# Allow importing from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_cors import CORS

from bot.database import (
    init_db, get_jobs, get_stats, update_job_status, get_job,
)

app = Flask(__name__)
CORS(app)

VALID_STATUSES = ["discovered", "applied", "needs_manual", "manually_applied", "skipped", "failed"]


@app.before_request
def ensure_db():
    init_db()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    stats = get_stats()
    return render_template("index.html", stats=stats)


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/api/jobs")
def api_jobs():
    status = request.args.get("status")
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    jobs = get_jobs(status=status, limit=limit, offset=offset)
    return jsonify(jobs)


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)


@app.route("/api/jobs/<job_id>/status", methods=["POST"])
def api_update_status(job_id):
    data = request.get_json()
    new_status = data.get("status")
    notes = data.get("notes")
    if new_status not in VALID_STATUSES:
        return jsonify({"error": f"Invalid status. Must be one of: {VALID_STATUSES}"}), 400
    update_job_status(job_id, new_status, notes)
    return jsonify({"ok": True, "status": new_status})


if __name__ == "__main__":
    init_db()
    print("\n🚀  Portal running at http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
