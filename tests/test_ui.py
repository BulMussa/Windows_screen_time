"""Exercises the Sync tab of the app against a fake server, with scratch logs and a test credential."""
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import app as app_mod, peer, sync, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_ui_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
sync.delete_token()
peer.PAIRING_TARGET, peer.KEY_TARGET = "ScreenTimePhonePairingTEST", "ScreenTimePhoneKeyTEST"
sync.cred_delete(peer.PAIRING_TARGET)
sync.cred_delete(peer.KEY_TARGET)
app_mod.autostart_enabled = lambda: False
app_mod.set_autostart = lambda enabled: None

pushes = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        data = json.dumps({"intervals": [], "app_usage_daily": [], "cursor": "0", "has_more": False}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if self.path == "/v1/auth/login":
            ok = body == {"name": "tester", "password": "secret"}
            code, out = (200, {"session_token": "s", "expires_at": "x"}) if ok else (401, {"detail": "invalid name or password"})
        elif self.path == "/v1/devices":
            code, out = 201, {"device_id": 1, "name": body["name"], "type": "windows", "token": uuid.uuid4().hex}
        else:
            pushes.append(body)
            code, out = 200, {k: {"accepted": len(v), "ignored": 0, "rejected": []} for k, v in body.items()}
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_address[1]}"


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def pump(seconds=0.3, until=None, limit=15):
    end = time.time() + (limit if until else seconds)
    while time.time() < end:
        root.update()
        if until and until():
            return True
        time.sleep(0.05)
    return not until




root = app_mod.tk.Tk()
root.geometry("+80+60")
app = app_mod.App(root, tracker.Tracker())
# The phone listener starts with the Phone directly and Both modes: on 127.0.0.1 here, so nothing
# listens on the network, and with a stand-in for the firewall check (that one runs PowerShell)
app.peer.host, app.peer.port, app.peer.discovery_port = "127.0.0.1", 0, 0
firewall_asked = []


def fake_firewall(ip, port, discovery_port, program=None):  # the real check: tests/test_firewall.py
    firewall_asked.append((ip, port, discovery_port))
    return firewall_answer[0]


firewall_answer = [("blocked", "Windows Firewall blocks Screen Time on this public network (rule \"x\").")]
app_mod.firewall.check = fake_firewall
pump(0.5)
tabs = [app.tabs.tab(t, "text") for t in app.tabs.tabs()]
check(tabs == ["Report", "History", "Settings", "Connect"], f"tabs {tabs}")
app.tabs.select(3)
pump(0.5)
check(app.sync_status.get() == "Not connected" and app.sync_form.winfo_ismapped(), "starts on the connect form")
check(not app.sync_connected.winfo_ismapped(), "connected panel hidden")
check(app.sync_fields["device"].get() == sync.default_device_name(), "device name prefilled")
check(app.sync_fields["password"].get() == "", "password field empty")

app.sync_fields["server"].set("http://example.com")
app.sync_fields["name"].set("tester")
app.sync_fields["password"].set("secret")
app.connect_sync()
check(pump(until=lambda: app.sync_error.get() != ""), "bad address shows an error")
check("https://" in app.sync_error.get() and app.sync_form.winfo_ismapped(), f"error text: {app.sync_error.get()}")
check(str(app.connect_button.cget("state")) == "normal", "Connect button usable again")

app.sync_fields["server"].set(URL)
app.sync_fields["password"].set("wrong")
app.connect_sync()
check(pump(until=lambda: "invalid name or password" in app.sync_error.get()), "wrong password shows server's message")

app.sync_fields["password"].set("secret")
app.connect_sync()
check(pump(until=lambda: app.sync_connected.winfo_ismapped()), "connect switches to the connected panel")
check(app.sync_fields["password"].get() == "", "password cleared from the form after connecting")
check(pump(until=lambda: app.sync_status.get().startswith("Synced at")), f"worker synced: {app.sync_status.get()}")
check(len(pushes) >= 1 and sync.load_token(), "server received a push; token stored")
pump(0.5)

before = len(pushes)
app.start_sync()
check(app.sync_status.get() == "Syncing..." or app.sync_status.get().startswith("Synced"), "Sync now responds")
pump(2)
check(app.sync_status.get().startswith("Synced at"), "Sync now ends with a synced status")

app_mod.messagebox.askyesno = lambda *a, **k: False
app.disconnect_sync()
pump(0.4)
check(app.sync_connected.winfo_ismapped(), "declining the confirmation keeps the connection")
app_mod.messagebox.askyesno = lambda *a, **k: True
app.disconnect_sync()
pump(0.4)
check(app.sync_form.winfo_ismapped() and sync.load_token() is None, "disconnect returns to the form and removes the token")

# --- sync mode ---
check(app.sync_mode.get() == "server" and len(app.mode_buttons) == 3, "mode choice shows Server")
check(all(b.winfo_ismapped() for b in app.mode_buttons + app.backup_buttons), "mode and backup controls visible")


