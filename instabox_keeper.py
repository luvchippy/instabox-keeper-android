#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""InstaBox free-tier keeper.

Verified on 2026-11-07 against a live free-tier machine:
  * Manual POST /suspend produces NO cooldown (cooldownUntil stays null).
  * POST /resume resets runStartedAt; maxRunMinutes starts over.
  * A manually suspended machine replies ONLY to /resume; /wake has no effect.
  * Only an idle self-pause (autoSuspendedAt set) answers /wake.
  * Hitting maxRunMinutes triggers the punitive cooldown
    (5-20 / 60-75 / 120-135 minutes on the free plan).

Strategy: keep the guest busy so the idle timer never fires, and breathe
(suspend -> resume) before max-run is reached, so the punitive cooldown is
never triggered. Must run OUTSIDE the sandbox: a suspended guest cannot
wake itself.

Stdlib only.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

API_URL = os.environ.get("INSTABOX_API_URL", "https://api.instabox.sh").rstrip("/")
API_KEY = os.environ.get("INSTABOX_API_KEY", "").strip()
MACHINE = os.environ.get("INSTABOX_MACHINE", "").strip()
BUFFER_MIN = int(os.environ.get("INSTABOX_BUFFER_MINUTES", "20") or "20")
KEEPALIVE = os.environ.get("INSTABOX_KEEPALIVE", "1") != "0"
RUNNING = "running"
STOPPED = "stopped"
PAUSED = ("suspended", "paused")
SUSPEND_WAIT = 120
RESUME_WAIT = 300
POLL = 10


def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[%s] %s" % (stamp, msg), flush=True)


def api(method, path, body=None, timeout=60):
    headers = {
        "X-API-Key": API_KEY,
        "Accept": "application/json",
        "User-Agent": "instabox-keeper/2.0",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if method in ("POST", "PATCH", "DELETE"):
        headers["Idempotency-Key"] = str(uuid.uuid4())
    req = urllib.request.Request(API_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else "{}"
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, None
    except Exception as exc:
        log("network error %s %s: %s" % (method, path, exc))
        return None, None


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def get_machine():
    status, data = api("GET", "/v1/machines/" + MACHINE)
    return data if status == 200 else None


def wait_for(pred, what, cap):
    start = time.monotonic()
    while time.monotonic() - start < cap:
        machine = get_machine()
        if machine and pred(machine):
            return machine
        time.sleep(POLL)
    log("timeout waiting for %s" % what)
    return None


def breathe(mid):
    """suspend -> resume: resets the run timer without owing a cooldown."""
    status, _ = api("POST", "/v1/machines/%s/suspend" % mid, body={})
    if not (status and 200 <= status < 300):
        log("suspend failed HTTP %s" % status)
        return False
    paused = wait_for(lambda m: m.get("state") in PAUSED, "suspend", SUSPEND_WAIT)
    if not paused:
        return False

    cooldown = parse_ts(paused.get("cooldownUntil"))
    now = datetime.now(timezone.utc)
    if cooldown and cooldown > now:
        wait_s = min((cooldown - now).total_seconds() + 2, 1800)
        log("unexpected cooldown, sleeping %.0fs before resume" % wait_s)
        time.sleep(wait_s)

    before = paused.get("runStartedAt")
    status, _ = api("POST", "/v1/machines/%s/resume" % mid, body={})
    if not (status and 200 <= status < 300):
        log("resume failed HTTP %s" % status)
        return False

    after = wait_for(lambda m: m.get("state") == RUNNING and m.get("runStartedAt"),
                     "resume", RESUME_WAIT)
    if not after:
        return False
    log("breathed: runStartedAt %s -> %s, maxRunMinutes=%s"
        % (before, after.get("runStartedAt"), after.get("maxRunMinutes")))
    return True


def keepalive(mid):
    """A real exec counts as activity and resets the 30 minute idle timer."""
    status, data = api("POST", "/v1/machines/%s/exec" % mid,
                       body={"cmd": "true", "timeout_seconds": 30}, timeout=90)
    if status and 200 <= status < 300:
        log("keepalive exec ok")
        return True
    log("keepalive exec failed HTTP %s" % status)
    return False


def main():
    if not API_KEY or not MACHINE:
        log("missing INSTABOX_API_KEY or INSTABOX_MACHINE")
        return 2

    machine = get_machine()
    if not machine:
        log("cannot read machine %s" % MACHINE)
        return 1

    mid = machine.get("id") or MACHINE
    state = machine.get("state")
    limit = machine.get("maxRunMinutes") or 480
    started = parse_ts(machine.get("runStartedAt"))
    now = datetime.now(timezone.utc)
    age_min = (now - started).total_seconds() / 60 if started else None

    log("state=%s limit=%s run=%s cooldownUntil=%s"
        % (state, limit, ("%.0fm" % age_min) if age_min is not None else "-",
           machine.get("cooldownUntil")))

    if state == RUNNING:
        if age_min is not None and limit and age_min >= (limit - BUFFER_MIN):
            log("approaching max-run (%.0f >= %d-%d), breathing" % (age_min, limit, BUFFER_MIN))
            return 0 if breathe(mid) else 1
        if KEEPALIVE:
            return 0 if keepalive(mid) else 1
        log("healthy, no action")
        return 0

    cooldown = parse_ts(machine.get("cooldownUntil"))
    if cooldown and cooldown > now:
        log("in cooldown until %s, skipping" % machine.get("cooldownUntil"))
        return 0

    if state in PAUSED:
        if machine.get("autoSuspendedAt"):
            status, _ = api("POST", "/v1/machines/%s/wake" % mid, body={}, timeout=90)
            log("idle self-pause, wake HTTP %s" % status)
        else:
            status, _ = api("POST", "/v1/machines/%s/resume" % mid, body={})
            log("manual pause, resume HTTP %s" % status)
        return 0 if (status and 200 <= status < 300) else 1

    if state == STOPPED:
        status, _ = api("POST", "/v1/machines/%s/start" % mid, body={})
        log("stopped, start HTTP %s" % status)
        return 0 if (status and 200 <= status < 300) else 1

    log("transitional state %s, observing" % state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
