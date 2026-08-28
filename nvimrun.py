#!/usr/bin/env python3
"""Run a motion exercise inside a real headless Neovim.

We never emulate vim. The user's keystrokes are fed to an actual nvim process
started with `-u NONE`, so the result is whatever real vim would do - and it
cannot drift from the editor they're training for.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

TIMEOUT = 6


def _vim_list(lines):
    """Python list of str -> vimscript list literal, single-quote escaped."""
    return "[" + ",".join("'" + s.replace("'", "''") + "'" for s in lines) + "]"


def available():
    try:
        subprocess.run(["nvim", "--version"], capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def run(start_lines, cursor, keys):
    """Apply `keys` to `start_lines` and report the resulting buffer.

    cursor is (row, col) 1-indexed row, 1-indexed col as vim's cursor() wants.
    Returns dict with lines, cursor, ok, error.
    """
    with tempfile.TemporaryDirectory(prefix="keydrill-nvim-") as td:
        out = Path(td) / "out.json"

        # Keys travel through the environment so no vimscript quoting rules
        # can be tripped by whatever the user pressed. feedkeys() takes the
        # string literally rather than re-parsing it as an Ex command line,
        # which `execute 'normal! ' . keys` would do - a `|` or `"` in the
        # keys would otherwise split or comment out the command.
        env = dict(os.environ)
        env["KEYDRILL_KEYS"] = keys

        script = [
            # -u NONE gives vanilla defaults, but noexpandtab/sw=8 would make
            # indent drills disagree with the LazyVim setup being trained for.
            "-c", "silent! set expandtab shiftwidth=2 tabstop=2",
            "-c", "silent! call setline(1, %s)" % _vim_list(start_lines),
            "-c", "silent! call cursor(%d, %d)" % (cursor[0], cursor[1]),
            "-c", "silent! call feedkeys($KEYDRILL_KEYS, 'nx')",
            "-c", ("silent! call writefile([json_encode({"
                   "'lines': getline(1, '$'), "
                   "'cursor': [line('.'), col('.')], "
                   "'mode': mode()})], '%s')" % out),
            "-c", "qa!",
        ]

        try:
            p = subprocess.run(
                ["nvim", "--headless", "-u", "NONE", "-n", "-i", "NONE"] + script,
                capture_output=True, text=True, timeout=TIMEOUT,
                env=env, cwd=td)
        except subprocess.TimeoutExpired:
            # An unterminated operator or a stray macro can hang the process.
            return {"ok": False, "error": "timed out - unfinished command?"}
        except OSError as exc:
            return {"ok": False, "error": "could not run nvim: %s" % exc}

        if not out.is_file():
            err = (p.stderr or "").strip()[:200]
            return {"ok": False, "error": err or "nvim produced no result"}

        try:
            data = json.loads(out.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            return {"ok": False, "error": "unreadable result: %s" % exc}

        return {"ok": True, "lines": data.get("lines", []),
                "cursor": data.get("cursor", [1, 1]),
                "mode": data.get("mode", "n")}


def check(exercise, keys):
    """Run the exercise and decide whether the goal was reached."""
    res = run(exercise["start"], exercise.get("cursor", [1, 1]), keys)
    if not res["ok"]:
        return {"correct": False, "error": res["error"], "lines": None}

    got = res["lines"]
    want = exercise["goal"]
    correct = got == want

    # Some exercises are about landing the cursor, not changing text.
    if correct and exercise.get("goal_cursor"):
        correct = list(res["cursor"]) == list(exercise["goal_cursor"])

    return {"correct": correct, "lines": got, "cursor": res["cursor"],
            "want": want, "error": None}


if __name__ == "__main__":
    # Self-test: a handful of motions with awkward characters in them.
    cases = [
        ("dw", ["local foo = bar_value"], [1, 13], ["local foo = "]),
        ("ciw" + "new" + "\x1b", ["local foo = bar"], [1, 7],
         ["local new = bar"]),
        ('ci"' + "hi" + "\x1b", ['msg = "hello world"'], [1, 10],
         ['msg = "hi"']),
        ("da(", ["call(a, b) end"], [1, 6], ["call end"]),
        ("yyp", ["dup me"], [1, 1], ["dup me", "dup me"]),
        ("3dd", ["a", "b", "c", "d"], [1, 1], ["d"]),
        ("A!" + "\x1b", ["end of line"], [1, 1], ["end of line!"]),
        ("d|", ["pipes | here"], [1, 1], ["pipes | here"]),
    ]
    ok = 0
    for keys, start, cur, want in cases:
        r = run(start, cur, keys)
        got = r.get("lines")
        good = got == want
        ok += good
        shown = keys.replace("\x1b", "<Esc>")
        print(("PASS  " if good else "FAIL  ") + "%-14s %r" % (shown, got))
        if not good:
            print("        wanted %r  err=%s" % (want, r.get("error")))
    print("\n%d/%d passed" % (ok, len(cases)))
