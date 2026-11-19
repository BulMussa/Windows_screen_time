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
from screentime import app as app_mod, sync, tracker  # noqa: E402

SCRATCH = tempfile.mkdtemp(prefix="st_ui_")
tracker.LOG_DIR = os.path.join(SCRATCH, "logs")
os.makedirs(tracker.LOG_DIR)
sync.CRED_TARGET = "ScreenTimeSyncTEST"
sync.delete_token()
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
app.sync_from_report()
check(app.tabs.index(app.tabs.select()) == 3, "server mode, not connected: Report's Sync opens the Connect tab")
app.tabs.select(0)
app.sync_mode.set("direct")
app.change_sync_mode()
check(pump(until=lambda: app.sync_status.get() == sync.DIRECT_NOT_READY), f"direct: status {app.sync_status.get()}")
check(sync.get_mode() == "direct", "direct mode saved")
pump(0.2)
app.sync_from_report()
check(app.tabs.index(app.tabs.select()) == 0, "direct mode: Report's Sync syncs instead of opening the Connect tab")
check(pump(until=lambda: app.sync_status.get() == sync.DIRECT_NOT_READY), "... and says direct isn't ready")
app.sync_mode.set("server")
app.change_sync_mode()
check(pump(until=lambda: sync.get_mode() == "server" and app.sync_status.get() == "Not connected"),
      "back to server mode")
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

app.quit()
sync.delete_token()
server.shutdown()
server.server_close()
print("UI CHECKS PASSED")
