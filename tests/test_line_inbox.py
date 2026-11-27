"""Issue #2 synthetic tests: adapter, dedup, test/prod isolation, schema, pricing, rich menu."""
import json
from pathlib import Path

import pytest

from gpr.handoff import price_booking
from gpr.line_inbox import Config, KVSource, extract_events, file_source, normalise, pull
from gpr.richmenu import validate

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "line_webhook_synthetic.json"
SCHEMA = json.loads((ROOT / "schemas" / "gpr_inbox_message.schema.json").read_text(encoding="utf-8"))
U1, U3 = "U00000000000000000000000000000001", "U00000000000000000000000000000003"


def cfg(tmp_path, env="test", **extra):
    e = {"GPR_DATA_DIR": str(tmp_path), "GPR_ID_SALT": "unit-test-salt-0123456789", **extra}
    return Config.from_env(env, e)


def outbox(tmp_path, env):
    p = tmp_path / env / "outbox.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_extract_all_wrapper_shapes():
    items = list(file_source(FIX))
    counts = [len(extract_events(i.payload)) for i in items]
    assert counts == [1, 1, 1, 1, 2, 0]  # body, body, {event}, {body: str}, 2 events, junk


def test_text_and_image_normalised(tmp_path):
    c = cfg(tmp_path)
    ev_text, ev_img = (extract_events(i.payload)[0] for i in list(file_source(FIX))[:2])
    t, im = normalise(ev_text, "k1", c), normalise(ev_img, "k2", c)
    assert t["message"]["type"] == "text" and "素万那普" in t["message"]["text"]
    assert t["pricing_hint"]["route_id"] == "BKK-BANGKOK"
    assert im["message"]["image"]["fetch"] == "server_side_only"
    assert "image_needs_human_check" in im["needs_review"]
    for r in (t, im):
        assert r["reply_allowed"] is False and r["booking_confirmed"] is False and r["payment_confirmed"] is False
        assert U1 not in json.dumps(r) and "replyToken" not in json.dumps(r)  # no raw ids / reply token


def test_dedup_redelivery_and_rerun(tmp_path):
    c = cfg(tmp_path)
    s1 = pull(file_source(FIX), c)
    assert s1 == {"read": 6, "new": 5, "duplicate": 1, "unparsed": 1, "skipped_non_test": 0, "by_env": {"test": 5, "prod": 0}}
    s2 = pull(file_source(FIX), c)  # same data again -> nothing new
    assert s2["new"] == 0 and s2["duplicate"] == 6
    keys = [r["dedup_key"] for r in outbox(tmp_path, "test")]
    assert len(keys) == len(set(keys)) == 5


def test_test_prod_isolation(tmp_path):
    c = cfg(tmp_path, env="prod", GPR_TEST_USER_IDS=U3)
    s = pull(file_source(FIX), c)
    assert s["by_env"] == {"test": 1, "prod": 4}
    assert all(r["env"] == "test" for r in outbox(tmp_path, "test"))
    assert all(r["env"] == "prod" for r in outbox(tmp_path, "prod"))
    assert (tmp_path / "test" / "state.sqlite").exists() and (tmp_path / "prod" / "state.sqlite").exists()


def test_prod_requires_salt(tmp_path):
    with pytest.raises(SystemExit):
        Config.from_env("prod", {"GPR_DATA_DIR": str(tmp_path)})


def test_unknown_identity_and_unsupported_flagged(tmp_path):
    pull(file_source(FIX), cfg(tmp_path))
    recs = {r["event_id"]: r for r in outbox(tmp_path, "test")}
    assert "non_message_event:follow" in recs["01TESTEVENT0000000000000004"]["needs_review"]
    sticker = recs["01TESTEVENT0000000000000005"]
    assert "unknown_identity" in sticker["needs_review"] and sticker["source"]["customer_ref"] is None


