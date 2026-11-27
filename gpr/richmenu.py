"""
Validate (default) or create (explicit --apply) the GPR LINE rich menu.

    python -m gpr.richmenu validate line/richmenu/richmenu.json
    python -m gpr.richmenu apply line/richmenu/richmenu.json --image line/richmenu/richmenu-admin.jpg --link-admin

apply needs LINE_CHANNEL_ACCESS_TOKEN and GPR_ADMIN_USER_ID (Gift's LINE userId) in the
environment (never in the repo/chat) and changes the PRODUCTION LINE account -> Gift runs it.
The admin menu is linked ONLY to Gift's userId; it is never set as the default menu.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

API, DATA_API = "https://api.line.me/v2/bot", "https://api-data.line.me/v2/bot"
ALLOWED_SIZES = {(2500, 1686), (2500, 843), (1200, 810), (1200, 405), (800, 540), (800, 270)}


def validate(menu: dict, expected_areas: int = 6) -> list[str]:
    errs: list[str] = []
    w, h = menu.get("size", {}).get("width"), menu.get("size", {}).get("height")
    if (w, h) not in ALLOWED_SIZES:
        errs.append(f"size {w}x{h} not allowed")
    if not menu.get("name") or len(menu["name"]) > 300:
        errs.append("name missing or > 300 chars")
    if not menu.get("chatBarText") or len(menu["chatBarText"]) > 14:
        errs.append("chatBarText missing or > 14 chars")
    areas = menu.get("areas", [])
    if len(areas) != expected_areas:
        errs.append(f"expected {expected_areas} areas, got {len(areas)}")
    covered = 0
    rects = []
    for i, a in enumerate(areas):
        b, act = a.get("bounds", {}), a.get("action", {})
        x, y, bw, bh = (b.get(k, -1) for k in ("x", "y", "width", "height"))
        if min(x, y) < 0 or bw <= 0 or bh <= 0 or x + bw > (w or 0) or y + bh > (h or 0):
            errs.append(f"area {i}: bounds outside menu")
        rects.append((x, y, x + bw, y + bh))
        covered += max(bw, 0) * max(bh, 0)
        if act.get("type") not in ("message", "uri", "postback"):
            errs.append(f"area {i}: action type {act.get('type')} not allowed here")
        if act.get("type") == "message" and not (0 < len(act.get("text", "")) <= 300):
            errs.append(f"area {i}: message text empty or > 300")
        if len(act.get("label", "")) > 20:
            errs.append(f"area {i}: label > 20 chars")
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                errs.append(f"areas {i} and {j} overlap")
    if w and h and covered != w * h:
        errs.append(f"areas cover {covered} px of {w * h}")
    return errs


def validate_image(path: Path, menu: dict) -> list[str]:
    errs = []
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        errs.append("image must be PNG or JPEG")
    if path.stat().st_size > 1_000_000:
        errs.append("image must be <= 1 MB")
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.size != (menu["size"]["width"], menu["size"]["height"]):
                errs.append(f"image {im.size} != menu size")
    except ImportError:
        pass
    return errs


def _post(url: str, token: str, body: bytes, ctype: str) -> dict:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def apply(menu: dict, image: Path, link_admin: bool) -> str:
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    admin = os.environ.get("GPR_ADMIN_USER_ID", "")
    if not token:
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN not set")
    if link_admin and not admin:
        raise SystemExit("GPR_ADMIN_USER_ID not set")
    body = {k: v for k, v in menu.items() if not k.startswith("_")}
    rid = _post(f"{API}/richmenu", token, json.dumps(body, ensure_ascii=False).encode(), "application/json")["richMenuId"]
    ctype = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    _post(f"{DATA_API}/richmenu/{rid}/content", token, image.read_bytes(), ctype)
    if link_admin:  # per-user link; customers keep the default (or no) menu
        _post(f"{API}/user/{admin}/richmenu/{rid}", token, b"", "application/json")
    return rid


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="gpr.richmenu")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("menu", type=Path)
    p = sub.add_parser("apply")
    p.add_argument("menu", type=Path)
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("--link-admin", action="store_true", help="link to GPR_ADMIN_USER_ID only")
    a = ap.parse_args(argv)

    menu = json.loads(a.menu.read_text(encoding="utf-8"))
    errs = validate(menu)
    if errs:
        print("INVALID:\n- " + "\n- ".join(errs))
        sys.exit(1)
    img = a.image if a.cmd == "apply" else Path(menu.get("_image", ""))
    if img and img.is_file():
        errs += validate_image(img, menu)
    if errs:
        print("INVALID:\n- " + "\n- ".join(errs))
        sys.exit(1)
    if a.cmd == "validate":
        print("OK: 6 areas, no overlap, full coverage, actions valid" + (f", image {img} OK" if img and img.is_file() else ", no image checked"))
        return
    if input("This changes the PRODUCTION LINE account. Type APPLY to continue: ") != "APPLY":
        sys.exit("aborted")
    print("richMenuId:", apply(menu, a.image, a.link_admin))


if __name__ == "__main__":
    main()
