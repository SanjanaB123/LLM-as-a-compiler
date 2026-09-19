"""
SPIKE PROBE 2 (Python, via CDP) — same-session control transfer.
Run locally:  python3 probe_handoff.py

launch_server() is JS-only. The Python + production-correct approach is CDP:
launch a real Chromium with an open remote-debugging port, then have BOTH
controllers attach over the Chrome DevTools Protocol to the SAME running browser.

Why this is the better design: CDP is the browser's native remote protocol, so
the live session can be driven by our automation, a second process standing in
for a human, OR a real human's browser pointed at the same port. One session,
transferable control.
"""
import pathlib, subprocess, time, socket, urllib.request, json, sys
from playwright.sync_api import sync_playwright

PAGE = pathlib.Path(__file__).parent / "members.html"
PORT = 9222

def free_port(p):
    s = socket.socket(); 
    try:
        s.bind(("127.0.0.1", p)); s.close(); return True
    except OSError:
        s.close(); return False

# --- find the Chromium executable Playwright installed ---
with sync_playwright() as p:
    chromium_path = p.chromium.executable_path

# --- launch ONE long-lived browser with a remote-debugging port ---
# In production THIS process is the session; controllers come and go.
proc = subprocess.Popen(
    [chromium_path, f"--remote-debugging-port={PORT}",
     "--user-data-dir=/tmp/spike-profile", "--headless=new", "--no-first-run"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

# wait for the debug endpoint to come up
cdp_url = None
for _ in range(50):
    try:
        info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version"))
        cdp_url = info["webSocketDebuggerUrl"]; break
    except Exception:
        time.sleep(0.2)
if not cdp_url:
    print("could not reach CDP endpoint"); proc.terminate(); sys.exit(1)
print("live browser on CDP port", PORT)

with sync_playwright() as p:
    # --- CONTROLLER 1: AUTOMATION attaches over CDP, navigates deep ---
    auto = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    ctx = auto.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"file://{PAGE}")
    page.get_by_role("textbox", name="Member ID").fill("12345")
    page.get_by_role("button", name="Search").click()
    print("[automation] at member detail; RELEASING (detach, browser stays up)")
    auto.close()  # detaches this client; the browser process keeps running

    # --- CONTROLLER 2: HUMAN attaches to the SAME live browser ---
    human = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    hpage = human.contexts[0].pages[0]
    print("[human] attached to SAME session; sees balance:",
          hpage.get_by_label("Savings Balance").inner_text())
    hpage.get_by_role("textbox", name="Member ID").fill("54321")
    hpage.get_by_role("button", name="Search").click()
    print("[human] did manual action; RELEASING")
    human.close()

    # --- CONTROLLER 1 resumes: re-attach, RE-OBSERVE (don't assume position) ---
    auto2 = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    rpage = auto2.contexts[0].pages[0]
    print("[automation] re-attached; re-observed after human:",
          rpage.get_by_label("Savings Balance").inner_text())
    auto2.close()

proc.terminate()
print("\nPROBE 2 OK — one session, three control transfers over CDP")
