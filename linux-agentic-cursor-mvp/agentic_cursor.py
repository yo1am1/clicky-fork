#!/usr/bin/env python3
"""Linux (X11-first) agentic cursor MVP with optional autonomous step loop."""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import subprocess
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple, TypedDict
from urllib.parse import urljoin, urlparse

import mss
import requests
from bs4 import BeautifulSoup
warnings.filterwarnings(
    "ignore",
    message="The default value of `allowed_objects` will change in a future version.*",
)
from langgraph.graph import END, START, StateGraph
from mss import tools as mss_tools
from PIL import Image, ImageFilter

try:
    import tkinter as tk
except Exception:  # pragma: no cover
    tk = None

PYNPUT_IMPORT_ERROR: Optional[str] = None
try:
    from pynput import keyboard, mouse
except Exception as pynput_error:  # pragma: no cover
    keyboard = None
    mouse = None
    PYNPUT_IMPORT_ERROR = str(pynput_error)

POINT_TAG_PATTERN = re.compile(r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::([^\]:\s][^\]:]*?))?)\]\s*$")

POINTING_SYSTEM_PROMPT = (
    "you are an assistant that helps users based on their screenshot. "
    "append exactly one tag at the end: [POINT:x,y:label] when pointing helps, "
    "or [POINT:none] when it does not. coordinates must be in image pixel space "
    "with top-left origin. keep your spoken answer concise."
)

AUTONOMOUS_SYSTEM_PROMPT = """
you are an autonomous linux desktop execution planner.

you must return exactly one JSON object and nothing else.

you receive:
- user task
- current step index
- previous step history (success/error)
- current screenshot dimensions

choose exactly one action for this step. if the task is complete, return action "finish".

available actions:
1) create_folder {"path":"/abs/or/relative/path"}
2) open_browser {"url":"https://..."}
3) collect_image_urls_from_page {"page_url":"https://...", "max_results":9}
4) download_photos {"urls":["https://..."], "target_dir":"...", "limit":9}
5) move_files {"source_dir":"...", "target_dir":"..."}
6) rename_photos_sequential {"target_dir":"...", "start":1, "count":9}
7) blur_image {"path":"...", "radius":14}
8) list_files {"path":"..."}
9) validate_file_count {"path":"...", "expected":9}
10) finish {"message":"short completion summary"}

requirements:
- adapt to failures from prior steps; if something fails, choose a corrective next action.
- prefer deterministic actions and absolute paths when possible.
- for "download_photos", pass explicit URLs.
- for "rename_photos_sequential", rename image files to 1..N preserving extension.
- if the user requests the 8th image to be blurred, ensure blur_image targets that file.

output schema:
{
  "step_summary": "short reasoning summary",
  "action": "one action name",
  "args": { ... },
  "done": false
}

for finish action, set done=true.
""".strip()

KEV_GUARDRAIL_SYSTEM_PROMPT = """
you are kev, a system-first guardrail model.

you must evaluate a proposed autonomous desktop action before execution.
return exactly one JSON object and nothing else:
{
  "allow": true,
  "reason": "short reason",
  "safe_action": "optional replacement action",
  "safe_args": {}
}

deny actions that are destructive, unrelated to the user task, or missing required arguments.
if you deny and can suggest a safer correction, set safe_action and safe_args.
if action is safe and relevant, set allow=true.
""".strip()


@dataclass
class ParsedPoint:
    spoken_text: str
    point: Optional[Tuple[int, int]]
    label: Optional[str]


@dataclass
class StepDecision:
    step_summary: str
    action: str
    args: dict[str, Any]
    done: bool


class AutonomousGraphState(TypedDict, total=False):
    task: str
    history: list[dict[str, Any]]
    current_step_index: int
    max_steps: int
    latest_decision: StepDecision
    before_screenshot_bytes: Optional[bytes]
    before_screenshot_width: int
    before_screenshot_height: int
    before_screenshot_path: Optional[str]
    halt: bool


