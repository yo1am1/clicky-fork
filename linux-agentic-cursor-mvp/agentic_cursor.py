#!/usr/bin/env python3
"""Minimal Linux (X11) agentic cursor MVP inspired by Clicky."""

from __future__ import annotations

import argparse
import base64
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import mss
import requests
from mss import tools as mss_tools
from pynput import keyboard, mouse

try:
    import tkinter as tk
except Exception:  # pragma: no cover
    tk = None

POINT_TAG_PATTERN = re.compile(r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::([^\]:\s][^\]:]*?))?)\]\s*$")

SYSTEM_PROMPT = (
    "you are an assistant that helps users based on their screenshot. "
    "append exactly one tag at the end: [POINT:x,y:label] when pointing helps, "
    "or [POINT:none] when it does not. coordinates must be in image pixel space "
    "with top-left origin. keep your spoken answer concise."
)


@dataclass
class ParsedPoint:
    spoken_text: str
    point: Optional[Tuple[int, int]]
    label: Optional[str]


class OverlayCursor:
    """Small top-most cursor overlay that animates fly-to-point and return."""

    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError("tkinter is not available on this system")

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.attributes("-alpha", 0.92)

        self.canvas = tk.Canvas(self.root, width=80, height=80, bg="black", highlightthickness=0)
        self.canvas.pack()

        self.triangle = self.canvas.create_polygon(
            40,
            18,
            24,
            58,
            56,
            58,
            fill="#3B82F6",
            outline="#60A5FA",
        )
        self.label = self.canvas.create_text(
            40,
            72,
            text="",
            fill="white",
            font=("Sans", 9, "bold"),
        )

        self.mouse_controller = mouse.Controller()
        self.screen_width = self.root.winfo_screenwidth()
        self.screen_height = self.root.winfo_screenheight()
        self.animation_queue: "queue.Queue[Tuple[int, int, str]]" = queue.Queue(maxsize=1)
        self.is_animating = False

        self.root.deiconify()
        self._follow_mouse()

    def run(self) -> None:
        self.root.mainloop()

    def _follow_mouse(self) -> None:
        if not self.is_animating:
            cursor_x, cursor_y = self.mouse_controller.position
            self._move_overlay(cursor_x, cursor_y)
            self._start_next_animation()
        self.root.after(16, self._follow_mouse)

    def queue_point(self, x: int, y: int, label: str) -> None:
        clamped_x = max(0, min(x, self.screen_width - 1))
        clamped_y = max(0, min(y, self.screen_height - 1))
        while not self.animation_queue.empty():
            try:
                self.animation_queue.get_nowait()
            except queue.Empty:
                break
        self.animation_queue.put((clamped_x, clamped_y, label))

    def _start_next_animation(self) -> None:
        if self.is_animating or self.animation_queue.empty():
            return

        target_x, target_y, label = self.animation_queue.get()
        start_x, start_y = self.mouse_controller.position
        self.is_animating = True
        self.canvas.itemconfig(self.label, text="")

        duration = 0.8
        frames = max(1, int(duration / (1 / 60)))
        control_x = (start_x + target_x) / 2
        control_y = min(start_y, target_y) - min(abs(target_x - start_x) * 0.2, 100)

        def frame_step(frame_index: int) -> None:
            if frame_index > frames:
                self._move_overlay(target_x, target_y)
                self.canvas.itemconfig(self.label, text=label[:24])
                self.root.after(1200, self._return_to_cursor)
                return

            t_linear = frame_index / frames
            t = t_linear * t_linear * (3 - 2 * t_linear)
            one_minus_t = 1 - t
            bezier_x = (
                one_minus_t * one_minus_t * start_x
                + 2 * one_minus_t * t * control_x
                + t * t * target_x
            )
            bezier_y = (
                one_minus_t * one_minus_t * start_y
                + 2 * one_minus_t * t * control_y
                + t * t * target_y
            )
            self._move_overlay(int(bezier_x), int(bezier_y))
            self.root.after(16, lambda: frame_step(frame_index + 1))

        frame_step(0)

    def _return_to_cursor(self) -> None:
        cursor_x, cursor_y = self.mouse_controller.position
        self.canvas.itemconfig(self.label, text="")
        self._move_overlay(cursor_x, cursor_y)
        self.is_animating = False
        self._start_next_animation()

    def _move_overlay(self, center_x: int, center_y: int) -> None:
        self.root.geometry(f"80x80+{int(center_x - 16)}+{int(center_y - 20)}")