def sections():
    pump(0.2)
    return app.phone_section.winfo_ismapped(), app.server_section.winfo_ismapped()


check(sections() == (False, True), "server mode: Server section only")
app.sync_mode.set("both")
check(sections() == (True, True), "both: Phone and Server sections")
app.sync_mode.set("direct")
check(sections() == (True, False), "phone directly: Phone section only")
app.sync_mode.set("server")
check(sections() == (False, True), "server again: Server section only")
app.sync_from_report()
check(app.tabs.index(app.tabs.select()) == 3, "server mode, not connected: Report's Sync opens the Connect tab")
app.tabs.select(0)
app.sync_mode.set("direct")
app.change_sync_mode()
check(pump(until=lambda: app.sync_status.get() == sync.DIRECT_MODE_STATUS), f"direct: status {app.sync_status.get()}")
check(sync.get_mode() == "direct", "direct mode saved")
check(pump(until=lambda: app.phone_status.get().startswith("Listening on")), f"direct: listening: {app.phone_status.get()}")
check(app.phone_row.winfo_manager() == "pack", "direct: the listener's state shows under Sync with")
pump(0.2)
app.sync_from_report()
check(app.tabs.index(app.tabs.select()) == 0, "direct mode: Report's Sync syncs instead of opening the Connect tab")
check(pump(until=lambda: app.sync_status.get() == sync.DIRECT_MODE_STATUS), "... and says server sync is off")
check(app.sync_buttons[0].button.cget("text") == "Phone syncs", "direct mode: the Sync button says the phone syncs, not failed")
app.sync_mode.set("server")
app.change_sync_mode()
check(pump(until=lambda: sync.get_mode() == "server" and app.sync_status.get() == "Not connected"),
      "back to server mode")
check(pump(until=lambda: app.phone_status.get() == app_mod.PHONE_OFF and app.peer.thread is None),
      "server mode: listener stopped")
check(app.phone_row.winfo_manager() == "", "server mode: no phone listener text under Sync with")
app.tabs.select(3)

# --- backup buttons ---
backup_file = os.path.join(SCRATCH, "ui-backup.json")
errors = []
app_mod.messagebox.showerror = lambda title, text: errors.append(text)
app_mod.filedialog.asksaveasfilename = lambda **k: ""
app.export_backup()
pump(0.3)
check(app.backup_status.get() == "" and not os.path.exists(backup_file), "cancelled export does nothing")
app_mod.filedialog.asksaveasfilename = lambda **k: backup_file
app.export_backup()
check(pump(until=lambda: app.backup_status.get().startswith("Saved ")), f"export: {app.backup_status.get()}")
check(os.path.exists(backup_file) and sync.read_backup(backup_file)["device"]["install_id"],
      "export wrote a readable backup")
check(all(str(b.cget("state")) == "normal" for b in app.backup_buttons), "backup buttons usable again")
app_mod.filedialog.askopenfilename = lambda **k: backup_file
app.import_backup()
check(pump(until=lambda: app.backup_status.get().startswith("Imported")), f"import: {app.backup_status.get()}")
check(app.backup_status.get() == "Imported: 0 phone/sleep periods, 0 days of this PC.", "importing its own fresh backup changes nothing")
junk = os.path.join(SCRATCH, "junk.json")
with open(junk, "w") as f:
    f.write("[]")
app_mod.filedialog.askopenfilename = lambda **k: junk
app.import_backup()
check(pump(until=lambda: errors), "a bad file shows an error")
check("isn't a Screen Time backup" in errors[-1] and app.backup_status.get() == "", f"error: {errors[-1]}")

# --- phone sync ---
import http.client  # noqa: E402
import socket  # noqa: E402
import ssl  # noqa: E402
from datetime import datetime  # noqa: E402
check(app.phone_status.get() == app_mod.PHONE_OFF and app.peer.thread is None,
      "server mode (the default): nothing listening")
check(not hasattr(app, "allow_phone") and not app.pair_button.winfo_ismapped(),
      "no Allow phone sync box; Pair phone hidden in server mode")
check(app.paired_text.get() == "No phone paired" and "disabled" in app.unpair_button.state(),
      "nothing paired: Remove pairing disabled")
firewall_asked.clear()
firewall_answer[0] = ("blocked", "Windows Firewall blocks Screen Time on this public network (rule \"x\").")
app.pair_phone()  # switches Server to Both by itself
check(pump(until=lambda: app.pair_dialog is not None), "Pair phone opens the QR window")
check(app.sync_mode.get() == "both" and sync.get_mode() == "both" and app.phone_status.get().startswith("Listening on"),
      f"pairing switched to Both and listens: {app.phone_status.get()}")
check(pump(until=lambda: app.firewall_text.get()), "listening: the firewall is checked")
check(firewall_asked[-1][:2] == app.peer.address and app.firewall_text.get().startswith("Windows Firewall blocks"),
      f"a blocking firewall is shown under the listener state: {app.firewall_text.get()}")