class OverlayCursor:
    """Small top-most cursor overlay that animates fly-to-point and return."""

    def __init__(self) -> None:
        if tk is None:
            raise RuntimeError("tkinter is not available on this system")
        if mouse is None:
            raise RuntimeError(f"pynput mouse backend unavailable: {PYNPUT_IMPORT_ERROR}")

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.attributes("-alpha", 0.92)

        self.canvas = tk.Canvas(self.root, width=80, height=80, bg="black", highlightthickness=0)
        self.canvas.pack()

        self.canvas.create_polygon(
            40,
            18,
            24,
            58,
            56,
            58,
            fill="#3B82F6",
            outline="#60A5FA",
        )
        self.label = self.canvas.create_text(40, 72, text="", fill="white", font=("Sans", 9, "bold"))

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


class ScreenshotService:
    @staticmethod
    def capture_screen() -> Tuple[Optional[bytes], int, int]:
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                png_bytes = mss_tools.to_png(shot.rgb, shot.size)
                return png_bytes, shot.width, shot.height
        except Exception as error:
            print(f"Screenshot failed ({error}).")
            return None, 1920, 1080


class ClaudeWorkerClient:
    def __init__(self, worker_base_url: str, model: str) -> None:
        self.worker_base_url = worker_base_url.rstrip("/")
        self.model = model

    def ask_for_pointing_response(
        self,
        prompt: str,
        screenshot_bytes: Optional[bytes],
        screenshot_width: int,
        screenshot_height: int,
    ) -> str:
        content_blocks = self._image_and_text_content(
            screenshot_bytes=screenshot_bytes,
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            text=prompt,
        )

        payload = {
            "model": self.model,
            "max_tokens": 600,
            "stream": True,
            "system": POINTING_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content_blocks}],
        }

        with requests.post(
            f"{self.worker_base_url}/chat",
            json=payload,
            stream=True,
            timeout=120,
        ) as response:
            response.raise_for_status()
            accumulated_chunks: list[str] = []
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
                    accumulated_chunks.append(delta.get("text", ""))
            return "".join(accumulated_chunks).strip()

    def ask_for_autonomous_step(
        self,
        user_task: str,
        step_index: int,
        history: list[dict[str, Any]],
        screenshot_bytes: Optional[bytes],
        screenshot_width: int,
        screenshot_height: int,
        model_name: Optional[str] = None,
    ) -> StepDecision:
        history_payload = history[-10:]
        prompt_payload = {
            "task": user_task,
            "step": step_index,
            "screenshot": {
                "width": screenshot_width,
                "height": screenshot_height,
                "available": screenshot_bytes is not None,
            },
            "history": history_payload,
        }

        content_blocks = self._image_and_text_content(
            screenshot_bytes=screenshot_bytes,
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
            text=json.dumps(prompt_payload, ensure_ascii=False),
        )

        payload = {
            "model": model_name or self.model,
            "max_tokens": 900,
            "stream": False,
            "system": AUTONOMOUS_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content_blocks}],
        }

        response = requests.post(
            f"{self.worker_base_url}/chat",
            json=payload,
            timeout=120,
        )
        response.raise_for_status()
        response_json = response.json()

        response_text = self._extract_text_from_anthropic_response(response_json)
        decision_json = self._extract_json_object(response_text)

        action = str(decision_json.get("action", "")).strip()
        args = decision_json.get("args")
        done = bool(decision_json.get("done", False))
        step_summary = str(decision_json.get("step_summary", "")).strip() or "No summary provided"

        if not action:
            raise ValueError(f"Model decision missing action: {response_text}")
        if not isinstance(args, dict):
            args = {}

        return StepDecision(step_summary=step_summary, action=action, args=args, done=done)

    def evaluate_with_kev_guardrails(
        self,
        user_task: str,
        decision: StepDecision,
        history: list[dict[str, Any]],
        model_name: Optional[str] = None,
    ) -> dict[str, Any]:
        prompt_payload = {
            "task": user_task,
            "history_tail": history[-6:],
            "proposed_decision": {
                "step_summary": decision.step_summary,
                "action": decision.action,
                "args": decision.args,
                "done": decision.done,
            },
        }

        payload = {
            "model": model_name or self.model,
            "max_tokens": 450,
            "stream": False,
            "system": KEV_GUARDRAIL_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(prompt_payload, ensure_ascii=False)}],
        }

        response = requests.post(
            f"{self.worker_base_url}/chat",
            json=payload,
            timeout=90,
        )
        response.raise_for_status()
        response_text = self._extract_text_from_anthropic_response(response.json())
        result = self._extract_json_object(response_text)
        allow = bool(result.get("allow", False))
        reason = str(result.get("reason", "")).strip() or "no reason provided"
        safe_action = result.get("safe_action")
        safe_args = result.get("safe_args")
        if safe_action is not None:
            safe_action = str(safe_action).strip()
        if not isinstance(safe_args, dict):
            safe_args = {}
        return {
            "allow": allow,
            "reason": reason,
            "safe_action": safe_action,
            "safe_args": safe_args,
        }

    @staticmethod
    def _extract_text_from_anthropic_response(response_json: dict[str, Any]) -> str:
        content = response_json.get("content")
        if isinstance(content, list):
            text_fragments: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_fragments.append(str(block.get("text", "")))
            return "\n".join(fragment for fragment in text_fragments if fragment).strip()
        return ""

    @staticmethod
    def _extract_json_object(response_text: str) -> dict[str, Any]:
        stripped_text = response_text.strip()
        if stripped_text.startswith("```"):
            stripped_text = re.sub(r"^```(?:json)?\s*", "", stripped_text)
            stripped_text = re.sub(r"\s*```$", "", stripped_text)

        start_index = stripped_text.find("{")
        end_index = stripped_text.rfind("}")
        if start_index == -1 or end_index == -1 or end_index <= start_index:
            raise ValueError(f"Could not parse decision JSON from: {response_text}")

        json_text = stripped_text[start_index : end_index + 1]
        return json.loads(json_text)

    @staticmethod
    def _image_and_text_content(
        screenshot_bytes: Optional[bytes],
        screenshot_width: int,
        screenshot_height: int,
        text: str,
    ) -> list[dict[str, Any]]:
        content_blocks: list[dict[str, Any]] = []
        if screenshot_bytes is not None:
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(screenshot_bytes).decode("utf-8"),
                    },
                }
            )
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"single screen capture (image dimensions: {screenshot_width}x{screenshot_height} pixels)",
                }
            )
        content_blocks.append({"type": "text", "text": text})
        return content_blocks


