"""
GPR LINE Inbox adapter (Issue #2, JIHO part).

Reads LINE webhook events that the EXISTING Cloudflare Worker (gpr-line-webhook) stored in
Workers KV (namespace gpr-line-inbox, binding LINE_INBOX), normalises them into the central
GPR message JSON (schemas/gpr_inbox_message.schema.json) and writes them to a PRIVATE local
outbox for Bo. It never replies on LINE, never writes to KV, never creates a webhook.

Sources
  * file  - a JSON/JSONL file of synthetic or exported events (tests, dry runs)
  * kv    - Cloudflare KV REST API, READ-ONLY (token needs only "Workers KV Storage: Read")

Config comes only from environment variables (never from the repo or chat):
  CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN   # kv source only
  GPR_ID_SALT          # HMAC key for pseudonymous customer refs (required for --env prod)
  GPR_TEST_USER_IDS    # comma-separated LINE userIds whose events are always treated as test
  GPR_DATA_DIR         # private data dir, default ~/.gpr (outside the repo)

Usage
  python -m gpr.line_inbox pull --env test --source file --path tests/fixtures/line_webhook_synthetic.json
  python -m gpr.line_inbox pull --env prod --source kv [--prefix PREFIX] [--limit 200]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from gpr.pricing import load as load_pricing
from gpr.pricing import resolve_route

SCHEMA_VERSION = "1.0"
ENVS = ("test", "prod")
CF_API = "https://api.cloudflare.com/client/v4"


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    env: str
    data_dir: Path
    id_salt: bytes
    test_user_ids: frozenset[str]

    @classmethod
    def from_env(cls, env: str, environ: dict | None = None) -> "Config":
        e = os.environ if environ is None else environ
        if env not in ENVS:
            raise SystemExit(f"--env must be one of {ENVS}")
        salt = e.get("GPR_ID_SALT", "")
        if env == "prod" and len(salt) < 16:
            raise SystemExit("GPR_ID_SALT (>=16 chars) is required for --env prod")
        base = Path(e.get("GPR_DATA_DIR", Path.home() / ".gpr")).expanduser()
        ids = frozenset(x.strip() for x in e.get("GPR_TEST_USER_IDS", "").split(",") if x.strip())
        return cls(env, base, (salt or "gpr-test-only-salt").encode(), ids)

    def dir_for(self, env: str) -> Path:
        d = self.data_dir / env
        d.mkdir(parents=True, exist_ok=True)
        return d


# --------------------------------------------------------------------------- sources
@dataclass
class RawItem:
    key: str          # KV key or "file:<n>"
    payload: object   # parsed JSON value


def file_source(path: Path) -> Iterator[RawItem]:
    text = path.read_text(encoding="utf-8").strip()
    if path.suffix == ".jsonl":
        for i, line in enumerate(text.splitlines()):
            if line.strip():
                yield RawItem(f"file:{i}", json.loads(line))
    else:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
        for i, obj in enumerate(items):
            key = obj.get("_kv_key", f"file:{i}") if isinstance(obj, dict) else f"file:{i}"
            yield RawItem(key, obj)


class KVSource:
    """Read-only Cloudflare Workers KV client (list keys + get values)."""

    def __init__(self, environ: dict | None = None, opener=urllib.request.urlopen):
        e = os.environ if environ is None else environ
        try:
            self.account = e["CF_ACCOUNT_ID"]
            self.namespace = e["CF_KV_NAMESPACE_ID"]
            self._token = e["CF_API_TOKEN"]
        except KeyError as k:
            raise SystemExit(f"missing environment variable {k} for --source kv")
        self._open = opener

    def _get(self, path: str, params: dict | None = None) -> bytes:
        url = f"{CF_API}/accounts/{self.account}/storage/kv/namespaces/{self.namespace}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}"}, method="GET")
        with self._open(req, timeout=30) as r:
            return r.read()

    def keys(self, prefix: str = "", limit: int = 200) -> list[str]:
        out, cursor = [], None
        while len(out) < limit:
            body = json.loads(self._get("/keys", {"prefix": prefix, "cursor": cursor, "limit": min(1000, limit)}))
            if not body.get("success", False):
                raise RuntimeError(f"KV list failed: {body.get('errors')}")
            out += [k["name"] for k in body.get("result", [])]
            cursor = (body.get("result_info") or {}).get("cursor")
            if not cursor:
                break
        return out[:limit]

    def items(self, prefix: str = "", limit: int = 200) -> Iterator[RawItem]:
        for key in self.keys(prefix, limit):
            raw = self._get("/values/" + urllib.parse.quote(key, safe=""))
            try:
                yield RawItem(key, json.loads(raw))
            except json.JSONDecodeError:
                yield RawItem(key, {"_unparsed": True})


# --------------------------------------------------------------------------- normalise
def extract_events(payload: object) -> list[dict]:
    """Accept a LINE webhook body, a single event, or a wrapper {event: ...}/{body: ...}."""
    if isinstance(payload, list):
        return [e for p in payload for e in extract_events(p)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("events"), list):
        return [e for e in payload["events"] if isinstance(e, dict)]
    for wrap in ("event", "body", "payload", "data"):
        inner = payload.get(wrap)
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                continue
        if isinstance(inner, (dict, list)):
            found = extract_events(inner)
            if found:
                return found
    if "type" in payload and ("webhookEventId" in payload or "message" in payload):
        return [payload]
    return []


def _ref(prefix: str, raw: str | None, salt: bytes) -> str | None:
    if not raw:
        return None
    return f"{prefix}_" + hmac.new(salt, raw.encode(), hashlib.sha256).hexdigest()[:16]


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def dedup_key(event: dict) -> str:
    """Stable id: LINE webhookEventId (same on redelivery); fallback message id / hash."""
    if event.get("webhookEventId"):
        return "line:evt:" + event["webhookEventId"]
    mid = (event.get("message") or {}).get("id")
    if mid:
        return "line:msg:" + str(mid)
    blob = json.dumps(event, sort_keys=True, ensure_ascii=False).encode()
    return "line:sha:" + hashlib.sha256(blob).hexdigest()[:24]


def normalise(event: dict, kv_key: str, cfg: Config, pricing_data: dict | None = None) -> dict:
    src = event.get("source") or {}
    user_id = src.get("userId")
    msg = event.get("message") or {}
    mtype = msg.get("type") if event.get("type") == "message" else None
    review: list[str] = []

    env = cfg.env
    if user_id and user_id in cfg.test_user_ids:
        env = "test"
    if (event.get("mode") or "active") != "active":
        review.append("channel_standby_mode")

    message: dict = {"id": msg.get("id"), "type": mtype or event.get("type", "unknown")}
    pricing_hint = None
    if mtype == "text":
        message["text"] = msg.get("text", "")
        rid = resolve_route(message["text"], "", pricing_data or load_pricing())
        pricing_hint = {"route_id": rid, "source": "keyword_match_on_text", "note": "hint only; Bo extracts booking fields then runs gpr.pricing"}
    elif mtype == "image":
        cp = msg.get("contentProvider") or {}
        message["image"] = {
            "content_provider": cp.get("type", "line"),
            "fetch": "server_side_only",  # content via LINE API with channel token; never in repo
            "image_set": (msg.get("imageSet") or {}).get("id"),
        }
        review.append("image_needs_human_check")  # e.g. payment slip = evidence only, not confirmation
    elif event.get("type") == "message":
        review.append(f"unsupported_message_type:{mtype}")
    else:
        review.append(f"non_message_event:{event.get('type')}")

    if src.get("type") in ("group", "room"):
        review.append("group_or_room_source")
    if not user_id:
        review.append("unknown_identity")

    return {
        "schema_version": SCHEMA_VERSION,
        "env": env,
        "channel": "line",
        "event_id": event.get("webhookEventId"),
        "dedup_key": dedup_key(event),
        "is_redelivery": bool((event.get("deliveryContext") or {}).get("isRedelivery", False)),
        "occurred_at": _iso(event.get("timestamp")),
        "source": {
            "type": src.get("type"),
            "customer_ref": _ref("cust", user_id, cfg.id_salt),
            "group_ref": _ref("grp", src.get("groupId") or src.get("roomId"), cfg.id_salt),
        },
        "message": message,
        "pricing_hint": pricing_hint,
        "needs_review": review,
        "reply_allowed": False,       # this pipeline never replies; Gift sends manually
        "booking_confirmed": False,   # only Gift can confirm
        "payment_confirmed": False,   # only Gift can confirm
        "kv_key": kv_key,
    }


# --------------------------------------------------------------------------- dedup store
class Store:
    """Per-env SQLite set of processed dedup keys + JSONL outbox (both outside the repo)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._db: dict[str, sqlite3.Connection] = {}

    def _conn(self, env: str) -> sqlite3.Connection:
        if env not in self._db:
            c = sqlite3.connect(self.cfg.dir_for(env) / "state.sqlite")
            c.execute("CREATE TABLE IF NOT EXISTS processed (dedup_key TEXT PRIMARY KEY, kv_key TEXT, first_seen TEXT)")
            self._db[env] = c
        return self._db[env]

    def accept(self, rec: dict) -> bool:
        """Write rec to its env outbox once. Returns False for duplicates."""
        env = rec["env"]
        c = self._conn(env)
        with c:
            cur = c.execute(
                "INSERT OR IGNORE INTO processed VALUES (?,?,?)",
                (rec["dedup_key"], rec["kv_key"], datetime.now(timezone.utc).isoformat()),
            )
            if cur.rowcount == 0:
                return False
            with (self.cfg.dir_for(env) / "outbox.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True


# --------------------------------------------------------------------------- pipeline
def pull(items: Iterable[RawItem], cfg: Config) -> dict:
    store, pricing_data = Store(cfg), load_pricing()
    stats = {"read": 0, "new": 0, "duplicate": 0, "unparsed": 0, "by_env": {"test": 0, "prod": 0}}
    for item in items:
        events = extract_events(item.payload)
        if not events:
            stats["unparsed"] += 1
            continue
        for ev in events:
            stats["read"] += 1
            rec = normalise(ev, item.key, cfg, pricing_data)
            if store.accept(rec):
                stats["new"] += 1
                stats["by_env"][rec["env"]] += 1
            else:
                stats["duplicate"] += 1
    return stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="gpr.line_inbox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="read inbox -> private outbox (read-only, never replies)")
    p.add_argument("--env", required=True, choices=ENVS)
    p.add_argument("--source", required=True, choices=("file", "kv"))
    p.add_argument("--path", type=Path)
    p.add_argument("--prefix", default="")
    p.add_argument("--limit", type=int, default=200)
    a = ap.parse_args(argv)

    cfg = Config.from_env(a.env)
    if a.source == "file":
        if not a.path:
            ap.error("--path is required for --source file")
        items = file_source(a.path)
    else:
        if a.env == "test" and not a.prefix:
            ap.error("--env test with --source kv needs --prefix (keep test keys separate)")
        items = KVSource().items(a.prefix, a.limit)
    stats = pull(items, cfg)
    print(json.dumps({"env": a.env, "outbox_dir": str(cfg.data_dir), **stats}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
