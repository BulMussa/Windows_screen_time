"""Tests for screentime.firewall: assess() on made-up firewall facts for every verdict, and one real,
read-only query of this PC's firewall (it changes nothing)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from screentime import firewall  # noqa: E402


def check(cond, name):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit(1)


def profiles(enabled="True", default="Block", allow_rules="True"):
    return [{"Name": n, "Enabled": enabled, "DefaultInboundAction": default, "AllowInboundRules": allow_rules}
            for n in ("Domain", "Private", "Public")]


def rule(action="Allow", profile="Private, Public", protocol="TCP", port="Any", enabled="True", name="Screen Time"):
    return {"Name": name, "Enabled": enabled, "Action": action, "Profile": profile, "Protocol": protocol, "LocalPort": port}


ALLOWED = [rule(), rule(protocol="UDP")]  # what Windows' own prompt makes


def verdict(category="Private", profs=None, rules=None):
    return firewall.assess({"Category": category, "Profiles": profs or profiles(),
                            "Rules": ALLOWED if rules is None else rules}, 47613, 47614)


check(verdict() == ("ok", ""), "allowed by the usual TCP and UDP rules: nothing to say")
check(verdict(rules=[rule(protocol="Any")]) == ("ok", ""), "one rule for any protocol covers both")
check(verdict(profs=profiles(enabled="False"), rules=[]) == ("ok", ""), "firewall off for this network: fine")
check(verdict(profs=profiles(default="Allow"), rules=[]) == ("ok", ""), "inbound allowed by default: fine")
check(verdict(category="DomainAuthenticated", rules=[rule(profile="Domain"), rule(profile="Domain", protocol="UDP")])
      == ("ok", ""), "a domain network uses the Domain profile")
check(verdict(rules=[rule(port="47613"), rule(protocol="UDP", port="47000-48000")]) == ("ok", ""),
      "rules limited to the right ports and port ranges count")

v, msg = verdict(profs=profiles(allow_rules="False"))
check(v == "blocked" and "blocks all incoming connections" in msg, f"shields up: blocked ({msg})")
v, msg = verdict(rules=ALLOWED + [rule(action="Block", protocol="Any", name="Screen Time (blocked)")])
check(v == "blocked" and "Screen Time (blocked)" in msg and "Allow an app through firewall" in msg,
      f"a block rule wins over allow rules and is named: {msg}")
v, msg = verdict(rules=[rule(action="Block", protocol="TCP")])
check(v == "blocked", "a block rule (as made when the prompt is cancelled) blocks")
v, msg = verdict(rules=[rule(action="Block", enabled="False")] + ALLOWED)
check(v == "ok", "a disabled block rule doesn't count")

v, msg = verdict(category="Public", rules=[rule(profile="Private"), rule(profile="Private", protocol="UDP")])
check(v == "warn" and "public network" in msg and "for Public networks" in msg and "set it to Private" in msg,
      f"allowed only on private networks, but this one is public: says what to do ({msg})")
v, msg = verdict(category="Private", rules=[])
check(v == "warn" and "no rule allows Screen Time on this private network" in msg and "set it to Private" not in msg,
      f"no rule at all: may block ({msg})")
v, msg = verdict(category=None, rules=[rule(profile="Private")])
check(v == "warn" and "public network" in msg, "an unknown network is treated as public")
v, msg = verdict(rules=[rule(port="80")])
check(v == "warn", "a rule for another port doesn't count")

v, msg = verdict(rules=[rule()])
check(v == "warn" and "discovery (UDP 47614)" in msg, f"TCP allowed, UDP not: discovery may be blocked ({msg})")
v, msg = verdict(rules=ALLOWED + [rule(action="Block", protocol="UDP")])
check(v == "warn" and "discovery" in msg, "a UDP block rule: discovery blocked, the connection still works")

check(firewall.assess({"Category": "Private", "Profiles": [], "Rules": []}, 47613, 47614)[0] == "unknown",
      "no profile facts: unknown, no message")

# --- the real query on this PC (read only) ---
exe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "ScreenTime.exe")
start = time.time()
v, msg = firewall.check("127.0.0.1", 47613, 47614, exe)
took = time.time() - start
check(v in ("ok", "warn", "blocked", "unknown") and took < firewall.TIMEOUT + 5,
      f"the real query answers ({v}, {took:.1f} s): {msg or 'no message'}")
facts = firewall.query("127.0.0.1", exe)
check(isinstance(facts.get("Profiles"), list) and len(facts["Profiles"]) == 3 and isinstance(facts.get("Rules"), list),
      f"facts: 3 profiles, {len(facts['Rules'])} rules for ScreenTime.exe")
print("ALL FIREWALL CHECKS PASSED")
