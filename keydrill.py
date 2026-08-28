#!/usr/bin/env python3
"""keydrill - learn and drill Hyprland, Neovim and terminal skills.

Two loops over the same material:

  learn  introduces an item with the answer visible, has you perform it, then
         hides the answer and asks again. Graduates after two clean recalls.
  drill  pure recall, weighted toward what you get wrong or answer slowly.

Hotkey chords are captured by holding Hyprland in a submap so the compositor
doesn't act on them. Neovim motions are checked by running real headless nvim.

Stdlib only. Run it, open the URL it prints.
"""

import atexit
import json
import os
import random
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import decks
import nvimrun

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = ROOT / "keydrill.db"

SUBMAP_NAME = "keydrill"
HEARTBEAT_TIMEOUT = 3.0
DEFAULT_PORT = 8777

# How many items are being actively learned at once. Small on purpose - a
# batch you can hold in your head beats a list you can only skim.
LEARN_BATCH = 5
# Clean recalls needed before an item stops appearing in learn mode.
GRADUATE_AT = 2


# --------------------------------------------------------------------------
# Hyprland submap
# --------------------------------------------------------------------------

def hypr(args, timeout=5):
    try:
        p = subprocess.run(["hyprctl"] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip()
    except Exception as exc:                                  # noqa: BLE001
        return False, str(exc)


def hypr_eval(lua):
    return hypr(["eval", lua])


class SubmapGuard:
    """Owns the Hyprland submap, and guarantees we leave it.

    Every path out goes through here: the user stopping, the browser tab
    dying, the process being signalled, or an unhandled crash.
    """

    def __init__(self):
        self.active = False
        self.last_beat = 0.0
        self.lock = threading.RLock()
        self.defined = False

    def define(self):
        lua = ('hl.define_submap("%s", function()\n'
               '  hl.bind("ESCAPE", hl.dsp.submap("reset"))\n'
               'end)' % SUBMAP_NAME)
        ok, out = hypr_eval(lua)
        self.defined = ok
        return ok, out

    def enter(self):
        with self.lock:
            if not self.defined:
                ok, out = self.define()
                if not ok:
                    return False, "could not define submap: %s" % out
            ok, out = hypr_eval('hl.dispatch(hl.dsp.submap("%s"))' % SUBMAP_NAME)
            if not ok:
                return False, out
            self.active = True
            self.last_beat = time.time()
            return True, "entered"

    def leave(self, reason="requested"):
        with self.lock:
            if not self.active:
                return True, "not active"
            ok, out = hypr_eval('hl.dispatch(hl.dsp.submap("reset"))')
            self.active = False
            if reason != "requested":
                print("[keydrill] left submap (%s)" % reason, flush=True)
            return ok, out

    def beat(self):
        with self.lock:
            if self.active:
                self.last_beat = time.time()

    def reconcile(self):
        """Notice an ESC exit.

        Pressing ESC leaves the submap at the compositor, which never tells
        us. Compare our belief against Hyprland's truth and yield to it.
        """
        with self.lock:
            if not self.active:
                return False
            if self.current() != SUBMAP_NAME:
                self.active = False
                return True
            return False

    def current(self):
        ok, out = hypr(["submap"])
        return out if ok else "?"

    def watchdog(self):
        while True:
            time.sleep(0.5)
            with self.lock:
                stale = (self.active
                         and time.time() - self.last_beat > HEARTBEAT_TIMEOUT)
            if stale:
                self.leave("heartbeat lost")


GUARD = SubmapGuard()


def panic_exit(*_args):
    try:
        GUARD.leave("shutdown")
    finally:
        os._exit(0)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    item_id    TEXT NOT NULL,
    deck       TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    answer     TEXT NOT NULL,
    mode       TEXT NOT NULL DEFAULT 'drill',
    correct    INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_item ON attempts(item_id, ts DESC);

CREATE TABLE IF NOT EXISTS learning (
    item_id   TEXT PRIMARY KEY,
    deck      TEXT NOT NULL,
    stage     INTEGER NOT NULL DEFAULT 1,
    streak    INTEGER NOT NULL DEFAULT 0,
    seen      INTEGER NOT NULL DEFAULT 0,
    graduated INTEGER NOT NULL DEFAULT 0,
    updated   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS typing_runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    source   TEXT NOT NULL,
    wpm      REAL NOT NULL,
    accuracy REAL NOT NULL,
    chars    INTEGER NOT NULL,
    seconds  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS key_stats (
    ch     TEXT PRIMARY KEY,
    hits   INTEGER NOT NULL DEFAULT 0,
    misses INTEGER NOT NULL DEFAULT 0
);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Item registry
# --------------------------------------------------------------------------

class Registry:
    """Holds every item, refreshed from the live system on demand."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            self.items = decks.load_all()
        return len(self.items)

    def get(self, item_id):
        return self.items.get(item_id)

    def select(self, deck=None, category=None):
        out = []
        for it in self.items.values():
            if deck and it["deck"] != deck:
                continue
            if category and category != "all" and it["category"] != category:
                continue
            out.append(it)
        return out


REG = Registry()


def siblings(item):
    """Every item in the same deck answering the same prompt.

    Omarchy binds both SUPER+SHIFT+RETURN and SUPER+SHIFT+B to "Browser", and
    LazyVim double-binds several of its own. Either chord is a correct answer
    to that prompt, so accept all of them and show all of them.
    """
    return [i for i in REG.items.values()
            if i["deck"] == item["deck"] and i["prompt"] == item["prompt"]]


def all_answers(item):
    seen, out = set(), []
    for sb in siblings(item):
        if sb["answer"] not in seen:
            seen.add(sb["answer"])
            out.append(sb["answer"])
    return out


def public(item):
    """Strip the answer for drill mode; learn mode asks for it explicitly."""
    keep = ("id", "deck", "category", "prompt", "input", "hint")
    out = {k: item[k] for k in keep if k in item}
    if item["input"] == "nvim":
        out["start"] = item["start"]
        out["cursor"] = item["cursor"]
        out["goal"] = item["goal"]
        out["goal_cursor"] = item.get("goal_cursor")
    return out


# --------------------------------------------------------------------------
# Learn engine
# --------------------------------------------------------------------------

def learn_state(item_ids):
    if not item_ids:
        return {}
    marks = ",".join("?" * len(item_ids))
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM learning WHERE item_id IN (%s)" % marks,
            item_ids).fetchall()
    return {r["item_id"]: dict(r) for r in rows}


def learn_next(deck, category, exclude=None):
    """Next item to learn: finish the active batch before opening a new one."""
    pool = REG.select(deck, category)
    if not pool:
        return None, {}

    state = learn_state([i["id"] for i in pool])
    active = [i for i in pool
              if i["id"] in state and not state[i["id"]]["graduated"]]
    fresh = [i for i in pool if i["id"] not in state]

    while len(active) < LEARN_BATCH and fresh:
        active.append(fresh.pop(0))

    if not active:
        return None, {"done": True}

    # Least recently touched first, so the batch rotates rather than
    # hammering one item until it sticks.
    active.sort(key=lambda i: state.get(i["id"], {}).get("updated", 0.0))
    # Skipping must actually move on. Without this the same item comes back
    # forever, because skipping never updates the ordering key.
    if exclude and len(active) > 1:
        active = [i for i in active if i["id"] != exclude] or active
    item = active[0]
    st = state.get(item["id"], {"stage": 1, "streak": 0, "graduated": 0})

    total = len(pool)
    grad = sum(1 for i in pool
               if state.get(i["id"], {}).get("graduated"))
    return item, {"stage": st["stage"], "streak": st["streak"],
                  "learned": grad, "total": total,
                  "batch": len(active)}


def learn_record(item, correct):
    """Advance or reset an item's learning stage."""
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM learning WHERE item_id=?",
                           (item["id"],)).fetchone()
        if row is None:
            stage, streak, seen, grad = 1, 0, 0, 0
        else:
            stage, streak = row["stage"], row["streak"]
            seen, grad = row["seen"], row["graduated"]

        seen += 1
        if stage <= 1:
            # Guided stage: performing it correctly once is enough to move on.
            if correct:
                stage, streak = 2, 0
        else:
            if correct:
                streak += 1
                if streak >= GRADUATE_AT:
                    grad = 1
            else:
                stage, streak = 1, 0      # back to seeing the answer

        conn.execute("""
            INSERT INTO learning (item_id, deck, stage, streak, seen,
                                  graduated, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                stage=excluded.stage, streak=excluded.streak,
                seen=excluded.seen, graduated=excluded.graduated,
                updated=excluded.updated
        """, (item["id"], item["deck"], stage, streak, seen, grad, now))

    return {"stage": stage, "streak": streak, "graduated": bool(grad)}


# --------------------------------------------------------------------------
# Drill scheduling
# --------------------------------------------------------------------------

def drill_pick(deck, category, exclude=None):
    pool = REG.select(deck, category)
    if not pool:
        return None

    now = time.time()
    stats = {}
    with db() as conn:
        rows = conn.execute("""
            SELECT item_id, COUNT(*) n, SUM(correct) ok,
                   AVG(latency_ms) avg_ms, MAX(ts) last_ts
            FROM (SELECT * FROM attempts WHERE deck=?
                  ORDER BY ts DESC LIMIT 600)
            GROUP BY item_id
        """, (deck,)).fetchall()
        for r in rows:
            stats[r["item_id"]] = r

    weighted = []
    for it in pool:
        if exclude and it["id"] == exclude:
            continue
        s = stats.get(it["id"])
        if s is None:
            weighted.append((it, 6.0))
            continue
        n = s["n"] or 1
        miss = 1.0 - ((s["ok"] or 0) / n)
        w = 1.0 + 5.0 * miss
        avg = s["avg_ms"] or 0
        if avg > 4000:
            w += 1.5
        elif avg > 2000:
            w += 0.6
        idle_h = max(0.0, (now - (s["last_ts"] or now)) / 3600.0)
        w += min(2.0, idle_h * 0.5)
        weighted.append((it, max(0.15, w)))

    if not weighted:
        return None
    total = sum(w for _, w in weighted)
    r = random.uniform(0, total)
    upto = 0.0
    for it, w in weighted:
        upto += w
        if upto >= r:
            return it
    return weighted[-1][0]


# --------------------------------------------------------------------------
# Typing corpora
# --------------------------------------------------------------------------

CORPORA = {
    "symbols": [
        "{ } [ ] ( ) < > | \\ / ~ ` ! @ # $ % ^ & * - _ = + ; : ' \" , . ?",
        "$HOME/.config/hypr/bindings.lua",
        "${VAR:-default} && echo \"$?\" || exit 1",
        "if [[ -f \"$f\" ]]; then rm -- \"$f\"; fi",
        "grep -rnE '^\\s*bind' ~/.config/ | awk -F: '{print $1}'",
        "local t = {} ; t[#t+1] = k .. \" (\" .. type(v) .. \")\"",
        "cmd | tee >(wc -l) 2>&1 | sed -n '1,20p'",
        "for i in {1..9}; do printf '%02d\\n' \"$i\"; done",
        "@media (prefers-color-scheme: dark) { :root { --bg: #111; } }",
        "hl.bind(\"SUPER + W\", hl.dsp.window({ action = \"close\" }))",
        "path = os.getenv(\"PATH\") or \"/usr/local/bin:/usr/bin\"",
        "curl -sS \"https://x.dev/api?q=1&n=2\" | jq '.data[] | .id'",
        "sed -i 's/\\(foo\\)/[\\1]/g' *.txt && git diff --stat",
        "def f(*args, **kw) -> dict[str, int]: return {**kw}",
        "~/.local/share/omarchy/default/hypr/*.lua",
    ],
    "lua": [
        "o.bind(\"SUPER + RETURN\", \"Terminal\", { omarchy = \"terminal\" })",
        "local function shell_quote(value) return \"'\" .. value .. \"'\" end",
        "for directory in (path .. \":\"):gmatch(\"([^:]*):\") do",
        "if _G.omarchy_preinstalled_bindings ~= nil then return true end",
        "hl.define_submap(\"keydrill\", function() hl.bind(\"ESCAPE\", r) end)",
        "return output:find(\"OK\", 1, true) ~= nil",
        "local f = io.open(path, \"r\") ; if f then f:close() end",
        "table.sort(t) ; return table.concat(t, \", \")",
        "hl.env(\"XDG_SESSION_TYPE\", \"wayland\")",
        "opts.description = description or \"\"",
    ],
    "shell": [
        "systemctl --user restart waybar.service",
        "hyprctl -j binds | jq -r '.[] | .description'",
        "find . -type f -name '*.lua' -newermt '-2 days'",
        "rsync -avz --delete ~/src/ user@host:/srv/app/",
        "git rebase -i HEAD~3 && git push --force-with-lease",
        "pacman -Qi ktouch | grep -E 'Version|Size'",
        "ps aux | awk '$3 > 5.0 {print $2, $11}'",
        "tar czf backup-$(date +%F).tar.gz ~/.config/",
        "export PATH=\"$HOME/.local/bin:$PATH\"",
        "ln -sfn ~/Projects/keydrill ~/.local/share/keydrill",
    ],
    "prose": [
        "the quick brown fox jumps over the lazy dog every single time",
        "practice does not make perfect it makes permanent so practice well",
        "typing speed comes from rhythm and accuracy rather than raw hurry",
        "a steady pace with no errors beats a fast burst full of mistakes",
        "keep your wrists level and let your fingers do the walking around",
    ],
}


def weak_keys(limit=8):
    with db() as conn:
        rows = conn.execute("""
            SELECT ch, CAST(misses AS REAL)/(hits+misses) AS rate
            FROM key_stats WHERE hits + misses >= 5
            ORDER BY rate DESC LIMIT ?
        """, (limit,)).fetchall()
    return [r["ch"] for r in rows if r["rate"] > 0.02]


def build_drill(source="symbols", lines=4):
    pool = list(CORPORA.get(source, CORPORA["symbols"]))
    weak = set(weak_keys())
    if weak:
        pool.sort(key=lambda l: sum(1 for c in l if c in weak), reverse=True)
        head = pool[:max(3, len(pool) // 2)]
        chosen = [random.choice(head) for _ in range(lines)]
    else:
        chosen = random.sample(pool, min(lines, len(pool)))
        while len(chosen) < lines:
            chosen.append(random.choice(pool))
    return {"text": "\n".join(chosen), "weak_keys": sorted(weak)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "keydrill"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            return {}

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        one = lambda k, d=None: (q.get(k) or [d])[0]      # noqa: E731

        if u.path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")

        if u.path == "/api/decks":
            return self._send(200, self._decks())

        if u.path == "/api/learn/next":
            item, meta = learn_next(one("deck"), one("category"),
                                    one("exclude"))
            if item is None:
                return self._send(200, {"item": None, "meta": meta})
            payload = public(item)
            # Learn mode may reveal it - show every chord that would count.
            payload["answer"] = item["answer"]
            payload["answers"] = all_answers(item)
            return self._send(200, {"item": payload, "meta": meta})

        if u.path == "/api/drill/next":
            item = drill_pick(one("deck"), one("category"), one("exclude"))
            if item is None:
                return self._send(200, {"item": None})
            return self._send(200, {"item": public(item)})

        if u.path == "/api/reveal":
            item = REG.get(one("id"))
            if not item:
                return self._send(404, {"error": "unknown item"})
            return self._send(200, {"answer": item["answer"],
                                    "hint": item.get("hint", "")})

        if u.path == "/api/drill":
            return self._send(200, build_drill(one("source", "symbols")))

        if u.path == "/api/stats":
            return self._send(200, self._stats())

        if u.path == "/api/submap/status":
            return self._send(200, {"active": GUARD.active,
                                    "current": GUARD.current()})

        return self._send(404, {"error": "not found"})

    # ---------------- POST ----------------
    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()

        if u.path == "/api/submap/enter":
            ok, msg = GUARD.enter()
            return self._send(200 if ok else 500,
                              {"ok": ok, "message": msg,
                               "current": GUARD.current()})

        if u.path == "/api/submap/exit":
            ok, msg = GUARD.leave()
            return self._send(200, {"ok": ok, "message": msg,
                                    "current": GUARD.current()})

        if u.path == "/api/heartbeat":
            GUARD.beat()
            exited = GUARD.reconcile()
            return self._send(200, {"ok": True, "active": GUARD.active,
                                    "exited": exited})

        if u.path == "/api/check":
            item = REG.get(body.get("item_id", ""))
            if not item:
                return self._send(404, {"error": "unknown item"})
            given = body.get("given", "")
            correct = any(decks.check_text(sb, given) for sb in siblings(item))
            return self._send(200, {"correct": correct,
                                    "answer": " or ".join(all_answers(item))})

        if u.path == "/api/nvim/check":
            item = REG.get(body.get("item_id", ""))
            if not item or item["input"] != "nvim":
                return self._send(404, {"error": "unknown exercise"})
            res = nvimrun.check(item, body.get("keys", ""))
            res["answer"] = item["answer"]
            return self._send(200, res)

        if u.path == "/api/attempt":
            item = REG.get(body.get("item_id", ""))
            if item:
                with db() as conn:
                    conn.execute("""
                        INSERT INTO attempts (ts, item_id, deck, prompt,
                                              answer, mode, correct, latency_ms)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (time.time(), item["id"], item["deck"],
                          item["prompt"], item["answer"],
                          body.get("mode", "drill"),
                          1 if body.get("correct") else 0,
                          int(body.get("latency_ms") or 0)))
                if body.get("mode") == "learn":
                    st = learn_record(item, bool(body.get("correct")))
                    return self._send(200, {"ok": True, "learn": st})
            return self._send(200, {"ok": True})

        if u.path == "/api/typing":
            with db() as conn:
                conn.execute("""
                    INSERT INTO typing_runs (ts, source, wpm, accuracy,
                                             chars, seconds)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (time.time(), body.get("source", "symbols"),
                      float(body.get("wpm") or 0),
                      float(body.get("accuracy") or 0),
                      int(body.get("chars") or 0),
                      float(body.get("seconds") or 0)))
                for ch, d in (body.get("keys") or {}).items():
                    conn.execute("""
                        INSERT INTO key_stats (ch, hits, misses)
                        VALUES (?, ?, ?)
                        ON CONFLICT(ch) DO UPDATE SET
                            hits = hits + excluded.hits,
                            misses = misses + excluded.misses
                    """, (ch, int(d.get("hits") or 0), int(d.get("misses") or 0)))
            return self._send(200, {"ok": True})

        if u.path == "/api/learn/skip":
            # Touch `updated` so the ordering key moves and the item rotates
            # out, without recording it as an answer either way.
            item = REG.get(body.get("item_id", ""))
            if item:
                with db() as conn:
                    conn.execute("""
                        INSERT INTO learning (item_id, deck, stage, streak,
                                              seen, graduated, updated)
                        VALUES (?, ?, 1, 0, 0, 0, ?)
                        ON CONFLICT(item_id) DO UPDATE SET
                            updated=excluded.updated
                    """, (item["id"], item["deck"], time.time()))
            return self._send(200, {"ok": True})

        if u.path == "/api/reload":
            n = REG.load()
            decks.deck_nvim_keys(refresh=True)
            n = REG.load()
            return self._send(200, {"ok": True, "items": n})

        return self._send(404, {"error": "not found"})

    def _file(self, name, ctype):
        path = STATIC / name
        if not path.is_file():
            return self._send(404, {"error": "missing %s" % name})
        return self._send(200, path.read_bytes(), ctype)

    # ---------------- aggregates ----------------
    def _decks(self):
        with db() as conn:
            rows = conn.execute(
                "SELECT item_id, graduated FROM learning").fetchall()
        grad = {r["item_id"] for r in rows if r["graduated"]}

        out = []
        for name in decks.BUILDERS:
            items = REG.select(name)
            if not items:
                continue
            cats = {}
            for it in items:
                c = cats.setdefault(it["category"],
                                    {"name": it["category"], "total": 0,
                                     "learned": 0})
                c["total"] += 1
                if it["id"] in grad:
                    c["learned"] += 1
            out.append({
                "deck": name,
                "label": decks.DECK_LABELS.get(name, name),
                "input": items[0]["input"],
                "total": len(items),
                "learned": sum(1 for i in items if i["id"] in grad),
                "categories": sorted(cats.values(), key=lambda c: c["name"]),
            })
        return {"decks": out}

    def _stats(self):
        with db() as conn:
            tot = conn.execute(
                "SELECT COUNT(*) n, SUM(correct) ok FROM attempts").fetchone()
            per_deck = conn.execute("""
                SELECT deck, COUNT(*) n, SUM(correct) ok, AVG(latency_ms) avg_ms
                FROM attempts GROUP BY deck
            """).fetchall()
            worst = conn.execute("""
                SELECT prompt, answer, deck, COUNT(*) n, SUM(correct) ok,
                       AVG(latency_ms) avg_ms
                FROM attempts GROUP BY item_id
                HAVING n >= 2
                ORDER BY (CAST(SUM(correct) AS REAL)/COUNT(*)) ASC, avg_ms DESC
                LIMIT 10
            """).fetchall()
            runs = conn.execute("""
                SELECT ts, source, wpm, accuracy FROM typing_runs
                ORDER BY ts DESC LIMIT 12
            """).fetchall()
            keys = conn.execute("""
                SELECT ch, hits, misses FROM key_stats
                WHERE hits + misses >= 3
                ORDER BY CAST(misses AS REAL)/(hits+misses) DESC LIMIT 12
            """).fetchall()
            learned = conn.execute("""
                SELECT deck, COUNT(*) n FROM learning WHERE graduated=1
                GROUP BY deck
            """).fetchall()

        return {
            "total": tot["n"] or 0,
            "correct": tot["ok"] or 0,
            "per_deck": [dict(r) for r in per_deck],
            "learned": {r["deck"]: r["n"] for r in learned},
            "worst": [dict(r) for r in worst],
            "recent_runs": [dict(r) for r in runs],
            "worst_keys": [dict(r) for r in keys],
        }


def main():
    port = DEFAULT_PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    no_open = "--no-open" in sys.argv

    init_db()

    ok, out = GUARD.define()
    if not ok:
        print("[keydrill] WARNING: could not define submap: %s" % out, flush=True)
        print("[keydrill] chord capture will not work.", flush=True)
    else:
        print("[keydrill] submap '%s' defined (ESC always exits)" % SUBMAP_NAME,
              flush=True)

    atexit.register(lambda: GUARD.leave("atexit"))
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, panic_exit)
        except (ValueError, OSError):
            pass

    threading.Thread(target=GUARD.watchdog, daemon=True).start()

    n = REG.load()
    counts = {}
    for it in REG.items.values():
        counts[it["deck"]] = counts.get(it["deck"], 0) + 1
    print("[keydrill] %d items: %s" % (n, counts), flush=True)
    if not nvimrun.available():
        print("[keydrill] nvim not found - motion exercises disabled",
              flush=True)

    url = "http://127.0.0.1:%d/" % port
    print("[keydrill] serving %s" % url, flush=True)
    print("[keydrill] ctrl-c to quit (submap is released on exit)", flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if not no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        GUARD.leave("shutdown")


if __name__ == "__main__":
    main()
