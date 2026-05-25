#!/usr/bin/env python3
import os, sys, base64, io, ctypes
from PIL import Image, ImageGrab
import cv2
import numpy as np
import webview as pywebview
import pydirectinput

pydirectinput.PAUSE    = 0      # we own all timing
pydirectinput.FAILSAFE = False  # don't raise on corner moves

# ── EasyOCR (loaded once, reused) ────────────────────────────────────────────
try:
    import easyocr
    _reader = easyocr.Reader(['en'], gpu=True, verbose=False)
    HAVE_OCR = True
except ImportError:
    _reader = None
    HAVE_OCR = False

# ── Word list & solver ────────────────────────────────────────────────────────
MIN_LEN = 3

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def load_words(path=None):
    if path is None:
        path = os.path.join(_SCRIPT_DIR, 'words_sowpods.txt')
    with open(path, encoding='utf-8') as f:
        return set(w.strip().upper() for w in f if len(w.strip()) >= MIN_LEN and w.strip().isalpha())

def build_prefixes(word_set):
    p = set()
    for w in word_set:
        for i in range(1, len(w) + 1):
            p.add(w[:i])
    return p

def solve_grid(grid, word_set, prefixes):
    rows, cols = len(grid), len(grid[0])
    found = {}  # word -> first path found
    def dfs(r, c, visited, word, path):
        if word not in prefixes:
            return
        if len(word) >= MIN_LEN and word in word_set and word not in found:
            found[word] = list(path)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in visited:
                    visited.add((nr, nc))
                    path.append([nr, nc])
                    dfs(nr, nc, visited, word + grid[nr][nc], path)
                    path.pop()
                    visited.remove((nr, nc))
    for r in range(rows):
        for c in range(cols):
            dfs(r, c, {(r, c)}, grid[r][c], [[r, c]])
    return found

# ── OCR ───────────────────────────────────────────────────────────────────────
def _ocr_cell(cell_bgr):
    """Read one letter from a tile using EasyOCR."""
    h, w = cell_bgr.shape[:2]
    # Crop 18% margins to avoid tile borders/shadows
    mh, mw = max(1, int(h * .18)), max(1, int(w * .18))
    cell = cell_bgr[mh:h - mh, mw:w - mw]
    # Upscale so the letter is large enough for the model
    cell = cv2.resize(cell, (96, 96), interpolation=cv2.INTER_CUBIC)
    results = _reader.readtext(cell, detail=1, allowlist='ABCDEFGHIJKLMNOPQRSTUVWXYZ')
    if not results:
        return '?'
    # Pick highest-confidence single letter
    best, best_conf = '?', -1
    for (_, text, conf) in results:
        text = text.strip().upper().replace(' ', '')
        if text and text.isalpha() and conf > best_conf:
            best_conf, best = conf, text[0]
    return best

