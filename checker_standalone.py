"""
Standalone Game Checker
=======================

Usage:
    python checker_standalone.py [--vk] [--debug]

    --vk      Enable VK notifications (requires vk_token + vk_peer_id in config).
    --debug   Dump every raw frame sent and received to debug.log.

Terminal UI:
    - Top section : scrolling log pane (never overwrites the prompt).
    - Separator   : thin horizontal line.
    - Bottom row  : CMD> input (always visible, always at the bottom).
    Colors: DEBUG=cyan  INFO=default  WARNING=yellow  ERROR=red  CRITICAL=red+bold

Robots file  (robots.json):
    [
        {
            "name":       "Robot-Cocaine",
            "hwid":       "1c44e963-f886-418f-8330-a68b9dda09ff",
            "uniq":       "o1nk1",
            "hash":       "10V118S18F1P",
            "id":         "1341",
            "isLoggedIn": false
        }
    ]

Wire frame format (confirmed from debug session):
    [uint32-LE total_length_including_prefix] [U/B/J type] [2-char event] [payload]
    TY binary header: [4-char inner type][uint32-LE time][uint32-LE x][uint32-LE y]
"""

import argparse
import collections
import curses
import hashlib
import json
import logging
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# TUNABLE FRAME CONSTANTS
# ---------------------------------------------------------------------------
# Confirmed from debug.log: first packet 0b 00 00 00  USTInit
# Total length 11 includes the 4-byte prefix → body = 7 = USTInit
FRAME_PREFIX_LEN:          int  = 4
FRAME_LEN_INCLUDES_PREFIX: bool = True

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "server_host":          "90.188.7.54",
    "server_port":          8090,
    "vk_token":             "",
    "vk_peer_id":           0,
    "reconnect_delay":      5,
    "script_file":          "az.txt",
    "excluded_coords":      [],
    "craft_alerts_enabled": True,
}
CONFIG_FILE = "checker_config.json"
ROBOTS_FILE = "robots.json"
DEBUG_LOG   = "debug.log"


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[WARN] Could not read {CONFIG_FILE}: {e}")
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LOGGING  (file only — stdout is owned by curses after startup)
# ---------------------------------------------------------------------------
_debug_mode = False
_LOG_FMT    = "[%(asctime)s] %(levelname)-8s %(message)s"
_DATE_FMT   = "%H:%M:%S"

log = logging.getLogger("checker")


