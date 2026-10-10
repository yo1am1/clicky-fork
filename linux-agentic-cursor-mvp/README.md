# Linux Agentic Cursor MVP (X11)

Minimal Linux MVP inspired by Clicky's pointing flow.

## Scope

- X11 only (Wayland intentionally deferred)
- Global trigger hotkey (`ctrl+alt` by default)
- Single-screen screenshot capture
- Sends prompt + screenshot to existing Worker `/chat`
- Parses `[POINT:x,y:label]` and animates a lightweight overlay cursor
- One-point-at-a-time animation queue
- Autonomous multi-step mode using a LangGraph runtime

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

Interactive one-shot cursor mode:

```bash
python3 agentic_cursor.py --mode interactive --worker-base-url http://localhost:8787
```

Then press `ctrl+alt`, type a prompt in the terminal, and hit Enter.

Autonomous step-loop mode (plan -> kev guardrail -> execute -> screenshot -> replan):

```bash
python3 agentic_cursor.py \
  --mode autonomous \
  --worker-base-url http://localhost:8787 \
  --task "create folder, download nine photos, rename 1 to 9, blur the eighth photo" \
  --workspace-dir /absolute/path/to/workspace \
  --run-artifacts-dir /absolute/path/to/runs \
  --max-steps 25
```

Optional model controls:

- `--planner-model` for the planning node
- `--kev-model` for KEV system-first guardrails

If omitted, both default to `--model`.

## Guardrails implemented

- If no `[POINT:...]` tag is present, no cursor animation runs
- If point is out of bounds, it is clamped to the single-screen bounds
- If screenshot capture fails, request still goes out without image context
- If overlay init fails, prompt/response flow still works without animation
- Autonomous mode stores `before` and `after` screenshots for every step
- Autonomous mode writes `steps.jsonl` logs with decision, result, and errors
- KEV guardrail pass validates or overrides each planned action before execution