def image_to_grid(pil_img, size):
    img = cv2.cvtColor(np.array(pil_img.convert('RGB')), cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    ch, cw = h // size, w // size
    return [[_ocr_cell(img[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw]) for c in range(size)] for r in range(size)]

def pil_to_b64(img):
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()

# ── pywebview API ─────────────────────────────────────────────────────────────
class Api:
    def __init__(self, word_set, prefixes):
        self._words = word_set
        self._pre = prefixes
        self._win = None

    def minimize(self):
        if self._win:
            self._win.minimize()

    def close(self):
        if self._win:
            self._win.destroy()
        os._exit(0)

    # ── Calibration & auto-play ───────────────────────────────────────────────
    def start_calibrate(self):
        """Hide window, capture 2 clicks (top-left letter, bottom-right letter)."""
        import threading
        from pynput import mouse as pmouse

        self._calib = []
        self._stop_flag = False

        def capture():
            import time
            time.sleep(0.3)
            self._win.minimize()
            time.sleep(0.2)

            def on_click(x, y, button, pressed):
                if pressed and button == pmouse.Button.left:
                    self._calib.append((int(x), int(y)))
                    if len(self._calib) == 2:
                        p1, p2 = self._calib
                        self._win.restore()
                        self._win.evaluate_js(
                            f'onCalibrateComplete({p1[0]},{p1[1]},{p2[0]},{p2[1]})'
                        )
                        return False

            with pmouse.Listener(on_click=on_click) as l:
                l.join()

        threading.Thread(target=capture, daemon=True).start()
        return True

    def auto_play(self, paths, grid_size, delay_ms=600):
        """Drag mouse through each word path on screen (pydirectinput for Roblox/DX11)."""
        import threading, time

        if len(getattr(self, '_calib', [])) < 2:
            return {'error': 'Calibrate grid first'}

        (x1, y1), (x2, y2) = self._calib
        n = grid_size
        step_x = (x2 - x1) / max(n - 1, 1)
        step_y = (y2 - y1) / max(n - 1, 1)

        def center(r, c):
            return (int(x1 + c * step_x), int(y1 + r * step_y))

        self._stop_flag  = False
        self._mouse_down = False

        from pynput import keyboard as pkeyboard
        def on_press(key):
            if key == pkeyboard.Key.esc:
                self.stop_play()
                return False
        kb_listener = pkeyboard.Listener(on_press=on_press)
        kb_listener.start()

        def play():
            self._win.minimize()
            time.sleep(0.5)

            # 1 ms timer resolution so time.sleep is accurate on Windows
            ctypes.windll.winmm.timeBeginPeriod(1)
            try:
                move_ms = 80
                steps   = max(4, move_ms // 16)   # ~16 ms per step
                step_s  = (move_ms / 1000) / steps

                def smooth_move(x0, y0, x1, y1):
                    for i in range(1, steps + 1):
                        if self._stop_flag:
                            return
                        t = i / steps
                        pydirectinput.moveTo(int(x0 + (x1 - x0) * t),
                                             int(y0 + (y1 - y0) * t))
                        time.sleep(step_s)

                # Single click on grid centre to give Roblox focus
                cx = int((x1 + x2) / 2)
                cy = int((y1 + y2) / 2)
                pydirectinput.click(cx, cy)
                time.sleep(0.4)

                prev_end = (cx, cy)

                for path in paths:
                    if self._stop_flag:
                        break

                    pts = [center(r, c) for r, c in path]

                    # Glide to first tile
                    smooth_move(prev_end[0], prev_end[1], pts[0][0], pts[0][1])
                    pydirectinput.moveTo(pts[0][0], pts[0][1])  # land exactly
                    time.sleep(0.08)

                    # Press and hold — Roblox needs ≥100 ms before drag is registered
                    pydirectinput.mouseDown(button='left')
                    self._mouse_down = True
                    time.sleep(0.15)

                    # Drag through remaining tiles
                    prev = pts[0]
                    for px, py in pts[1:]:
                        if self._stop_flag:
                            break
                        smooth_move(prev[0], prev[1], px, py)
                        prev = (px, py)

                    time.sleep(0.08)
                    pydirectinput.mouseUp(button='left')
                    self._mouse_down = False
                    time.sleep(0.08)

                    prev_end = pts[-1]
                    time.sleep(delay_ms / 1000)

            finally:
                ctypes.windll.winmm.timeEndPeriod(1)
                kb_listener.stop()
                self._win.restore()
                self._win.evaluate_js('onAutoPlayDone()')

        threading.Thread(target=play, daemon=True).start()
        return {'ok': True}

    def stop_play(self):
        self._stop_flag = True
        if getattr(self, '_mouse_down', False):
            pydirectinput.mouseUp(button='left')
            self._mouse_down = False
        if self._win:
            self._win.restore()

    def _find_hwnd(self):
        import ctypes, ctypes.wintypes, os
        pid = os.getpid()
        found = []
        ProcType = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def cb(hwnd, _):
            dpid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(dpid))
            if dpid.value == pid and ctypes.windll.user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True
        ctypes.windll.user32.EnumWindows(ProcType(cb), 0)
        return found[0] if found else None

    def toggle_on_top(self):
        import ctypes
        if not hasattr(self, '_hwnd') or not self._hwnd:
            self._hwnd = self._find_hwnd()
        if not self._hwnd:
            return False
        HWND_TOPMOST   = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE     = 0x0002
        SWP_NOSIZE     = 0x0001
        self._on_top = not getattr(self, '_on_top', False)
        ctypes.windll.user32.SetWindowPos(
            self._hwnd,
            HWND_TOPMOST if self._on_top else HWND_NOTOPMOST,
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE
        )
        return self._on_top

    def get_word_files(self):
        """Return sorted list of .txt files in the script directory."""
        return sorted(f for f in os.listdir(_SCRIPT_DIR) if f.lower().endswith('.txt'))

    def set_word_file(self, filename):
        """Reload word list from a different .txt file in the script directory."""
        import re
        if not re.match(r'^[\w\-. ]+\.txt$', filename):
            return {'error': 'Invalid filename'}
        path = os.path.join(_SCRIPT_DIR, filename)
        if not os.path.isfile(path):
            return {'error': 'File not found'}
        try:
            word_set = load_words(path)
            self._words = word_set
            self._pre   = build_prefixes(word_set)
            return {'ok': True, 'count': len(word_set)}
        except Exception as e:
            return {'error': str(e)}

    def paste(self, size):
        img = ImageGrab.grabclipboard()
        if not isinstance(img, Image.Image):
            return {'error': 'No image in clipboard — use Win+Shift+S first.'}
        preview = pil_to_b64(img.copy())
        if not HAVE_OCR:
            return {'error': 'pytesseract not installed.', 'preview': preview}
        try:
            grid = image_to_grid(img, size)
            return {'preview': preview, 'grid': grid}
        except Exception as e:
            return {'error': str(e), 'preview': preview}

    def solve(self, letters, size):
        letters = [l.strip().upper() for l in letters if l.strip()]
        if len(letters) != size * size:
            return {'error': f'Expected {size * size} letters, got {len(letters)}.'}
        grid = [letters[r * size:(r + 1) * size] for r in range(size)]
        found = solve_grid(grid, self._words, self._pre)
        entries = [{'word': w, 'path': p} for w, p in found.items()]
        entries.sort(key=lambda e: (-len(e['word']), e['word']))
        return {'words': entries}

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Boggle Solver</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* Design tokens — Card & Board Game dark palette */
  :root {
    --bg:           #0F172A;
    --bg-card:      #192134;
    --bg-raised:    #1e2a40;
    --primary:      #15803D;
    --primary-glow: rgba(21,128,61,.3);
    --accent:       #D97706;
    --accent-bg:    rgba(217,119,6,.12);
    --fg:           #F1F5F9;
    --fg-muted:     #64748B;
    --border:       rgba(255,255,255,.07);
    --border-hover: rgba(255,255,255,.14);
    --danger:       #DC2626;
    --r-tile:       11px;
    --r-btn:        8px;
    --r-chip:       6px;
    --ease:         cubic-bezier(.16,1,.3,1);
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }

  body {
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
    background: var(--bg);
    color: var(--fg);
    display: flex;
    flex-direction: column;
    -webkit-font-smoothing: antialiased;
  }

  /* ── Custom titlebar ── */
  .titlebar {
    height: 38px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 14px;
    background: var(--bg-card);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
    -webkit-app-region: drag;
    user-select: none;
  }
  .tb-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--primary);
    flex-shrink: 0;
  }
  .tb-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--fg-muted);
    letter-spacing: .1px;
  }
  .tb-space { flex: 1; }
  .tb-btns {
    display: flex;
    gap: 4px;
    -webkit-app-region: no-drag;
  }
  .wm {
    width: 26px; height: 26px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--fg-muted);
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 120ms, color 120ms, border-color 120ms;
  }
  .wm:hover { background: var(--bg-raised); color: var(--fg); border-color: var(--border-hover); }
  .wm.close:hover { background: rgba(220,38,38,.18); color: #fca5a5; border-color: rgba(220,38,38,.3); }
  .wm.pin.active { background: rgba(21,128,61,.2); color: var(--primary); border-color: rgba(21,128,61,.4); }

  /* ── Main layout ── */
  .main { display: flex; flex: 1; overflow: hidden; }

  /* ── Left panel ── */
  .left {
    width: 220px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    gap: 9px;
    padding: 11px;
    border-right: 1px solid var(--border);
    overflow: hidden;
  }

  /* Image preview */
  .preview {
    width: 100%;
    height: 76px;
    background: var(--bg-card);
    border: 1px dashed rgba(255,255,255,.1);
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    flex-shrink: 0;
    transition: border-color 200ms;
  }
  .preview.filled { border-style: solid; border-color: var(--border); }
  .preview img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .preview-ph {
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    color: rgba(255,255,255,.18);
    font-size: 12px;
  }

  /* Section label */
  .slabel {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    text-transform: uppercase;
    color: rgba(255,255,255,.2);
  }

  /* Size row */
  .size-row {
    display: flex;
    align-items: center;
    gap: 8px;
    -webkit-app-region: no-drag;
  }
  .size-row .slabel { flex-shrink: 0; }
  .size-row select {
    flex: 1;
    background: var(--bg-card);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--r-btn);
    padding: 5px 9px;
    font: 13px/1 'Inter', system-ui;
    cursor: pointer;
    outline: none;
    transition: border-color 120ms;
    -webkit-app-region: no-drag;
  }
  .size-row select:focus { border-color: var(--primary); }

  /* Grid */
  .grid-container { position: relative; }
  #grid { display: grid; gap: 6px; }
  #path-svg {
    position: absolute;
    top: 0; left: 0;
    width: 100%; height: 100%;
    pointer-events: none;
    overflow: visible;
  }

  .tile-wrap { position: relative; aspect-ratio: 1; }

  .tile {
    width: 100%; height: 100%;
    background: var(--bg-card);
    border: 1.5px solid var(--border);
    border-radius: var(--r-tile);
    text-align: center;
    font: 700 20px/1 'Inter', system-ui;
    color: var(--fg);
    caret-color: var(--primary);
    outline: none;
    padding: 0;
    text-transform: uppercase;
    cursor: text;
    transition:
      border-color 120ms var(--ease),
      background  120ms var(--ease),
      box-shadow  120ms var(--ease);
  }
  .tile:hover:not(:focus) {
    border-color: var(--border-hover);
    background: var(--bg-raised);
  }
  .tile:focus {
    border-color: var(--primary);
    background: var(--bg-raised);
    box-shadow: 0 0 0 3px var(--primary-glow);
  }
  .tile.err {
    border-color: var(--danger) !important;
    box-shadow: 0 0 0 3px rgba(220,38,38,.2) !important;
  }
  /* Path highlight states */
  .tile.path-active {
    border-color: var(--primary) !important;
    background: rgba(21,128,61,.12) !important;
    box-shadow: 0 0 0 3px var(--primary-glow) !important;
  }
  .tile.path-start {
    border-color: var(--accent) !important;
    background: rgba(217,119,6,.15) !important;
    box-shadow: 0 0 0 3px rgba(217,119,6,.3) !important;
  }
  /* Step badge */
  .tile-badge {
    position: absolute;
    top: 4px; right: 4px;
    min-width: 17px; height: 17px;
    padding: 0 4px;
    border-radius: 9px;
    background: var(--primary);
    color: #fff;
    font: 700 9px/17px 'Inter', system-ui;
    text-align: center;
    pointer-events: none;
    display: none;
    z-index: 2;
  }
  .tile-badge.first { background: var(--accent); }

  /* Buttons */
  .btn-row { display: flex; gap: 7px; }
  .btn {
    flex: 1;
    padding: 9px 10px;
    border: none;
    border-radius: var(--r-btn);
    font: 600 13px/1 'Inter', system-ui;
    cursor: pointer;
    letter-spacing: .1px;
    transition: opacity 120ms, transform 100ms var(--ease), box-shadow 150ms;
  }
  .btn:active { transform: scale(.97); }
  .btn:disabled { opacity: .35; cursor: not-allowed; transform: none !important; }

  .btn-paste {
    background: var(--bg-raised);
    color: var(--fg);
    border: 1px solid var(--border-hover);
  }
  .btn-paste:hover:not(:disabled) { border-color: rgba(255,255,255,.22); }

  .btn-solve {
    background: var(--primary);
    color: #fff;
    box-shadow: 0 2px 10px var(--primary-glow);
  }
  .btn-solve:hover:not(:disabled) { box-shadow: 0 4px 18px var(--primary-glow); opacity: .92; }

  .btn-clear {
    flex: 0 0 auto;
    padding: 9px 13px;
    background: transparent;
    color: var(--fg-muted);
    border: 1px solid var(--border);
  }
  .btn-clear:hover:not(:disabled) { color: var(--fg); border-color: var(--border-hover); }

  /* Status */
  #status {
    font-size: 11.5px;
    color: var(--fg-muted);
    min-height: 14px;
    text-align: center;
    transition: color 150ms;
  }
  #status.err { color: #f87171; }
  #status.ok  { color: #4ade80; }

  /* Spinner */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spin {
    display: inline-block;
    width: 11px; height: 11px;
    border: 1.5px solid rgba(255,255,255,.15);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin .65s linear infinite;
    vertical-align: middle;
    margin-right: 5px;
  }

  /* ── Right panel ── */
  .right { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

  .results-bar {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .results-bar .slabel { line-height: 1; }
  .badge {
    background: var(--primary);
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 20px;
    display: none;
  }
  .badge.on { display: inline-block; }

  #results {
    flex: 1;
    overflow-y: auto;
    padding: 12px 16px 20px;
  }
  #results::-webkit-scrollbar { width: 4px; }
  #results::-webkit-scrollbar-thumb { background: rgba(255,255,255,.08); border-radius: 4px; }
  #results::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.14); }

  .len-group { margin-bottom: 14px; }
  .len-hdr {
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    color: rgba(255,255,255,.18);
    margin-bottom: 7px;
  }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; }
  .chip {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r-chip);
    padding: 4px 9px;
    font-size: 12.5px;
    font-weight: 500;
    color: #fff;
    letter-spacing: .2px;
    cursor: default;
    transition: background 100ms, border-color 100ms;
  }
  .chip:hover { background: var(--bg-raised); border-color: var(--border-hover); }
  .chip.gold {
    background: var(--accent-bg);
    border-color: rgba(217,119,6,.35);
    color: var(--accent);
  }
  .chip.gold:hover { background: rgba(217,119,6,.2); }
  .chip.selected { outline: 1.5px solid var(--primary); outline-offset: 1px; }
  .chip.gold.selected { outline-color: var(--accent); }

  .placeholder {
    color: rgba(255,255,255,.15);
    font-size: 13px;
    margin-top: 72px;
    text-align: center;
    line-height: 2;
  }
  .placeholder strong { color: rgba(255,255,255,.28); font-weight: 600; }

  /* ── Auto-solver section ── */
  .auto-sep {
    height: 1px;
    background: var(--border);
    margin: 0 -12px;
    flex-shrink: 0;
  }
  .btn-calib {
    background: var(--bg-raised);
    color: var(--fg);
    border: 1px solid var(--border-hover);
  }
  .btn-calib:hover:not(:disabled) { border-color: rgba(255,255,255,.22); }
  .btn-autoplay {
    background: var(--accent);
    color: #fff;
    box-shadow: 0 2px 10px rgba(217,119,6,.3);
  }
  .btn-autoplay:hover:not(:disabled) { opacity: .9; }
  .btn-stop {
    flex: 0 0 auto;
    padding: 9px 13px;
    background: transparent;
    color: var(--fg-muted);
    border: 1px solid var(--border);
  }
  .btn-stop:hover:not(:disabled) { color: var(--fg); border-color: var(--border-hover); }
  #calibStatus {
    font-size: 10px;
    color: var(--fg-muted);
    text-align: center;
    line-height: 1.4;
    min-height: 14px;
    word-break: break-all;
  }
  .delay-row {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .delay-row .slabel { flex-shrink: 0; }
  .delay-row input[type=number] {
    flex: 1;
    min-width: 0;
    background: var(--bg-card);
    color: var(--fg);
    border: 1px solid var(--border);
    border-radius: var(--r-btn);
    padding: 5px 9px;
    font: 13px/1 'Inter', system-ui;
    outline: none;
    transition: border-color 120ms;
    -moz-appearance: textfield;
  }
  .delay-row input[type=number]::-webkit-inner-spin-button,
  .delay-row input[type=number]::-webkit-outer-spin-button { opacity: 1; }
  .delay-row input[type=number]:focus { border-color: var(--primary); }
  .delay-row .unit {
    font-size: 11px;
    color: var(--fg-muted);
    flex-shrink: 0;
  }


  /* ── Big stop bar ── */
  #stopBar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    height: 64px;
    background: var(--danger);
    color: #fff;
    font: 700 18px/1 'Inter', system-ui;
    letter-spacing: .5px;
    border: none;
    cursor: pointer;
    display: none;
    align-items: center;
    justify-content: center;
    gap: 10px;
    z-index: 999;
    transition: opacity 120ms;
  }
  #stopBar:hover { opacity: .88; }
  #stopBar:active { opacity: .75; }
  #stopBar.visible { display: flex; }
