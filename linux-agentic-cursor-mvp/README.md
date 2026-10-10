# Linux Agentic Cursor MVP (X11)

Minimal Linux MVP inspired by Clicky's pointing flow.

## Scope

- X11 only (Wayland intentionally deferred)
- Global trigger hotkey (`ctrl+alt` by default)
- Single-screen screenshot capture
- Sends prompt + screenshot to existing Worker `/chat`
- Parses `[POINT:x,y:label]` and animates a lightweight overlay cursor
- One-point-at-a-time animation queue

## What this is not (yet)

- Not a port of the macOS Swift app
- No voice transcription or TTS in this MVP
- No multi-monitor logic
- No Wayland support

## Setup

```bash
cd /home/runner/work/clicky-fork/clicky-fork/linux-agentic-cursor-mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python3 agentic_cursor.py --worker-base-url http://localhost:8787
```

Then press `ctrl+alt`, type a prompt in the terminal, and hit Enter.

## Guardrails implemented

- If no `[POINT:...]` tag is present, no cursor animation runs
- If point is out of bounds, it is clamped to the single-screen bounds
- If screenshot capture fails, request still goes out without image context
- If overlay init fails, prompt/response flow still works without animation
