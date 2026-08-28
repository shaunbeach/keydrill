#!/usr/bin/env python3
"""End-to-end tests. Start the server first, then: python3 tests.py

Covers the learn engine, all four decks, answer checking, and the Hyprland
submap lifecycle. The submap tests run against the live compositor rather than
a mock, because the failure being guarded against - a desktop with no working
keybindings - only exists there.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8777"
FAILS = []


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


def post(path, data=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(data or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def submap():
    p = subprocess.run(["hyprctl", "submap"], capture_output=True, text=True)
    return p.stdout.strip()


def chk(label, got, want=True):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + "%s: %r" % (label, got)
          + ("" if ok else " (want %r)" % (want,)))
    if not ok:
        FAILS.append(label)


def section(name):
    print("\n== %s ==" % name)


# --------------------------------------------------------------------------

def test_decks():
    section("decks load")
    d = get("/api/decks")["decks"]
    chk("four decks", len(d), 4)
    for x in d:
        print("    %-14s %4d items, %d categories"
              % (x["deck"], x["total"], len(x["categories"])))
        chk("%s non-empty" % x["deck"], x["total"] > 0)


def test_learn_progression():
    section("learn: guided -> recall -> graduated")
    it = get("/api/learn/next?deck=term&category=git")["item"]
    r = get("/api/learn/next?deck=term&category=git")
    chk("starts guided", r["meta"]["stage"], 1)
    a = post("/api/attempt", {"item_id": it["id"], "correct": True,
                              "mode": "learn", "latency_ms": 500})
    chk("promoted to recall", a["learn"]["stage"], 2)
    a = post("/api/attempt", {"item_id": it["id"], "correct": True,
                              "mode": "learn", "latency_ms": 500})
    chk("one clean recall is not enough", a["learn"]["graduated"], False)
    a = post("/api/attempt", {"item_id": it["id"], "correct": True,
                              "mode": "learn", "latency_ms": 500})
    chk("graduates on the second", a["learn"]["graduated"], True)


def test_learn_demotion():
    section("learn: getting it wrong drops back to guided")
    it = get("/api/learn/next?deck=term&category=files")["item"]
    post("/api/attempt", {"item_id": it["id"], "correct": True,
                          "mode": "learn", "latency_ms": 500})
    a = post("/api/attempt", {"item_id": it["id"], "correct": False,
                              "mode": "learn", "latency_ms": 500})
    chk("demoted to stage 1", a["learn"]["stage"], 1)


def test_batch_size():
    section("learn: batch stays small")
    seen = set()
    for _ in range(14):
        r = get("/api/learn/next?deck=nvim_motion&category=motions")
        if r["item"]:
            seen.add(r["item"]["id"])
            post("/api/attempt", {"item_id": r["item"]["id"], "correct": False,
                                  "mode": "learn", "latency_ms": 300})
    chk("at most 5 in flight", len(seen) <= 5)


def test_skip_advances():
    section("learn: skip actually moves on")
    ids, cur = [], None
    for _ in range(6):
        q = "/api/learn/next?deck=hypr&category=all"
        if cur:
            q += "&exclude=" + cur
        it = get(q)["item"]
        ids.append(it["id"])
        post("/api/learn/skip", {"item_id": it["id"]})
        cur = it["id"]
    chk("no consecutive repeat",
        all(ids[i] != ids[i + 1] for i in range(len(ids) - 1)))
    chk("rotates through several", len(set(ids)) >= 4)


def test_sibling_answers():
    section("prompts with more than one valid answer")
    target = None
    for _ in range(200):
        it = get("/api/learn/next?deck=hypr&category=applications")["item"]
        if it["prompt"] == "Browser":
            target = it
            break
        post("/api/learn/skip", {"item_id": it["id"]})
    if not target:
        print("    (no ambiguous prompt on this machine - skipping)")
        return
    chk("both chords offered", len(target.get("answers", [])), 2)
    for chord in target["answers"]:
        chk("%s accepted" % chord,
            post("/api/check", {"item_id": target["id"], "given": chord})["correct"])
    chk("wrong chord rejected",
        post("/api/check", {"item_id": target["id"], "given": "CTRL + Q"})["correct"],
        False)


def test_text_checking():
    section("terminal answers")
    cases = [
        ("t-tar-c", "tar czf out.tar.gz dir", True),
        ("t-tar-c", "tar -czf out.tar.gz dir", True),
        ("t-tar-c", "tar  czf   out.tar.gz dir", True),
        ("t-tar-c", "tar xzf out.tar.gz", False),
        ("t-git-commit", 'git commit -m "fix bug"', True),
    ]
    for item_id, given, want in cases:
        chk("%-28s -> %s" % (given, want),
            post("/api/check", {"item_id": item_id, "given": given})["correct"],
            want)


def test_nvim():
    section("neovim motions run in real nvim")
    r = post("/api/nvim/check", {"item_id": "nv-ciw", "keys": "ciwname\x1b"})
    chk("ciw solution passes", r["correct"])
    r = post("/api/nvim/check", {"item_id": "nv-ciw", "keys": "dw"})
    chk("wrong keys fail", r["correct"], False)
    r = post("/api/nvim/check", {"item_id": "nv-percent", "keys": "%"})
    chk("cursor-only motion passes", r["correct"])
    r = post("/api/nvim/check", {"item_id": "nv-percent", "keys": "w"})
    chk("cursor-only wrong spot fails", r["correct"], False)


def test_drill_hides_answers():
    section("drill never leaks the answer")
    for deck in ("hypr", "nvim_keys", "nvim_motion", "term"):
        it = get("/api/drill/next?deck=%s&category=all" % deck)["item"]
        chk("%s hides answer" % deck, "answer" in it, False)
        chk("%s hides answers" % deck, "answers" in it, False)


def test_submap():
    section("submap lifecycle against live Hyprland")
    chk("starts clean", submap(), "default")
    post("/api/submap/enter")
    chk("enters", submap(), "keydrill")
    chk("heartbeat reports active", post("/api/heartbeat")["active"])
    post("/api/submap/exit")
    chk("exits", submap(), "default")

    print("    watchdog: going silent for 4.5s (timeout is 3.0s)")
    post("/api/submap/enter")
    time.sleep(4.5)
    chk("watchdog released it", submap(), "default")


def main():
    try:
        get("/api/decks")
    except (urllib.error.URLError, OSError):
        print("Server is not running. Start it with:\n"
              "  python3 keydrill.py --no-open")
        return 2

    for fn in (test_decks, test_learn_progression, test_learn_demotion,
               test_batch_size, test_skip_advances, test_sibling_answers,
               test_text_checking, test_nvim, test_drill_hides_answers,
               test_submap):
        fn()

    print("\n" + ("ALL PASSED" if not FAILS else "FAILURES: %s" % FAILS))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
