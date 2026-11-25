"""
GPR Phase 2 PoC: Phone Link -> Xiaohongshu DM -> read -> agent reply -> type draft (NEVER send).

Run on the Windows PC that has Phone Link paired with the Samsung phone.
Intended operator: Jiho (Codex) or a human. Every step pauses for confirmation
unless --auto is given. The script NEVER presses Enter and NEVER clicks a send button.

Setup (PowerShell):
    py -3.12 -m venv .venv ; .venv\\Scripts\\Activate.ps1
    pip install -r requirements.txt

Usage:
    python xhs_draft_poc.py --step all            # interactive, one gate per step
    python xhs_draft_poc.py --step capture        # only screenshot + OCR the XHS window
    python xhs_draft_poc.py --step type --reply-file out\\reply.txt

Hand-off with the agent (steps 5-6) is file based:
    out\\message.json   <- written by this script (OCR text + screenshot path)
    out\\reply.txt      <- written by the agent (Chinese reply, one paragraph)

Safety rules enforced in code:
  * Only the mirrored Xiaohongshu window is captured (not the full desktop).
  * Newlines are stripped from the reply; no Enter / Return key is ever sent.
  * pyautogui FAILSAFE is on: slam the mouse into a screen corner to abort.
  * Every action is logged to out\\poc_log.jsonl with a screenshot as evidence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).with_name("out")
OUT.mkdir(exist_ok=True)
LOG = OUT / "poc_log.jsonl"

PHONE_LINK_AUMID = r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"
XHS_TITLES = ("小红书", "Xiaohongshu", "RED", "rednote", "REDnote")
DM_TAB_LABELS = ("消息",)                    # bottom tab in the XHS app
INPUT_HINTS = ("发消息", "说点什么", "输入", "Say something")  # input-box placeholders
SEND_WORDS = ("发送", "Send")                # we must never click these


def log(event: str, **data) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{rec['ts']}] {event} {json.dumps(data, ensure_ascii=False)}")


def gate(msg: str, auto: bool) -> None:
    if auto:
        return
    ans = input(f"\n>>> {msg}  [Enter = continue, q = abort] ")
    if ans.strip().lower() == "q":
        log("aborted_by_operator", at=msg)
        sys.exit(1)


# ---------------------------------------------------------------- step 1
def open_phone_link() -> None:
    subprocess.Popen(["explorer.exe", PHONE_LINK_AUMID])
    log("step1_open_phone_link", method="AUMID")
    time.sleep(5)


# ---------------------------------------------------------------- step 2
def find_xhs_window(timeout: float = 30.0):
    """Return the pywinauto wrapper of the mirrored XHS window, or None."""
    from pywinauto import Desktop

    end = time.time() + timeout
    while time.time() < end:
        for w in Desktop(backend="uia").windows():
            title = w.window_text() or ""
            if any(t.lower() in title.lower() for t in XHS_TITLES):
                return w
        time.sleep(1)
    return None


def open_xhs(auto: bool):
    win = find_xhs_window(timeout=3)
    if win is None:
        # Phone Link's own UI is UIA-accessible; try to click the app tile by name.
        try:
            from pywinauto import Desktop

            pl = Desktop(backend="uia").window(title_re=".*Phone Link.*|.*โทรศัพท์.*|.*连接至.*")
            pl.set_focus()
            for name in XHS_TITLES:
                hits = pl.descendants(title=name)
                if hits:
                    hits[0].click_input()
                    log("step2_clicked_app_tile", name=name)
                    break
        except Exception as e:  # noqa: BLE001
            log("step2_tile_click_failed", error=str(e))
        win = find_xhs_window(timeout=30)
    if win is None:
        gate("Open Xiaohongshu manually in Phone Link > Apps, then continue", auto)
        win = find_xhs_window(timeout=30)
    if win is None:
        log("step2_FAIL", reason="mirrored XHS window not found")
        sys.exit(2)
    win.set_focus()
    r = win.rectangle()
    log("step2_xhs_window", title=win.window_text(), rect=[r.left, r.top, r.right, r.bottom])
    return win


# ---------------------------------------------------------------- capture + OCR
def capture(win, name: str) -> Path:
    import mss
    import mss.tools

    r = win.rectangle()
    box = {"left": r.left, "top": r.top, "width": r.width(), "height": r.height()}
    path = OUT / f"{datetime.now():%H%M%S}_{name}.png"
    with mss.mss() as s:
        img = s.grab(box)
        mss.tools.to_png(img.rgb, img.size, output=str(path))
    return path


def ocr(path: Path) -> list[dict]:
    """RapidOCR (local, free, good for Simplified Chinese). Returns [{text, box, score}]."""
    from rapidocr_onnxruntime import RapidOCR

    if not hasattr(ocr, "engine"):
        ocr.engine = RapidOCR()
    result, _ = ocr.engine(str(path))
    return [
        {"text": t, "box": [[int(x), int(y)] for x, y in b], "score": round(float(s), 3)}
        for b, t, s in (result or [])
    ]


def click_text(win, lines: list[dict], labels: tuple[str, ...]) -> bool:
    import pyautogui

    r = win.rectangle()
    for ln in lines:
        if any(lb in ln["text"] for lb in labels) and not any(sw == ln["text"].strip() for sw in SEND_WORDS):
            xs = [p[0] for p in ln["box"]]
            ys = [p[1] for p in ln["box"]]
            x, y = r.left + sum(xs) // 4, r.top + sum(ys) // 4
            pyautogui.click(x, y)
            log("click_text", label=ln["text"], at=[x, y])
            return True
    return False


# ---------------------------------------------------------------- step 3
def go_to_dm(win, auto: bool) -> None:
    lines = ocr(capture(win, "home"))
    if not click_text(win, lines, DM_TAB_LABELS):
        gate("Could not find the 消息 tab by OCR. Open the DM page manually, then continue", auto)
    time.sleep(2)
    gate("Open the TEST conversation (test account only), then continue", auto)
    log("step3_dm_page")


# ---------------------------------------------------------------- step 4-5
def read_message(win) -> dict:
    shot = capture(win, "conversation")
    lines = ocr(shot)
    payload = {
        "source": "xiaohongshu_dm_test",
        "screenshot": str(shot),
        "ocr_lines": [ln["text"] for ln in lines],
        "ocr_detail": lines,
        "instruction_for_agent": (
            "Identify the customer's latest message in ocr_lines, then write a polite "
            "Simplified-Chinese reply (one paragraph, no line breaks) to out/reply.txt. "
            "Do not include phone numbers, WeChat IDs or links."
        ),
    }
    (OUT / "message.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log("step4_5_message_written", lines=len(lines), file="out/message.json")
    return payload


# ---------------------------------------------------------------- step 6
def wait_reply(reply_file: Path, timeout: float = 900) -> str:
    end = time.time() + timeout
    while time.time() < end:
        if reply_file.exists() and reply_file.stat().st_size > 0:
            text = reply_file.read_text(encoding="utf-8")
            log("step6_reply_received", chars=len(text))
            return text
        time.sleep(2)
    log("step6_FAIL", reason="no reply within timeout")
    sys.exit(3)


# ---------------------------------------------------------------- step 7
def type_draft(win, reply: str, auto: bool) -> None:
    import pyautogui
    import pyperclip

    pyautogui.FAILSAFE = True
    clean = " ".join(reply.replace("\r", " ").replace("\n", " ").split())
    if not clean:
        log("step7_FAIL", reason="empty reply")
        sys.exit(4)

    lines = ocr(capture(win, "before_type"))
    if not click_text(win, lines, INPUT_HINTS):
        gate("Could not find the input box by OCR. Click it manually, then continue", auto)
    time.sleep(0.8)

    # Method A: clipboard paste (needs Phone Link cross-device copy & paste for Samsung).
    pyperclip.copy(clean)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(1.5)
    after = capture(win, "after_paste")
    found = any(clean[:6] in ln["text"] for ln in ocr(after))
    log("step7_paste_attempt", ok=found, screenshot=str(after))

    if not found:
        # Method B: Unicode key injection. Results vary with the phone IME; record outcome.
        gate("Paste not detected. Try Unicode typing instead?", auto)
        from pywinauto.keyboard import send_keys  # pyautogui cannot type CJK

        send_keys(clean, with_spaces=True, pause=0.03)  # no {ENTER} ever
        after = capture(win, "after_unicode_type")
        found = any(clean[:6] in ln["text"] for ln in ocr(after))
        log("step7_unicode_attempt", ok=found, screenshot=str(after))

    log("step7_done_NOT_SENT", draft=clean, verified_on_screen=found)
    print("\nDraft is in the input box. It has NOT been sent. Review it on the phone.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", default="all", choices=["all", "capture", "type"])
    ap.add_argument("--reply-file", default=str(OUT / "reply.txt"))
    ap.add_argument("--auto", action="store_true", help="skip confirmation gates (not recommended)")
    a = ap.parse_args()

    if a.step in ("all",):
        gate("Step 1: open Phone Link", a.auto)
        open_phone_link()
    gate("Step 2: open Xiaohongshu in Phone Link", a.auto)
    win = open_xhs(a.auto)
    if a.step == "all":
        gate("Step 3: go to the DM page", a.auto)
        go_to_dm(win, a.auto)
    if a.step in ("all", "capture"):
        gate("Step 4-5: capture + OCR + write out/message.json", a.auto)
        read_message(win)
    if a.step in ("all", "type"):
        reply = wait_reply(Path(a.reply_file))
        gate(f"Step 7: type this draft WITHOUT sending:\n{reply}", a.auto)
        type_draft(win, reply, a.auto)


if __name__ == "__main__":
    main()
