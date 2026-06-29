"""
screen_reader.py v5 — Focused-window capture + improved OCR
Captures ONLY the foreground/active window (~4× faster than full-screen).
Falls back to full-screen if the focused-window approach is unavailable.

pip install mss pillow pytesseract pywin32   (Windows)
pip install mss pillow pytesseract python3-xlib  (Linux/X11)
"""

import os
import platform
import subprocess
import tempfile
from pathlib import Path

_SYSTEM = platform.system()

try:
    from PIL import Image, ImageFilter, ImageEnhance
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import pytesseract
    TESSERACT_OK = True
except ImportError:
    TESSERACT_OK = False


def _find_tesseract_windows() -> str | None:
    candidates = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for pf in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)")):
        if pf:
            candidates.append(str(Path(pf) / "Tesseract-OCR" / "tesseract.exe"))
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def _cmd_exists(cmd: str) -> bool:
    try:
        if _SYSTEM == "Windows":
            return subprocess.run(["where", cmd], capture_output=True).returncode == 0
        return subprocess.run(["which", cmd], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _has_display() -> bool:
    if _SYSTEM in ("Windows", "Darwin"):
        return True
    # XDG_SESSION_TYPE=tty means no display, skip
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session and session.lower() != "tty":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _is_wayland() -> bool:
    return (bool(os.environ.get("WAYLAND_DISPLAY")) or
            os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland")


# ── Focused-window grabbers ────────────────────────────────────────────────────

def _grab_focused_windows() -> "tuple[Image.Image, tuple] | tuple[None, None]":
    """Windows: grab only the foreground window. Returns (image, (x,y,w,h)) or (None,None)."""
    if not PIL_OK:
        return None, None
    try:
        import ctypes
        from ctypes import wintypes
        import win32gui, win32ui, win32con
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None, None
        # Get window title so we can log it
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            title = "?"
        rect = win32gui.GetWindowRect(hwnd)
        x, y, x2, y2 = rect
        w = x2 - x
        h = y2 - y
        if w <= 0 or h <= 0:
            return None, None

        # Use PrintWindow for off-screen / layered windows; fall back to BitBlt
        hwnd_dc = win32gui.GetDC(hwnd)
        hdc_screen = win32ui.CreateDCFromHandle(hwnd_dc)
        hdc_mem    = hdc_screen.CreateCompatibleDC()
        bmp        = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc_screen, w, h)
        old_bmp = hdc_mem.SelectObject(bmp)
        # PW_RENDERFULLCONTENT = 2 — works for DX/GPU-rendered windows (VS Code, browsers)
        try:
            ctypes.windll.user32.PrintWindow(hwnd, hdc_mem.GetSafeHdc(), 2)
        except Exception:
            hdc_mem.BitBlt((0, 0), (w, h), hdc_screen, (0, 0), win32con.SRCCOPY)
        bmp_info = bmp.GetInfo()
        bmp_bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]),
                               bmp_bits, "raw", "BGRX", 0, 1)
        # Restore original bitmap before cleanup (correct order)
        hdc_mem.SelectObject(old_bmp)
        win32gui.DeleteObject(bmp.GetHandle())
        hdc_mem.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        print(f"[ScreenReader] Captured focused window: '{title}' {w}×{h}")
        return img, (x, y, w, h)
    except Exception as e:
        print(f"[ScreenReader] Focused grab (Windows) failed: {e}")
        return None, None