</style>
</head>
<body>

<div class="titlebar">
  <div class="tb-dot"></div>
  <span class="tb-title">Boggle Solver</span>
  <div class="tb-space"></div>
  <div class="tb-btns">
    <button class="wm pin" id="btnPin" onclick="togglePin()" title="Always on top">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/></svg>
    </button>
    <button class="wm" onclick="pywebview.api.minimize()" title="Minimise">
      <svg width="10" height="2" viewBox="0 0 10 2"><rect width="10" height="2" rx="1" fill="currentColor"/></svg>
    </button>
    <button class="wm close" onclick="pywebview.api.close()" title="Close">
      <svg width="9" height="9" viewBox="0 0 9 9"><line x1="1" y1="1" x2="8" y2="8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><line x1="8" y1="1" x2="1" y2="8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
    </button>
  </div>
</div>

<div class="main">
  <div class="left">

    <div class="preview" id="preview">
      <div class="preview-ph">
        <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".5">
          <rect x="3" y="3" width="18" height="18" rx="3"/>
          <path d="M3 9h18M9 21V9"/>
        </svg>
        <span>No image</span>
      </div>
    </div>

    <div class="size-row">
      <span class="slabel">Size</span>
      <select id="sizeSelect" onchange="buildGrid()">
        <option value="3">3 × 3</option>
        <option value="4" selected>4 × 4</option>
        <option value="5">5 × 5</option>
        <option value="6">6 × 6</option>
      </select>
    </div>
    <div class="size-row">
      <span class="slabel">Words</span>
      <select id="wordFile" onchange="setWordFile()">
        <option value="">Loading…</option>
      </select>
    </div>

    <div>
      <div class="slabel" style="margin-bottom:8px">Grid — click to edit</div>
      <div class="grid-container">
        <div id="grid"></div>
        <svg id="path-svg" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
    </div>

    <div id="status"></div>

    <div class="btn-row">
      <button class="btn btn-paste" id="btnPaste" onclick="paste()">Paste from Clipboard</button>
    </div>
    <div class="btn-row">
      <button class="btn btn-solve" id="btnSolve" onclick="solve()">Solve</button>
      <button class="btn btn-clear" onclick="clearAll()">Clear</button>
    </div>

    <div class="auto-sep"></div>
    <div class="slabel">Auto Solver</div>

    <div class="btn-row">
      <button class="btn btn-calib" id="btnCalib" onclick="startCalib()">Set Grid</button>
    </div>
    <div id="calibStatus">Not calibrated</div>
    <div class="delay-row">
      <span class="slabel">Delay</span>
      <input type="number" id="delayInput" value="600" min="50" max="5000" step="50">
      <span class="unit">ms</span>
    </div>
    <div class="btn-row">
      <button class="btn btn-autoplay" id="btnAutoPlay" onclick="autoPlay()" disabled>Auto Play</button>
      <button class="btn btn-stop" id="btnStop" onclick="stopPlay()" disabled>Stop</button>
    </div>

  </div>

  <div class="right">
    <div class="results-bar">
      <span class="slabel">Words found</span>
      <span class="badge" id="badge"></span>
    </div>
    <div id="results">
      <div class="placeholder">
        Snip your grid with <strong>Win + Shift + S</strong><br>
        then click <strong>Paste from Clipboard</strong>
      </div>
    </div>
  </div>
