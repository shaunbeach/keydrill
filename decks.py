#!/usr/bin/env python3
"""Deck loading.

Every trainable thing in keydrill is an "item" with the same shape, whatever
domain it came from:

    id        stable identifier, used as the learning-progress key
    deck      hypr | nvim_keys | nvim_motion | term
    category  grouping within the deck, used to pick a lesson
    prompt    what the user is asked to do
    answer    the canonical answer, shown while learning
    input     chord | text | nvim  - how the answer is given
    hint      one line of teaching, shown in learn mode

Chord items add mods/key. Text items add accept[]. Nvim items add
start/cursor/goal/solution.
"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

OMARCHY_BINDINGS = Path("/usr/share/omarchy/default/hypr/bindings")

MOD_BITS = [(64, "SUPER"), (4, "CTRL"), (8, "ALT"), (1, "SHIFT")]

DECK_LABELS = {
    "hypr": "Hyprland",
    "nvim_keys": "Neovim keys",
    "nvim_motion": "Neovim motions",
    "term": "Terminal",
}

# Which leader group a LazyVim mapping belongs to, keyed by the char after
# <leader>. Anything unlisted falls back to "other".
LEADER_GROUPS = {
    "f": "find", "s": "search", "g": "git", "b": "buffer", "c": "code",
    "d": "debug", "u": "toggle", "x": "diagnostics", "q": "session",
    "w": "window", "t": "test", "l": "lazy", "n": "notify", "a": "ai",
    "p": "project", "r": "refactor", "h": "hunk", "o": "overseer",
}


# --------------------------------------------------------------------------
# Hyprland bindings
# --------------------------------------------------------------------------

def _hypr_categories():
    """Map binding description -> source file stem, for lesson grouping.

    Omarchy splits its bindings across tiling.lua, applications.lua and so on.
    hyprctl doesn't report which file a bind came from, but the descriptions
    are unique enough to match back to the source.
    """
    cats = {}
    if not OMARCHY_BINDINGS.is_dir():
        return cats
    pat = re.compile(r'o\.bind[a-z_]*\(\s*"[^"]+"\s*,\s*"([^"]+)"')
    for f in sorted(OMARCHY_BINDINGS.glob("*.lua")):
        try:
            text = f.read_text()
        except OSError:
            continue
        for m in pat.finditer(text):
            cats[m.group(1)] = f.stem
    return cats


def deck_hypr():
    try:
        p = subprocess.run(["hyprctl", "-j", "binds"], capture_output=True,
                           text=True, timeout=5)
        raw = json.loads(p.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    cats = _hypr_categories()
    seen, items = set(), []

    for b in raw:
        desc = (b.get("description") or "").strip()
        key = (b.get("key") or "").strip().upper()
        mask = b.get("modmask", 0)

        if not desc or not key or b.get("mouse"):
            continue
        if key.startswith(("XF86", "MOUSE")):
            continue
        if key == "ESCAPE" and mask == 0:      # reserved as the submap exit
            continue
        if (mask, key) in seen:
            continue
        seen.add((mask, key))

        mods = [n for bit, n in MOD_BITS if mask & bit]
        items.append({
            "id": "hy-%d:%s" % (mask, key),
            "deck": "hypr",
            "category": cats.get(desc, "other"),
            "prompt": desc,
            "answer": " + ".join(mods + [key]) if mods else key,
            "input": "chord",
            "hint": "Press the chord itself - Hyprland is held in a submap "
                    "so it won't fire.",
            "mods": mods,
            "key": key,
        })

    items.sort(key=lambda x: (x["category"], x["prompt"].lower()))
    return items


# --------------------------------------------------------------------------
# Neovim keymaps, read from the user's real config
# --------------------------------------------------------------------------

_NVIM_KEYS_CACHE = None


def _display_lhs(lhs):
    """Leader is expanded to a literal space by nvim; show it as <leader>."""
    if lhs.startswith(" "):
        return "<leader>" + lhs[1:]
    return lhs


def deck_nvim_keys(refresh=False):
    global _NVIM_KEYS_CACHE
    if _NVIM_KEYS_CACHE is not None and not refresh:
        return _NVIM_KEYS_CACHE

    items = []
    with tempfile.TemporaryDirectory(prefix="keydrill-maps-") as td:
        out = Path(td) / "maps.json"
        lua = (
            "local out={} "
            "for _,v in ipairs(vim.api.nvim_get_keymap('n')) do "
            "  if v.desc and v.desc ~= '' then "
            "    table.insert(out,{lhs=v.lhs, desc=v.desc}) end end "
            "vim.fn.writefile({vim.json.encode(out)}, '%s')" % out
        )
        try:
            subprocess.run(["nvim", "--headless", "-c", "lua " + lua,
                            "-c", "qa!"],
                           capture_output=True, text=True, timeout=25)
            raw = json.loads(out.read_text())
        except (OSError, subprocess.SubprocessError,
                json.JSONDecodeError, FileNotFoundError):
            _NVIM_KEYS_CACHE = []
            return []

    seen = set()
    for m in raw:
        lhs, desc = m.get("lhs", ""), (m.get("desc") or "").strip()
        if not lhs or not desc or lhs in seen:
            continue
        # Single-key remaps of core motions teach nothing useful here.
        if len(lhs) == 1 and not lhs.startswith(" "):
            continue
        seen.add(lhs)

        shown = _display_lhs(lhs)
        if lhs.startswith(" ") and len(lhs) > 1:
            cat = LEADER_GROUPS.get(lhs[1], "other")
        elif lhs.startswith("["):
            cat = "prev"
        elif lhs.startswith("]"):
            cat = "next"
        elif lhs.startswith("g"):
            cat = "goto"
        else:
            cat = "other"

        items.append({
            "id": "nk-" + lhs,
            "deck": "nvim_keys",
            "category": cat,
            "prompt": desc,
            "answer": shown,
            "input": "text",
            "hint": "Type the key sequence. Use <leader> or a leading space "
                    "for the leader key.",
            "accept": [shown, lhs],
        })

    items.sort(key=lambda x: (x["category"], x["prompt"].lower()))
    _NVIM_KEYS_CACHE = items
    return items


# --------------------------------------------------------------------------
# Static JSON decks
# --------------------------------------------------------------------------

def _load_json_deck(name):
    path = DATA / name
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def deck_nvim_motion():
    items = _load_json_deck("nvim_motions.json")
    for it in items:
        it.setdefault("input", "nvim")
        it["prompt"] = it.get("title", it.get("prompt", ""))
        it["answer"] = it.get("solution", "")
    return items


def deck_term():
    items = _load_json_deck("terminal.json")
    for it in items:
        it.setdefault("input", "text")
    return items


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

BUILDERS = {
    "hypr": deck_hypr,
    "nvim_keys": deck_nvim_keys,
    "nvim_motion": deck_nvim_motion,
    "term": deck_term,
}


def load_all():
    """All items from all decks, keyed by id."""
    items = {}
    for name, build in BUILDERS.items():
        try:
            for it in build():
                items[it["id"]] = it
        except Exception as exc:                              # noqa: BLE001
            print("[keydrill] deck %s failed: %s" % (name, exc), flush=True)
    return items


# --------------------------------------------------------------------------
# Answer checking for text-input decks
# --------------------------------------------------------------------------

def normalize_text(s):
    """Loose comparison so trivial spacing/quoting differences still pass."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    s = s.replace('"', "'")
    s = s.replace("<leader>", " ")
    return s


def normalize_chord(s):
    """SUPER + SHIFT + W and shift+super+w are the same chord."""
    parts = [p.strip().upper() for p in (s or "").split("+") if p.strip()]
    return "+".join(sorted(parts))


def check_text(item, given):
    if item.get("input") == "chord":
        return normalize_chord(given) == normalize_chord(item.get("answer", ""))
    accepted = item.get("accept") or [item.get("answer", "")]
    g = normalize_text(given)
    return any(g == normalize_text(a) for a in accepted)


if __name__ == "__main__":
    import collections
    all_items = load_all()
    print("total items: %d" % len(all_items))
    per = collections.Counter(i["deck"] for i in all_items.values())
    for d, n in per.items():
        cats = collections.Counter(i["category"] for i in all_items.values()
                                   if i["deck"] == d)
        print("  %-12s %3d  %s" % (d, n, dict(cats)))