def _grab_focused_x11() -> "tuple[Image.Image, tuple] | tuple[None, None]":
    """X11 Linux: grab the active window via xdotool + scrot or import."""
    if not PIL_OK:
        return None, None
    # Strategy 1: scrot --focused
    if _cmd_exists("scrot"):
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp = f.name
            r = subprocess.run(["scrot", "--focused", "--silent", tmp],
                               capture_output=True, timeout=8)
            if r.returncode == 0 and Path(tmp).exists():
                img = Image.open(tmp).copy()
                return img, None
        except Exception:
            pass
        finally:
            if tmp and Path(tmp).exists():
                try: os.unlink(tmp)
                except Exception: pass
    # Strategy 2: xdotool + xwd
    if _cmd_exists("xdotool"):
        tmp_xwd = None
        tmp_png = None
        try:
            r = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, timeout=4, text=True)
            wid = r.stdout.strip()
            if wid and wid != "0":
                if not _cmd_exists("xwd"):
                    # xwd needed for this strategy
                    pass
                else:
                    with tempfile.NamedTemporaryFile(suffix=".xwd", delete=False) as f:
                        tmp_xwd = f.name
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                        tmp_png = f.name
                    subprocess.run(["xwd", "-id", wid, "-out", tmp_xwd], capture_output=True, timeout=6)
                    if _cmd_exists("convert"):
                        subprocess.run(["convert", tmp_xwd, tmp_png], capture_output=True, timeout=6)
                    if tmp_png and Path(tmp_png).exists() and Path(tmp_png).stat().st_size > 0:
                        img = Image.open(tmp_png).copy()
                        print(f"[ScreenReader] Captured focused window via xdotool+xwd")
                        return img, None
        except Exception:
            pass
        finally:
            for p in (tmp_xwd, tmp_png):
                if p and Path(p).exists():
                    try: os.unlink(p)
                    except Exception: pass
    return None, None


def _grab_focused_macos() -> "tuple[Image.Image, tuple] | tuple[None, None]":
    """macOS: capture only the frontmost window via screencapture -l."""
    if not PIL_OK:
        return None, None
    try:
        # Get frontmost window ID via osascript
        script = ('tell application "System Events" to get unix id of every process '
                  'whose frontmost is true')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        pid = r.stdout.strip().split(",")[0].strip()
        if not pid:
            return None, None
        # screencapture -l <window_id> not trivially available; use -R with window bounds via
        # Quartz (if available), else fall back to full-screen
        try:
            import Quartz
            options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
            for win in window_list:
                if str(win.get("kCGWindowOwnerPID", "")) == pid:
                    bounds = win.get("kCGWindowBounds", {})
                    x = int(bounds.get("X", 0))
                    y = int(bounds.get("Y", 0))
                    w = int(bounds.get("Width", 0))
                    h = int(bounds.get("Height", 0))
                    if w > 0 and h > 0:
                        tmp = None
                        try:
                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                                tmp = f.name
                            subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", tmp],
                                           capture_output=True, timeout=8)
                            if tmp and Path(tmp).exists() and Path(tmp).stat().st_size > 0:
                                img = Image.open(tmp).copy()
                                print(f"[ScreenReader] Captured focused window (macOS) {w}×{h}")
                                return img, (x, y, w, h)
                        finally:
                            if tmp and Path(tmp).exists():
                                try: os.unlink(tmp)
                                except Exception: pass
        except ImportError:
            pass
    except Exception as e:
        print(f"[ScreenReader] Focused grab (macOS) failed: {e}")
    return None, None


# ── Full-screen fallback grabbers ──────────────────────────────────────────────

def _grab_mss():
    if not PIL_OK: return None
    try:
        import mss
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[0])
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception: return None

def _grab_imagegrab():
    if not PIL_OK: return None
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception: return None

def _grab_scrot_full():
    if not PIL_OK or not _cmd_exists("scrot"): return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: tmp = f.name
        if subprocess.run(["scrot", "--silent", tmp], capture_output=True, timeout=10).returncode != 0:
            return None
        img = Image.open(tmp).copy(); return img
    except Exception: return None
    finally:
        if tmp and Path(tmp).exists():
            try: os.unlink(tmp)
            except Exception: pass

def _grab_grim():
    if not PIL_OK or not _cmd_exists("grim"): return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: tmp = f.name
        if subprocess.run(["grim", tmp], capture_output=True, timeout=10).returncode != 0:
            return None
        img = Image.open(tmp).copy(); return img
    except Exception: return None
    finally:
        if tmp and Path(tmp).exists():
            try: os.unlink(tmp)
            except Exception: pass

