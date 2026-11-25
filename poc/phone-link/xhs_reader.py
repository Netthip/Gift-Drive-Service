"""
GPR xhs_reader: watch the mirrored Xiaohongshu window and emit TEXT ONLY for Bo.

Why: Bo reading screenshots via Computer Use costs a lot of quota. This script does the
seeing locally (screenshot + local OCR, zero AI tokens) and writes only new text lines,
so Bo processes a few hundred characters instead of images.

READ-ONLY: this script never clicks, never types, never presses a key.
Open the conversation in Phone Link yourself (or let Bo/Jiho open it once), then run:

    python xhs_reader.py                 # poll every 5 s, print + append new lines
    python xhs_reader.py --interval 10 --keep-shots

Output: out/inbox.jsonl, one record per change:
    {"ts": "...", "window": "小红书", "new_lines": ["...", "..."], "all_lines": [...], "screenshot": null}

Token-saving rules built in:
  * OCR runs only when the window image actually changed (cheap pixel hash).
  * Only lines not seen in the previous capture are reported as new_lines.
  * Screenshots are deleted after OCR unless --keep-shots (also minimises customer data on disk).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from xhs_draft_poc import OUT, capture, find_xhs_window, ocr

INBOX = OUT / "inbox.jsonl"
MIN_SCORE = 0.6  # drop low-confidence OCR noise


def image_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--keep-shots", action="store_true")
    a = ap.parse_args()

    win = find_xhs_window(timeout=30)
    if win is None:
        raise SystemExit("Xiaohongshu window not found. Open it in Phone Link first.")
    print(f"Watching: {win.window_text()}  (Ctrl+C to stop)")

    last_hash, seen = None, []
    while True:
        shot = capture(win, "watch")
        h = image_hash(shot)
        if h != last_hash:
            lines = [ln["text"].strip() for ln in ocr(shot) if ln["score"] >= MIN_SCORE and ln["text"].strip()]
            new = [ln for ln in lines if ln not in seen]
            if new:
                rec = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "window": win.window_text(),
                    "new_lines": new,
                    "all_lines": lines,
                    "screenshot": str(shot) if a.keep_shots else None,
                }
                with INBOX.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print("\n".join(f"  + {ln}" for ln in new))
            seen, last_hash = lines, h
        if not a.keep_shots:
            shot.unlink(missing_ok=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
