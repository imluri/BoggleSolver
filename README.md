# Boggle Solver

A desktop Boggle solver with OCR grid capture and an auto-play mouse driver for Roblox (or any Boggle-style game).

![screenshot](https://i.imgur.com/placeholder.png)

## Features

- **OCR capture** — snip your board with `Win + Shift + S`, paste it in; EasyOCR reads the letters automatically
- **Solver** — DFS trie search finds every valid word, sorted by length; hover a word to trace its path on the board
- **Auto-play** — calibrate the grid corners once, then let the solver drag the mouse through every word
- Multiple word lists — drop any `.txt` word list (one word per line) into the folder and switch on the fly
- Sizes 3 × 3 through 6 × 6
- Frameless dark UI, always-on-top pin, smooth mouse movement for DirectInput games

## Requirements

- Windows 10/11
- Python 3.10+
- A GPU is recommended for EasyOCR but not required (falls back to CPU)

## Setup

```bat
pip install -r requirements.txt
```

## Usage

```bat
python boggle.py
```

### Solving a board manually

1. Click the grid tiles and type the letters, or use arrow keys to navigate
2. Press **Solve** (or `Enter`)
3. Hover any word in the results panel to see its path highlighted on the board; click to lock it

### OCR capture

1. Snip the Boggle grid with `Win + Shift + S`
2. Click **Paste from Clipboard** — the letters are filled in automatically
3. Correct any misread tiles, then **Solve**

### Auto-play

1. Solve the board first
2. Click **Set Grid** — the window minimises; click the **top-left letter centre**, then the **bottom-right letter centre** on screen
3. Set a **Delay** (ms between words; 400–800 ms works well)
4. Click **Auto Play** — press `Esc` or the red **STOP** bar to abort at any time

## Word lists

The solver ships with `words_sowpods.txt` (SOWPODS tournament list). Add any `.txt` file — one uppercase or lowercase word per line — to the project folder and select it from the **Words** dropdown.

## Dependencies

| Package | Purpose |
|---|---|
| `pywebview` | Chromium-based UI window |
| `Pillow` | Clipboard image grab |
| `opencv-python` | Image preprocessing for OCR |
| `numpy` | Array ops |
| `easyocr` | Letter recognition |
| `pydirectinput` | DirectInput mouse for Roblox/DX games |
| `pynput` | Global mouse/keyboard listener for calibration |