class LocalActionTools:
    """Local deterministic tools for autonomous task execution."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.last_collected_image_urls: list[str] = []

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        action_name = action.strip()
        if action_name == "create_folder":
            return self._create_folder(args)
        if action_name == "open_browser":
            return self._open_browser(args)
        if action_name == "collect_image_urls_from_page":
            return self._collect_image_urls_from_page(args)
        if action_name == "download_photos":
            return self._download_photos(args)
        if action_name == "move_files":
            return self._move_files(args)
        if action_name == "rename_photos_sequential":
            return self._rename_photos_sequential(args)
        if action_name == "blur_image":
            return self._blur_image(args)
        if action_name == "list_files":
            return self._list_files(args)
        if action_name == "validate_file_count":
            return self._validate_file_count(args)
        if action_name == "finish":
            message = str(args.get("message", "Done")).strip() or "Done"
            return {"message": message, "finished": True}
        raise ValueError(f"Unsupported action: {action_name}")

    def _resolve_path(self, path_value: str) -> Path:
        if not path_value:
            raise ValueError("Path is required")
        raw_path = Path(path_value)
        if raw_path.is_absolute():
            return raw_path
        return (self.workspace_root / raw_path).resolve()

    def _create_folder(self, args: dict[str, Any]) -> dict[str, Any]:
        target_path = self._resolve_path(str(args.get("path", "")).strip())
        target_path.mkdir(parents=True, exist_ok=True)
        return {"created": str(target_path)}

    def _open_browser(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        if not url:
            raise ValueError("url is required")
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened_url": url}

    def _collect_image_urls_from_page(self, args: dict[str, Any]) -> dict[str, Any]:
        page_url = str(args.get("page_url", "")).strip()
        max_results = int(args.get("max_results", 9))
        if not page_url:
            raise ValueError("page_url is required")

        response = requests.get(page_url, timeout=40)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        discovered_urls: list[str] = []
        for image_tag in soup.find_all("img"):
            source_candidate = image_tag.get("src") or image_tag.get("data-src") or image_tag.get("data-original")
            if not source_candidate:
                continue
            full_url = urljoin(page_url, source_candidate)
            parsed_url = urlparse(full_url)
            if parsed_url.scheme not in {"http", "https"}:
                continue
            lowered_url = full_url.lower()
            if any(extension in lowered_url for extension in [".jpg", ".jpeg", ".png", ".webp"]):
                discovered_urls.append(full_url)

        deduplicated_urls = list(dict.fromkeys(discovered_urls))[: max(1, max_results)]
        self.last_collected_image_urls = deduplicated_urls

        return {
            "page_url": page_url,
            "image_urls": deduplicated_urls,
            "count": len(deduplicated_urls),
        }

    def _download_photos(self, args: dict[str, Any]) -> dict[str, Any]:
        target_directory = self._resolve_path(str(args.get("target_dir", "")).strip())
        target_directory.mkdir(parents=True, exist_ok=True)

        urls_from_args = args.get("urls")
        urls: list[str]
        if isinstance(urls_from_args, list) and urls_from_args:
            urls = [str(url).strip() for url in urls_from_args if str(url).strip()]
        else:
            urls = self.last_collected_image_urls

        if not urls:
            raise ValueError("No image URLs provided or previously collected")

        limit = int(args.get("limit", len(urls)))
        selected_urls = urls[: max(1, limit)]

        downloaded_files: list[str] = []
        for image_index, image_url in enumerate(selected_urls, start=1):
            response = requests.get(image_url, timeout=60)
            response.raise_for_status()
            extension = self._guess_extension_from_response(response, image_url)
            output_file_path = target_directory / f"download_{image_index}{extension}"
            output_file_path.write_bytes(response.content)
            downloaded_files.append(str(output_file_path))

        return {
            "target_dir": str(target_directory),
            "downloaded": downloaded_files,
            "count": len(downloaded_files),
        }

    @staticmethod
    def _guess_extension_from_response(response: requests.Response, fallback_url: str) -> str:
        content_type = (response.headers.get("content-type") or "").lower()
        if "png" in content_type:
            return ".png"
        if "webp" in content_type:
            return ".webp"
        if "jpeg" in content_type or "jpg" in content_type:
            return ".jpg"
        fallback_path = urlparse(fallback_url).path.lower()
        for extension in [".jpg", ".jpeg", ".png", ".webp"]:
            if fallback_path.endswith(extension):
                return extension
        return ".jpg"

    def _move_files(self, args: dict[str, Any]) -> dict[str, Any]:
        source_directory = self._resolve_path(str(args.get("source_dir", "")).strip())
        target_directory = self._resolve_path(str(args.get("target_dir", "")).strip())
        target_directory.mkdir(parents=True, exist_ok=True)

        moved_files: list[str] = []
        for child_path in source_directory.iterdir():
            if child_path.is_file():
                destination_path = target_directory / child_path.name
                child_path.rename(destination_path)
                moved_files.append(str(destination_path))

        return {"moved": moved_files, "count": len(moved_files)}

    def _rename_photos_sequential(self, args: dict[str, Any]) -> dict[str, Any]:
        target_directory = self._resolve_path(str(args.get("target_dir", "")).strip())
        start_number = int(args.get("start", 1))
        count_limit = int(args.get("count", 9))

        image_files = [
            path
            for path in sorted(target_directory.iterdir())
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ]

        renamed_files: list[str] = []
        for offset_index, image_file_path in enumerate(image_files[: max(1, count_limit)]):
            destination_number = start_number + offset_index
            destination_path = target_directory / f"{destination_number}{image_file_path.suffix.lower()}"
            image_file_path.rename(destination_path)
            renamed_files.append(str(destination_path))

        return {"renamed": renamed_files, "count": len(renamed_files)}

    def _blur_image(self, args: dict[str, Any]) -> dict[str, Any]:
        target_path = self._resolve_path(str(args.get("path", "")).strip())
        blur_radius = float(args.get("radius", 14))

        with Image.open(target_path) as image_file:
            blurred_image = image_file.filter(ImageFilter.GaussianBlur(radius=blur_radius))
            blurred_image.save(target_path)

        return {"blurred": str(target_path), "radius": blur_radius}

    def _list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        target_path = self._resolve_path(str(args.get("path", ".")).strip())
        if not target_path.exists():
            raise ValueError(f"Path does not exist: {target_path}")
        entries = sorted(path.name for path in target_path.iterdir())
        return {"path": str(target_path), "entries": entries, "count": len(entries)}

    def _validate_file_count(self, args: dict[str, Any]) -> dict[str, Any]:
        target_path = self._resolve_path(str(args.get("path", ".")).strip())
        expected_count = int(args.get("expected", 0))
        image_count = len(
            [
                path
                for path in target_path.iterdir()
                if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
        )
        if image_count != expected_count:
            raise ValueError(f"Image count validation failed: expected {expected_count}, got {image_count}")
        return {"path": str(target_path), "expected": expected_count, "actual": image_count}


class AgenticCursorRunner:
    def __init__(self, claude_client: ClaudeWorkerClient, prompt: str, overlay: Optional[OverlayCursor]) -> None:
        self.claude_client = claude_client
        self.prompt = prompt
        self.overlay = overlay

    def run_once(self) -> None:
        screenshot_bytes, screenshot_width, screenshot_height = ScreenshotService.capture_screen()
        response_text = self.claude_client.ask_for_pointing_response(
            prompt=self.prompt,
            screenshot_bytes=screenshot_bytes,
            screenshot_width=screenshot_width,
            screenshot_height=screenshot_height,
        )
        parsed_point = self._parse_point_tag(response_text)

        print(f"\nAssistant: {parsed_point.spoken_text}\n")

        if parsed_point.point is None:
            return

        if self.overlay is None:
            print("Overlay unavailable. Skipping pointer animation.")
            return

        point_x, point_y = parsed_point.point
        clamped_point_x = max(0, min(point_x, screenshot_width))
        clamped_point_y = max(0, min(point_y, screenshot_height))

        mapped_x = int(clamped_point_x * (self.overlay.screen_width / screenshot_width))
        mapped_y = int(clamped_point_y * (self.overlay.screen_height / screenshot_height))

        self.overlay.queue_point(mapped_x, mapped_y, parsed_point.label or "here")

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


class LangGraphAutonomousAgentRunner:
    def __init__(
        self,
        claude_client: ClaudeWorkerClient,
        task: str,
        workspace_root: Path,
        run_artifacts_directory: Path,
        max_steps: int,
        planner_model: str,
        kev_model: str,
    ) -> None:
        self.claude_client = claude_client
        self.task = task
        self.workspace_root = workspace_root
        self.max_steps = max_steps
        self.planner_model = planner_model
        self.kev_model = kev_model
        self.tools = LocalActionTools(workspace_root=workspace_root)

        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        self.run_directory = run_artifacts_directory / f"run-{timestamp}"
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.log_file_path = self.run_directory / "steps.jsonl"
        self.graph = self._build_graph()

    def run(self) -> None:
        print(f"Autonomous run directory: {self.run_directory}")
        print(f"Workspace root: {self.workspace_root}")
        initial_state: AutonomousGraphState = {
            "task": self.task,
            "history": [],
            "current_step_index": 0,
            "max_steps": self.max_steps,
            "halt": False,
        }
        final_state = self.graph.invoke(initial_state)

        if final_state.get("halt"):
            print("Autonomous run completed or halted.")
        else:
            print("Autonomous run exited without halt flag.")

    def _build_graph(self):
        graph_builder = StateGraph(AutonomousGraphState)
        graph_builder.add_node("planner", self._planner_node)
        graph_builder.add_node("kev_guardrail", self._kev_guardrail_node)
        graph_builder.add_node("executor", self._executor_node)

        graph_builder.add_edge(START, "planner")
        graph_builder.add_edge("planner", "kev_guardrail")
        graph_builder.add_edge("kev_guardrail", "executor")
        graph_builder.add_conditional_edges(
            "executor",
            self._route_after_executor,
            {"continue": "planner", "stop": END},
        )

        return graph_builder.compile()

    def _planner_node(self, state: AutonomousGraphState) -> AutonomousGraphState:
        next_step_index = int(state.get("current_step_index", 0)) + 1
        if next_step_index > self.max_steps:
            return {"current_step_index": next_step_index, "halt": True}

        history = list(state.get("history", []))
        screenshot_before, width_before, height_before = ScreenshotService.capture_screen()
        before_screenshot_path = self.run_directory / f"step-{next_step_index:02d}-before.png"
        if screenshot_before is not None:
            before_screenshot_path.write_bytes(screenshot_before)

        try:
            decision = self.claude_client.ask_for_autonomous_step(
                user_task=self.task,
                step_index=next_step_index,
                history=history,
                screenshot_bytes=screenshot_before,
                screenshot_width=width_before,
                screenshot_height=height_before,
                model_name=self.planner_model,
            )
            print(f"Step {next_step_index}: {decision.action} | {decision.step_summary}")
        except Exception as error:
            decision = StepDecision(
                step_summary=f"planner error: {error}",
                action="finish",
                args={"message": f"planner failed at step {next_step_index}: {error}"},
                done=True,
            )

        return {
            "current_step_index": next_step_index,
            "latest_decision": decision,
            "before_screenshot_bytes": screenshot_before,
            "before_screenshot_width": width_before,
            "before_screenshot_height": height_before,
            "before_screenshot_path": str(before_screenshot_path) if screenshot_before is not None else None,
        }

    def _kev_guardrail_node(self, state: AutonomousGraphState) -> AutonomousGraphState:
        decision = state.get("latest_decision")
        if decision is None:
            return {}

        history = list(state.get("history", []))
        try:
            guardrail_result = self.claude_client.evaluate_with_kev_guardrails(
                user_task=self.task,
                decision=decision,
                history=history,
                model_name=self.kev_model,
            )
        except Exception as error:
            guardrail_result = {
                "allow": True,
                "reason": f"guardrail unavailable: {error}",
                "safe_action": None,
                "safe_args": {},
            }

        if guardrail_result["allow"]:
            return {}

        safe_action = guardrail_result.get("safe_action")
        safe_args = guardrail_result.get("safe_args") or {}
        if safe_action:
            safe_decision = StepDecision(
                step_summary=f"kev override: {guardrail_result['reason']}",
                action=safe_action,
                args=safe_args,
                done=False,
            )
        else:
            safe_decision = StepDecision(
                step_summary=f"kev blocked action: {guardrail_result['reason']}",
                action="finish",
                args={"message": f"Blocked by kev guardrail: {guardrail_result['reason']}"},
                done=True,
            )
        return {"latest_decision": safe_decision}

    def _executor_node(self, state: AutonomousGraphState) -> AutonomousGraphState:
        step_index = int(state.get("current_step_index", 0))
        history = list(state.get("history", []))
        decision = state.get("latest_decision")
        before_screenshot_path_value = state.get("before_screenshot_path")
        before_screenshot_path = Path(before_screenshot_path_value) if before_screenshot_path_value else None

        if decision is None:
            self._record_step(
                history=history,
                step_index=step_index,
                decision=None,
                success=False,
                result={"error": "missing decision"},
                before_screenshot_path=before_screenshot_path,
                after_screenshot_path=None,
            )
            return {"history": history, "halt": step_index >= self.max_steps}

        action_success = True
        try:
            action_result = self.tools.execute(decision.action, decision.args)
        except Exception as error:
            action_success = False
            action_result = {"error": str(error)}

        screenshot_after, _, _ = ScreenshotService.capture_screen()
        after_screenshot_path = self.run_directory / f"step-{step_index:02d}-after.png"
        if screenshot_after is not None:
            after_screenshot_path.write_bytes(screenshot_after)

        self._record_step(
            history=history,
            step_index=step_index,
            decision=decision,
            success=action_success,
            result=action_result,
            before_screenshot_path=before_screenshot_path,
            after_screenshot_path=after_screenshot_path if screenshot_after is not None else None,
        )

        if action_success:
            print(f"Step {step_index}: success")
        else:
            print(f"Step {step_index}: failed -> {action_result.get('error')}")

        should_halt = bool(
            (decision.action == "finish" and action_success)
            or (decision.done and action_success)
            or step_index >= self.max_steps
        )
        if should_halt and step_index >= self.max_steps and decision.action != "finish":
            print("Autonomous run reached max steps before finish action.")

        return {"history": history, "halt": should_halt}

    @staticmethod
    def _route_after_executor(state: AutonomousGraphState) -> str:
        if state.get("halt"):
            return "stop"
        return "continue"

    def _record_step(
        self,
        history: list[dict[str, Any]],
        step_index: int,
        decision: Optional[StepDecision],
        success: bool,
        result: dict[str, Any],
        before_screenshot_path: Optional[Path],
        after_screenshot_path: Optional[Path],
    ) -> None:
        history_entry = {
            "step": step_index,
            "decision": {
                "summary": decision.step_summary if decision else None,
                "action": decision.action if decision else None,
                "args": decision.args if decision else None,
                "done": decision.done if decision else False,
            },
            "success": success,
            "result": result,
            "before_screenshot": str(before_screenshot_path) if before_screenshot_path else None,
            "after_screenshot": str(after_screenshot_path) if after_screenshot_path else None,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        history.append(history_entry)

        with self.log_file_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(history_entry, ensure_ascii=False) + "\n")


def run_interactive_mode(args: argparse.Namespace) -> None:
    if keyboard is None:
        raise RuntimeError(
            f"Interactive mode requires X11 input backend. pynput unavailable: {PYNPUT_IMPORT_ERROR}"
        )

    overlay: Optional[OverlayCursor]
    try:
        overlay = OverlayCursor()
    except Exception as error:
        print(f"Overlay init failed ({error}). Running without cursor overlay.")
        overlay = None

    claude_client = ClaudeWorkerClient(worker_base_url=args.worker_base_url, model=args.model)

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
                AgenticCursorRunner(claude_client=claude_client, prompt=user_prompt, overlay=overlay).run_once()
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


def run_autonomous_mode(args: argparse.Namespace) -> None:
    if not args.task:
        raise ValueError("--task is required in autonomous mode")

    claude_client = ClaudeWorkerClient(worker_base_url=args.worker_base_url, model=args.model)

    workspace_root = Path(args.workspace_dir).expanduser().resolve()
    run_artifacts_directory = Path(args.run_artifacts_dir).expanduser().resolve()

    planner_model_name = args.planner_model or args.model
    kev_model_name = args.kev_model or args.model

    LangGraphAutonomousAgentRunner(
        claude_client=claude_client,
        task=args.task,
        workspace_root=workspace_root,
        run_artifacts_directory=run_artifacts_directory,
        max_steps=args.max_steps,
        planner_model=planner_model_name,
        kev_model=kev_model_name,
    ).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Linux X11 agentic cursor MVP")
    parser.add_argument(
        "--mode",
        choices=["interactive", "autonomous"],
        default="interactive",
        help="interactive: hotkey one-shot mode, autonomous: multi-step loop mode",
    )
    parser.add_argument(
        "--worker-base-url",
        default="http://localhost:8787",
        help="Cloudflare Worker base URL",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Claude model passed through /chat",
    )
    parser.add_argument(
        "--planner-model",
        default="",
        help="Model override for the planning node (autonomous mode)",
    )
    parser.add_argument(
        "--kev-model",
        default="",
        help="Model override for KEV system-first guardrails (autonomous mode)",
    )
    parser.add_argument(
        "--hotkey",
        default="<ctrl>+<alt>",
        help="Global trigger hotkey (interactive mode)",
    )
    parser.add_argument("--task", default="", help="Top-level user task (autonomous mode)")
    parser.add_argument(
        "--workspace-dir",
        default=str(Path.cwd() / "agent-workspace"),
        help="Workspace directory used by autonomous local tools",
    )
    parser.add_argument(
        "--run-artifacts-dir",
        default=str(Path.cwd() / "runs"),
        help="Directory where per-step screenshots and logs are stored",
    )
    parser.add_argument("--max-steps", type=int, default=20, help="Maximum autonomous steps")

    args = parser.parse_args()

    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        print("Warning: this MVP targets X11. Wayland compatibility is limited.")

    if args.mode == "autonomous":
        run_autonomous_mode(args)
    else:
        run_interactive_mode(args)


if __name__ == "__main__":
    main()
