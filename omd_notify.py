#!/usr/bin/env python3
"""Notify when aircraft with a given callsign prefix (default: OMD, Nomadic
Aviation Group) show up on ADS-B, using the free OpenSky Network API.

One request downloads every aircraft OpenSky currently sees; we then pick out
the ones whose callsign starts with the prefix.

Settings come from environment variables:
  OPENSKY_CLIENT_ID      OpenSky API client id        [required]
  OPENSKY_CLIENT_SECRET  OpenSky API client secret    [required]
  NTFY_TOPIC             ntfy.sh topic name (phone)   [optional]
  DISCORD_WEBHOOK_URL    Discord webhook URL          [optional]
  CALLSIGN_PREFIX        default OMD
  TEST                   "true" = just send a test notification
At least one of NTFY_TOPIC / DISCORD_WEBHOOK_URL is required.
"""
import json
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
STATES_URL = "https://opensky-network.org/api/states/all"
UA = "omd-notify/2.0 (personal ADS-B callsign watcher)"
PREFIX = os.environ.get("CALLSIGN_PREFIX", "OMD").strip().upper()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FORGET_AFTER = 6 * 3600   # seen again after 6h of silence = new flight


# Prefix + 1-4 digits + optional 1-2 letters (e.g. OMD123, OMD7A).
# Deliberately NOT a plain "starts with": registration-style callsigns
# such as OMDYE (aircraft OM-DYE) must not match.
CALLSIGN_RE = re.compile(rf"^{re.escape(PREFIX)}\d{{1,4}}[A-Z]{{0,2}}$")


def is_match(callsign):
    return bool(CALLSIGN_RE.match(callsign))


def _on_alarm(signum, frame):
    raise TimeoutError("request exceeded its hard time limit")


def http(url, data=None, headers=None, timeout=30):
    """HTTP request that can never hang. Returns (body_bytes, headers)."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(timeout + 15)  # catches slow-trickle responses the socket timeout misses
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers
    finally:
        signal.alarm(0)


def get_token(client_id, client_secret):
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    body, _ = http(TOKEN_URL, data=data,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(body)["access_token"]


def fetch_states(token):
    """Return (list_of_state_vectors, remaining_credits_or_None)."""
    body, hdrs = http(STATES_URL, headers={"Authorization": f"Bearer {token}"})
    return json.loads(body).get("states") or [], hdrs.get("X-Rate-Limit-Remaining")


def to_ac(s):
    """Convert an OpenSky state vector (a list) to a simple dict."""
    alt_m, vel = s[7], s[9]
    return {
        "hex": s[0],
        "flight": (s[1] or "").strip().upper(),
        "lat": s[6],
        "lon": s[5],
        "alt_baro": "ground" if s[8] else (round(alt_m * 3.28084) if alt_m is not None else None),
        "gs": round(vel * 1.94384) if vel is not None else None,
    }


def describe(ac):
    cs = (ac.get("flight") or "").strip()
    alt = ac.get("alt_baro")
    if alt == "ground":
        status = "on the ground"
    elif isinstance(alt, (int, float)):
        status = f"airborne, {int(alt):,} ft"
    else:
        status = "airborne"
    gs = ac.get("gs")
    if gs and alt != "ground":
        status += f", {int(gs)} kt"
    lines = [f"{cs} ({status})", f"Transponder: {ac.get('hex', '').upper()}"]
    if ac.get("lat") is not None and ac.get("lon") is not None:
        lines.append(f"Position: {ac['lat']:.2f}, {ac['lon']:.2f}")
    link = f"https://globe.airplanes.live/?icao={ac.get('hex', '')}"
    return cs, "\n".join(lines), link


def send_ntfy(topic, title, body, link):
    http(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Click": link, "Tags": "airplane"},
        timeout=15,
    )


def send_discord(webhook, title, body, link):
    payload = {"content": f"**{title}**\n{body}\n{link}"}
    http(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=15,
    )


def notify(title, body, link):
    """Send to every configured channel. True if at least one succeeded."""
    ok = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if topic:
        try:
            send_ntfy(topic, title, body, link)
            ok = True
        except Exception as e:  # noqa: BLE001
            print(f"ntfy failed: {e}", file=sys.stderr)
    if hook:
        try:
            send_discord(hook, title, body, link)
            ok = True
        except Exception as e:  # noqa: BLE001
            print(f"discord failed: {e}", file=sys.stderr)
    return ok


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    if not (os.environ.get("NTFY_TOPIC", "").strip()
            or os.environ.get("DISCORD_WEBHOOK_URL", "").strip()):
        print("No NTFY_TOPIC or DISCORD_WEBHOOK_URL set.", file=sys.stderr)
        return 1

    if os.environ.get("TEST", "").lower() == "true":
        ok = notify(f"{PREFIX} watcher test", "If you can read this, notifications work.",
                    "https://globe.airplanes.live/")
        print("Test sent." if ok else "Test failed.")
        return 0 if ok else 1

    cid = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
    csec = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        print("OPENSKY_CLIENT_ID / OPENSKY_CLIENT_SECRET secrets are not set.", file=sys.stderr)
        return 1

    # 1) log in, 2) download all aircraft (one retry on network trouble)
    states, remaining = None, None
    for attempt in range(2):
        try:
            token = get_token(cid, csec)
            t0 = time.time()
            states, remaining = fetch_states(token)
            print(f"OpenSky OK in {time.time() - t0:.1f}s: {len(states)} aircraft worldwide, "
                  f"credits left today: {remaining}", flush=True)
            break
        except urllib.error.HTTPError as e:
            print(f"OpenSky HTTP error {e.code}: {e.reason}", file=sys.stderr, flush=True)
            if e.code in (401, 403, 429):
                break  # retrying won't help (bad credentials / out of credits / blocked)
        except Exception as e:  # noqa: BLE001
            print(f"OpenSky request failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        if attempt == 0:
            time.sleep(3)
    if states is None:
        print("Could not get data; keeping old state.", file=sys.stderr)
        return 1

    now = time.time()
    state = load_state()
    found = {}
    for s in states:
        cs = (s[1] or "").strip().upper()
        if is_match(cs):
            ac = to_ac(s)
            found[f"{ac['hex']}:{cs}"] = ac

    sent = 0
    for key, ac in found.items():
        last = state.get(key)
        if last is None or now - last > FORGET_AFTER:
            cs, body, link = describe(ac)
            if notify(f"{cs} spotted", body, link):
                state[key] = now
                sent += 1
        else:
            state[key] = now  # still flying; refresh

    state = {k: v for k, v in state.items() if now - v < 7 * 86400}
    save_state(state)
    print(f"{len(found)} {PREFIX} aircraft right now, {sent} notifications sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
