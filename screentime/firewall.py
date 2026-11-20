"""Does Windows Firewall let the phone reach the phone-sync listener? (Windows; read only.)

check() asks PowerShell's NetSecurity cmdlets about the network the listener is on (its firewall
profile: Domain, Private or Public) and the inbound rules for this program, and assess() turns that
into a verdict for the window. It changes nothing. It takes a few seconds, so call it off the Tk thread.

Only rules tied to this program are looked at. A rule that opens the port for every program also
lets the phone in, so a missing program rule is reported as "may block", not "blocks".
"""
import base64
import json
import os
import subprocess
import sys

TIMEOUT = 30  # seconds for the PowerShell query
OK, WARN, BLOCKED, UNKNOWN = "ok", "warn", "blocked", "unknown"


def allow_how(profile):
    """How to let Screen Time in on networks of this profile."""
    text = ("Allow it in Windows Security > Firewall & network protection > Allow an app through firewall: "
            f"tick Screen Time for {profile} networks.")
    if profile == "Public":
        text += " Or, if this is your home Wi-Fi, set it to Private in Settings > Network & internet."
    return text

SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ip = $env:ST_IP; $program = $env:ST_PROGRAM
$category = $null
try {
    $category = [string](Get-NetIPAddress -IPAddress $ip | Get-NetConnectionProfile | Select-Object -First 1).NetworkCategory
} catch {}
$profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore | ForEach-Object {
    [pscustomobject]@{ Name = [string]$_.Name; Enabled = [string]$_.Enabled
                       DefaultInboundAction = [string]$_.DefaultInboundAction; AllowInboundRules = [string]$_.AllowInboundRules } })
$rules = @()
$filters = @(Get-NetFirewallApplicationFilter -PolicyStore ActiveStore | Where-Object {
    $_.Program -and ([Environment]::ExpandEnvironmentVariables($_.Program) -ieq $program) })
foreach ($f in $filters) {
    foreach ($r in @($f | Get-NetFirewallRule)) {
        if ([string]$r.Direction -ne 'Inbound') { continue }
        $pf = $r | Get-NetFirewallPortFilter
        $rules += [pscustomobject]@{ Name = [string]$r.DisplayName; Enabled = [string]$r.Enabled; Action = [string]$r.Action
                                     Profile = [string]$r.Profile; Protocol = [string]$pf.Protocol
                                     LocalPort = (@($pf.LocalPort) -join ',') }
    }
}
[pscustomobject]@{ Category = $category; Profiles = @($profiles); Rules = @($rules) } | ConvertTo-Json -Depth 4 -Compress
'''


def program_path():
    """The program the firewall sees listening: ScreenTime.exe, or python(w).exe when run from source."""
    return os.path.abspath(sys.executable)


def query(ip, program, timeout=TIMEOUT):
    """The raw facts as a dict ({"Category", "Profiles", "Rules"}); raises OSError or ValueError."""
    encoded = base64.b64encode(SCRIPT.encode("utf-16-le")).decode()
    env = dict(os.environ, ST_IP=ip, ST_PROGRAM=program)  # passed as data, never pasted into the script
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=timeout, env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout).strip()[:300] or f"PowerShell exited with {done.returncode}")
    return json.loads(done.stdout)


def _profile_name(category):
    """Firewall profile for a network category; an unknown network counts as Public (the strictest usual one)."""
    return {"DomainAuthenticated": "Domain", "Private": "Private", "Public": "Public"}.get(category or "", "Public")


def _covers_profile(rule, profile):
    names = [p.strip() for p in (rule.get("Profile") or "Any").split(",")]
    return "Any" in names or profile in names


def _covers(rule, protocol, port):
    if (rule.get("Protocol") or "Any") not in ("Any", protocol):
        return False
    ports = [p.strip() for p in (rule.get("LocalPort") or "Any").split(",") if p.strip()]
    for p in ports or ["Any"]:
        if p == "Any":
            return True
        low, _, high = p.partition("-")
        if low.isdigit() and int(low) <= port <= int(high if high.isdigit() else low):
            return True
    return False


def assess(info, port, discovery_port):
    """(verdict, message) from query()'s facts: OK (message ""), WARN, BLOCKED or UNKNOWN."""
    profile = _profile_name(info.get("Category"))
    network = f"{profile.lower()} network" if profile != "Domain" else "work (domain) network"
    settings = {p.get("Name"): p for p in info.get("Profiles") or []}.get(profile)
    if settings is None:
        return UNKNOWN, ""
    if settings.get("Enabled") != "True":
        return OK, ""  # the firewall is off for this network
    if settings.get("AllowInboundRules") == "False":
        return BLOCKED, (f"Windows Firewall blocks all incoming connections on this {network} "
                         "(\"Blocks all incoming connections\" is on), so the phone can't connect.")
    rules = [r for r in info.get("Rules") or [] if r.get("Enabled") == "True" and _covers_profile(r, profile)]
    blocked_by = [r for r in rules if r.get("Action") == "Block" and _covers(r, "TCP", port)]
    if blocked_by:  # a block rule wins over any allow rule
        return BLOCKED, (f"Windows Firewall blocks Screen Time on this {network} (rule \"{blocked_by[0].get('Name')}\"), "
                         f"so the phone can't connect. {allow_how(profile)}")
    default_blocks = settings.get("DefaultInboundAction") in ("Block", "NotConfigured")  # NotConfigured = Block
    allows = lambda protocol, p: any(r.get("Action") == "Allow" and _covers(r, protocol, p) for r in rules)
    if default_blocks and not allows("TCP", port):
        return WARN, (f"Windows Firewall may block the phone: no rule allows Screen Time on this {network}. "
                      f"{allow_how(profile)}")
    udp_blocked = any(r.get("Action") == "Block" and _covers(r, "UDP", discovery_port) for r in rules)
    if udp_blocked or (default_blocks and not allows("UDP", discovery_port)):
        return WARN, (f"Windows Firewall may block discovery (UDP {discovery_port}) on this {network}, so the phone "
                      "finds this PC only at the address it already knows.")
    return OK, ""


def check(ip, port, discovery_port, program=None):
    """assess() for the listener at ip:port; (UNKNOWN, why) if the firewall couldn't be asked."""
    try:
        return assess(query(ip, program or program_path()), port, discovery_port)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return UNKNOWN, f"Couldn't check Windows Firewall: {e}"
