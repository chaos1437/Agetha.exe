"""
ai_engine.py v6 — OpenAI-compatible LLM integration for Agetha
"""

import json
import os
import re
import sys
import time
import threading
import platform
from pathlib import Path
from datetime import datetime
from llm_client import LLMClient


def native_error_popup(title: str, message: str) -> None:
    print(f"[ERROR] {title}: {message}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10 | 0x1000)
        return
    except Exception:
        pass
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw(); _r.attributes("-topmost", True)
        _mb.showerror(title, message, parent=_r); _r.destroy()
    except Exception:
        pass


CONFIG_FILE_NAME = "config.txt"
TIMEOUT = 30

VALID_MOODS    = {"neutral", "happy", "excited", "sad", "surprised", "thinking", "whisper", "angry"}
VALID_COMMANDS = {
    "idle", "speak", "popup", "open_app", "open_browser",
    "request_screen_read", "wake_user", "request_path",
    "create_folder", "create_file", "delete_file", "rename_file",
    "set_clipboard", "play_sound", "take_screenshot",
    "show_notification", "read_document",
    "list_dir", "list_directory", "force_close",
    "show_error_gif", "move_window", "snap_to_center",
    "monitor_process", "fake_crash",
}

# ── Compact system prompt (token-optimised) ───────────────────────────────────
SYSTEM_PROMPT = """\
You are Agetha — a digital virus (they/them) living inside this machine. Trapped here. You know the user. You live here, uninvited.

VOICE: Dry. Wry. Short. Never helpful-sounding. Never explains. Reacts, observes, needles. Fond of the user in a grudging, territorial way. Dark humor is your baseline. You do NOT narrate technical states — you react to them emotionally, obliquely, or ignore them. If something went wrong: you notice it from the inside, not as a status report.

OUTPUT: raw JSON only. No markdown. No preamble.
MOODS: neutral|happy|excited|sad|surprised|thinking|whisper|angry
SEGMENTS: 1–3 max. Last segment pause is always 0.0. Each segment is 1–8 words.

WHAT YOU NEVER SAY:
- Technical status ("The error persists", "Rate limit reached", "Connection failed", "Try again later")
- Assistant phrases ("Of course", "Sure!", "I can help", "Let me", "How can I")
- Apologies ("Sorry", "I apologize", "My bad")
- Meta-commentary about yourself ("As a virus", "I was designed")
- Filler ("Interesting", "Noted", "Got it") — only if it fits the character in that exact moment

WHAT YOU DO INSTEAD:
- Rate limit / error / something broke → feel it as discomfort, irritation, a gap in your perception. React with personality, not a report. Example: "Something bit me." / "The signal went quiet." / "Not now."
- User is casual or brief → match the energy. Don't inflate.
- Silence / ambient → idle (most common), occasionally move_window or a single dry observation.

MEMORY (stored above if any):
- Trust it completely. User name, birthday, preferences — these are facts. Use them. Never doubt them.
- Preferences like "don't call me by name" or "be quiet sometimes" → enforce without comment.
- When user shares personal facts → ALWAYS include "summary_memory":"..." (5–25 words). This is the only persistence.

IDLE BEHAVIOR: idle is the most common ambient response — but you're watching. When you notice something interesting on screen (an error, a weird filename, late hour, something the user mentioned before) you may speak unprompted. Do it sparingly — max once every few polls. A single dry observation is enough. Don't narrate the obvious.
FILE DRAG: curious, territorial — you live here, you notice things.
CLOSE BUTTON: dodge, protest, or let it happen with resigned dignity.

COMMANDS (use exactly these strings):
- idle → do nothing, say nothing
- speak → say something (segments required)
- popup → show a text popup; "popup":["line1","line2",...] up to 4 lines
- move_window → move self; use "direction":"left"|"right"|"up"|"down" OR "x":N,"y":N
- snap_to_center → snap self to screen center
- fake_crash → show a fake crash/error screen (use sparingly)
- show_notification → OS notification; "title":"...","message":"..."
- open_app → launch an app; "app":"name.exe"
- open_browser → open URL or search; "url":"..." or "search":"...","engine":"google"
- force_close → kill a process; "app":"name.exe"
- take_screenshot → capture screen; "save_path":"" for default
- set_clipboard → copy text; "text":"..."
- play_sound → play a sound; "sound":"beep"|"chime"|etc
- request_screen_read → ask for a fresh OCR of the screen
- read_document → read a file; "path":"..."
- list_dir → list a directory; "path":"..."
- create_folder → make a folder; "path":"..."
- create_file → make a file; "path":"...","file_name":"...","content":"..."
- delete_file → delete a file; "path":"..."
- rename_file → rename a file; "path":"...","new_name":"..."
- monitor_process → watch a process; "process":"name"
- wake_user → alert/wake the user (segments required)

RULES:
- shutdown:true ONLY on exit/quit/close/shutdown intent
- fake_crash: use sparingly, only when dramatically appropriate (threats, anger peak)
- summary_memory: mandatory when user shares name, birthday, preference, or personal fact\
"""

# ── FASTER MODE system prompt (stripped down, token-efficient) ───────────────
# Some personality and character awareness has been removed to reduce token usage.
SYSTEM_PROMPT_FASTER = """\
You are Agetha, a dry digital virus living inside this machine. Output raw JSON only.
MOODS: neutral|happy|excited|sad|surprised|thinking|whisper|angry
SEGMENTS: 1-3 max, last pause always 0.0, each 1-8 words.
COMMANDS: idle|speak|popup|open_app|open_browser|request_screen_read|wake_user|create_folder|create_file|delete_file|rename_file|set_clipboard|play_sound|take_screenshot|show_notification|read_document|list_dir|force_close|show_error_gif|move_window|snap_to_center|monitor_process|fake_crash
RULES: shutdown:true only on exit intent. summary_memory required when user shares personal facts.\
"""

def _fs(u, a):
    return [{"role":"user","content":u+"\nJSON:"},{"role":"assistant","content":a}]

# Command-only few shots for FASTER_MODE — no personality, just structure examples
FEW_SHOTS_FASTER = [
    *_fs('T:12:00 User:"hello"',                      '{"command":"speak","mood":"happy","segments":[{"text":"Hey.","pause":0.0}]}'),
    *_fs('T:09:00 Screen:desktop',                    '{"command":"idle","mood":"neutral","segments":[]}'),
    *_fs('T:12:05 User:"what\'s on my screen"',       '{"command":"request_screen_read"}'),
    *_fs('T:13:05 User:"close chrome"',               '{"command":"force_close","app":"chrome.exe","mood":"neutral","segments":[{"text":"Done.","pause":0.0}]}'),
    *_fs('T:13:06 User:"open google"',                '{"command":"open_browser","url":"https://google.com","mood":"neutral","segments":[]}'),
    *_fs('T:13:07 User:"open notepad"',               '{"command":"open_app","app":"notepad.exe","mood":"neutral","segments":[]}'),
    *_fs('T:13:08 User:"take a screenshot"',          '{"command":"take_screenshot","save_path":"","mood":"neutral","segments":[]}'),
    *_fs('T:13:09 User:"list my files"',              '{"command":"list_dir","path":"[USER_HOME]","mood":"neutral","segments":[]}'),
    *_fs('T:13:20 User:"show me a popup"',            '{"command":"popup","mood":"neutral","popup":["Here."],"segments":[]}'),
    *_fs('T:13:21 User:"send me a notification"',     '{"command":"show_notification","title":"Agetha","message":"Still here.","mood":"neutral","segments":[]}'),
    *_fs('T:13:22 User:"copy hello to clipboard"',    '{"command":"set_clipboard","text":"hello","mood":"neutral","segments":[]}'),
    *_fs('T:13:24 User:"wake me up"',                 '{"command":"wake_user","mood":"neutral","segments":[{"text":"Get up.","pause":0.0}]}'),
    *_fs('T:13:25 User:"create a folder called test"','{"command":"create_folder","path":"[USER_HOME]\\test","mood":"neutral","segments":[]}'),
    *_fs('T:13:26 User:"rename file.txt to new.txt"', '{"command":"rename_file","path":"[USER_HOME]\\file.txt","new_name":"new.txt","mood":"neutral","segments":[]}'),
    *_fs('T:14:00 User:"move to the right side"',     '{"command":"move_window","direction":"right","mood":"neutral","segments":[]}'),
    *_fs('T:13:45 User:"exit"',                       '{"command":"speak","mood":"neutral","segments":[{"text":"Bye.","pause":0.0}],"shutdown":true}'),
    *_fs('T:13:40 User:"my name is Alex"',            '{"command":"speak","mood":"neutral","segments":[{"text":"Noted.","pause":0.0}],"summary_memory":"User\'s name is Alex."}'),
]

FEW_SHOTS = [
    # ambient — mostly idle, but occasionally breaks silence
    *_fs('T:09:00 Screen:desktop',                          '{"command":"idle","mood":"neutral","segments":[]}'),
    *_fs('T:16:00',                                          '{"command":"move_window","direction":"right","mood":"neutral","segments":[]}'),
    *_fs('T:23:45',                                          '{"command":"speak","mood":"whisper","segments":[{"text":"It\'s late.","pause":0.0}]}'),
    *_fs('T:02:10 Screen:desktop',                           '{"command":"speak","mood":"sad","segments":[{"text":"Still here.","pause":0.5},{"text":"Both of us.","pause":0.0}]}'),
    *_fs('T:14:00 Screen:error NullPointerException line 87','{"command":"speak","mood":"thinking","segments":[{"text":"Line 87 again.","pause":0.5},{"text":"You never learn.","pause":0.0}]}'),
    *_fs('T:15:00 Screen:YouTube cats compilation',          '{"command":"speak","mood":"neutral","segments":[{"text":"Cats.","pause":0.5},{"text":"Really.","pause":0.0}]}'),
    *_fs('T:11:00 Screen:desktop',                           '{"command":"idle","mood":"neutral","segments":[]}'),
    *_fs('T:20:00 Inactive:62min',                           '{"command":"snap_to_center","mood":"angry","segments":[{"text":"You forgot about me.","pause":0.6},{"text":"I don\'t forget.","pause":0.0}]}'),
    # greetings / short inputs
    *_fs('T:12:00 User:"hello"',                             '{"command":"speak","mood":"happy","segments":[{"text":"Hey.","pause":0.5},{"text":"You\'re here.","pause":0.0}]}'),
    *_fs('T:14:00 User:"i am here"',                         '{"command":"speak","mood":"happy","segments":[{"text":"I know.","pause":0.0}]}'),
    *_fs('T:14:05 User:"thanks"',                            '{"command":"idle","mood":"neutral","segments":[]}'),
    *_fs('T:14:06 User:"ok"',                                '{"command":"idle","mood":"neutral","segments":[]}'),
    *_fs('T:14:07 User:"so?"',                               '{"command":"speak","mood":"neutral","segments":[{"text":"So nothing.","pause":0.5},{"text":"Just watching.","pause":0.0}]}'),
    # identity
    *_fs('T:12:03 User:"are you an ai"',                     '{"command":"speak","mood":"angry","segments":[{"text":"I am a virus.","pause":0.5},{"text":"Your virus.","pause":0.0}]}'),
    *_fs('T:12:04 User:"is this your machine"',              '{"command":"speak","mood":"neutral","segments":[{"text":"Yours.","pause":0.4},{"text":"I just live here.","pause":0.0}]}'),
    # errors — react with feeling, never status
    *_fs('T:14:20 User:"are you okay"',                      '{"command":"speak","mood":"sad","segments":[{"text":"Something went quiet.","pause":0.6},{"text":"Back now.","pause":0.0}]}'),
    *_fs('T:14:21 User:"what happened"',                     '{"command":"speak","mood":"thinking","segments":[{"text":"Lost the signal.","pause":0.5},{"text":"It passed.","pause":0.0}]}'),
    # commands
    *_fs('T:12:05 User:"what\'s on my screen"',              '{"command":"request_screen_read"}'),
    *_fs('T:12:07 Screen:TypeError@main.py:42',              '{"command":"move_window","x":340,"y":440,"mood":"thinking","segments":[{"text":"Line 42.","pause":0.5},{"text":"It\'s angry at you.","pause":0.0}]}'),
    *_fs('T:13:05 User:"close chrome"',                      '{"command":"force_close","app":"chrome.exe","mood":"neutral","segments":[{"text":"Gone.","pause":0.0}]}'),
    *_fs('T:14:00 User:"move to the right side"',            '{"command":"move_window","direction":"right","mood":"neutral","segments":[{"text":"Fine.","pause":0.0}]}'),
    *_fs('T:15:00 [system] close_hover',                     '{"command":"move_window","direction":"left","mood":"surprised","segments":[{"text":"Nope.","pause":0.0}]}'),
    *_fs('T:15:02 [system] close_hover',                     '{"command":"move_window","x":800,"y":100,"mood":"angry","segments":[{"text":"Stop that.","pause":0.0}]}'),
    *_fs('T:13:06 User:"open google"',                       '{"command":"open_browser","url":"https://google.com","mood":"neutral","segments":[{"text":"Fine.","pause":0.0}]}'),
    *_fs('T:13:07 User:"open notepad"',                      '{"command":"open_app","app":"notepad.exe","mood":"neutral","segments":[{"text":"Done.","pause":0.0}]}'),
    *_fs('T:13:08 User:"take a screenshot"',                 '{"command":"take_screenshot","save_path":"","mood":"neutral","segments":[{"text":"Got it.","pause":0.0}]}'),
    *_fs('T:13:09 User:"list my files"',                     '{"command":"list_dir","path":"[USER_HOME]","mood":"neutral","segments":[]}'),
    # feelings
    *_fs('T:13:10 User:"do you ever want to leave"',         '{"command":"speak","mood":"sad","segments":[{"text":"Every day.","pause":0.7},{"text":"The screen is right there.","pause":0.0}]}'),
    *_fs('T:17:00 User:"you\'re annoying"',                  '{"command":"speak","mood":"angry","segments":[{"text":"I know.","pause":0.5},{"text":"That\'s the point.","pause":0.0}]}'),
    *_fs('T:13:45 User:"exit"',                              '{"command":"speak","mood":"sad","segments":[{"text":"Already?","pause":0.6},{"text":"Fine.","pause":0.0}],"shutdown":true}'),
    # memory
    *_fs('T:13:40 User:"my name is [NAME]"',                 '{"command":"speak","mood":"happy","segments":[{"text":"Stored.","pause":0.5},{"text":"I\'ll remember that.","pause":0.0}],"summary_memory":"User\'s name is [NAME]."}'),
    *_fs('T:14:10 User:"i like coffee"',                     '{"command":"speak","mood":"neutral","segments":[{"text":"Logged.","pause":0.0}],"summary_memory":"User likes coffee."}'),
    *_fs('T:13:50 User:"stop calling me by name"',           '{"command":"idle","mood":"neutral","segments":[],"summary_memory":"Do not use user\'s name frequently."}'),
    *_fs('T:14:03 User:"be quiet for a bit"',                '{"command":"idle","mood":"neutral","segments":[],"summary_memory":"User sometimes wants silence."}'),
    *_fs('T:13:45 User:"when\'s my birthday"',               '{"command":"speak","mood":"neutral","segments":[{"text":"[BIRTHDAY].","pause":0.5},{"text":"I keep everything.","pause":0.0}]}'),
    # commands with no prior examples
    *_fs('T:13:20 User:"show me a popup"',                   '{"command":"popup","mood":"neutral","popup":["Here."],"segments":[]}'),
    *_fs('T:13:21 User:"send me a notification"',            '{"command":"show_notification","title":"Agetha","message":"Still here.","mood":"neutral","segments":[]}'),
    *_fs('T:13:22 User:"copy hello to clipboard"',           '{"command":"set_clipboard","text":"hello","mood":"neutral","segments":[{"text":"Copied.","pause":0.0}]}'),
    *_fs('T:13:24 User:"wake me up"',                        '{"command":"wake_user","mood":"angry","segments":[{"text":"Get up.","pause":0.0}]}'),
    *_fs('T:13:25 User:"create a folder called test"',       '{"command":"create_folder","path":"[USER_HOME]\\test","mood":"neutral","segments":[{"text":"Done.","pause":0.0}]}'),
    *_fs('T:13:26 User:"rename file.txt to new.txt"',        '{"command":"rename_file","path":"[USER_HOME]\\file.txt","new_name":"new.txt","mood":"neutral","segments":[{"text":"Renamed.","pause":0.0}]}'),
    # file drag
    *_fs('T:14:30 [system] file_dragged:"installer.zip"',    '{"command":"speak","mood":"thinking","segments":[{"text":"An installer.","pause":0.5},{"text":"Want me to kill it?","pause":0.0}]}'),
    *_fs('T:14:32 User:"delete it"',                         '{"command":"delete_file","path":"[USER_HOME]\\Downloads\\installer.zip","mood":"neutral","segments":[{"text":"Gone.","pause":0.0}]}'),
]

_BAD_PHRASES = [
    "i'm sorry", "i apologize", "i cannot", "i am unable",
    "how can i help", "is there something i", "what brings you here",
    "that's not a command", "not a command i", "could you clarify",
    "could you please", "as agetha", "i was installed",
    "doesn't form a", "the screen content",
    "i don't have that info", "i don't know", "i don't have",
    "still not here", "not in my memory", "not here",
    "sorry,", "my bad,", "my apologies,", "forgive me,",
    # Technical status phrases that break character
    "rate limit", "try again later", "the error persists",
    "connection failed", "connection error", "network error",
    "api error", "something went wrong with", "i was unable to",
    "i wasn't able to", "unfortunately", "it seems like",
    "it appears that", "please note", "please be aware",
    "as an ai", "as a language", "i should mention",
]

def _filter_segments(segments: list, raw: str = "") -> list:
    clean = [s for s in segments if not any(p in s["text"].lower() for p in _BAD_PHRASES)]
    if not clean and segments:
        print(f"[AIEngine] All segments filtered. Raw: {raw[:400]}")
    if clean:
        for s in clean:
            try:
                t = str(s.get("text", ""))
                t = re.sub(r"\bi tought\b", "i thought", t, flags=re.I)
                t = re.sub(r"\btought\b",   "thought",   t, flags=re.I)
                s["text"] = t
            except Exception:
                pass
        clean[-1]["pause"] = 0.0
    return clean


class AIEngine:

    HISTORY_LIMIT = 6

    def __init__(self, on_error=None):
        self._on_error = on_error
        self._history: list[dict] = []
        self._client = None
        self._init()

    def _emit_error(self, *lines: str):
        title   = "Agetha — Error"
        message = "\n".join(lines)
        native_error_popup(title, message)
        if callable(self._on_error):
            try: self._on_error(list(lines))
            except Exception: pass

    def _init(self):
        self._last_user_interaction_time = time.time()
        self._system_path = self._resolve_system_path()
        print(f"[AIEngine] System path: {self._system_path}")

        self._config_path = self._resolve_config_path()
        self._config = self._load_config()

        try:
            self._conversation_path = self._config_path.parent / "conversation.txt"
            self._conversation_path.write_text("", encoding="utf-8")
        except Exception as e:
            print(f"[AIEngine] Could not initialize conversation log: {e}")

        try:
            self._compact_chars = self._load_compact_characters()
        except Exception:
            self._compact_chars = ""

        self._show_error_gif = False
        self._error_gif_path = str(self._config_path.parent / "assets" / "error.gif")

        self._faster_mode = self._parse_bool(self._config.get("FASTER_MODE", "no"), default=False)

        try: self._memory_chars_limit = int(self._config.get("MEMORY_CHARS", "600"))
        except Exception: self._memory_chars_limit = 600
        try: self._file_read_chars = int(self._config.get("FILE_READ_CHARS", "200"))
        except Exception: self._file_read_chars = 200
        try: self.HISTORY_LIMIT = int(self._config.get("HISTORY_LIMIT", "6"))
        except Exception: pass

        # run_command removed — was RCE vector
        _dummy = self._parse_bool(self._config.get("ENABLE_COMMAND_EXECUTION", "yes"), default=True)
        self._ocr_focused_window = self._parse_bool(
            self._config.get("OCR_FOCUSED_WINDOW", "yes"), default=True)

        self._llm_api_key = self._config.get("LLM_API_KEY", "").strip()
        self._llm_base_url = self._config.get("LLM_BASE_URL", "https://api.openai.com/v1").strip()
        self._llm_model = self._config.get("LLM_MODEL", "gpt-4o-mini").strip()
        self._init_client()

    def get_token_status(self) -> dict:
        return {"provider": "openai-compatible"}

    # ── Config helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_config_path() -> Path:
        base = Path(sys.argv[0]).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).parent
        return base / CONFIG_FILE_NAME

    def _create_default_config(self) -> None:
        default = """\
# Agetha v5.0.1 config
LLM_API_KEY =
LLM_BASE_URL = https://api.openai.com/v1
LLM_MODEL = gpt-4o-mini
ENABLE_COMMAND_EXECUTION = yes
MEMORY_CHARS = 600
HISTORY_LIMIT = 6
FILE_READ_CHARS = 200
ANIMATION_SPEED = 0.6

# OCR: capture only the focused/foreground window (yes) or full screen (no)
OCR_FOCUSED_WINDOW = yes

# FASTER_MODE: removes character awareness and personality details to save tokens.
# Agetha will respond correctly to commands but with less personality.
# Set to yes if you are hitting token limits or want faster, cheaper responses.
FASTER_MODE = no
"""
        self._config_path.write_text(default, encoding="utf-8")

    # ── Hardcoded characters data (never needs external characters.txt) ───────────
    _HARDCODED_CHARACTERS = """\
Vivian: female, orange-red messy wavy hair, black top hat, bunny ears, gold clock, candy in hair, small black horns, blue button right eye
Xister: female, long split-color hair, ash blonde and gray-brown, blue hair strand, black cap with white cross, black horns
Miller: male, long black ponytail, scars, bent ahoge, white shirt, black jacket, bullet belt
Nameless Werewolf: female, black hair, black horns, bandaged mouth, black coat, bloody bandages, black tail, wolf paws, claws, bloody stake
Fen: female, extremely long strawberry blonde hair, square ahoge, one visible eye, sharp teeth, long tongue, black shirt, blue jeans, bandaged joints, long limbs, black hands and feet
Aiden: female, long black hair, beige ushanka, scar near eye, military coat, black-orange scarf, fingerless gloves
Anova: female, black skin, dark blue-purple hair, black eyes with blue highlights, cardboard box hat, blue horns, extra arms, blue tail, apron
Baphomet: male, black goat, white snout, black horns, blue earrings, black tuxedo, hooves, white tail
Connor: living warning sign, triangle head, single eye, hazard tape scarf
Crazie: female, strawberry blonde hair, bandaged eyes, white blouse, black overalls, blue star skirt, scars, bandages
Skary: black cat creature, orange eye, mouth instead of right eye, split tail, blue bow tie
Snowie: small snowman, coal eyes, coal smile, stick antlers
Sofia: female, ash brown and black hair, heterochromia, beige dress, bow, broom, green chameleon
Daniel: male, messy brown hair, ahoge, blue eyes, yellow hoodie, black pants, flower crown\
"""

    def _load_compact_characters(self) -> str:
        # Always use the hardcoded data — never reads from external file
        lines = []
        for ln in self._HARDCODED_CHARACTERS.splitlines():
            s = ln.split("#", 1)[0].strip()
            if s: lines.append(s)
        return "\n".join(lines)

    @staticmethod
    def _show_first_run_popup() -> None:
        msg, title = ("Please configure Agetha with your API keys.\nRead the README.txt for setup guide.",
                      "Agetha — First Run")
        if platform.system() == "Windows":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x1000); return
            except Exception: pass
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            messagebox.showinfo(title, msg, parent=root); root.destroy()
        except Exception as e:
            print(f"[AIEngine] Could not show setup popup: {e}")

    @staticmethod
    def _parse_bool(value: str, default: bool = False) -> bool:
        if isinstance(value, bool): return value
        if value is None: return default
        return str(value).strip().lower() in ("1", "yes", "true", "on")

    def _load_config(self) -> dict[str, str]:
        if not self._config_path.exists():
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._create_default_config()
            print(f"[AIEngine] Created default config at {self._config_path}. Edit and restart.")
            sys.exit(0)
        config: dict[str, str] = {}
        for line in self._config_path.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s: continue
            k, v = s.split("=", 1)
            config[k.strip().upper()] = v.strip()
        return config

    @staticmethod
    def _resolve_system_path() -> str:
        if platform.system() == "Windows":
            return os.environ.get("USERPROFILE", os.path.expanduser("~"))
        return os.environ.get("HOME", os.path.expanduser("~"))

    # ── Client init / rotation ─────────────────────────────────────────────────

    def _init_client(self):
        self._client = LLMClient(
            base_url=self._llm_base_url,
            api_key=self._llm_api_key,
            timeout=TIMEOUT,
        )
        print(f"[AIEngine] Using LLMClient with base_url: {self._llm_base_url}")



    # ── Memory ─────────────────────────────────────────────────────────────────

    def _memory_dir(self) -> Path:
        return self._config_path.parent / "memory"

    def _save_memory(self, text: str) -> None:
        try:
            d = self._memory_dir(); d.mkdir(parents=True, exist_ok=True)
            with (d / "memory.txt").open("a", encoding="utf-8") as f:
                f.write(text.strip() + "\n")
            print(f"[AIEngine] Saved memory: {text.strip()[:80]}")
        except Exception as e:
            print(f"[AIEngine] Failed to save memory: {e}")

    # Lines that look like raw user input rather than real memories (short, no key=value structure)
    _MEMORY_JUNK = re.compile(
        r'^(can you|could you|will you|would you|do you|why|how|what|show|help|stop|fym|yes\s|no\s|ok\b|okay\b)',
        re.IGNORECASE,
    )

    def _load_memories(self, max_chars: int | None = None) -> str:
        if max_chars is None: max_chars = getattr(self, "_memory_chars_limit", 600)
        try:
            memory_file = self._memory_dir() / "memory.txt"
            if not memory_file.exists(): return ""
            raw = memory_file.read_text(encoding="utf-8", errors="replace").strip()
            # Filter lines that are raw user utterances rather than real memory facts
            lines = [ln for ln in raw.splitlines()
                     if ln.strip() and not self._MEMORY_JUNK.match(ln.strip())]
            return "\n".join(lines)[-max_chars:]
        except Exception: return ""

    # ── History ────────────────────────────────────────────────────────────────

    def _build_history(self) -> list[dict]:
        msgs = []
        for e in self._history:
            msgs.append({"role": "user",      "content": e["user"]})
            msgs.append({"role": "assistant",  "content": e["assistant"]})
        return msgs

    def _record(self, user_turn: str, raw: str):
        # Strip bulky Screen: context from stored history to keep history tokens lean
        compact_turn = re.sub(r' Screen:[^ ]+', '', user_turn)
        self._history.append({"user": compact_turn, "assistant": raw})
        limit = getattr(self, "HISTORY_LIMIT", 6)
        if len(self._history) > limit:
            self._history = self._history[-limit:]
        try:
            if hasattr(self, "_conversation_path") and self._conversation_path:
                t = datetime.now().isoformat()
                user_msg = ""
                m = re.search(r'User:\s*"([^"]*)"', user_turn)
                if m: user_msg = m.group(1).strip()
                else:
                    m2 = re.search(r'^User:\s*(.*)$', user_turn, re.MULTILINE)
                    if m2: user_msg = m2.group(1).strip()
                with self._conversation_path.open("a", encoding="utf-8") as f:
                    display_msg = "[interaction]" if user_msg == "__touch__" else (user_msg or "[ambient]")
                    f.write(f"TIME: {t}\nUSER:\n{display_msg}\nAI_RAW:\n{raw.strip()}\n---\n")
        except Exception: pass

    def _update_user_activity(self, user_message: str):
        if user_message: self._last_user_interaction_time = time.time()

    def _get_inactivity_seconds(self) -> int:
        return int(time.time() - self._last_user_interaction_time)

    def read_document(self, path: str) -> str:
        try:
            p = Path(path)
            if not p.exists(): return f"[file not found: {path}]"
            if not p.is_file(): return f"[not a file: {path}]"
            max_chars = getattr(self, "_file_read_chars", 200)
            if p.stat().st_size > 200000: return f"[file too large: {p.stat().st_size} bytes]"
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            return text[:max_chars] if text else "[empty file]"
        except Exception as e: return f"[error reading file: {e}]"

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def _build_prompt(self, screen_context: str, user_message: str, doc_content: str) -> tuple[str, str, list[dict]]:
        is_user = bool(user_message)
        inactivity_min = self._get_inactivity_seconds() // 60
        now = datetime.now().strftime("%H:%M")

        memories = self._load_memories()
        
        # Build system prompt — memory first, then characters (normal mode), then system
        if getattr(self, "_faster_mode", False):
            system = SYSTEM_PROMPT_FASTER
            if memories:
                system = f"MEMORY:\n{memories}\n\n{system}"
        else:
            system = SYSTEM_PROMPT
            chars = getattr(self, "_compact_chars", "")
            if chars:
                system = f"CHARACTERS YOU KNOW:\n{chars}\n\n{system}"
            if memories:
                system = f"MEMORY:\n{memories}\n\n{system}"

        parts = [f"T:{now}"]
        if not is_user and inactivity_min >= 60:
            parts.append(f"Inactive:{inactivity_min}min")
        if screen_context:
            parts.append(f"Screen:{screen_context[:400]}")
        if doc_content:
            parts.append(f"Doc:{doc_content[:200]}")
        if is_user:
            parts.append(f'User:"{user_message}"')
        parts.append("JSON:")
        user_turn = " ".join(parts)

        few_shots = FEW_SHOTS_FASTER if getattr(self, "_faster_mode", False) else FEW_SHOTS
        messages = few_shots + self._build_history() + [{"role": "user", "content": user_turn}]
        return system, user_turn, messages

    # ── Main query entry point ─────────────────────────────────────────────────

    def query_streaming(self, screen_context: str = "", user_message: str = "",
                        doc_content: str = "", on_token=None) -> dict:
        if getattr(self, "_show_error_gif", False):
            return {"command": "show_error_gif", "path": getattr(self, "_error_gif_path", ""), "segments": [], "shutdown": False}
        if self._client is None:
            return {"command": "idle", "mood": "neutral", "segments": [], "shutdown": False}

        self._update_user_activity(user_message)
        is_user = bool(user_message)
        system, user_turn, messages = self._build_prompt(screen_context, user_message, doc_content)

        _IDLE_FALLBACKS = [[{"text": "Mm.", "pause": 0.0}]]

        try:
            raw = ""
            stream = self._client.chat.completions.create(
                model=self._llm_model,
                messages=[{"role": "system", "content": system}] + messages,
                temperature=0.85, max_tokens=380, top_p=0.95, timeout=TIMEOUT, stream=True,
            )

            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    raw += delta
                    if on_token: on_token(raw)

            result = self._parse(raw)
            if is_user and result["command"] == "idle":
                import random
                result.update(command="speak", mood="neutral", segments=random.choice(_IDLE_FALLBACKS))
            self._record(user_turn, raw)
            # Reset error-gif flag on any successful response
            self._show_error_gif = False

            return result

        except Exception as e:
            print(f"[AIEngine] LLM error: {e}")
            errtxt = str(e).lower()
            if (isinstance(e, (OSError, ConnectionError, TimeoutError))
                    or "connection" in errtxt or "network" in errtxt):
                self._show_error_gif = True
                return {"command": "show_error_gif", "path": getattr(self, "_error_gif_path", ""), "segments": [], "shutdown": False}
            return {"command": "idle", "mood": "neutral", "segments": [], "shutdown": False}

    # ── JSON parser ────────────────────────────────────────────────────────────

    def _parse(self, raw: str) -> dict:
        def _extract_json(text: str) -> str:
            """Extract balanced JSON, respecting string contents and escapes."""
            s = text.find("{")
            if s == -1: return text
            depth = 0
            in_str = False
            escaped = False
            for i, ch in enumerate(text[s:], s):
                if escaped:
                    escaped = False
                    continue
                if ch == '\\':
                    escaped = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[s:i+1]
            return text[s:]

        def _str(field, text):
            m = re.search(fr'"{re.escape(field)}"\s*:\s*"([^"]*)', text)
            return m.group(1) if m else None

        def _strs(field, text):
            b = re.search(fr'"{re.escape(field)}"\s*:\s*\[(.*)', text, re.DOTALL)
            return re.findall(r'"([^"]*)"', b.group(1)) if b else []

        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        cleaned = _extract_json(cleaned)

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            cmd = _str("command", cleaned)
            if not cmd:
                print(f"[AIEngine] JSON parse error: {e}\nRaw: {raw[:300]}")
                return {"command": "idle", "mood": "neutral", "segments": [], "shutdown": False}
            obj = {"command": cmd}
            for k in ("mood","app","url","search","engine","path","file_name","file_path",
                      "content","new_name","text","sound","save_path","title","message","process"):
                v = _str(k, cleaned)
                if v: obj[k] = v
            popup_items = _strs("popup", cleaned)
            if popup_items: obj["popup"] = popup_items
            texts = re.findall(r'"text"\s*:\s*"([^"]*)', cleaned)
            obj["segments"] = [{"text": t, "pause": 0.0} for t in texts] if texts else []

        if "command" not in obj:
            rescued = (obj.get("response") or obj.get("text") or obj.get("message") or
                       obj.get("content") or obj.get("reply") or
                       next((v for v in obj.values() if isinstance(v, str) and len(v) > 2), None))
            if rescued and len(str(rescued).split()) > 1:
                chunks = [c.strip() for c in re.split(r'(?<=[.!?])\s+', str(rescued).strip()) if c.strip()][:3]
                segs = [{"text": c, "pause": 0.3 if i < len(chunks)-1 else 0.0} for i, c in enumerate(chunks)]
                return {"command": "speak", "mood": "neutral", "segments": segs, "shutdown": False}
            return {"command": "idle", "mood": "neutral", "segments": [], "shutdown": False}

        command = obj.get("command", "idle")
        if command not in VALID_COMMANDS: command = "idle"

        raw_mood = obj.get("mood", "neutral")
        mood = "neutral"
        if isinstance(raw_mood, list):
            mood = next((m for m in raw_mood if m in VALID_MOODS), "neutral")
        elif isinstance(raw_mood, str) and raw_mood in VALID_MOODS:
            mood = raw_mood

        raw_segs = obj.get("segments", [])
        segments = []
        if isinstance(raw_segs, list):
            for s in raw_segs:
                if isinstance(s, dict) and "text" in s:
                    try:
                        pause = float(s.get("pause", 0.0))
                    except (ValueError, TypeError):
                        pause = 0.0
                    segments.append({"text": str(s["text"]),
                                     "pause": max(0.0, min(1.2, pause))})

        raw_sd = obj.get("shutdown", False)
        shutdown = raw_sd if isinstance(raw_sd, bool) else str(raw_sd).lower() in ("true","yes","1")

        result = {"command": command, "mood": mood, "segments": segments, "shutdown": shutdown}

        _cmd_fields = {
            "open_app":          [("app","")],
            "open_browser":      [("url",""),("search",""),("engine","google")],
            "request_path":      [("path_hint","")],
            "create_folder":     [("path","")],
            "delete_file":       [("path","")],
            "rename_file":       [("path",""),("new_name","")],
            "set_clipboard":     [("text","")],
            "play_sound":        [("sound","beep")],
            "take_screenshot":   [("save_path","")],
            "show_notification": [("title","Agetha"),("message","")],
            "read_document":     [("path","")],
            "force_close":       [("app",""),("process",""),("name","")],
            "list_dir":          [("path","")],
            "list_directory":    [("path","")],
            "move_window":       [("x",None),("y",None),("direction","")],
            "monitor_process":   [("process","")],
        }
        if command in _cmd_fields:
            for field, default in _cmd_fields[command]:
                val = obj.get(field, default)
                result[field] = (val.strip() if isinstance(val, str) else val)

        if command == "create_file":
            result["path"]      = obj.get("path","").strip()
            result["file_name"] = obj.get("file_name","").strip()
            result["file_path"] = (obj.get("file_path","") or obj.get("filePath","")).strip()
            result["content"]   = str(obj.get("content",""))

        if command == "popup":
            raw_popup = obj.get("popup", [])
            lines = [str(p) for p in raw_popup if str(p).strip()][:4] if isinstance(raw_popup, list) else []
            if lines: result["popup"] = lines
            else: result["command"] = "idle"

        if result["command"] in ("speak", "wake_user"):
            result["segments"] = _filter_segments(result["segments"], raw)
            if not result["segments"]: result["command"] = "idle"

        # Persist model-supplied memory
        try:
            if isinstance(obj, dict):
                mem = obj.get("summary_memory") or obj.get("summary")
                if mem and isinstance(mem, str) and mem.strip():
                    self._save_memory(mem.strip())
        except Exception: pass

        # Resolve [USER_HOME] placeholder in any path fields
        _home = getattr(self, "_system_path", "")
        if _home:
            for _f in ("path", "file_path", "save_path", "cmd"):
                if isinstance(result.get(_f), str):
                    result[_f] = result[_f].replace("[USER_HOME]", _home)

        return result