def test_customer_ref_stable_and_salted(tmp_path):
    ev = extract_events(next(file_source(FIX)).payload)[0]
    a = normalise(ev, "k", cfg(tmp_path))["source"]["customer_ref"]
    b = normalise(ev, "k", cfg(tmp_path))["source"]["customer_ref"]
    other = normalise(ev, "k", Config.from_env("test", {"GPR_DATA_DIR": str(tmp_path), "GPR_ID_SALT": "another-salt-xxxxxxxx"}))
    assert a == b != other["source"]["customer_ref"]


def _check_schema(rec, schema):
    for k in schema["required"]:
        assert k in rec, k
    assert set(rec) <= set(schema["properties"]), set(rec) - set(schema["properties"])
    assert rec["env"] in schema["properties"]["env"]["enum"]


def test_records_match_schema(tmp_path):
    pull(file_source(FIX), cfg(tmp_path))
    for r in outbox(tmp_path, "test"):
        _check_schema(r, SCHEMA)


def test_handoff_uses_existing_pricing(tmp_path):
    rec = normalise(extract_events(next(file_source(FIX)).payload)[0], "k", cfg(tmp_path))
    booking = {"from": "素万那普机场", "to": "素坤逸", "time": "23:40", "adults": 2, "children": 1,
               "bags": {"sizes_inch": [28, 24]}}
    out = price_booking(rec, booking)
    assert out["route_id"] == "BKK-BANGKOK" and out["quote"]["total_min"] == 900
    assert out["group"] == "ทั่วไป" and out["reply_allowed"] is False
    disc = price_booking(rec, {"from": "曼谷", "to": "芭提雅", "time": "10:00", "adults": 2,
                               "bags": {"sizes_inch": [24]}, "discount_requested": True})
    assert disc["group"] == "ส่งให้ Gift"


def test_kv_source_is_read_only_and_paginates():
    calls = []

    class Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.body

    pages = [{"success": True, "result": [{"name": "a"}], "result_info": {"cursor": "c2"}},
             {"success": True, "result": [{"name": "b"}], "result_info": {"cursor": ""}}]

    def opener(req, timeout):
        calls.append((req.get_method(), req.full_url))
        if "/keys" in req.full_url:
            return Resp(json.dumps(pages.pop(0)).encode())
        return Resp(json.dumps({"events": []}).encode())

    src = KVSource({"CF_ACCOUNT_ID": "acc", "CF_KV_NAMESPACE_ID": "ns", "CF_API_TOKEN": "t"}, opener=opener)
    items = list(src.items(prefix="", limit=10))
    assert [i.key for i in items] == ["a", "b"]
    assert all(m == "GET" for m, _ in calls)


def test_richmenu_draft_valid():
    menu = json.loads((ROOT / "line" / "richmenu" / "richmenu.json").read_text(encoding="utf-8"))
    assert validate(menu) == []
    bad = json.loads(json.dumps(menu))
    bad["areas"][1]["bounds"]["x"] = 800  # overlap
    assert any("overlap" in e for e in validate(bad))


def _text_event(user, text, eid):
    return {"type": "message", "mode": "active", "timestamp": 1764230400000, "webhookEventId": eid,
            "source": {"type": "user", "userId": user}, "message": {"id": eid, "type": "text", "text": text}}


ADMIN = "U0000000000000000000000000000000A"


def test_admin_command_only_from_admin(tmp_path):
    c = cfg(tmp_path, GPR_ADMIN_USER_IDS=ADMIN)
    ok = normalise(_text_event(ADMIN, "#GPR คำนวณราคา", "e1"), "k", c)
    assert ok["admin_command"] == "calculate_price" and ok["pricing_hint"] is None
    pay = normalise(_text_event(ADMIN, "#GPR รับเงิน", "e2"), "k", c)
    assert pay["admin_command"] == "payment_review" and pay["payment_confirmed"] is False
    spoof = normalise(_text_event(U1, "#GPR สรุปงาน", "e3"), "k", c)
    assert spoof["admin_command"] is None and "admin_command_from_non_admin_or_unknown" in spoof["needs_review"]
    normal = normalise(_text_event(U1, "ไปดอนเมืองเท่าไหร่", "e4"), "k", c)
    assert normal["admin_command"] is None and normal["pricing_hint"]["route_id"] == "DMK-BANGKOK"