def _grab_spectacle():
    if not PIL_OK or not _cmd_exists("spectacle"): return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: tmp = f.name
        r = subprocess.run(["spectacle", "--background", "--nonotify", "--fullscreen", "--output", tmp],
                           capture_output=True, timeout=15)
        if r.returncode != 0 or not Path(tmp).stat().st_size: return None
        img = Image.open(tmp).copy(); return img
    except Exception: return None
    finally:
        if tmp and Path(tmp).exists():
            try: os.unlink(tmp)
            except Exception: pass

def _grab_screencapture():
    if not PIL_OK: return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f: tmp = f.name
        if subprocess.run(["screencapture", "-x", tmp], capture_output=True, timeout=10).returncode != 0:
            return None
        img = Image.open(tmp).copy(); return img
    except Exception: return None
    finally:
        if tmp and Path(tmp).exists():
            try: os.unlink(tmp)
            except Exception: pass


def _preprocess_for_ocr(img: "Image.Image") -> "Image.Image":
    """
    Improve OCR accuracy through a multi-step preprocessing pipeline:
      1. Upscale 2× (Tesseract works best at ~150-300 DPI equivalent)
      2. Convert to grayscale
      3. Sharpen to make text edges crisper
      4. Boost contrast so light/dark text is unambiguous
    """
    w, h = img.size
    # 1. Upscale — but cap at 3840px wide to avoid OOM on large monitors
    scale = 2
    if w * scale > 3840:
        scale = max(1, 3840 // w)
    if scale > 1:
        img = img.resize((w * scale, h * scale), Image.LANCZOS)
    # 2. Grayscale
    img = img.convert("L")
    # 3. Sharpen (helps with anti-aliased screen fonts)
    img = img.filter(ImageFilter.SHARPEN)
    # 4. Contrast boost
    img = ImageEnhance.Contrast(img).enhance(1.8)
    return img


class ScreenReader:
    """Captures the active/foreground window and extracts text via Tesseract OCR."""

    # Store last captured window geometry so AI can use real coordinates
    last_window_rect: tuple | None = None   # (x, y, w, h) in screen space

    def __init__(self, ocr_focused_window: bool = True):
        if _SYSTEM == "Windows" and TESSERACT_OK:
            tess_path = _find_tesseract_windows()
            if tess_path:
                pytesseract.pytesseract.tesseract_cmd = tess_path
            else:
                print("[ScreenReader] WARNING: Tesseract not found.")

        self._ocr_focused_window = ocr_focused_window
        self._focused_fn = self._choose_focused_backend() if ocr_focused_window else None
        self._fallback_fn = self._choose_fallback_backend()
        self._available = TESSERACT_OK and (self._focused_fn is not None or self._fallback_fn is not None)

        if self._focused_fn:
            print(f"[ScreenReader] Focused-window capture: ready")
        elif self._fallback_fn:
            if not ocr_focused_window:
                print(f"[ScreenReader] Focused capture disabled by config — using full-screen")
            else:
                print(f"[ScreenReader] Focused capture unavailable, using full-screen fallback")
        else:
            print(f"[ScreenReader] Screen capture disabled (no backend found)")

    def _choose_focused_backend(self):
        if _SYSTEM == "Windows":
            try:
                import win32gui, win32ui, win32con
                return _grab_focused_windows
            except ImportError:
                print("[ScreenReader] pywin32 not installed — focused capture disabled. pip install pywin32")
                return None
        elif _SYSTEM == "Linux" and not _is_wayland():
            # Check if any focused-window tool is available without taking a screenshot
            if _cmd_exists("scrot") or (_cmd_exists("xdotool") and _cmd_exists("xwd")):
                return _grab_focused_x11
        elif _SYSTEM == "Darwin":
            return _grab_focused_macos
        return None

    def _choose_fallback_backend(self):
        if not _has_display():
            return None
        # Just check if any backend is available without actually taking a screenshot
        candidates = []
        if _SYSTEM == "Windows":
            # mss or Pillow ImageGrab are always available if installed
            try:
                import mss
                candidates.append(_grab_mss)
            except ImportError:
                pass
            try:
                from PIL import ImageGrab
                candidates.append(_grab_imagegrab)
            except ImportError:
                pass
        elif _SYSTEM == "Darwin":
            if _cmd_exists("screencapture"):
                candidates.append(_grab_screencapture)
            try:
                import mss
                candidates.append(_grab_mss)
            except ImportError:
                pass
            try:
                from PIL import ImageGrab
                candidates.append(_grab_imagegrab)
            except ImportError:
                pass
        else:
            if _is_wayland():
                if _cmd_exists("spectacle"):
                    candidates.append(_grab_spectacle)
                if _cmd_exists("grim"):
                    candidates.append(_grab_grim)
            else:
                if _cmd_exists("scrot"):
                    candidates.append(_grab_scrot_full)
                try:
                    import mss
                    candidates.append(_grab_mss)
                except ImportError:
                    pass
                try:
                    from PIL import ImageGrab
                    candidates.append(_grab_imagegrab)
                except ImportError:
                    pass
        # Return the first candidate; actual capture will happen on first call
        return candidates[0] if candidates else None

    def capture_image(self) -> "Image.Image | None":
        """Capture the focused window (or fall back to full screen)."""
        if self._focused_fn:
            try:
                img, rect = self._focused_fn()
                if img is not None:
                    ScreenReader.last_window_rect = rect
                    return img
            except Exception as e:
                print(f"[ScreenReader] Focused capture error: {e}")
        if self._fallback_fn:
            try:
                img = self._fallback_fn()
                ScreenReader.last_window_rect = None
                return img
            except Exception:
                pass
        return None

    def capture_text(self, max_chars: int = 3000) -> str:
        if not self._available:
            return ""
        try:
            screenshot = self.capture_image()
            if screenshot is None:
                return ""
            processed = _preprocess_for_ocr(screenshot)
            # PSM 3 = fully automatic page segmentation (best for mixed screen content)
            # OEM 1 = LSTM neural engine only (most accurate modern engine)
            config = "--psm 3 --oem 1"
            text = pytesseract.image_to_string(processed, lang="eng", config=config)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            result = "\n".join(lines)[:max_chars]
            print(f"[ScreenReader] OCR captured {len(result)} chars")
            return result
        except Exception as e:
            print(f"[ScreenReader] OCR error: {e}")
            return ""

    def capture_text_with_positions(self, max_chars: int = 2000) -> str:
        """Capture text WITH word positions. Returns compact format: word@(x,y) per line.
        Uses real screen coordinates if a focused window was captured."""
        if not self._available:
            return ""
        try:
            screenshot = self.capture_image()
            if screenshot is None:
                return ""
            orig_w, orig_h = screenshot.size
            rect = ScreenReader.last_window_rect  # (wx, wy, ww, wh) or None
            processed = _preprocess_for_ocr(screenshot)
            proc_w, _ = processed.size
            # Compute actual scale factor (may be 1 or 2 depending on monitor size)
            scale = proc_w // orig_w if orig_w > 0 else 2
            if scale < 1:
                scale = 1
            # Get detailed word data
            data = pytesseract.image_to_data(processed, lang="eng",
                                             config="--psm 3 --oem 1",
                                             output_type=pytesseract.Output.DICT)
            lines = []
            n = len(data["text"])
            for i in range(n):
                word = data["text"][i].strip()
                if not word:
                    continue
                try:
                    conf = int(data["conf"][i])
                except (ValueError, TypeError):
                    conf = 0
                if conf < 50:
                    continue
                # Scale coordinates back from the upscale factor
                px = data["left"][i] // scale
                py = data["top"][i] // scale
                # Translate to screen coordinates if we have window rect
                if rect:
                    wx, wy = rect[0], rect[1]
                    px += wx
                    py += wy
                lines.append(f"{word}@({px},{py})")
            result = " ".join(lines)[:max_chars]
            return result
        except Exception as e:
            print(f"[ScreenReader] Positional OCR error: {e}")
            return self.capture_text(max_chars)