dialog = app.pair_dialog
check(dialog.image.width() >= 200 and dialog.status.get() == "Waiting for the phone...", "QR code shown, waiting")
app.pair_phone()
check(app.pair_dialog is dialog, "a second click brings the same window forward")
secret = sync.cred_read(peer.PAIRING_TARGET)
CTX = ssl.create_default_context(cafile=peer.certificate()[0])
CTX.check_hostname = False


def phone_call(method, path, key, body=None):
    """What the phone does: a nonce, then the request with its HMAC proof."""
    conn = http.client.HTTPSConnection(*app.peer.address, context=CTX, timeout=5)
    try:
        conn.request("GET", "/peer/nonce")
        nonce = json.loads(conn.getresponse().read())["nonce"]
    finally:
        conn.close()
    data = json.dumps(body).encode() if body is not None else b""
    conn = http.client.HTTPSConnection(*app.peer.address, context=CTX, timeout=5)
    try:
        conn.request(method, path, body=data if method == "POST" else None,
                     headers={"Authorization": f"HMAC {nonce}:{peer.request_mac(key, method, path, nonce, data)}"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


start = time.time()
code, answer = phone_call("POST", "/peer/pair", secret, {"device_id": "ui-phone", "name": "Test Phone"})
check(code == 200 and time.time() - start < 5, "the phone pairs while the window runs")
check(pump(until=lambda: dialog.done), "the QR window notices")
check(dialog.status.get().startswith("Paired with Test Phone") and str(dialog.qr.cget("image")) == "",
      "it says so and hides the spent code")
check(app.paired_text.get() == "Paired with Test Phone"
      and "disabled" not in app.unpair_button.state(), f"Connect tab shows the paired phone: {app.paired_text.get()}")
key = answer["key"]
check(phone_call("GET", "/peer/hello", key)[0] == 200, "the phone's key works")
check(app.last_sync_text.get() == "Not synced yet.", f"no server, phone not synced yet: {app.last_sync_text.get()}")
check(phone_call("GET", "/peer/pull?since=0", key)[0] == 200, "the phone pulls")
check(pump(until=lambda: app.last_sync_text.get().startswith("Last synced at ")), "the window notices the sync")
check(app.last_sync_text.get().startswith("Last synced at "), f"Last synced at ...: {app.last_sync_text.get()}")
check(app_mod.last_synced_text("2026-11-19T18:02:00", now=datetime(2026, 11, 20, 9, 0)) == "Last synced yesterday at 18:02."
      and app_mod.last_synced_text("2026-11-02T07:05:00", now=datetime(2026, 11, 20, 9, 0)) == "Last synced on 2 Nov at 07:05."
      and app_mod.last_synced_text(None) == "Not synced yet.", "older syncs say the day")
dialog.close()
pump(0.3)
check(app.pair_dialog is None and peer.paired_phone()["name"] == "Test Phone", "closing after pairing keeps it")

app.pair_phone()
check(pump(until=lambda: app.pair_dialog is not None), "pairing window again")
spare = sync.cred_read(peer.PAIRING_TARGET)
app.pair_dialog.close()
pump(0.5)
check(sync.cred_read(peer.PAIRING_TARGET) is None and phone_call("POST", "/peer/pair", spare,
                                                                 {"device_id": "x", "name": "x"})[0] == 401,
      "cancelling the window stops its code")
check(phone_call("GET", "/peer/hello", key)[0] == 200, "...and the paired phone still works")

app_mod.messagebox.askyesno = lambda *a, **k: False
app.remove_pairing()
check(peer.paired_phone() is not None, "declining keeps the pairing")
app_mod.messagebox.askyesno = lambda *a, **k: True
app.remove_pairing()
check(app.paired_text.get() == "No phone paired" and phone_call("GET", "/peer/hello", key)[0] == 401,
      "Remove pairing revokes the phone")

app.sync_mode.set("server")
app.change_sync_mode()
check(pump(until=lambda: app.phone_status.get() == app_mod.PHONE_OFF and app.peer.thread is None),
      "Server: listener stopped")
check(app.firewall_text.get() == "", "Server: no firewall message")
firewall_answer[0] = ("ok", "")
app.sync_mode.set("direct")
app.change_sync_mode()
check(pump(until=lambda: app.phone_status.get().startswith("Listening")), "Phone directly: listening again")
asked = len(firewall_asked)
check(pump(until=lambda: len(firewall_asked) >= asked and app.firewall_text.get() == "") and len(firewall_asked) >= 2,
      "firewall checked again; a firewall that lets the phone in shows nothing")
listening = app.peer.address

app.quit()
try:
    socket.create_connection(listening, timeout=2).close()
    check(False, "quitting stops the listener")
except OSError:
    check(True, "quitting stops the listener")
sync.delete_token()
sync.cred_delete(peer.PAIRING_TARGET)
sync.cred_delete(peer.KEY_TARGET)
server.shutdown()
server.server_close()
print("UI CHECKS PASSED")