def test_richmenu_commands_match_adapter():
    from gpr.line_inbox import ADMIN_COMMANDS, ADMIN_PREFIX
    menu = json.loads((ROOT / "line" / "richmenu" / "richmenu.json").read_text(encoding="utf-8"))
    for a in menu["areas"]:
        t = a["action"]["text"]
        assert t.startswith(ADMIN_PREFIX) and t[len(ADMIN_PREFIX):].strip() in ADMIN_COMMANDS
    assert menu["name"].startswith("gpr-admin")


# ---------------------------------------------------------------- real worker.js KV layout
KVFIX = ROOT / "tests" / "fixtures" / "line_kv_worker_synthetic.json"


def test_worker_record_shape_parsed(tmp_path):
    c = cfg(tmp_path, GPR_ADMIN_USER_IDS=ADMIN)
    s = pull(file_source(KVFIX), c)
    assert s["unparsed"] == 0 and s["new"] == 4
    recs = {r["kv_key"]: r for r in outbox(tmp_path, "test")}
    t = recs["event:01TESTKV00000000000000001"]
    assert t["dedup_key"] == "line:evt:01TESTKV00000000000000001"
    assert t["message"]["type"] == "text" and t["pricing_hint"]["route_id"] == "DMK-BANGKOK"
    assert recs["event:01TESTKV00000000000000002"]["message"]["image"]["content_provider"] == "line"
    assert recs["event:01TESTKV00000000000000003"]["admin_command"] == "daily_summary"
    g = recs["event:01TESTKV00000000000000004"]
    assert "group_or_room_source" in g["needs_review"] and g["source"]["group_ref"].startswith("grp_")
    for r in recs.values():
        _check_schema(r, SCHEMA)


def test_kv_key_maps_to_same_dedup_as_record(tmp_path):
    from gpr.line_inbox import kv_key_to_dedup
    pull(file_source(KVFIX), cfg(tmp_path))
    for r in outbox(tmp_path, "test"):
        assert kv_key_to_dedup(r["kv_key"]) == r["dedup_key"]
    assert kv_key_to_dedup("other:1") is None


def test_test_mode_keeps_only_test_users(tmp_path):
    c = cfg(tmp_path, GPR_TEST_USER_IDS=U1)
    s = pull(file_source(KVFIX), c, only_test_users=True)
    assert s["new"] == 1 and s["skipped_non_test"] == 3
    assert not (tmp_path / "prod" / "outbox.jsonl").exists()


def test_kv_skip_avoids_value_reads(tmp_path):
    from gpr.line_inbox import Store, kv_key_to_dedup
    c = cfg(tmp_path)
    store = Store(c)
    pull(file_source(KVFIX), c, store=store)  # first run stores all 4
    gets = []

    class Resp:
        def __init__(self, b): self.b = b
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.b

    names = [{"name": "event:01TESTKV0000000000000000" + i} for i in "12345"]

    def opener(req, timeout):
        if "/keys" in req.full_url:
            return Resp(json.dumps({"success": True, "result": names, "result_info": {"cursor": ""}}).encode())
        gets.append(req.full_url)
        return Resp(json.dumps({"id": "01TESTKV00000000000000005", "timestamp": 1, "source": {"type": "user", "userId": U1},
                                "message": {"id": "5", "type": "text", "text": "hi"}}).encode())

    src = KVSource({"CF_ACCOUNT_ID": "a", "CF_KV_NAMESPACE_ID": "n", "CF_API_TOKEN": "t"}, opener=opener)
    s = pull(src.items(skip=lambda k: store.seen(kv_key_to_dedup(k))), c, store=store)
    assert len(gets) == 1 and s["new"] == 1  # only the unseen key was fetched


# ---------------------------------------------------------------- preflight check
import urllib.error

from gpr.line_inbox import check

SECRET = "cfut_FAKE_TOKEN_FOR_TEST_ONLY_1234567890"
ENV_OK = {"CF_ACCOUNT_ID": "acc", "CF_KV_NAMESPACE_ID": "ns", "CF_API_TOKEN": SECRET, "GPR_TEST_USER_IDS": U1}


