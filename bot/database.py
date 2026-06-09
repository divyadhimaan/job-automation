"""
Database layer — MongoDB via pymongo.

Drop-in replacement for the SQLite version: same function signatures,
same return shapes (dicts with string/int keys, no ObjectId leakage).

Connection string is read from (in priority order):
  1. MONGODB_URI environment variable
  2. config.yaml  →  mongodb_uri key
  3. Fallback: mongodb://localhost:27017/jobbot
"""
import os
import json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId


# ── Connection ─────────────────────────────────────────────────────────────

def _get_uri() -> str:
    if os.environ.get("MONGODB_URI"):
        return os.environ["MONGODB_URI"]
    try:
        import yaml
        cfg_path = Path(__file__).parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        uri = cfg.get("mongodb_uri") or cfg.get("database", {}).get("uri")
        if uri:
            return uri
    except Exception:
        pass
    return "mongodb://localhost:27017/jobbot"


def _db():
    uri = _get_uri()
    try:
        import certifi
        client = MongoClient(uri, serverSelectionTimeoutMS=5000, tlsCAFile=certifi.where())
    except ImportError:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db_name = uri.rstrip("/").split("/")[-1].split("?")[0] or "jobbot"
    return client[db_name]


# ── Bootstrap ──────────────────────────────────────────────────────────────

def init_db():
    """Create indexes. Safe to call multiple times."""
    db = _db()
    db.jobs.create_index("linkedin_id", unique=True)
    db.jobs.create_index("status")
    db.jobs.create_index("company")
    db.jobs.create_index([("created_at", DESCENDING)])


# ── Helpers ────────────────────────────────────────────────────────────────

def _clean(doc: dict) -> dict:
    """Convert ObjectId → string id and remove internal _id."""
    if doc is None:
        return None
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Jobs ───────────────────────────────────────────────────────────────────

def upsert_job(job: dict) -> str:
    """Insert or update a job. Returns the string _id."""
    db = _db()
    now = _now()

    # Fields that should never be overwritten once set
    preserve_if_set = ("applied_at",)

    doc = {
        "linkedin_id":    job["linkedin_id"],
        "title":          job["title"],
        "company":        job["company"],
        "location":       job.get("location", ""),
        "job_type":       job.get("job_type", ""),
        "workplace_type": job.get("workplace_type", ""),
        "linkedin_url":   job.get("linkedin_url", ""),
        "external_url":   job.get("external_url"),
        "apply_method":   job["apply_method"],
        "status":         job.get("status", "discovered"),
        "description":    job.get("description"),
        "salary":         job.get("salary"),
        "posted_at":      job.get("posted_at"),
        "applied_at":     job.get("applied_at"),
        "notes":          job.get("notes"),
        "updated_at":     now,
    }

    existing = db.jobs.find_one({"linkedin_id": job["linkedin_id"]})
    if existing:
        # Never overwrite terminal statuses
        if existing.get("status") in ("applied", "manually_applied", "skipped"):
            doc["status"] = existing["status"]
        # Never overwrite applied_at once set
        if existing.get("applied_at"):
            doc["applied_at"] = existing["applied_at"]
        db.jobs.update_one({"linkedin_id": job["linkedin_id"]}, {"$set": doc})
        return str(existing["_id"])
    else:
        doc["created_at"] = now
        result = db.jobs.insert_one(doc)
        return str(result.inserted_id)


def update_job_status(job_id: str, status: str, notes: str = None):
    db = _db()
    now = _now()
    update = {"status": status, "updated_at": now}
    if status in ("applied", "manually_applied"):
        update["applied_at"] = now
    if notes:
        update["notes"] = notes
    db.jobs.update_one({"_id": ObjectId(job_id)}, {"$set": update})


def get_job(job_id: str) -> dict | None:
    db = _db()
    doc = db.jobs.find_one({"_id": ObjectId(job_id)})
    return _clean(doc) if doc else None


def get_jobs(status: str = None, limit: int = 200, offset: int = 0) -> list[dict]:
    db = _db()
    query = {"status": status} if status else {}
    cursor = db.jobs.find(query).sort("created_at", DESCENDING).skip(offset).limit(limit)
    return [_clean(d) for d in cursor]


def get_stats() -> dict:
    db = _db()
    pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    stats = {r["_id"]: r["count"] for r in db.jobs.aggregate(pipeline)}
    stats["total"] = sum(stats.values())
    last_run = db.runs.find_one({}, sort=[("started_at", DESCENDING)])
    stats["last_run"] = _clean(last_run) if last_run else None
    return stats


def job_exists(linkedin_id: str) -> bool:
    return _db().jobs.count_documents({"linkedin_id": linkedin_id}, limit=1) > 0


# ── Runs ───────────────────────────────────────────────────────────────────

def start_run() -> str:
    db = _db()
    result = db.runs.insert_one({"started_at": _now()})
    return str(result.inserted_id)


def finish_run(run_id: str, discovered: int, applied: int, external: int, failed: int, error_log: str = None):
    db = _db()
    db.runs.update_one({"_id": ObjectId(run_id)}, {"$set": {
        "finished_at":     _now(),
        "jobs_discovered": discovered,
        "jobs_applied":    applied,
        "jobs_external":   external,
        "jobs_failed":     failed,
        "error_log":       error_log,
    }})