</div>

<button id="stopBar" onclick="stopPlay()">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>
  STOP
</button>

<script>
  const $ = id => document.getElementById(id);

  let calibrated = false;
  let allPaths = [];

  function gridSize() { return parseInt($('sizeSelect').value); }

  function buildGrid(letters = []) {
    const n = gridSize();
    const el = $('grid');
    el.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
    el.innerHTML = '';
    for (let i = 0; i < n * n; i++) {
      const wrap = document.createElement('div');
      wrap.className = 'tile-wrap';

      const inp = document.createElement('input');
      inp.type = 'text';
      inp.maxLength = 1;
      inp.className = 'tile';
      inp.value = letters[i] || '';
      inp.addEventListener('input', e => {
        const v = e.target.value.replace(/[^a-zA-Z]/g, '').toUpperCase();
        e.target.value = v;
        e.target.classList.remove('err');
        if (v) { const nx = el.children[i + 1]; if (nx) nx.querySelector('.tile').focus(); }
      });
      inp.addEventListener('keydown', e => {
        const n = gridSize();
        const move = {
          'ArrowRight': i + 1,
          'ArrowLeft':  i - 1,
          'ArrowDown':  i + n,
          'ArrowUp':    i - n,
        }[e.key];
        if (move !== undefined) {
          e.preventDefault();
          const target = el.children[move];
          if (target) target.querySelector('.tile').focus();
          return;
        }
        if (e.key === 'Backspace' && !e.target.value) {
          const pv = el.children[i - 1];
          if (pv) { const t = pv.querySelector('.tile'); t.focus(); t.value = ''; }
        }
        if (e.key === 'Enter') solve();
      });

      const badge = document.createElement('div');
      badge.className = 'tile-badge';

      wrap.appendChild(inp);
      wrap.appendChild(badge);
      el.appendChild(wrap);
    }
  }

  function letters() {
    return [...$('grid').querySelectorAll('.tile')].map(t => t.value.trim());
  }

  let lockedChip = null;

  function showPath(path) {
    clearPath();
    const n = gridSize();
    const gridEl = $('grid');
    const gridRect = gridEl.getBoundingClientRect();

    // Collect tile centers relative to the grid
    const centers = path.map(([r, c]) => {
      const wrap = gridEl.children[r * n + c];
      const rect = wrap.getBoundingClientRect();
      return [
        rect.left - gridRect.left + rect.width  / 2,
        rect.top  - gridRect.top  + rect.height / 2,
      ];
    });

    // Draw SVG path
    const svg = $('path-svg');
    const NS = 'http://www.w3.org/2000/svg';

    if (centers.length > 1) {
      const poly = document.createElementNS(NS, 'polyline');
      poly.setAttribute('points', centers.map(([x,y]) => `${x},${y}`).join(' '));
      poly.setAttribute('fill', 'none');
      poly.setAttribute('stroke', '#15803D');
      poly.setAttribute('stroke-width', '2.5');
      poly.setAttribute('stroke-opacity', '0.75');
      poly.setAttribute('stroke-linecap', 'round');
      poly.setAttribute('stroke-linejoin', 'round');
      poly.setAttribute('stroke-dasharray', '0');
      svg.appendChild(poly);
    }

    // Dots at each step (amber for start, green for rest)
    centers.forEach(([x, y], i) => {
      const dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', y);
      dot.setAttribute('r', i === 0 ? 5 : 3.5);
      dot.setAttribute('fill', i === 0 ? '#D97706' : '#15803D');
      dot.setAttribute('fill-opacity', '0.9');
      svg.appendChild(dot);
    });

    // Tile highlights + badges
    path.forEach(([r, c], i) => {
      const wrap = gridEl.children[r * n + c];
      if (!wrap) return;
      wrap.querySelector('.tile').classList.add(i === 0 ? 'path-start' : 'path-active');
      const badge = wrap.querySelector('.tile-badge');
      badge.textContent = i + 1;
      badge.className = 'tile-badge' + (i === 0 ? ' first' : '');
      badge.style.display = 'block';
    });
  }

  function clearPath() {
    $('grid').querySelectorAll('.tile').forEach(t => t.classList.remove('path-active', 'path-start'));
    $('grid').querySelectorAll('.tile-badge').forEach(b => { b.style.display = 'none'; });
    $('path-svg').innerHTML = '';
  }

  function status(html, cls = '') {
    const el = $('status');
    el.innerHTML = html;
    el.className = cls;
  }

  function busy(id, on) { $(id).disabled = on; }

  async function paste() {
    busy('btnPaste', true);
    status('<span class="spin"></span>Reading clipboard…');
    const r = await window.pywebview.api.paste(gridSize());
    busy('btnPaste', false);
    if (r.error) { status(r.error, 'err'); return; }
    // Clear previous results
    if (lockedChip) { lockedChip.classList.remove('selected'); lockedChip = null; }
    clearPath();
    $('results').innerHTML = '';
    const badge = $('badge');
    badge.textContent = '';
    badge.classList.remove('on');

    const pv = $('preview');
    pv.innerHTML = `<img src="data:image/png;base64,${r.preview}">`;
    pv.classList.add('filled');
    buildGrid(r.grid.flat().map(l => l === '?' ? '' : l));
    status('OCR done — check tiles, then Solve', 'ok');
  }

  async function solve() {
    const n = gridSize();
    const ls = letters();
    const empties = [...$('grid').querySelectorAll('.tile')].filter(t => !t.value);
    if (empties.length) {
      empties.forEach(t => t.classList.add('err'));
      status(`Fill all ${n * n} tiles first`, 'err');
      return;
    }
    busy('btnSolve', true);
    status('<span class="spin"></span>Solving…');
    const r = await window.pywebview.api.solve(ls, n);
    busy('btnSolve', false);
    if (r.error) { status(r.error, 'err'); return; }

    const entries = r.words; // [{word, path}, ...]
    allPaths = entries.map(e => e.path);
    updateAutoPlayBtn();
    const badge = $('badge');
    badge.textContent = entries.length;
    badge.classList.add('on');
    status('Hover a word to trace its path');

    const box = $('results');
    if (!entries.length) { box.innerHTML = '<div class="placeholder">No words found.</div>'; return; }

    let html = '', curLen = null, grp = [];
    const flush = () => {
      if (!grp.length) return;
      const chips = grp.map(({word, path}) => {
        const cls = word.length >= 7 ? 'chip gold' : 'chip';
        const pd = encodeURIComponent(JSON.stringify(path));
        return `<span class="${cls}" data-path="${pd}"
          onmouseenter="if(!lockedChip) showPath(JSON.parse(decodeURIComponent(this.dataset.path)))"
          onmouseleave="if(!lockedChip) clearPath()"
          onclick="clickChip(this)"
        >${word}</span>`;
      }).join('');
      html += `<div class="len-group"><div class="len-hdr">${curLen} letters</div><div class="chips">${chips}</div></div>`;
      grp = [];
    };
    for (const entry of entries) {
      if (entry.word.length !== curLen) { flush(); curLen = entry.word.length; }
      grp.push(entry);
    }
    flush();
    box.innerHTML = html;
  }

  function clearAll() {
    lockedChip = null;
    allPaths = [];
    updateAutoPlayBtn();
    buildGrid();
    const pv = $('preview');
    pv.innerHTML = '<div class="preview-ph"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" opacity=".5"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 21V9"/></svg><span>No image</span></div>';
    pv.classList.remove('filled');
    $('results').innerHTML = '<div class="placeholder">Snip your grid with <strong>Win + Shift + S</strong><br>then click <strong>Paste from Clipboard</strong></div>';
    const badge = $('badge');
    badge.textContent = '';
    badge.classList.remove('on');
    status('');
  }

  function clickChip(el) {
    if (lockedChip === el) {
      // Clicking the same word unlocks it
      lockedChip.classList.remove('selected');
      lockedChip = null;
      clearPath();
    } else {
      // Switch lock to new word
      if (lockedChip) lockedChip.classList.remove('selected');
      lockedChip = el;
      lockedChip.classList.add('selected');
      showPath(JSON.parse(decodeURIComponent(el.dataset.path)));
    }
  }

  async function togglePin() {
    const active = await window.pywebview.api.toggle_on_top();
    $('btnPin').classList.toggle('active', active);
    $('btnPin').title = active ? 'Always on top (on)' : 'Always on top';
  }

  // ── Auto-solver ──────────────────────────────────────────────────────────────

  function updateAutoPlayBtn() {
    $('btnAutoPlay').disabled = !(calibrated && allPaths.length);
  }

  function startCalib() {
    $('calibStatus').textContent = 'Click top-left letter, then bottom-right…';
    $('calibStatus').style.color = 'var(--accent)';
    $('btnCalib').disabled = true;
    window.pywebview.api.start_calibrate();
  }

  function onCalibrateComplete(x1, y1, x2, y2) {
    calibrated = true;
    $('calibStatus').textContent = `(${x1},${y1}) → (${x2},${y2})`;
    $('calibStatus').style.color = 'var(--primary)';
    $('btnCalib').disabled = false;
    updateAutoPlayBtn();
  }

  async function autoPlay() {
    if (!calibrated || !allPaths.length) return;
    const delay = Math.max(50, parseInt($('delayInput').value) || 600);
    busy('btnAutoPlay', true);
    $('btnStop').disabled = false;
    $('stopBar').classList.add('visible');
    status('<span class="spin"></span>Auto playing…');
    await window.pywebview.api.auto_play(allPaths, gridSize(), delay);
  }

  function stopPlay() {
    window.pywebview.api.stop_play();
    $('btnStop').disabled = true;
    $('stopBar').classList.remove('visible');
    busy('btnAutoPlay', false);
    status('Stopped');
  }

  function onAutoPlayDone() {
    $('btnStop').disabled = true;
    $('stopBar').classList.remove('visible');
    busy('btnAutoPlay', false);
    status('Done!', 'ok');
  }

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('btnStop').disabled) {
      e.preventDefault();
      stopPlay();
    }
  });

  async function initWordFiles() {
    const files = await window.pywebview.api.get_word_files();
    const sel = $('wordFile');
    sel.innerHTML = files.map(f =>
      `<option value="${f}"${f === 'words_sowpods.txt' ? ' selected' : ''}>${f.replace(/\.txt$/i, '')}</option>`
    ).join('');
  }

  async function setWordFile() {
    const f = $('wordFile').value;
    if (!f) return;
    status('<span class="spin"></span>Loading words…');
    const r = await window.pywebview.api.set_word_file(f);
    if (r.error) { status(r.error, 'err'); return; }
    status(`${r.count.toLocaleString()} words loaded`, 'ok');
  }

  buildGrid();
  window.addEventListener('pywebviewready', initWordFiles);
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import traceback
    try:
        print('Loading word list...')
        word_set = load_words()
        prefixes = build_prefixes(word_set)
        print(f'{len(word_set):,} words ready.')

        api = Api(word_set, prefixes)
        window = pywebview.create_window(
            'Boggle Solver',
            html=HTML,
            js_api=api,
            width=540,
            height=760,
            min_size=(480, 520),
            frameless=True,
        )
        api._win = window
        pywebview.start()
    except Exception:
        err = traceback.format_exc()
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'boggle_error.log')
        with open(log, 'w') as f:
            f.write(err)
        print(err)
        input('Press Enter to exit...')