class _R:
    def __init__(self, b): self.b = b
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.b


def test_check_ok_never_prints_secret(tmp_path):
    lines = []
    ok = check({**ENV_OK, "GPR_DATA_DIR": str(tmp_path)},
               opener=lambda r, timeout: _R(json.dumps({"success": True, "result": [{"name": "event:x"}], "result_info": {}}).encode()),
               out=lines.append)
    text = "\n".join(lines)
    assert ok and "KV read OK" in text and SECRET not in text and "GPR_ID_SALT          missing" in text


def test_check_403_and_missing(tmp_path):
    def deny(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    lines = []
    assert not check({**ENV_OK, "GPR_DATA_DIR": str(tmp_path)}, opener=deny, out=lines.append)
    assert any("HTTP 403" in x and "KV Storage: Read" in x for x in lines) and SECRET not in "\n".join(lines)
    lines = []
    assert not check({"GPR_DATA_DIR": str(tmp_path)}, out=lines.append)
    assert any("SKIPPED" in x for x in lines)


def test_check_warns_data_dir_in_repo():
    lines = []
    check({"GPR_DATA_DIR": str(ROOT / "data")}, out=lines.append)
    assert any("inside the repo" in x for x in lines)


# ---------------------------------------------------------------- image download
from gpr.line_inbox import LineTokenError, fetch_images

LINE_TOKEN = "LINE_FAKE_CHANNEL_TOKEN_abcdef123456"


class _IR:
    def __init__(self, body, ctype):
        self.body, self.headers = body, {"Content-Type": ctype}
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self.body


def _seed(tmp_path):
    c = cfg(tmp_path)
    pull(file_source(KVFIX), c)  # contains 1 image record
    return c


def test_fetch_images_saves_privately_and_skips_done(tmp_path):
    c = _seed(tmp_path)
    calls = []

    def opener(req, timeout):
        calls.append(req.full_url)
        assert req.get_method() == "GET" and "api-data.line.me/v2/bot/message/910000000000000002/content" in req.full_url
        return _IR(b"\x89PNG fake", "image/png")

    s = fetch_images(c, "test", environ={"LINE_CHANNEL_ACCESS_TOKEN": LINE_TOKEN}, opener=opener)
    assert s["images_found"] == 1 and s["fetched"] == 1
    files = list((tmp_path / "test" / "images").iterdir())
    assert len(files) == 1 and files[0].suffix == ".png" and str(files[0]).startswith(str(tmp_path))
    s2 = fetch_images(c, "test", environ={"LINE_CHANNEL_ACCESS_TOKEN": LINE_TOKEN}, opener=opener)
    assert s2["already"] == 1 and s2["fetched"] == 0 and len(calls) == 1
    assert LINE_TOKEN not in json.dumps(s) + json.dumps(s2)


def test_fetch_images_expired_and_bad_token(tmp_path):
    c = _seed(tmp_path)

    def gone(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    s = fetch_images(c, "test", environ={"LINE_CHANNEL_ACCESS_TOKEN": LINE_TOKEN}, opener=gone)
    assert s["expired"] == 1 and not any((tmp_path / "test" / "images").iterdir())

    c2 = _seed(tmp_path / "b")

    def denied(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with pytest.raises(LineTokenError) as e:
        fetch_images(c2, "test", environ={"LINE_CHANNEL_ACCESS_TOKEN": LINE_TOKEN}, opener=denied)
    assert LINE_TOKEN not in str(e.value)
    with pytest.raises(LineTokenError):
        fetch_images(c2, "test", environ={})


def test_payment_never_confirmed_by_image(tmp_path):
    c = _seed(tmp_path)
    fetch_images(c, "test", environ={"LINE_CHANNEL_ACCESS_TOKEN": LINE_TOKEN},
                 opener=lambda r, timeout: _IR(b"x", "image/jpeg"))
    assert all(r["payment_confirmed"] is False for r in outbox(tmp_path, "test"))
