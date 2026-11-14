"""Optional managed services: Upstash Redis (cache, rate limits) and MongoDB Atlas (event log).

Both stay switched off unless their credentials are in the environment:

    UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
    MONGODB_URI, MONGODB_DB (optional, default "examflow")

Every call is best effort with a short timeout. A missing or unreachable service
must never break a request, so failures are swallowed and reported only through
``status()``.
"""
import json
import os
import time
import urllib.error
import urllib.request

REDIS_URL = (os.environ.get("UPSTASH_REDIS_REST_URL") or "").rstrip("/")
REDIS_TOKEN = (os.environ.get("UPSTASH_REDIS_REST_TOKEN") or "").strip()
REDIS_ENABLED = bool(REDIS_URL and REDIS_TOKEN)

MONGO_URI = (os.environ.get("MONGODB_URI") or "").strip()
MONGO_DB = (os.environ.get("MONGODB_DB") or "examflow").strip()
MONGO_ENABLED = bool(MONGO_URI)

TIMEOUT = 2.0
_mongo_client = None
_last_error = {"redis": None, "mongo": None}


# ------------------------------------------------------------------ redis
def _redis_command(*args):
    """Run one Redis command over the Upstash REST API."""
    if not REDIS_ENABLED:
        return None
    body = json.dumps([str(a) for a in args]).encode()
    request = urllib.request.Request(
        REDIS_URL, data=body, method="POST",
        headers={"Authorization": "Bearer " + REDIS_TOKEN,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode() or "{}")
        if payload.get("error"):
            _last_error["redis"] = payload["error"]
            return None
        return payload.get("result")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _last_error["redis"] = str(exc)[:200]
        return None


def cache_get(key):
    value = _redis_command("GET", key)
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def cache_set(key, value, ttl=60):
    try:
        payload = json.dumps(value, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return False
    return _redis_command("SET", key, payload, "EX", int(ttl)) is not None


def cache_delete(key):
    return _redis_command("DEL", key) is not None


def rate_limit(bucket, limit, window_seconds):
    """Fixed-window counter. Returns (allowed, remaining). Fails open."""
    if not REDIS_ENABLED:
        return True, limit
    key = "rl:" + bucket
    count = _redis_command("INCR", key)
    if count is None:
        return True, limit
    count = int(count)
    if count == 1:
        _redis_command("EXPIRE", key, window_seconds)
    return count <= limit, max(0, limit - count)


# ------------------------------------------------------------------ mongo
def _mongo_collection(name):
    global _mongo_client
    if not MONGO_ENABLED:
        return None
    try:
        if _mongo_client is None:
            import pymongo  # imported lazily so the app runs without the driver
            _mongo_client = pymongo.MongoClient(
                MONGO_URI, serverSelectionTimeoutMS=int(TIMEOUT * 1000),
                connectTimeoutMS=int(TIMEOUT * 1000), tz_aware=True)
        return _mongo_client[MONGO_DB][name]
    except Exception as exc:  # pragma: no cover - driver or DNS problems
        _last_error["mongo"] = str(exc)[:200]
        return None


def log_event(collection, document):
    """Append one document to a Mongo collection. Best effort."""
    coll = _mongo_collection(collection)
    if coll is None:
        return False
    payload = dict(document)
    payload.setdefault("recorded_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        coll.insert_one(payload)
        return True
    except Exception as exc:  # pragma: no cover
        _last_error["mongo"] = str(exc)[:200]
        return False


def recent_events(collection, limit=20):
    coll = _mongo_collection(collection)
    if coll is None:
        return []
    try:
        cursor = coll.find({}, {"_id": 0}).sort("recorded_at", -1).limit(int(limit))
        return list(cursor)
    except Exception as exc:  # pragma: no cover
        _last_error["mongo"] = str(exc)[:200]
        return []


# ------------------------------------------------------------------ status
def status():
    report = {"redis": {"enabled": REDIS_ENABLED, "reachable": False, "error": _last_error["redis"]},
              "mongo": {"enabled": MONGO_ENABLED, "database": MONGO_DB,
                        "reachable": False, "error": _last_error["mongo"]}}
    if REDIS_ENABLED:
        report["redis"]["reachable"] = _redis_command("PING") is not None
    if MONGO_ENABLED:
        try:
            coll = _mongo_collection("health")
            if coll is not None:
                coll.database.client.admin.command("ping")
                report["mongo"]["reachable"] = True
        except Exception as exc:  # pragma: no cover
            _last_error["mongo"] = str(exc)[:200]
            report["mongo"]["error"] = _last_error["mongo"]
    return report