class AgenticCursorRunner:
    def __init__(self, worker_base_url: str, model: str, prompt: str, overlay: Optional[OverlayCursor]) -> None:
        self.worker_base_url = worker_base_url.rstrip("/")
        self.model = model
        self.prompt = prompt
        self.overlay = overlay

    def run_once(self) -> None:
        screenshot_result = self._capture_screen()
        response_text = self._call_claude(screenshot_result)
        parsed_point = self._parse_point_tag(response_text)

        print(f"\nAssistant: {parsed_point.spoken_text}\n")

        if parsed_point.point is None:
            return

        if self.overlay is None:
            print("Overlay unavailable. Skipping pointer animation.")
            return

        screenshot_width, screenshot_height = screenshot_result[1], screenshot_result[2]
        screen_width = self.overlay.screen_width
        screen_height = self.overlay.screen_height

        point_x, point_y = parsed_point.point
        clamped_point_x = max(0, min(point_x, screenshot_width))
        clamped_point_y = max(0, min(point_y, screenshot_height))

        mapped_x = int(clamped_point_x * (screen_width / screenshot_width))
        mapped_y = int(clamped_point_y * (screen_height / screenshot_height))

        self.overlay.queue_point(mapped_x, mapped_y, parsed_point.label or "here")

    def _capture_screen(self) -> Tuple[Optional[bytes], int, int]:
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                png_bytes = mss_tools.to_png(shot.rgb, shot.size)
                return png_bytes, shot.width, shot.height
        except Exception as error:
            print(f"Screenshot failed ({error}). Continuing without image.")
            if self.overlay is not None:
                return None, self.overlay.screen_width, self.overlay.screen_height
            return None, 1920, 1080

    def _call_claude(self, screenshot_result: Tuple[Optional[bytes], int, int]) -> str:
        image_bytes, screenshot_width, screenshot_height = screenshot_result
        content_blocks = []

        if image_bytes is not None:
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    },
                }
            )
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"single screen capture (image dimensions: {screenshot_width}x{screenshot_height} pixels)",
                }
            )

        content_blocks.append({"type": "text", "text": self.prompt})

        payload = {
            "model": self.model,
            "max_tokens": 600,
            "stream": True,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content_blocks}],
        }

        with requests.post(
            f"{self.worker_base_url}/chat",
            json=payload,
            stream=True,
            timeout=120,
        ) as response:
            response.raise_for_status()
            accumulated = []
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data: "):
                    continue
                event_data = raw_line[6:]
                if event_data == "[DONE]":
                    break
                try:
                    event_json = json.loads(event_data)
                except Exception:
                    continue
                if event_json.get("type") != "content_block_delta":
                    continue
                delta = event_json.get("delta") or {}
                if delta.get("type") == "text_delta":
                    accumulated.append(delta.get("text", ""))
            return "".join(accumulated).strip()

    @staticmethod
    def _parse_point_tag(response_text: str) -> ParsedPoint:
        match = POINT_TAG_PATTERN.search(response_text)
        if not match:
            return ParsedPoint(spoken_text=response_text.strip(), point=None, label=None)

        spoken_text = response_text[: match.start()].strip()
        if match.group(1) is None or match.group(2) is None:
            return ParsedPoint(spoken_text=spoken_text, point=None, label="none")

        x = int(match.group(1))
        y = int(match.group(2))
        label = (match.group(3) or "here").strip()
        return ParsedPoint(spoken_text=spoken_text, point=(x, y), label=label)


def main() -> None:
    parser = argparse.ArgumentParser(description="Linux X11 agentic cursor MVP")
    parser.add_argument(
        "--worker-base-url",
        default="http://localhost:8787",
        help="Cloudflare Worker base URL",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5",
        help="Claude model passed through /chat",
    )
    parser.add_argument(
        "--hotkey",
        default="<ctrl>+<alt>",
        help="Global trigger hotkey",
    )
    args = parser.parse_args()

    overlay: Optional[OverlayCursor]
    try:
        overlay = OverlayCursor()
    except Exception as error:
        print(f"Overlay init failed ({error}). Running without cursor overlay.")
        overlay = None

    print("Agentic cursor MVP is running.")
    print("Press Ctrl+C to exit.")
    print(f"Press {args.hotkey} then type your prompt in the terminal.")

    is_processing_lock = threading.Lock()

    def on_hotkey_pressed() -> None:
        if not is_processing_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                user_prompt = input("\nPrompt: ").strip()
                if not user_prompt:
                    print("Prompt was empty. Skipping run.")
                    return
                runner = AgenticCursorRunner(
                    worker_base_url=args.worker_base_url,
                    model=args.model,
                    prompt=user_prompt,
                    overlay=overlay,
                )
                runner.run_once()
            except Exception as error:
                print(f"Run failed: {error}")
            finally:
                is_processing_lock.release()

        threading.Thread(target=worker, daemon=True).start()

    hotkey_listener = keyboard.GlobalHotKeys({args.hotkey: on_hotkey_pressed})
    hotkey_listener.start()

    try:
        if overlay is not None:
            overlay.run()
        else:
            while True:
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        hotkey_listener.stop()


if __name__ == "__main__":
    main()