def _setup_file_logging(debug: bool):
    global _debug_mode
    _debug_mode = debug

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    fh = logging.FileHandler("checker.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
    root.addHandler(fh)

    if debug:
        dh = logging.FileHandler(DEBUG_LOG, encoding="utf-8", mode="w")
        dh.setFormatter(logging.Formatter(_LOG_FMT, _DATE_FMT))
        root.addHandler(dh)


def _hex(data: bytes, max_bytes: int = 128) -> str:
    s = data[:max_bytes].hex(" ")
    if len(data) > max_bytes:
        s += f"  ...({len(data)}B)"
    return s


def _printable(data: bytes, max_bytes: int = 128) -> str:
    s = "".join(chr(b) if 32 <= b < 127 else "." for b in data[:max_bytes])
    if len(data) > max_bytes:
        s += "..."
    return s


# ---------------------------------------------------------------------------
# TUI — curses split-screen terminal interface
# ---------------------------------------------------------------------------
class TUI:
    """
    Split-screen terminal UI built on curses.

    Layout:
        rows 0 .. rows-3  :  scrolling log pane
        row  rows-2       :  separator  (shows scroll position when not at bottom)
        row  rows-1       :  CMD> input line  (fixed, never scrolled)

    Log scrolling:
        Page Up / Ctrl+U    scroll up one page
        Page Down / Ctrl+D  scroll down one page
        Home / g            jump to oldest visible line
        End / G             jump to latest line (resume live tail)
        Any printable key   auto-returns to live tail

    Command history  (Up / Down  or  Ctrl+P / Ctrl+N):
        Up / Ctrl+P         previous command
        Down / Ctrl+N       next command  (or back to draft)

    Emacs line-editing bindings:
        Ctrl+A              beginning of line
        Ctrl+E              end of line
        Ctrl+F / Right      forward one character
        Ctrl+B / Left       backward one character
        Alt+F               forward one word
        Alt+B               backward one word
        Ctrl+D              delete character forward  (exit if line is empty)
        Ctrl+H / Backspace  delete character backward
        Ctrl+T              transpose characters
        Ctrl+K              kill to end of line  → kill ring
        Ctrl+U              kill whole line      → kill ring
        Ctrl+W              kill word backward   → kill ring
        Alt+D               kill word forward    → kill ring
        Ctrl+Y              yank (paste kill ring)
        Ctrl+L              redraw screen

    Thread safety: add_line() and output() may be called from any thread.
    The curses event loop runs exclusively on the main thread.
    """

    MAX_LINES    = 10_000
    MAX_HISTORY  = 500

    # curses color-pair IDs
    _CP_DEBUG    = 1
    _CP_INFO     = 2
    _CP_WARNING  = 3
    _CP_ERROR    = 4
    _CP_CRITICAL = 5
    _CP_OUTPUT   = 6
    _CP_PROMPT   = 7
    _CP_SEP      = 8
    _CP_SCROLL   = 9   # scroll-position indicator on separator

    # Pseudo level used for plain command responses
    OUTPUT = 25

    def __init__(self):
        # ---- log store ----
        self._lines: collections.deque = collections.deque(maxlen=self.MAX_LINES)
        self._lock   = threading.Lock()

        # ---- scroll state ----
        # _scroll == 0  → live tail (newest lines visible)
        # _scroll == N  → show lines ending N lines before the newest
        self._scroll: int = 0

        # ---- input / editing ----
        self._input:  str = ""     # current line buffer
        self._cursor: int = 0      # caret position within _input
        self._kill:   str = ""     # kill ring (single entry)

        # ---- command history ----
        self._history:    collections.deque = collections.deque(maxlen=self.MAX_HISTORY)
        self._hist_pos:   int = -1   # -1 = editing draft
        self._hist_draft: str = ""   # saved draft while browsing history

        # ---- misc ----
        self._scr:     Optional[object] = None
        self._checker: Optional["Checker"] = None
        self._stop     = threading.Event()

    # -------------------------------------------------------------------------
    # Public API (thread-safe)
    # -------------------------------------------------------------------------

    def add_line(self, level: int, text: str):
        """Append a formatted log line. Safe to call from any thread."""
        ts   = time.strftime("%H:%M:%S")
        name = logging.getLevelName(level) if level != self.OUTPUT else "OUTPUT "
        formatted = f"[{ts}] {name:<8} {text}"
        with self._lock:
            for part in formatted.splitlines():
                self._lines.append((level, part))
            # If the user has scrolled up, keep their view stable by advancing
            # the offset to compensate for lines added at the bottom.
            # (Only when truly scrolled — don't shift the live-tail view.)
            # We do nothing here; the redraw clamps _scroll to valid range.

    def output(self, text: str):
        """Append plain command output. Safe to call from any thread."""
        with self._lock:
            for line in text.splitlines():
                self._lines.append((self.OUTPUT, "  " + line))

    # -------------------------------------------------------------------------
    # Entry point
    # -------------------------------------------------------------------------

    def run(self, checker: "Checker"):
        self._checker = checker
        curses.wrapper(self._main)

    def _main(self, scr):
        self._scr = scr
        self._init_colors()
        curses.noecho()
        curses.cbreak()
        scr.keypad(True)
        curses.halfdelay(1)   # getch() times out after 100 ms → polling loop

        _install_tui_handler(self)

        threading.Thread(
            target=self._checker._reconnect_loop, daemon=True, name="reconnect"
        ).start()
        if _debug_mode:
            self._checker.monitor.start()

        while not self._stop.is_set():
            self._redraw()
            try:
                key = scr.getch()
            except curses.error:
                key = -1
            if key != -1:
                self._handle_key(key)

    # -------------------------------------------------------------------------
    # Colors
    # -------------------------------------------------------------------------

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(self._CP_DEBUG,    curses.COLOR_CYAN,    -1)
        curses.init_pair(self._CP_INFO,     -1,                   -1)
        curses.init_pair(self._CP_WARNING,  curses.COLOR_YELLOW,  -1)
        curses.init_pair(self._CP_ERROR,    curses.COLOR_RED,     -1)
        curses.init_pair(self._CP_CRITICAL, curses.COLOR_RED,     -1)
        curses.init_pair(self._CP_OUTPUT,   curses.COLOR_WHITE,   -1)
        curses.init_pair(self._CP_PROMPT,   curses.COLOR_GREEN,   -1)
        curses.init_pair(self._CP_SEP,      curses.COLOR_WHITE,   -1)
        curses.init_pair(self._CP_SCROLL,   curses.COLOR_CYAN,    -1)

    def _attr(self, level: int) -> int:
        if level >= logging.CRITICAL:
            return curses.color_pair(self._CP_CRITICAL) | curses.A_BOLD
        if level >= logging.ERROR:
            return curses.color_pair(self._CP_ERROR)
        if level >= logging.WARNING:
            return curses.color_pair(self._CP_WARNING)
        if level == self.OUTPUT:
            return curses.color_pair(self._CP_OUTPUT) | curses.A_BOLD
        if level <= logging.DEBUG:
            return curses.color_pair(self._CP_DEBUG)
        return curses.color_pair(self._CP_INFO)

    # -------------------------------------------------------------------------
    # Redraw
    # -------------------------------------------------------------------------

    def _redraw(self):
        scr = self._scr
        rows, cols = scr.getmaxyx()
        log_rows = max(1, rows - 2)
        sep_row  = rows - 2
        inp_row  = rows - 1

        with self._lock:
            total = len(self._lines)
            # Clamp scroll so we never scroll past the beginning
            max_scroll = max(0, total - log_rows)
            self._scroll = min(self._scroll, max_scroll)

            # Slice the window: index into the deque
            end_idx   = total - self._scroll
            start_idx = max(0, end_idx - log_rows)
            visible   = list(self._lines)[start_idx:end_idx]

        # --- log pane ---
        for row in range(log_rows):
            try:
                scr.move(row, 0)
                scr.clrtoeol()
                if row < len(visible):
                    level, text = visible[row]
                    scr.addstr(row, 0, text[:cols - 1], self._attr(level))
            except curses.error:
                pass

        # --- separator (shows scroll position when not live-tailing) ---
        try:
            sep_base = "\u2500" * (cols - 1)
            if self._scroll > 0:
                pct      = 100 * (total - self._scroll - log_rows) // max(total - log_rows, 1)
                pct      = max(0, min(100, pct))
                indicator = f" \u2191\u2191 -{self._scroll} lines  ({pct}%)  PgDn/G to resume "
                # Overwrite the centre of the separator line
                mid  = max(0, (cols - len(indicator)) // 2)
                line = sep_base[:mid] + indicator + sep_base[mid + len(indicator):]
                line = line[:cols - 1]
                scr.addstr(sep_row, 0, line, curses.color_pair(self._CP_SCROLL) | curses.A_BOLD)
            else:
                scr.addstr(sep_row, 0, sep_base[:cols - 1],
                           curses.color_pair(self._CP_SEP) | curses.A_DIM)
        except curses.error:
            pass

        # --- input line with cursor ---
        prompt     = "CMD> "
        prompt_len = len(prompt)
        max_inp    = max(0, cols - prompt_len - 1)

        # Compute a viewport into _input so the cursor stays visible
        # vp_start = first char of _input shown on screen
        if not hasattr(self, "_vp_start"):
            self._vp_start = 0
        if self._cursor < self._vp_start:
            self._vp_start = self._cursor
        if self._cursor >= self._vp_start + max_inp:
            self._vp_start = self._cursor - max_inp + 1

        vp_start  = self._vp_start
        inp_show  = self._input[vp_start: vp_start + max_inp]
        cur_col   = prompt_len + (self._cursor - vp_start)

        try:
            scr.move(inp_row, 0)
            scr.clrtoeol()
            scr.addstr(inp_row, 0, prompt,
                       curses.color_pair(self._CP_PROMPT) | curses.A_BOLD)
            scr.addstr(inp_row, prompt_len, inp_show)
            scr.move(inp_row, min(cur_col, cols - 1))
        except curses.error:
            pass

        scr.refresh()

    # -------------------------------------------------------------------------
    # Key handling — main dispatcher
    # -------------------------------------------------------------------------

    def _handle_key(self, key: int):
        scr = self._scr

        # ── Enter ──────────────────────────────────────────────────────────
        if key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
            self._scroll = 0   # snap back to live tail on any command
            cmd = self._input.strip()
            self._input  = ""
            self._cursor = 0
            self._vp_start = 0
            self._hist_pos   = -1
            self._hist_draft = ""
            if cmd:
                # Add to history (avoid consecutive duplicates)
                if not self._history or self._history[-1] != cmd:
                    self._history.append(cmd)
                self._dispatch(cmd)
            return

        # ── Terminal resize ────────────────────────────────────────────────
        if key == curses.KEY_RESIZE:
            scr.clear()
            return

        # ── Log-pane scrolling ─────────────────────────────────────────────
        rows, cols = scr.getmaxyx()
        log_rows   = max(1, rows - 2)

        if key == curses.KEY_PPAGE:          # Page Up
            self._scroll_by(+log_rows)
            return
        if key == curses.KEY_NPAGE:          # Page Down
            self._scroll_by(-log_rows)
            return
        if key == curses.KEY_HOME:           # jump to top of buffer
            with self._lock:
                self._scroll = max(0, len(self._lines) - log_rows)
            return
        if key == curses.KEY_END:            # jump to bottom (live tail)
            self._scroll = 0
            return

        # ── ESC — handle Alt sequences ────────────────────────────────────
        if key == 27:
            self._handle_alt()
            return

        # ── Any printable key typed → snap back to live tail ──────────────
        if 32 <= key <= 126:
            self._scroll = 0

        # ── Ctrl keys ─────────────────────────────────────────────────────
        # (values 1-26 correspond to Ctrl+A through Ctrl+Z)

        if key == 1:    # Ctrl+A — beginning of line
            self._cursor = 0
        elif key == 5:  # Ctrl+E — end of line
            self._cursor = len(self._input)
        elif key == 6:  # Ctrl+F — forward char
            self._cursor = min(len(self._input), self._cursor + 1)
        elif key == 2:  # Ctrl+B — backward char
            self._cursor = max(0, self._cursor - 1)
        elif key == curses.KEY_RIGHT:
            self._cursor = min(len(self._input), self._cursor + 1)
        elif key == curses.KEY_LEFT:
            self._cursor = max(0, self._cursor - 1)

        elif key == 4:  # Ctrl+D — delete forward, or quit if empty
            if self._input:
                self._input = self._input[:self._cursor] + self._input[self._cursor + 1:]
            # (empty line → do nothing; we don't want accidental quit)

        elif key in (curses.KEY_BACKSPACE, 127, 8):  # Ctrl+H / Backspace
            if self._cursor > 0:
                self._input  = self._input[:self._cursor - 1] + self._input[self._cursor:]
                self._cursor -= 1

        elif key == 20:  # Ctrl+T — transpose chars
            if 1 <= self._cursor <= len(self._input) - 1:
                lst = list(self._input)
                lst[self._cursor - 1], lst[self._cursor] = lst[self._cursor], lst[self._cursor - 1]
                self._input  = "".join(lst)
                self._cursor += 1

        elif key == 11:  # Ctrl+K — kill to end of line
            self._kill   = self._input[self._cursor:]
            self._input  = self._input[:self._cursor]

        elif key == 21:  # Ctrl+U — kill whole line
            self._kill   = self._input
            self._input  = ""
            self._cursor = 0

        elif key == 23:  # Ctrl+W — kill word backward
            new_inp, killed = self._kill_word_backward(self._input, self._cursor)
            self._kill   = killed
            self._input  = new_inp
            self._cursor = len(new_inp)  # cursor moved to where word was

        elif key == 25:  # Ctrl+Y — yank
            self._input  = self._input[:self._cursor] + self._kill + self._input[self._cursor:]
            self._cursor += len(self._kill)

        elif key == 12:  # Ctrl+L — redraw
            scr.clear()

        elif key == 16:  # Ctrl+P — previous history
            self._history_prev()
        elif key == 14:  # Ctrl+N — next history
            self._history_next()
        elif key == curses.KEY_UP:
            self._history_prev()
        elif key == curses.KEY_DOWN:
            self._history_next()

        elif 32 <= key <= 126:  # printable character
            ch           = chr(key)
            self._input  = self._input[:self._cursor] + ch + self._input[self._cursor:]
            self._cursor += 1

    # -------------------------------------------------------------------------
    # Alt-key sequences  (ESC + char, read with short timeout)
    # -------------------------------------------------------------------------

    def _handle_alt(self):
        scr = self._scr
        # Give the terminal ~50 ms for the follow-up byte
        curses.halfdelay(1)
        try:
            ch = scr.getch()
        except curses.error:
            ch = -1
        curses.halfdelay(1)   # restore normal timeout

        if ch == -1:
            # Bare Escape — clear input or just do nothing
            return

        if ch in (ord("f"), ord("F")):   # Alt+F — forward word
            self._cursor = self._word_end(self._input, self._cursor)
        elif ch in (ord("b"), ord("B")): # Alt+B — backward word
            self._cursor = self._word_start(self._input, self._cursor)
        elif ch in (ord("d"), ord("D")): # Alt+D — kill word forward
            end          = self._word_end(self._input, self._cursor)
            self._kill   = self._input[self._cursor:end]
            self._input  = self._input[:self._cursor] + self._input[end:]

    # -------------------------------------------------------------------------
    # Log scroll helpers
    # -------------------------------------------------------------------------

    def _scroll_by(self, delta: int):
        """Positive delta = scroll toward older lines; negative = toward newer."""
        rows, cols = self._scr.getmaxyx()
        log_rows   = max(1, rows - 2)
        with self._lock:
            total      = len(self._lines)
            max_scroll = max(0, total - log_rows)
        self._scroll = max(0, min(max_scroll, self._scroll + delta))

    # -------------------------------------------------------------------------
    # Word-motion helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _word_end(s: str, pos: int) -> int:
        """Move pos forward past any spaces, then past the next word."""
        n = len(s)
        while pos < n and s[pos] == " ":
            pos += 1
        while pos < n and s[pos] != " ":
            pos += 1
        return pos

    @staticmethod
    def _word_start(s: str, pos: int) -> int:
        """Move pos backward past any spaces, then backward past the next word."""
        pos -= 1
        while pos > 0 and s[pos] == " ":
            pos -= 1
        while pos > 0 and s[pos - 1] != " ":
            pos -= 1
        return max(0, pos)

    @staticmethod
    def _kill_word_backward(s: str, pos: int):
        """Kill word backward. Returns (new_string, killed_text)."""
        new_pos = TUI._word_start(s, pos)
        return s[:new_pos] + s[pos:], s[new_pos:pos]

    # -------------------------------------------------------------------------
    # Command history
    # -------------------------------------------------------------------------

    def _history_prev(self):
        if not self._history:
            return
        if self._hist_pos == -1:
            self._hist_draft = self._input   # save what we're typing
        max_pos = len(self._history) - 1
        if self._hist_pos < max_pos:
            self._hist_pos += 1
        # history[-1] is most recent; history[0] is oldest
        # _hist_pos 0 → most recent, increasing → older
        entry        = self._history[-(self._hist_pos + 1)]
        self._input  = entry
        self._cursor = len(entry)

    def _history_next(self):
        if self._hist_pos == -1:
            return
        self._hist_pos -= 1
        if self._hist_pos == -1:
            self._input  = self._hist_draft
            self._cursor = len(self._input)
        else:
            entry        = self._history[-(self._hist_pos + 1)]
            self._input  = entry
            self._cursor = len(entry)

    # -------------------------------------------------------------------------
    # Command dispatcher
    # -------------------------------------------------------------------------

    _HELP_TEXT = """\
Commands
--------
help                      show this help
stats                     print current building statistics
status                    print connection / session status
sendraw <hex>             send raw bytes to the server
runscript [file]          run az.txt or a named script file
exclude <x:y>             add coordinates to the exclusion list
unexclude <x:y>           remove coordinates from the exclusion list
listexcluded              show all excluded coordinates
craftalerts               toggle crafter alerts on/off
robot                     show the active robot name, id, and hwid
quit                      disconnect and exit

Log scrolling
-------------
Page Up                   scroll log up one page
Page Down                 scroll log down one page
Home                      jump to oldest buffered line
End                       jump back to live tail

Emacs line editing
------------------
Ctrl+A / Ctrl+E           beginning / end of line
Ctrl+F / Ctrl+B           forward / backward character
Alt+F  / Alt+B            forward / backward word
Ctrl+D                    delete character forward
Backspace / Ctrl+H        delete character backward
Ctrl+T                    transpose characters
Ctrl+K                    kill to end of line
Ctrl+U                    kill whole line
Ctrl+W                    kill word backward
Alt+D                     kill word forward
Ctrl+Y                    yank (paste last kill)
Ctrl+P / Up               previous command
Ctrl+N / Down             next command
Ctrl+L                    redraw screen"""

    def _dispatch(self, raw: str):
        self.add_line(self.OUTPUT, f"CMD> {raw}")
        parts = raw.split()
        cmd   = parts[0].lower()

        if cmd == "help":
            self.output(self._HELP_TEXT)
        elif cmd == "quit":
            if self._checker:
                self._checker._stop.set()
                self._checker.monitor.stop()
                self._checker.conn.disconnect()
            self._stop.set()
        elif self._checker:
            self._checker.execute_command(cmd, parts[1:], self.output)
        else:
            self.output("Checker not ready yet.")


class _TUIHandler(logging.Handler):
    """Logging handler that feeds records into the TUI log pane."""

    def __init__(self, tui: TUI):
        super().__init__()
        self._tui = tui

    def emit(self, record: logging.LogRecord):
        try:
            self._tui.add_line(record.levelno, self.format(record))
        except Exception:
            pass


def _install_tui_handler(tui: TUI):
    handler = _TUIHandler(tui)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)


# ---------------------------------------------------------------------------
# ROBOTS
# ---------------------------------------------------------------------------
def load_robots() -> List[dict]:
    if not os.path.exists(ROBOTS_FILE):
        return []
    try:
        with open(ROBOTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[ERROR] Cannot read {ROBOTS_FILE}: {e}")
        return []


def save_robots(robots: List[dict]):
    with open(ROBOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(robots, f, indent=4, ensure_ascii=False)


def choose_robot(robots: List[dict]) -> Optional[dict]:
    """Interactive robot selection (runs before curses starts)."""
    if not robots:
        print(f"\nNo robots found in {ROBOTS_FILE}.")
        print("Create the file with at least one robot entry (see module docstring).")
        return None

    print("\n--- Robot selection ---")
    for i, r in enumerate(robots):
        status = "logged-in" if r.get("isLoggedIn") else "offline"
        print(f"  [{i}]  {r.get('name', '?'):<24}  id={r.get('id', '?'):<8}  {status}")

    while True:
        try:
            idx = int(input("Choose number: ").strip())
            if 0 <= idx < len(robots):
                print(f"Selected: {robots[idx].get('name')}")
                return robots[idx]
            print(f"Enter 0..{len(robots) - 1}.")
        except (ValueError, EOFError):
            print("Invalid input.")


# ---------------------------------------------------------------------------
# GAME CONNECTION
# ---------------------------------------------------------------------------
class GameConnection:
    """
    Raw TCP + game frame protocol.

    Frame: [uint32-LE total_len_incl_prefix] [U/B/J] [2-char event] [payload]
    """

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._buf   = bytearray()
        self._send_lock = threading.Lock()
        self._connected = False

        # Server-time state (mirrors ServerTime.cs)
        self._server_t0:   int = -1
        self._client_t0:   int = 0
        self._last_sent_t: int = 0
        self.ready: bool = False

        self._handlers: Dict[str, Callable] = {}

        self.on_tcp_up:       Optional[Callable] = None
        self.on_time_synced:  Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None

        # Stats
        self.frames_in  = 0
        self.frames_out = 0
        self.bytes_in   = 0
        self.bytes_out  = 0

    def register(self, event: str, cb: Callable):
        self._handlers[event] = cb

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(15)
            s.connect((self.host, self.port))
            s.settimeout(None)
            self._sock = s
            self._connected = True
            self._buf = bytearray()
            self._server_t0 = -1
            self.ready = False
            self.frames_in = self.frames_out = self.bytes_in = self.bytes_out = 0
            threading.Thread(target=self._recv_loop, daemon=True, name="recv").start()
            log.info(f"TCP connected to {self.host}:{self.port}")
            if self.on_tcp_up:
                self.on_tcp_up()
            return True
        except Exception as e:
            log.error(f"Connect failed: {e}")
            return False

    def disconnect(self):
        self._connected = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected

    def now_ms(self) -> int:
        if self._server_t0 < 0:
            return 0
        return self._server_t0 + int(time.time() * 1000) - self._client_t0

    # -- sending -------------------------------------------------------------

    def send_u(self, event: str, payload: str) -> bool:
        return self._send_frame(b"U" + event.encode("ascii") + payload.encode("utf-8"))

    def send_b(self, event: str, payload: bytes) -> bool:
        return self._send_frame(b"B" + event.encode("ascii") + payload)

    def send_typical(self, inner_type: str, x: int, y: int, payload: str) -> bool:
        """Mirrors ServerTime.SendTypicalMessage."""
        if not self.ready:
            def _retry():
                for _ in range(50):
                    time.sleep(0.1)
                    if self.ready:
                        self.send_typical(inner_type, x, y, payload)
                        return
                log.warning(f"send_typical({inner_type!r}): timed out waiting for time sync")
            threading.Thread(target=_retry, daemon=True).start()
            return False

        t = max(self.now_ms(), self._last_sent_t)
        self._last_sent_t = t
        header = struct.pack(
            "<4sIII",
            inner_type.encode("ascii"),
            t & 0xFFFFFFFF,
            x & 0xFFFFFFFF,
            y & 0xFFFFFFFF,
        )
        return self.send_b("TY", header + payload.encode("utf-8"))

    def send_raw(self, data: bytes) -> bool:
        return self._raw_write(data)

    def _send_frame(self, body: bytes) -> bool:
        # Total length includes the 4-byte prefix itself
        prefix = struct.pack("<I", len(body) + FRAME_PREFIX_LEN)
        frame  = prefix + body
        if _debug_mode:
            event = body[1:3].decode("ascii", errors="?") if len(body) >= 3 else "??"
            log.debug(
                f"FRAME OUT  event={event!r}  {len(frame)}B  "
                f"hex={_hex(frame)}  txt={_printable(frame)}"
            )
        self.frames_out += 1
        self.bytes_out  += len(frame)
        return self._raw_write(frame)

    def _raw_write(self, data: bytes) -> bool:
        with self._send_lock:
            if self._sock and self._connected:
                try:
                    self._sock.sendall(data)
                    return True
                except Exception as e:
                    log.error(f"Send error: {e}")
        return False

    # -- receive / parse -----------------------------------------------------

    def _recv_loop(self):
        try:
            while self._connected:
                chunk = self._sock.recv(8192)
                if not chunk:
                    log.info("Server closed connection")
                    break
                self.bytes_in += len(chunk)
                if _debug_mode:
                    log.debug(
                        f"RECV raw  {len(chunk)}B  "
                        f"hex={_hex(chunk)}  txt={_printable(chunk)}"
                    )
                self._buf.extend(chunk)
                self._parse_frames()
        except Exception as e:
            if self._connected:
                log.error(f"Receive error: {e}")
        finally:
            self._connected = False
            if self.on_disconnected:
                try:
                    self.on_disconnected()
                except Exception:
                    pass

    def _parse_frames(self):
        buf  = self._buf
        pos  = 0
        plen = FRAME_PREFIX_LEN

        while pos + plen <= len(buf):
            (total,) = struct.unpack_from("<I", buf, pos)
            body_len  = total - plen if FRAME_LEN_INCLUDES_PREFIX else total
            end       = pos + plen + body_len
            if end > len(buf):
                break
            frame = buf[pos + plen: end]
            pos   = end

            if len(frame) < 3:
                if _debug_mode:
                    log.debug(f"FRAME IN  too short ({len(frame)}B) -- skipping")
                continue

            self.frames_in += 1
            type_byte = chr(frame[0])
            event     = frame[1:3].decode("ascii", errors="replace")
            payload   = frame[3:]

            if _debug_mode:
                log.debug(
                    f"FRAME IN  type={type_byte!r}  event={event!r}  "
                    f"payload={len(payload)}B  txt={_printable(payload, 80)}"
                )

            self._dispatch(type_byte, event, payload)

        self._buf = bytearray(buf[pos:])

        if len(self._buf) > 4096 and _debug_mode:
            log.warning(
                f"Receive buffer has {len(self._buf)} unprocessed bytes -- "
                "frame prefix width may be wrong."
            )

    def _dispatch(self, type_byte: str, event: str, payload: bytes):
        handler = self._handlers.get(event)
        if not handler:
            if _debug_mode:
                log.debug(f"No handler for event={event!r}")
            return
        try:
            if type_byte in ("U", "J"):
                handler(payload.decode("utf-8", errors="replace"))
            else:
                handler(payload)
        except Exception as e:
            log.error(f"Handler for event {event!r}: {e}")
            log.debug(traceback.format_exc())

    def handle_pi(self, msg: str):
        """PI: '<pong>:<serverTimeMs>:<pingStr>'"""
        parts = msg.split(":")
        if len(parts) < 2:
            return
        try:
            server_t = int(parts[1])
        except ValueError:
            return
        if self._server_t0 < 0:
            self._server_t0 = server_t
            self._client_t0 = int(time.time() * 1000)
            self._last_sent_t = server_t
            self.ready = True
            log.info(f"Time sync complete (server_t={server_t})")
            if self.on_time_synced:
                try:
                    self.on_time_synced()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# AUTH HANDLER
# ---------------------------------------------------------------------------
class AuthHandler:
    """AU / AH / AE handshake from AuthManager.cs."""

    def __init__(self, conn: GameConnection, robot: dict, cfg: dict):
        self.conn       = conn
        self.cfg        = cfg
        self.robot      = robot
        self._uniq      = ""
        self._user_id   = robot.get("id",   "")
        self._user_hash = robot.get("hash", "")
        self.done       = threading.Event()

    def attach(self):
        self.conn.register("AU", self._on_au)
        self.conn.register("AH", self._on_ah)
        self.conn.register("AE", self._on_ae)

    @staticmethod
    def _md5(s: str) -> str:
        return hashlib.md5(s.encode("ascii")).hexdigest()

    def _token(self) -> str:
        return (f"{self._uniq}_{self._user_id}_"
                f"{self._md5(self._user_hash + self._uniq)}")

    def _on_au(self, msg: str):
        self._uniq = msg
        log.info(f"Auth challenge (uniq={self._uniq!r})")
        if self._user_id and self._user_hash:
            log.info(f"Sending AU for user_id={self._user_id!r}")
            self.conn.send_u("AU", self._token())
        else:
            log.error("Robot has no id or hash -- cannot authenticate.")
            self.conn.send_u("AU", f"{self._uniq}_NO_AUTH")

    def _on_ah(self, msg: str):
        if msg == "BAD":
            log.warning("Auth rejected (AH BAD). Check id/hash in robots.json.")
            return
        parts = msg.split("_", 1)
        if len(parts) == 2:
            self._user_id, self._user_hash = parts
            self.robot["id"]   = self._user_id
            self.robot["hash"] = self._user_hash
            log.info(f"Auth confirmed (user_id={self._user_id!r})")
        self.conn.send_u("AU", self._token())
        self.done.set()

    def _on_ae(self, msg: str):
        log.error(f"Server auth error (AE): {msg!r}")


# ---------------------------------------------------------------------------
# STATUS MONITOR
# ---------------------------------------------------------------------------
class StatusMonitor:
    def __init__(self, conn: GameConnection, auth: AuthHandler, interval: int = 10):
        self.conn     = conn
        self.auth     = auth
        self.interval = interval
        self._stop    = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="status").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            log.info(
                f"STATUS  connected={self.conn.connected}  ready={self.conn.ready}  "
                f"auth={self.auth.done.is_set()}  "
                f"frames_in={self.conn.frames_in}  frames_out={self.conn.frames_out}  "
                f"bytes_in={self.conn.bytes_in}  bytes_out={self.conn.bytes_out}"
            )


# ---------------------------------------------------------------------------
# SCRIPT RUNNER
# ---------------------------------------------------------------------------
class ScriptRunner:
    def __init__(self, conn: GameConnection):
        self.conn = conn

    def run_file(self, filename: str):
        if not os.path.exists(filename):
            log.warning(f"Script file not found: {filename}")
            return
        log.info(f"Running script: {filename}")
        try:
            with open(filename, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except Exception as e:
            log.error(f"Cannot read script: {e}")
            return
        threading.Thread(target=self._run, args=(lines,), daemon=True).start()

    def _run(self, lines: List[str]):
        labels: Dict[str, int] = {}
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(":"):
                labels[s[1:]] = i

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            if not line or line.startswith("#") or line.startswith(":"):
                continue
            parts = line.split()
            cmd   = parts[0].lower()

            if cmd == "print":
                log.info("SCRIPT: " + " ".join(parts[1:]))
            elif cmd == "sendraw":
                if len(parts) >= 3:
                    try:
                        self.conn.send_raw(bytes.fromhex(parts[2]))
                    except ValueError:
                        log.warning(f"sendraw: invalid hex: {parts[2]!r}")
            elif cmd == "sleep":
                try:
                    time.sleep(int(parts[1]) / 1000.0)
                except (IndexError, ValueError):
                    pass
            elif cmd == "repeat":
                try:
                    for _ in range(int(parts[1])):
                        self.conn.send_raw(bytes.fromhex(parts[2]))
                except (IndexError, ValueError):
                    pass
            elif cmd in ("goto", "loop"):
                lbl = parts[1] if len(parts) > 1 else ""
                if lbl in labels:
                    i = labels[lbl]
                else:
                    log.warning(f"Script: unknown label {lbl!r}")
            elif cmd == "pps":
                try:
                    rate = int(parts[1])
                    raw  = bytes.fromhex(parts[2])
                    iv   = 1.0 / rate
                    def _flood(r=raw, iv=iv):
                        while self.conn.connected:
                            self.conn.send_raw(r)
                            time.sleep(iv)
                    threading.Thread(target=_flood, daemon=True).start()
                    log.info(f"Script: pps flood at {rate}/s started")
                except (IndexError, ValueError, ZeroDivisionError):
                    pass
            else:
                log.warning(f"Script: unknown command {cmd!r}")


# ---------------------------------------------------------------------------
# BUILDING PARSER
# ---------------------------------------------------------------------------
_BLDG_RE = re.compile(
    r"^(?P<type>[\w\s\-]+)\s+(?P<coords>\d+:\d+)\.\s+HP:\s+"
    r"(?P<hp>\d+)/(?P<hp_max>\d+)(?:\.\s+(?P<rest>.*))?$"
)


def _strip_html(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t)


def _parse_val(s: str) -> float:
    s = s.replace(",", ".").upper()
    mul = 1
    if "KK" in s:  mul = 1_000_000; s = s.replace("KK", "")
    elif "K" in s: mul = 1_000;     s = s.replace("K", "")
    try:
        return float(re.sub(r"[^\d.]", "", s)) * mul
    except (ValueError, TypeError):
        return 0.0


def parse_building(text: str) -> Optional[dict]:
    text = _strip_html(text).strip()
    m    = _BLDG_RE.match(text)
    if not m:
        return None
    d, rest = m.groupdict(), m.groupdict().get("rest") or ""
    res = {
        "type": d["type"].strip(), "coords": d["coords"],
        "hp": int(d["hp"]), "hp_max": int(d["hp_max"]),
        "rest": rest, "charge": None, "charge_max": None,
        "storage": [], "product": None, "prod_count": 0,
    }
    c = re.search(r"Заряд:\s+(\d+)/(\d+)", rest)
    if c:
        res["charge"], res["charge_max"] = int(c.group(1)), int(c.group(2))
    if "Хранилище" in rest:
        res["storage"] = [_parse_val(v) for v in re.findall(r"(\d+[.,]?\d*[KkМм]*)", rest)]
    p = re.search(r"Продукция:\s+([\w\s\-]+)\s+x\s+(\d+)", rest)
    if p:
        res["product"], res["prod_count"] = p.group(1).strip(), int(p.group(2))
    return res


# ---------------------------------------------------------------------------
# STATE TRACKER
# ---------------------------------------------------------------------------
_SKIP_ITEMS = {"text", "", "ВЫЙТИ", "exit", "НАЗАД", "<packs"}


class StateTracker:
    def __init__(self, excluded: set, craft_alerts: bool):
        self.excluded     = excluded
        self.craft_alerts = craft_alerts
        self.current: Dict[str, dict] = {}
        self._prev:   Dict[str, dict] = {}
        self._warmup       = 0
        self._warmup_limit = 2
        self.last_update   = "never"

    def update(self, ugu_json: dict) -> List[str]:
        new: Dict[str, dict] = {}
        for item in ugu_json.get("richList", []):
            clean = _strip_html(item)
            if clean in _SKIP_ITEMS:
                continue
            b = parse_building(clean)
            if b and b["coords"] not in self.excluded:
                new[b["coords"]] = b

        alerts: List[str] = []
        if self._warmup >= self._warmup_limit:
            alerts = self._diff(self._prev, new)
        else:
            self._warmup += 1
            log.info(f"Warmup pass {self._warmup}/{self._warmup_limit}")

        self.current     = new
        self._prev       = dict(new)
        self.last_update = time.strftime("%H:%M:%S")
        return alerts

    def _diff(self, prev: dict, curr: dict) -> List[str]:
        alerts = []
        for coords, old in prev.items():
            if coords not in curr:
                alerts.append(f"LOST: {old['type']} ({coords})")
        for coords, b in curr.items():
            old = prev.get(coords)
            if b["hp"] == 0 and (not old or old["hp"] > 0):
                alerts.append(f"HP ZERO: {b['type']} ({coords})")
            if b["charge"] == 0 and (not old or old.get("charge") != 0):
                alerts.append(f"DISCHARGED: {b['type']} ({coords})")
            if b["type"] == "Клановая Пушка" and old and b["charge"] is not None:
                if b["charge"] < old.get("charge", b["charge"]):
                    alerts.append(
                        f"GUN FIRED: {coords}  charge=({b['charge']}/{b['charge_max']})"
                    )
            if b["type"] == "Телепорт" and b["charge"] is not None and b["charge_max"]:
                ratio     = b["charge"] / b["charge_max"]
                old_ratio = (
                    old["charge"] / old["charge_max"]
                    if (old and old.get("charge_max")) else 1.0
                )
                if ratio < 0.2 and old_ratio >= 0.2:
                    alerts.append(f"TELEPORT LOW CHARGE (<20%): {coords}")
            if self.craft_alerts:
                if "НЕТ ПРОДУКЦИИ" in b["rest"] and (
                    not old or "НЕТ ПРОДУКЦИИ" not in old["rest"]
                ):
                    alerts.append(f"CRAFTER NO MATERIAL: {b['type']} ({coords})")
                if "ЗАВЕРШЕНО" in b["rest"] and (
                    not old or "ЗАВЕРШЕНО" not in old["rest"]
                ):
                    alerts.append(f"CRAFTER DONE: {b['type']} ({coords})")
        return alerts

    def get_stats(self) -> str:
        if not self.current:
            return "No data loaded yet."
        types: Dict[str, int] = {}
        total_c = 0.0
        idle_c: List[str] = []
        ready_c: List[str] = []
        low_hp:  List[str] = []
        low_ch:  List[str] = []
        for b in self.current.values():
            t = b["type"]
            types[t] = types.get(t, 0) + 1
            total_c += sum(b["storage"])
            if b["hp_max"] and b["hp"] / b["hp_max"] < 0.001:
                low_hp.append(b["coords"])
            if b["charge"] is not None and b["charge_max"] and \
               b["charge"] / b["charge_max"] < 0.001:
                low_ch.append(b["coords"])
            if b["type"] == "Крафтер":
                if "НЕТ ПРОДУКЦИИ" in b["rest"]: idle_c.append(b["coords"])
                if "ЗАВЕРШЕНО"     in b["rest"]: ready_c.append(b["coords"])
        lines = [
            f"Stats  (updated: {self.last_update})",
            f"Buildings : {len(self.current)}   excluded: {len(self.excluded)}",
        ]
        for t, n in sorted(types.items()):
            lines.append(f"  {t}: {n}")
        lines.append(f"Resources : {total_c:,.0f}".replace(",", " "))
        if low_hp:  lines.append(f"HP=0%       : {len(low_hp)}  -- {', '.join(low_hp)}")
        if low_ch:  lines.append(f"Charge=0%   : {len(low_ch)}  -- {', '.join(low_ch)}")
        if idle_c:  lines.append(f"No material : {len(idle_c)}  -- {', '.join(idle_c)}")
        if ready_c: lines.append(f"Craft done  : {len(ready_c)} -- {', '.join(ready_c)}")
        lines.append(f"Craft alerts: {'enabled' if self.craft_alerts else 'disabled'}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# VK NOTIFIER
# ---------------------------------------------------------------------------
class VKNotifier:
    def __init__(self, token: str, peer_id: int, enabled: bool):
        self.token   = token
        self.peer_id = peer_id
        self._ok     = enabled and bool(token) and bool(peer_id)
        if enabled and not self._ok:
            log.warning("VK flag set but vk_token or vk_peer_id is missing in config.")

    def send(self, msg: str):
        if not self._ok:
            return
        import urllib.request, urllib.parse
        params = {
            "peer_id": str(self.peer_id), "message": msg,
            "random_id": str(int(time.time() * 1000)),
            "access_token": self.token, "v": "5.131",
        }
        try:
            data = urllib.parse.urlencode(params).encode("utf-8")
            req  = urllib.request.Request(
                "https://api.vk.com/method/messages.send", data=data, method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                log.debug(f"VK response: {r.read().decode()[:100]}")
        except Exception as e:
            log.error(f"VK send error: {e}")


# ---------------------------------------------------------------------------
# CHECKER
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self, cfg: dict, robot: dict, vk_enabled: bool):
        self.cfg    = cfg
        self.robot  = robot
        self.vk     = VKNotifier(cfg["vk_token"], cfg["vk_peer_id"], vk_enabled)
        self.state  = StateTracker(
            excluded     = set(cfg.get("excluded_coords", [])),
            craft_alerts = cfg.get("craft_alerts_enabled", True),
        )
        self.conn    = GameConnection(cfg["server_host"], cfg["server_port"])
        self.auth    = AuthHandler(self.conn, robot, cfg)
        self.scripts = ScriptRunner(self.conn)
        self.monitor = StatusMonitor(self.conn, self.auth, interval=10)
        self._stop   = threading.Event()

        self.conn.on_disconnected = self._on_disconnected

        self.conn.register("PI", self.conn.handle_pi)
        self.auth.attach()
        self.conn.register("cf", self._on_cf)
        self.conn.register("GU", self._on_gu)
        self.conn.register("mU", self._on_mu)
        self.conn.register("ST", lambda m: log.info(f"Server status: {m!r}"))
        self.conn.register("RC", lambda m: log.warning(
            "Server RC: account logged in from another location"
        ))

    def _reconnect_loop(self):
        while not self._stop.is_set():
            if not self.conn.connected:
                log.info(f"Connecting as {self.robot.get('name')}...")
                self.conn.connect()
            self._stop.wait(1)

    def _on_disconnected(self):
        log.warning("Disconnected -- will reconnect")
        self.vk.send(
            f"Checker ({self.robot.get('name')}): connection lost, reconnecting..."
        )
        self._stop.wait(self.cfg["reconnect_delay"])

    def _on_cf(self, msg: str):
        try:
            w = json.loads(msg)
            log.info(
                f"World config: {w.get('width')}x{w.get('height')} "
                f"name={w.get('name', '?')!r}"
            )
        except Exception:
            log.warning(f"cf: could not parse JSON: {msg[:80]!r}")

        hwid = self.robot.get("hwid", "")
        if not hwid:
            log.warning("Robot has no hwid -- Rndm packet will be empty.")

        self.conn.send_typical("Rndm", 0, 0, f"hash={hwid}")
        time.sleep(0.05)
        self.conn.send_typical("Miss", 0, 0, "0")
        time.sleep(0.05)
        self.conn.send_typical("Chin", 0, 0, "_")
        log.info("World init sent (Rndm / Miss / Chin)")

        script = self.cfg.get("script_file", "az.txt")
        if os.path.exists(script):
            def _delayed():
                time.sleep(5)
                log.info(f"Auto-running script: {script}")
                self.scripts.run_file(script)
            threading.Thread(target=_delayed, daemon=True).start()

    def _on_gu(self, msg: str):
        if not msg.startswith("horb:"):
            log.debug(f"GU non-horb popup: {msg[:60]!r}")
            return
        json_str = msg[5:]
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open("packs.log", "w", encoding="utf-8") as f:
                f.write(f"[{ts}] UGUhorb:{json_str}\n")
        except Exception:
            pass
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            log.warning(f"GU JSON parse error: {e}")
            return
        if not any("КЛАНОВЫЕ ПАКИ" in item for item in data.get("richList", [])):
            log.debug("GU horb popup is not a clan pack list -- skipping")
            return
        alerts = self.state.update(data)
        if alerts:
            text = "\n".join(alerts)
            log.info(f"Alerts:\n{text}")
            self.vk.send(text)

    def _on_mu(self, msg: str):
        if len(msg) > 300:
            return
        try:
            start  = msg.index("{")
            end    = msg.rindex("}") + 1
            parsed = json.loads(msg[start:end])
        except (ValueError, json.JSONDecodeError):
            return
        h_list = parsed.get("h", [])
        if not h_list:
            return
        parts = h_list[0].split("±")
        if len(parts) >= 6 and parts[1] == "50":
            message = parts[5].strip()
            if message:
                log.info(f"Clan chat -> VK: {message!r}")
                self.vk.send(f"@{message}")

    # -- command execution (called by TUI) -----------------------------------

    def execute_command(self, cmd: str, args: List[str], out: Callable[[str], None]):
        """
        Execute a CLI command and report output via out().
        cmd  : first word, already lowercased
        args : remaining words
        out  : callable that appends text to the TUI log pane
        """
        if cmd == "stats":
            out(self.state.get_stats())

        elif cmd == "status":
            out(
                f"Connected  : {self.conn.connected}\n"
                f"Ready      : {self.conn.ready}\n"
                f"Auth done  : {self.auth.done.is_set()}\n"
                f"Frames in  : {self.conn.frames_in}\n"
                f"Frames out : {self.conn.frames_out}\n"
                f"Bytes in   : {self.conn.bytes_in}\n"
                f"Bytes out  : {self.conn.bytes_out}\n"
                f"Recv buf   : {len(self.conn._buf)} bytes\n"
                f"Robot      : {self.robot.get('name')}"
            )

        elif cmd == "sendraw":
            if not args:
                out("Usage: sendraw <hex>")
            else:
                try:
                    data = bytes.fromhex(args[0])
                    self.conn.send_raw(data)
                    out(f"Sent {len(data)} bytes.")
                except ValueError:
                    out("Invalid hex string.")

        elif cmd == "runscript":
            fname = args[0] if args else self.cfg.get("script_file", "az.txt")
            self.scripts.run_file(fname)
            out(f"Script started: {fname}")

        elif cmd == "exclude":
            if not args:
                out("Usage: exclude <x:y>")
            else:
                self.state.excluded.add(args[0])
                self.cfg["excluded_coords"] = list(self.state.excluded)
                save_config(self.cfg)
                out(f"Added {args[0]} to exclusions.")

        elif cmd == "unexclude":
            if not args:
                out("Usage: unexclude <x:y>")
            else:
                self.state.excluded.discard(args[0])
                self.cfg["excluded_coords"] = list(self.state.excluded)
                save_config(self.cfg)
                out(f"Removed {args[0]} from exclusions.")

        elif cmd == "listexcluded":
            if self.state.excluded:
                out("Excluded coordinates:\n" +
                    "\n".join(f"  {c}" for c in sorted(self.state.excluded)))
            else:
                out("Exclusion list is empty.")

        elif cmd == "craftalerts":
            self.state.craft_alerts = not self.state.craft_alerts
            self.cfg["craft_alerts_enabled"] = self.state.craft_alerts
            save_config(self.cfg)
            out(f"Craft alerts: {'enabled' if self.state.craft_alerts else 'disabled'}")

        elif cmd == "robot":
            out(
                f"Name : {self.robot.get('name')}\n"
                f"ID   : {self.robot.get('id')}\n"
                f"HWID : {self.robot.get('hwid')}"
            )

        else:
            out(f"Unknown command '{cmd}'. Type 'help' for a list of commands.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Game checker")
    parser.add_argument("--vk",    action="store_true", help="Enable VK notifications")
    parser.add_argument("--debug", action="store_true", help="Log raw frames to debug.log")
    args = parser.parse_args()

    # File logging starts before curses takes over the terminal
    _setup_file_logging(args.debug)

    cfg = load_config()
    save_config(cfg)

    robots = load_robots()
    if not robots:
        example = [{
            "name": "Robot-Example", "hwid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "uniq": "", "hash": "", "id": "", "isLoggedIn": False,
        }]
        save_robots(example)
        print(
            f"\nCreated example {ROBOTS_FILE}.\n"
            "Fill in credentials and restart.\n\n"
            "id / hash : run the game with the proxy, find the 'AH' event in\n"
            "            packets.log -- payload is '<id>_<hash>'.\n\n"
            "hwid      : find a TY binary packet with inner type 'Rndm'.\n"
            "            Payload after the 16-byte header is 'hash=<HWID>'.\n"
        )
        sys.exit(0)

    # Robot selection runs in normal terminal mode before curses starts
    robot = choose_robot(robots)
    if robot is None:
        sys.exit(1)

    print(
        f"\nStarting checker  robot={robot.get('name')!r}  "
        f"vk={'on' if args.vk else 'off'}  "
        f"debug={'on' if args.debug else 'off'}"
    )
    if args.debug:
        print(f"Raw frames -> {DEBUG_LOG}")
    print("Launching TUI...")
    time.sleep(0.5)   # brief pause so the user can read the above

    checker = Checker(cfg, robot, vk_enabled=args.vk)
    tui     = TUI()
    tui.run(checker)   # blocks until quit

    save_robots(robots)
    save_config(cfg)


if __name__ == "__main__":
    main()
