#!/usr/bin/env python3
"""Notify when aircraft with a given callsign prefix (default: OMD, Nomadic
Aviation Group) show up on ADS-B, using the free airplanes.live API.

airplanes.live only matches EXACT callsigns, so we ask for every plausible
callsign (OMD1 ... OMD999, OMD1A ... OMD99Z) in a few batched requests.

Settings come from environment variables:
  NTFY_TOPIC           ntfy.sh topic name (phone push)      [optional]
  DISCORD_WEBHOOK_URL  Discord webhook URL                  [optional]
  CALLSIGN_PREFIX      default OMD
  MAX_NUMBER           highest flight number to check, default 999
  TEST                 set to "true" to just send a test notification
At least one of NTFY_TOPIC / DISCORD_WEBHOOK_URL is required.
"""
import json
import os
import string
import sys
import time
import urllib.error
import urllib.request

API = "https://api.airplanes.live/v2/callsign/"
UA = "omd-notify/1.0 (personal ADS-B callsign watcher)"
PREFIX = os.environ.get("CALLSIGN_PREFIX", "OMD").strip().upper()
MAX_NUMBER = int(os.environ.get("MAX_NUMBER", "999"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FORGET_AFTER = 6 * 3600   # seen again after 6h of silence = new flight
MAX_CHUNK_CHARS = 7000    # API limit is 8000 characters per request
MAX_CHUNK_ITEMS = 800     # API limit is 1000 callsigns per request


def candidates(prefix=PREFIX, max_number=MAX_NUMBER):
    nums = [f"{prefix}{n}" for n in range(1, max_number + 1)]
    letters = [f"{prefix}{n}{c}" for n in range(1, 100) for c in string.ascii_uppercase]
    return nums + letters


def chunk(items, max_chars=MAX_CHUNK_CHARS, max_items=MAX_CHUNK_ITEMS):
    out, cur, size = [], [], 0
    for it in items:
        if cur and (size + len(it) + 1 > max_chars or len(cur) >= max_items):
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it) + 1
    if cur:
        out.append(cur)
    return out


def http(url, data=None, headers=None, timeout=20):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch(callsigns):
    url = API + ",".join(callsigns)
    for attempt in range(3):
        try:
            return json.loads(http(url)).get("ac", [])
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(5)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(3)
                continue
            raise
    return []


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
    lines = [f"{cs} ({status})"]
    ident = " / ".join(x for x in (ac.get("r"), ac.get("t"), ac.get("desc")) if x)
    if ident:
        lines.append(ident)
    if ac.get("lat") is not None and ac.get("lon") is not None:
        lines.append(f"Position: {ac['lat']:.2f}, {ac['lon']:.2f}")
    link = f"https://globe.airplanes.live/?icao={ac.get('hex', '')}"
    return cs, "\n".join(lines), link


def send_ntfy(topic, title, body, link):
    http(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Click": link, "Tags": "airplane"},
    )


def send_discord(webhook, title, body, link):
    payload = {"content": f"**{title}**\n{body}\n{link}"}
    http(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
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

    now = time.time()
    state = load_state()
    found, failures, chunks = {}, 0, chunk(candidates())
    for i, c in enumerate(chunks):
        if i:
            time.sleep(1.1)  # API limit: 1 request/second
        try:
            for ac in fetch(c):
                cs = (ac.get("flight") or "").strip().upper()
                if cs.startswith(PREFIX) and ac.get("hex"):
                    found[f"{ac['hex']}:{cs}"] = ac
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"request {i + 1}/{len(chunks)} failed: {e}", file=sys.stderr)

    if failures == len(chunks):
        print("All requests failed; keeping old state.", file=sys.stderr)
        return 1

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
    print(f"Checked {len(chunks)} batches, {len(found)} aircraft, {sent} notifications.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

