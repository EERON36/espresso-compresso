#!/usr/bin/env python3
"""Cozy desktop front end for the video folder compressor."""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from espresso_compresso_cli import (
    MODES,
    child_process_options,
    discover_videos,
    format_size,
    inspect_toolchain,
    output_location_error,
    preflight_mode,
)


APP_DIR = Path(__file__).resolve().parent
COMPRESSOR = APP_DIR / "espresso_compresso_cli.py"


class Palette:
    BACKGROUND = "#EFE5D5"
    SURFACE = "#FFF9F0"
    SURFACE_ALT = "#F3E8D7"
    INK = "#292621"
    MUTED = "#665F56"
    BORDER = "#CDBCA6"
    ORANGE = "#C5683A"
    ORANGE_DARK = "#91451F"
    BLUE = "#3E687E"
    BLUE_DARK = "#2D5267"
    GREEN = "#607D59"
    COFFEE = "#6B4737"
    STEAM = "#B79278"
    RED = "#A43F35"
    WHITE = "#FFFFFF"


def result_space_from_summary(line: str) -> str | None:
    """Keep result wording compact and testable without starting Tk."""
    if line.startswith("Compression reduction:"):
        return line.strip()
    if line.startswith("Compression increase:"):
        return line.strip()
    return None


def parse_terminal_result(line: str) -> dict[str, object] | None:
    """Read the CLI's stable terminal record without relying on display prose."""
    if not line.startswith("RESULT_JSON:"):
        return None
    try:
        result = json.loads(line.split(":", 1)[1].strip())
    except (json.JSONDecodeError, IndexError):
        return None
    return result if isinstance(result, dict) else None


def technical_log_path_from_output(line: str) -> Path | None:
    """Read the CLI's final log location without exposing it in the activity view."""
    value = line.strip()
    if not value.startswith("Log:"):
        return None
    path_text = value.split(":", 1)[1].strip()
    return Path(path_text) if path_text else None


def activity_from_output(line: str) -> str | None:
    """Turn stable CLI milestones into short, non-technical activity updates."""
    value = line.strip()
    result = parse_terminal_result(value)
    if result is not None:
        encoded = int(result.get("encoded", 0))
        existing = int(result.get("existing_verified", 0))
        skipped = int(result.get("no_benefit", 0))
        failed = int(result.get("failed", 0))
        smaller_label = "original already smaller" if skipped == 1 else "originals already smaller"
        return (
            f"Finished: {encoded} compressed • {existing} already complete • "
            f"{skipped} {smaller_label} • {failed} need attention"
        )
    file_match = re.match(r"\[(\d+)/(\d+)\]\s+(.+)", value)
    if file_match:
        return f"File {file_match.group(1)} of {file_match.group(2)}: {Path(file_match.group(3)).name}"
    if value.startswith("VALIDATED:"):
        return "Validated: " + value.split(":", 1)[1].strip()
    if value.startswith("VERIFIED EXISTING:"):
        return "Existing compressed copy verified."
    if value.startswith("SKIPPED:"):
        return "Skipped: already efficiently compressed."
    if value.startswith("NO SIZE BENEFIT:"):
        return "No size benefit; the original was kept."
    if value.startswith("Tracks preserved:"):
        return "Tracks preserved: " + value.split(":", 1)[1].strip()
    if value.startswith("ORIGINAL KEPT:") or value == "Original kept.":
        return "Original kept by safety checks."
    if value.startswith("Original permanently deleted"):
        return "Original deleted after every safety check passed."
    if value.startswith("Stopped by user"):
        return "Stopped. Completed outputs remain available."
    if (
        "FAILED" in value
        or "NEEDS ATTENTION" in value
        or value.startswith("VALIDATION FAILED")
    ):
        return "A file needs attention. Open the technical log for details."
    return None


def action_bar_grid_positions(width: int) -> dict[str, tuple[int, int]]:
    """Keep the action buttons readable at the app's minimum width."""
    if width < 700:
        return {
            "start": (0, 0), "stop": (0, 1),
            "log": (1, 0), "results": (1, 1),
        }
    return {
        "start": (0, 0), "stop": (0, 1),
        "log": (0, 3), "results": (0, 4),
    }


def deletion_choice_for_scope(scope: str, requested: str) -> str:
    """The three-file test is always non-destructive."""
    return "delete" if scope == "all" and requested == "delete" else "keep"


def removal_result_message(result: dict[str, object] | None) -> tuple[str, str, bool]:
    """Return accessible popup copy for a structured removal result."""
    if result is None:
        return (
            "Original removal results",
            "Disk-space change could not be confirmed. Open the technical log for the final record.",
            True,
        )
    deleted = int(result.get("originals_deleted", 0))
    kept = int(result.get("originals_retained_by_safeguards", 0))
    failed = int(result.get("failed", 0))
    outcome = str(result.get("outcome", "complete-with-issues"))
    needs_attention = outcome != "complete" or kept > 0 or failed > 0
    title = "Original removal results" if needs_attention else "Original removal complete"
    lines = [f"Originals deleted: {deleted}", f"Originals kept by safety checks: {kept}"]
    if outcome == "stopped":
        lines.insert(0, "The job was stopped before all files completed. Unprocessed originals remain unchanged.")
    elif outcome == "complete-with-issues":
        lines.insert(0, "Some files need attention.")
    volumes = result.get("free_space_volumes", [])
    if not isinstance(volumes, list) or not volumes:
        lines.append("Disk-space change could not be confirmed. Open the technical log.")
        needs_attention = True
        title = "Original removal results"
    else:
        for volume in volumes:
            if not isinstance(volume, dict):
                continue
            label = str(volume.get("volume", "This drive"))
            change = volume.get("change_bytes")
            if not isinstance(change, int):
                lines.append(f"{label}: disk-space change could not be confirmed.")
            elif change > 0:
                lines.append(f"{label}: Available disk space increased by {format_size(change)}.")
            elif change < 0:
                lines.append(f"{label}: No net disk space was freed; additional space used: {format_size(change)}.")
            else:
                lines.append(f"{label}: No net disk space was freed.")
    known_output = result.get("known_output_bytes")
    reduction = result.get("compression_reduction_bytes")
    if isinstance(known_output, int):
        lines.append(f"Known compressed copies: {format_size(known_output)}")
    if isinstance(reduction, int):
        label = "Compression reduction" if reduction >= 0 else "Compression increase"
        lines.append(f"{label}: {format_size(reduction)}")
    if failed:
        lines.append(f"Files needing attention: {failed}")
    return title, "\n".join(lines), needs_attention


class CompressorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1500)
        self.stopping = False
        self.current_index = 0
        self.total_files = 0
        self.completed_files = 0
        self.skipped_files = 0
        self.attention_files = 0
        self.file_finished = False
        self.output_path: Path | None = None
        self.advanced_visible = False
        self.activity_visible = False
        self.activity_line_count = 0
        self.technical_log_path: Path | None = None
        self.empty_folder = False
        self.receipt = {"compressed": 0, "complete": 0, "skipped": 0, "attention": 0}
        self.reader_error: str | None = None
        self.terminal_result: dict[str, object] | None = None
        self.delete_requested_for_job = False

        self.folder_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="quality")
        self.scope_var = tk.StringVar(value="test")
        self.fps_cap_var = tk.BooleanVar(value=True)
        self.originals_var = tk.StringVar(value="keep")
        self.output_var = tk.StringVar()

        self.status_var = tk.StringVar(value="Ready")
        self.current_file_var = tk.StringVar(value="Choose a folder to begin")
        self.overall_text_var = tk.StringVar(value="File 0 of 0")
        self.queue_summary_var = tk.StringVar(value="Your folder queue will appear here")
        self.file_text_var = tk.StringVar(value="Waiting")
        self.result_count_var = tk.StringVar(value="0 compressed")
        self.result_space_var = tk.StringVar(value="No results yet")

        self.toolchain = inspect_toolchain()
        self.fast_available = not preflight_mode(self.toolchain, MODES["fast"])
        if self.fast_available:
            self.mode_var.set("fast")
        self._preflight_after: str | None = None

        self._configure_window()
        self._configure_styles()
        self._build_interface()
        self.root.bind("<Configure>", self._adapt_to_window, add="+")
        self.scope_var.trace_add("write", self._scope_changed)
        self.folder_var.trace_add("write", self._schedule_preflight)
        self.output_var.trace_add("write", self._schedule_preflight)
        self._scope_changed()
        self._schedule_preflight()
        self.root.after(80, self._poll_messages)
        self.root.after(300, self._animate_coffee)

    def _configure_window(self) -> None:
        self.root.title("Espresso Compresso")
        usable_width = max(360, self.root.winfo_screenwidth() - 80)
        usable_height = max(320, self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{min(920, usable_width)}x{min(800, usable_height)}")
        self.root.minsize(min(520, usable_width), min(620, usable_height))
        self.root.configure(bg=Palette.BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self._close_window)
        try:
            self.root.tk.call("tk", "scaling", 1.25)
        except tk.TclError:
            pass
        # A small custom title-bar icon. The matching vector artwork is kept
        # beside the app as espresso_compresso_icon.svg for the future .exe package.
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(Palette.BACKGROUND, to=(0, 0, 32, 32))
        icon.put(Palette.INK, to=(6, 10, 24, 24))
        icon.put(Palette.ORANGE, to=(8, 12, 22, 22))
        icon.put(Palette.SURFACE, to=(10, 13, 21, 15))
        icon.put(Palette.INK, to=(22, 14, 28, 20))
        icon.put(Palette.BLUE, to=(6, 24, 26, 27))
        icon.put(Palette.STEAM, to=(12, 5, 14, 10))
        icon.put(Palette.STEAM, to=(18, 4, 20, 10))
        self.root.iconphoto(True, icon)
        self._icon_reference = icon

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Workshop.Horizontal.TProgressbar",
            troughcolor=Palette.SURFACE_ALT,
            background=Palette.ORANGE,
            bordercolor=Palette.BORDER,
            lightcolor=Palette.ORANGE,
            darkcolor=Palette.ORANGE,
            thickness=17,
        )
        style.configure(
            "File.Horizontal.TProgressbar",
            troughcolor=Palette.SURFACE_ALT,
            background=Palette.BLUE,
            bordercolor=Palette.BORDER,
            lightcolor=Palette.BLUE,
            darkcolor=Palette.BLUE,
            thickness=10,
        )
        style.configure(
            "Warm.TCheckbutton",
            background=Palette.SURFACE,
            foreground=Palette.INK,
            font=("Segoe UI", 11),
        )
        style.map(
            "Warm.TCheckbutton",
            background=[("active", Palette.SURFACE)],
            indicatorcolor=[("selected", Palette.BLUE), ("!selected", Palette.WHITE)],
        )
        style.configure(
            "Warm.TRadiobutton",
            background=Palette.SURFACE,
            foreground=Palette.INK,
            font=("Segoe UI", 11),
        )
        style.map(
            "Warm.TRadiobutton",
            background=[("active", Palette.SURFACE)],
            indicatorcolor=[("selected", Palette.BLUE), ("!selected", Palette.WHITE)],
        )

    def _build_interface(self) -> None:
        outer = tk.Frame(self.root, bg=Palette.BACKGROUND)
        outer.pack(fill="both", expand=True, padx=24, pady=18)

        self._build_header(outer)

        scroll_area = tk.Frame(outer, bg=Palette.BACKGROUND)
        scroll_area.pack(fill="both", expand=True, pady=(14, 0))
        self.main_canvas = tk.Canvas(
            scroll_area, bg=Palette.BACKGROUND, highlightthickness=1,
            highlightbackground=Palette.BACKGROUND, highlightcolor=Palette.BLUE_DARK,
            yscrollincrement=24, takefocus=True,
        )
        self.main_scrollbar = ttk.Scrollbar(
            scroll_area, orient="vertical", command=self.main_canvas.yview,
        )
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)
        self.main_canvas.pack(side="left", fill="both", expand=True)
        self.main_scrollbar.pack(side="right", fill="y")
        content = tk.Frame(self.main_canvas, bg=Palette.BACKGROUND)
        self._main_content_window = self.main_canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", self._update_main_scroll_region, add="+")
        self.main_canvas.bind("<Configure>", self._resize_main_content, add="+")
        self.main_canvas.bind("<Up>", lambda _event: self._scroll_main(-1))
        self.main_canvas.bind("<Down>", lambda _event: self._scroll_main(1))
        self.main_canvas.bind("<Prior>", lambda _event: self._scroll_main(-5))
        self.main_canvas.bind("<Next>", lambda _event: self._scroll_main(5))
        self.root.bind_all("<MouseWheel>", self._mouse_wheel, add="+")
        self.root.bind_all("<Button-4>", lambda event: self._linux_wheel(event, -3), add="+")
        self.root.bind_all("<Button-5>", lambda event: self._linux_wheel(event, 3), add="+")
        content.grid_columnconfigure(0, weight=1)

        self._build_folder_card(content)
        self._build_mode_card(content)
        self._build_job_card(content)
        self._build_advanced_card(content)
        self._build_action_bar(content)
        self._build_progress_card(content)
        self._build_activity_panel(content)

    def _update_main_scroll_region(self, _event: tk.Event) -> None:
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def _resize_main_content(self, event: tk.Event) -> None:
        self.main_canvas.itemconfigure(self._main_content_window, width=event.width)

    def _scroll_main(self, units: int) -> str:
        self.main_canvas.yview_scroll(units, "units")
        return "break"

    def _mouse_wheel(self, event: tk.Event) -> str | None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None or str(widget).startswith(str(self.activity_frame)):
            return None
        if str(widget).startswith(str(self.main_canvas)):
            delta = -1 if event.delta > 0 else 1
            return self._scroll_main(delta * 3)
        return None

    def _linux_wheel(self, event: tk.Event, units: int) -> str | None:
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is not None and str(widget).startswith(str(self.main_canvas)):
            return self._scroll_main(units)
        return None

    def _build_header(self, parent: tk.Widget) -> None:
        header = tk.Frame(parent, bg=Palette.BACKGROUND)
        header.pack(fill="x")

        icon = tk.Canvas(
            header,
            width=70,
            height=58,
            bg=Palette.BACKGROUND,
            highlightthickness=0,
        )
        icon.pack(side="left", padx=(0, 15))
        icon.create_rectangle(8, 19, 64, 53, fill=Palette.ORANGE, outline=Palette.INK, width=3)
        icon.create_rectangle(22, 9, 50, 20, fill=Palette.SURFACE, outline=Palette.INK, width=3)
        icon.create_line(14, 34, 58, 34, fill=Palette.INK, width=2)
        icon.create_rectangle(33, 29, 41, 39, fill=Palette.SURFACE, outline=Palette.INK, width=2)

        title_group = tk.Frame(header, bg=Palette.BACKGROUND)
        title_group.pack(side="left", fill="x", expand=True)
        tk.Label(
            title_group,
            text="ESPRESSO COMPRESSO",
            bg=Palette.BACKGROUND,
            fg=Palette.INK,
            font=("Segoe UI Semibold", 23),
        ).pack(anchor="w")
        tk.Label(
            title_group,
            text="A small, dependable tool for smaller recordings.",
            bg=Palette.BACKGROUND,
            fg=Palette.MUTED,
            font=("Segoe UI", 11),
        ).pack(anchor="w", pady=(2, 0))

        self.coffee_canvas = tk.Canvas(
            header,
            width=56,
            height=58,
            bg=Palette.BACKGROUND,
            highlightthickness=0,
        )
        self.coffee_canvas.pack(side="right", padx=(10, 2))
        self.coffee_canvas.create_oval(
            12, 35, 45, 45, fill=Palette.COFFEE, outline=Palette.INK, width=2,
        )
        self.coffee_canvas.create_rectangle(
            14, 24, 41, 40, fill=Palette.COFFEE, outline=Palette.INK, width=2,
        )
        self.coffee_canvas.create_arc(
            37, 27, 51, 38, start=270, extent=180, style="arc",
            outline=Palette.INK, width=2,
        )
        self.coffee_canvas.create_oval(
            16, 24, 39, 29, fill=Palette.ORANGE_DARK, outline=Palette.INK, width=1,
        )
        self._steam_lines = (
            self.coffee_canvas.create_line(
                21, 19, 18, 13, 22, 7, fill=Palette.STEAM, width=2,
            ),
            self.coffee_canvas.create_line(
                31, 19, 34, 13, 30, 7, fill=Palette.STEAM, width=2,
            ),
        )
        self._coffee_phase = 0

        self.status_badge = tk.Label(
            header,
            textvariable=self.status_var,
            bg=Palette.GREEN,
            fg=Palette.WHITE,
            font=("Segoe UI Semibold", 10),
            padx=13,
            pady=7,
        )
        self.status_badge.pack(side="right", padx=(10, 0))

    def _card(self, parent: tk.Widget) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=Palette.SURFACE,
            highlightbackground=Palette.BORDER,
            highlightthickness=1,
            padx=18,
            pady=14,
        )

    def _animate_coffee(self) -> None:
        """Give the little coffee cup a gentle, low-distraction steam loop."""
        if not self.coffee_canvas.winfo_exists():
            return
        self._coffee_phase = (self._coffee_phase + 1) % 4
        lift = self._coffee_phase
        self.coffee_canvas.coords(self._steam_lines[0], 21, 19, 18, 13 - lift, 22, 7 - lift)
        self.coffee_canvas.coords(self._steam_lines[1], 31, 19, 34, 13 - lift, 30, 7 - lift)
        self.root.after(450, self._animate_coffee)

    def _section_title(self, parent: tk.Widget, number: str, title: str) -> None:
        row = tk.Frame(parent, bg=Palette.SURFACE)
        row.pack(fill="x", pady=(0, 9))
        tk.Label(
            row,
            text=number,
            bg=Palette.BLUE,
            fg=Palette.WHITE,
            font=("Segoe UI Semibold", 10),
            width=3,
            pady=3,
        ).pack(side="left", padx=(0, 9))
        tk.Label(
            row,
            text=title,
            bg=Palette.SURFACE,
            fg=Palette.INK,
            font=("Segoe UI Semibold", 13),
        ).pack(side="left")

    def _build_folder_card(self, parent: tk.Widget) -> None:
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._section_title(card, "1", "Pick a recordings folder")
        row = tk.Frame(card, bg=Palette.SURFACE)
        row.pack(fill="x")
        self.folder_entry = tk.Entry(
            row,
            textvariable=self.folder_var,
            bg=Palette.WHITE,
            fg=Palette.INK,
            insertbackground=Palette.INK,
            relief="solid",
            bd=1,
            font=("Segoe UI", 11),
        )
        self.folder_entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.browse_button = self._button(row, "BROWSE", self._browse_folder, Palette.BLUE)
        self.browse_button.pack(side="left", padx=(10, 0), ipady=4)

    def _build_mode_card(self, parent: tk.Widget) -> None:
        card = self._card(parent)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self._section_title(card, "2", "Compression mode")
        self.mode_row = tk.Frame(card, bg=Palette.SURFACE)
        self.mode_row.pack(fill="x")
        for column in range(3):
            self.mode_row.grid_columnconfigure(column, weight=1, uniform="mode")

        choices = (
            ("fast", "FAST", "Uses your RTX GPU\nBest for large batches"),
            ("quality", "SMALLER", "Uses your CPU\nBest compression"),
            ("editing", "EDITING", "Creates H.264 MP4\nLarger, editor-friendly"),
        )
        self.mode_buttons: list[tk.Radiobutton] = []
        for column, (value, heading, description) in enumerate(choices):
            button = tk.Radiobutton(
                self.mode_row,
                text=f"{heading}\n{description}",
                variable=self.mode_var,
                value=value,
                indicatoron=False,
                command=self._refresh_mode_buttons,
                bg=Palette.SURFACE_ALT,
                fg=Palette.INK,
                selectcolor=Palette.BLUE,
                activebackground=Palette.BLUE,
                activeforeground=Palette.WHITE,
                relief="solid",
                bd=1,
                highlightthickness=2,
                highlightbackground=Palette.SURFACE,
                highlightcolor=Palette.BLUE_DARK,
                font=("Segoe UI Semibold", 11),
                justify="left",
                anchor="w",
                padx=13,
                pady=9,
                cursor="hand2",
            )
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
            if value == "fast" and not self.fast_available:
                button.configure(state="disabled", cursor="arrow")
            self.mode_buttons.append(button)
        if not self.fast_available:
            tk.Label(
                card,
                text="Fast needs an RTX/NVENC-enabled HandBrakeCLI. Smaller is selected instead.",
                bg=Palette.SURFACE,
                fg=Palette.MUTED,
                font=("Segoe UI", 10),
                anchor="w",
            ).pack(fill="x", pady=(7, 0))
        self._refresh_mode_buttons()

    def _refresh_mode_buttons(self) -> None:
        for button in getattr(self, "mode_buttons", []):
            selected = button.cget("value") == self.mode_var.get()
            button.configure(
                bg=Palette.BLUE if selected else Palette.SURFACE_ALT,
                fg=Palette.WHITE if selected else Palette.INK,
            )

    def _set_status(self, text: str, color: str) -> None:
        self.status_var.set(text)
        self.status_badge.configure(bg=color)

    def _build_job_card(self, parent: tk.Widget) -> None:
        card = self._card(parent)
        card.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self._section_title(card, "3", "Files to process")
        self.scope_row = tk.Frame(card, bg=Palette.SURFACE)
        self.scope_row.pack(fill="x")
        self.scope_row.grid_columnconfigure(0, weight=1)
        self.scope_row.grid_columnconfigure(1, weight=1)
        self.scope_row.grid_columnconfigure(2, weight=1)
        self.scope_controls: list[ttk.Radiobutton] = []
        test_scope = ttk.Radiobutton(
            self.scope_row,
            text="Test first 3 files (originals always kept)",
            variable=self.scope_var,
            value="test",
            style="Warm.TRadiobutton",
        )
        test_scope.grid(row=0, column=0, sticky="w", padx=(0, 14))
        all_scope = ttk.Radiobutton(
            self.scope_row,
            text="Process the entire folder",
            variable=self.scope_var,
            value="all",
            style="Warm.TRadiobutton",
        )
        all_scope.grid(row=0, column=1, sticky="w", padx=(0, 14))
        self.scope_controls.extend((test_scope, all_scope))
        self.fps_check = ttk.Checkbutton(
            self.scope_row,
            text="Reduce videos above 30 FPS",
            variable=self.fps_cap_var,
            style="Warm.TCheckbutton",
        )
        self.fps_check.grid(row=0, column=2, sticky="e")
        tk.Label(
            card,
            text="After compression",
            bg=Palette.SURFACE,
            fg=Palette.INK,
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w", pady=(12, 3))
        self.originals_row = tk.Frame(card, bg=Palette.SURFACE)
        self.originals_row.pack(fill="x")
        self.keep_originals_radio = ttk.Radiobutton(
            self.originals_row,
            text="Keep originals (recommended)",
            variable=self.originals_var,
            value="keep",
            style="Warm.TRadiobutton",
        )
        self.keep_originals_radio.pack(anchor="w")
        self.delete_originals_radio = ttk.Radiobutton(
            self.originals_row,
            text="Delete originals after verified compression",
            variable=self.originals_var,
            value="delete",
            style="Warm.TRadiobutton",
        )
        self.delete_originals_radio.pack(anchor="w", pady=(3, 0))
        self.deletion_note_var = tk.StringVar()
        self.deletion_note = tk.Label(
            card,
            textvariable=self.deletion_note_var,
            bg=Palette.SURFACE,
            fg=Palette.RED,
            font=("Segoe UI", 10),
            justify="left",
            anchor="w",
        )
        self.deletion_note.pack(anchor="w", pady=(3, 0))

    def _build_advanced_card(self, parent: tk.Widget) -> None:
        self.advanced_toggle = tk.Button(
            parent,
            text="MORE OPTIONS  +",
            command=self._toggle_advanced,
            bg=Palette.BACKGROUND,
            fg=Palette.BLUE_DARK,
            activebackground=Palette.BACKGROUND,
            activeforeground=Palette.ORANGE_DARK,
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=Palette.BACKGROUND,
            highlightcolor=Palette.BLUE_DARK,
            font=("Segoe UI Semibold", 11),
            cursor="hand2",
        )
        self.advanced_toggle.grid(row=3, column=0, sticky="w", pady=(0, 7))

        self.advanced_card = self._card(parent)
        self.advanced_card.grid_columnconfigure(0, weight=1)
        output_label = tk.Label(
            self.advanced_card,
            text="Output folder (leave blank to use _compressed)",
            bg=Palette.SURFACE,
            fg=Palette.INK,
            font=("Segoe UI Semibold", 10),
        )
        output_label.grid(row=0, column=0, sticky="w", columnspan=2)
        self.output_entry = tk.Entry(
            self.advanced_card,
            textvariable=self.output_var,
            bg=Palette.WHITE,
            fg=Palette.INK,
            insertbackground=Palette.INK,
            relief="solid",
            bd=1,
            font=("Segoe UI", 10),
        )
        self.output_entry.grid(row=1, column=0, sticky="ew", pady=(5, 11), ipady=6)
        self.output_button = self._button(
            self.advanced_card, "CHOOSE", self._browse_output, Palette.BLUE,
        )
        self.output_button.grid(row=1, column=1, padx=(9, 0), pady=(5, 11), ipady=2)

    def _build_action_bar(self, parent: tk.Widget) -> None:
        self.action_bar = tk.Frame(parent, bg=Palette.BACKGROUND)
        self.action_bar.grid(row=5, column=0, sticky="ew", pady=(1, 10))
        self.start_button = self._button(
            self.action_bar, "Compresso", self._start_job, Palette.ORANGE, font_size=12,
        )
        self.stop_button = self._button(
            self.action_bar, "STOP", self._stop_job, Palette.RED, font_size=11,
        )
        self.stop_button.configure(state="disabled", bg=Palette.MUTED)
        self.open_button = self._button(
            self.action_bar, "OPEN RESULTS", self._open_output, Palette.BLUE, font_size=10,
        )
        self.open_button.configure(state="disabled", bg=Palette.MUTED)
        self.open_log_button = self._button(
            self.action_bar, "OPEN TECHNICAL LOG", self._open_technical_log, Palette.BLUE, font_size=10,
        )
        self.open_log_button.configure(state="disabled", bg=Palette.MUTED)
        self._layout_action_bar(self.root.winfo_width())

    def _layout_action_bar(self, width: int) -> None:
        positions = action_bar_grid_positions(width)
        buttons = {
            "start": self.start_button,
            "stop": self.stop_button,
            "log": self.open_log_button,
            "results": self.open_button,
        }
        narrow = width < 700
        for column in range(5):
            self.action_bar.grid_columnconfigure(
                column,
                weight=1 if narrow and column < 2 else (1 if not narrow and column == 2 else 0),
                uniform="action" if narrow and column < 2 else "",
            )
        for name, button in buttons.items():
            row, column = positions[name]
            if narrow:
                button.grid_configure(
                    row=row, column=column, sticky="ew",
                    padx=(0, 5) if column == 0 else (5, 0),
                    pady=(0, 6) if row == 0 else 0,
                    ipady=5,
                )
            else:
                button.grid_configure(
                    row=row, column=column, sticky="w" if name in {"start", "stop"} else "e",
                    padx=(0, 9) if name == "start" else (9, 0) if name == "stop" else (0, 8) if name == "log" else 0,
                    pady=0,
                    ipady=7 if name == "start" else 5 if name == "stop" else 4,
                )

    def _build_progress_card(self, parent: tk.Widget) -> None:
        card = self._card(parent)
        card.grid(row=6, column=0, sticky="nsew")
        parent.grid_rowconfigure(6, weight=1)

        top = tk.Frame(card, bg=Palette.SURFACE)
        top.pack(fill="x")
        tk.Label(
            top,
            text="Progress",
            bg=Palette.SURFACE,
            fg=Palette.MUTED,
            font=("Segoe UI Semibold", 11),
        ).pack(side="left")
        tk.Label(
            top,
            textvariable=self.overall_text_var,
            bg=Palette.SURFACE,
            fg=Palette.MUTED,
            font=("Segoe UI", 10),
        ).pack(side="right")
        self.current_file_label = tk.Label(
            card,
            textvariable=self.current_file_var,
            bg=Palette.SURFACE,
            fg=Palette.INK,
            font=("Segoe UI Semibold", 12),
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.current_file_label.pack(fill="x", pady=(5, 4))
        self.queue_summary_label = tk.Label(
            card,
            textvariable=self.queue_summary_var,
            bg=Palette.SURFACE,
            fg=Palette.BLUE_DARK,
            font=("Segoe UI", 11),
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.queue_summary_label.pack(fill="x", pady=(0, 7))
        self.overall_progress = ttk.Progressbar(
            card,
            mode="determinate",
            maximum=100,
            style="Workshop.Horizontal.TProgressbar",
        )
        self.overall_progress.pack(fill="x")

        file_row = tk.Frame(card, bg=Palette.SURFACE)
        file_row.pack(fill="x", pady=(9, 0))
        self.file_progress = ttk.Progressbar(
            file_row,
            mode="determinate",
            maximum=100,
            style="File.Horizontal.TProgressbar",
        )
        self.file_progress.pack(side="left", fill="x", expand=True)
        tk.Label(
            file_row,
            textvariable=self.file_text_var,
            bg=Palette.SURFACE,
            fg=Palette.MUTED,
            font=("Segoe UI", 10),
            width=14,
            anchor="e",
        ).pack(side="right", padx=(10, 0))

        result = tk.Frame(card, bg=Palette.SURFACE_ALT, padx=12, pady=8)
        result.pack(fill="x", pady=(12, 0))
        result.grid_columnconfigure(0, weight=1)
        self.result_count_label = tk.Label(
            result,
            textvariable=self.result_count_var,
            bg=Palette.SURFACE_ALT,
            fg=Palette.INK,
            font=("Segoe UI Semibold", 11),
            anchor="w",
            justify="left",
            wraplength=700,
        )
        self.result_count_label.grid(row=0, column=0, sticky="ew")
        self.result_space_label = tk.Label(
            result,
            textvariable=self.result_space_var,
            bg=Palette.SURFACE_ALT,
            fg=Palette.BLUE_DARK,
            font=("Segoe UI Semibold", 11),
            anchor="w",
            justify="left",
            wraplength=700,
        )
        self.result_space_label.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    def _build_activity_panel(self, parent: tk.Widget) -> None:
        self.activity_toggle = tk.Button(
            parent,
            text="SHOW ACTIVITY  +",
            command=self._toggle_activity,
            bg=Palette.BACKGROUND,
            fg=Palette.MUTED,
            activebackground=Palette.BACKGROUND,
            activeforeground=Palette.INK,
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=Palette.BACKGROUND,
            highlightcolor=Palette.BLUE_DARK,
            font=("Segoe UI Semibold", 11),
            cursor="hand2",
        )
        self.activity_toggle.grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.activity_frame = tk.Frame(
            parent,
            bg=Palette.SURFACE_ALT,
            highlightbackground=Palette.BORDER,
            highlightthickness=1,
        )
        self.activity_text = tk.Text(
            self.activity_frame,
            height=8,
            bg=Palette.SURFACE,
            fg=Palette.INK,
            insertbackground=Palette.INK,
            relief="flat",
            font=("Segoe UI", 10),
            wrap="word",
            padx=10,
            pady=8,
            state="disabled",
        )
        scrollbar = ttk.Scrollbar(
            self.activity_frame,
            orient="vertical",
            command=self.activity_text.yview,
        )
        self.activity_text.configure(yscrollcommand=scrollbar.set)
        self.activity_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _button(
        self,
        parent: tk.Widget,
        text: str,
        command,
        color: str,
        font_size: int = 10,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg=Palette.WHITE,
            activebackground=Palette.INK,
            activeforeground=Palette.WHITE,
            disabledforeground="#D8D1C7",
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=Palette.SURFACE,
            highlightcolor=Palette.BLUE_DARK,
            padx=14,
            pady=7,
            font=("Segoe UI Semibold", font_size),
            cursor="hand2",
        )

    def _toggle_advanced(self) -> None:
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced_card.grid(row=4, column=0, sticky="ew", pady=(0, 10))
            self.advanced_toggle.configure(text="MORE OPTIONS  -")
        else:
            self.advanced_card.grid_remove()
            self.advanced_toggle.configure(text="MORE OPTIONS  +")
        self._fit_to_screen()

    def _toggle_activity(self) -> None:
        self.activity_visible = not self.activity_visible
        if self.activity_visible:
            self.activity_frame.grid(row=8, column=0, sticky="nsew", pady=(5, 0))
            self.activity_toggle.configure(text="HIDE ACTIVITY  -")
        else:
            self.activity_frame.grid_remove()
            self.activity_toggle.configure(text="SHOW ACTIVITY  +")
        self._fit_to_screen()

    def _browse_folder(self) -> None:
        initial = self.folder_var.get() or str(Path.home() / "Videos")
        selected = filedialog.askdirectory(title="Choose a recordings folder", initialdir=initial)
        if selected:
            self.folder_var.set(selected)

    def _browse_output(self) -> None:
        initial = self.output_var.get() or self.folder_var.get() or str(Path.home())
        selected = filedialog.askdirectory(title="Choose an output folder", initialdir=initial)
        if selected:
            self.output_var.set(selected)

    def _scope_changed(self, *_args) -> None:
        if not hasattr(self, "delete_originals_radio"):
            return
        if self.scope_var.get() == "test":
            self.originals_var.set("keep")
            self.delete_originals_radio.state(["disabled"])
            self.deletion_note_var.set(
                "A 3-file test never deletes originals. Choose Process the entire folder to enable deletion."
            )
        else:
            self.delete_originals_radio.state(["!disabled"])
            self.deletion_note_var.set(
                "Deletion is available only for full-folder jobs and requires confirmation plus all safety checks."
            )
        self._schedule_preflight()

    def _select_keep_originals(self) -> None:
        self.originals_var.set("keep")

    def _schedule_preflight(self, *_args) -> None:
        if self.process is not None:
            return
        if self._preflight_after is not None:
            try:
                self.root.after_cancel(self._preflight_after)
            except tk.TclError:
                pass
        self._preflight_after = self.root.after(350, self._refresh_preflight)

    def _refresh_preflight(self) -> None:
        self._preflight_after = None
        if self.process is not None:
            return
        folder_text = self.folder_var.get().strip().strip('"')
        if not folder_text:
            self._set_status("Ready", Palette.GREEN)
            self.current_file_var.set("Choose a recordings folder to inspect it.")
            self.queue_summary_var.set("Supported files, selected files, size, and output path appear here.")
            return
        folder = Path(folder_text).expanduser()
        if not folder.is_dir():
            self._set_status("Folder not found", Palette.RED)
            self.current_file_var.set("Choose an existing recordings folder.")
            self.queue_summary_var.set("No files were inspected.")
            return
        input_root = folder.resolve()
        output_text = self.output_var.get().strip().strip('"')
        output_path = Path(output_text).expanduser().resolve() if output_text else input_root / "_compressed"
        output_error = output_location_error(input_root, output_path)
        if output_error:
            self._set_status("Choose another output folder", Palette.RED)
            self.current_file_var.set(output_error)
            self.queue_summary_var.set(f"Output: {output_path}")
            return
        try:
            supported = discover_videos(input_root, output_path)
            selected = supported[:3] if self.scope_var.get() == "test" else supported
            source_bytes = sum(path.stat().st_size for path in selected)
        except OSError as error:
            self._set_status("Could not inspect folder", Palette.RED)
            self.current_file_var.set(str(error))
            return
        if not supported:
            self.empty_folder = True
            self._set_status("No videos found", Palette.MUTED)
            self.current_file_var.set("No supported source videos were found outside the output folder.")
            self.queue_summary_var.set(f"Supported: 0 • Selected: 0 • 0 B • Output: {output_path}")
            self.file_text_var.set("Nothing to do")
            return
        self.empty_folder = False
        self._set_status("Ready", Palette.GREEN)
        self.current_file_var.set("Ready to compress")
        self.queue_summary_var.set(
            f"Supported: {len(supported)} • Selected: {len(selected)} • "
            f"{format_size(source_bytes)} • Output: {output_path}"
        )
        self.file_text_var.set("Ready")

    def _fit_to_screen(self) -> None:
        self.root.update_idletasks()
        usable_width = max(360, self.root.winfo_screenwidth() - 80)
        usable_height = max(320, self.root.winfo_screenheight() - 100)
        desired_width = min(self.root.winfo_width(), usable_width)
        desired_height = min(self.root.winfo_reqheight(), usable_height)
        self.root.geometry(f"{desired_width}x{desired_height}")

    def _start_job(self) -> None:
        if self.process is not None:
            return
        folder = Path(self.folder_var.get().strip().strip('"')).expanduser()
        if not folder.is_dir():
            self._set_status("Folder not found", Palette.RED)
            messagebox.showerror(
                "Folder not found",
                "Choose a folder containing your recordings before starting.",
                parent=self.root,
            )
            self._select_keep_originals()
            return
        if not COMPRESSOR.is_file():
            messagebox.showerror(
                "Compressor missing",
                f"The compression engine could not be found:\n{COMPRESSOR}",
                parent=self.root,
            )
            self._select_keep_originals()
            return

        mode_errors = preflight_mode(self.toolchain, MODES[self.mode_var.get()])
        if mode_errors:
            messagebox.showerror("Mode unavailable", "\n".join(mode_errors), parent=self.root)
            self._select_keep_originals()
            return

        output_text = self.output_var.get().strip().strip('"')
        output_path = (
            Path(output_text).expanduser().resolve()
            if output_text else folder.resolve() / "_compressed"
        )
        output_error = output_location_error(folder.resolve(), output_path)
        if output_error:
            self._set_status("Choose another output folder", Palette.RED)
            messagebox.showerror("Output folder", output_error, parent=self.root)
            self._select_keep_originals()
            return
        try:
            videos = discover_videos(folder.resolve(), output_path)
            if self.scope_var.get() == "test":
                videos = videos[:3]
            source_bytes = sum(path.stat().st_size for path in videos)
        except OSError as error:
            self._set_status("Could not inspect folder", Palette.RED)
            messagebox.showerror("Could not inspect folder", str(error), parent=self.root)
            self._select_keep_originals()
            return
        if not videos:
            self.empty_folder = True
            self._set_status("No videos found", Palette.MUTED)
            self.current_file_var.set("No supported source videos were found outside the output folder.")
            self.queue_summary_var.set(f"Output folder: {output_path}")
            self.file_text_var.set("Nothing to do")
            self._select_keep_originals()
            return

        delete_originals = deletion_choice_for_scope(
            self.scope_var.get(), self.originals_var.get(),
        ) == "delete"
        if delete_originals:
            confirmed = messagebox.askyesno(
                "Permanent deletion",
                "Yes will start compression and permanently delete each original only after "
                "all safety checks pass. This is irreversible.\n\n"
                "If you stop later, already completed originals may already have been removed.\n\n"
                "Choose Yes to continue, or No to cancel.",
                parent=self.root,
            )
            if not confirmed:
                messagebox.showinfo(
                    "Deletion cancelled",
                    "The job was not started. Your originals are unchanged.",
                    parent=self.root,
                )
                self._select_keep_originals()
                return

        command = [
            sys.executable,
            "-u",
            str(COMPRESSOR),
            str(folder.resolve()),
            "--mode",
            self.mode_var.get(),
        ]
        if self.scope_var.get() == "test":
            command.extend(["--limit", "3"])
        if not self.fps_cap_var.get():
            command.append("--no-fps-cap")
        if output_text:
            command.extend(["--output-folder", str(output_path)])
        self.output_path = output_path
        self.delete_requested_for_job = delete_originals
        if delete_originals:
            command.append("--delete-originals")

        self._reset_progress()
        self._set_running(True)
        self._set_status("Compressing", Palette.ORANGE)
        self.current_file_var.set("Inspecting files...")
        self.queue_summary_var.set(
            f"{len(videos)} files • {format_size(source_bytes)} source • output: {output_path}"
        )
        self._append_activity(f"Starting: {len(videos)} files • {format_size(source_bytes)} source\n")
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                stdin=subprocess.PIPE if delete_originals else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **child_process_options(os.name, grouped=True),
            )
            if delete_originals and self.process.stdin:
                self.process.stdin.write("DELETE\n")
                self.process.stdin.flush()
                self.process.stdin.close()
        except OSError as error:
            self.process = None
            self._set_running(False)
            messagebox.showerror("Could not start", str(error), parent=self.root)
            self.delete_requested_for_job = False
            self._select_keep_originals()
            return

        self.reader_thread = threading.Thread(target=self._read_process, daemon=True)
        self.reader_thread.start()

    def _read_process(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdout is None:
            self.reader_error = "The subprocess did not provide a readable output stream."
            self._queue_message("reader_error", self.reader_error)
            if process.poll() is None:
                self._terminate_process_tree(process.pid)
            self._queue_message("done", process.wait())
            return
        return_code = 1
        try:
            for raw_line in process.stdout:
                for line in re.split(r"[\r\n]+", raw_line):
                    if line.strip():
                        self._queue_message("line", line.strip())
        except Exception as error:  # The GUI must report reader errors rather than vanish.
            self.reader_error = str(error)
            self._queue_message("reader_error", self.reader_error)
            if process.poll() is None:
                self._terminate_process_tree(process.pid)
        finally:
            try:
                return_code = process.wait()
            except (OSError, subprocess.TimeoutExpired) as error:
                self.reader_error = f"Process could not be reaped: {error}"
                self._queue_message("reader_error", self.reader_error)
            self._queue_message("done", return_code)

    def _queue_message(self, kind: str, payload: object) -> None:
        try:
            self.messages.put_nowait((kind, payload))
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait((kind, payload))

    def _poll_messages(self) -> None:
        try:
            processed = 0
            while processed < 80:
                kind, payload = self.messages.get_nowait()
                processed += 1
                if kind == "line":
                    self._handle_output_line(str(payload))
                elif kind == "done":
                    self._job_finished(int(payload))
                elif kind == "reader_error":
                    self.reader_error = str(payload)
                    self._append_activity("Output reader stopped. A file needs attention.\n")
                    self._set_status("Needs attention", Palette.RED)
                    self.current_file_var.set("The output reader stopped. Waiting for the process to end.")
        except queue.Empty:
            pass
        self.root.after(80, self._poll_messages)

    def _handle_output_line(self, line: str) -> None:
        log_path = technical_log_path_from_output(line)
        if log_path is not None:
            self.technical_log_path = log_path
            if log_path.is_file():
                self.open_log_button.configure(state="normal", bg=Palette.BLUE)

        activity = activity_from_output(line)
        if activity:
            self._append_activity(activity + "\n")

        result = parse_terminal_result(line)
        if result is not None:
            self.terminal_result = result
            return

        file_match = re.match(r"\[(\d+)/(\d+)\]\s+(.+)", line)
        if file_match:
            self.current_index = int(file_match.group(1))
            self.total_files = int(file_match.group(2))
            self.file_finished = False
            self.current_file_var.set(file_match.group(3))
            self.overall_text_var.set(f"File {self.current_index} of {self.total_files}")
            self._update_queue_summary()
            self.file_progress["value"] = 0
            self.file_text_var.set("Preparing")
            self._update_overall_progress(completed=False)
            return

        progress_match = re.search(r"Encoding:.*?(\d+(?:\.\d+)?)\s*%", line)
        if progress_match:
            percent = min(100.0, float(progress_match.group(1)))
            self.file_progress["value"] = percent
            self.file_text_var.set(f"{percent:.0f}%")
            return

        if "Frame rate will be limited" in line:
            self.file_text_var.set("Preparing 30 FPS")
        elif line.startswith("No supported video files"):
            self.empty_folder = True
            self._set_status("No videos found", Palette.MUTED)
            self.current_file_var.set("No supported source videos were found.")
            self.file_text_var.set("Nothing to do")
        elif line.startswith("VALIDATED:"):
            self._mark_file_finished("Validated")
        elif line.startswith("VERIFIED EXISTING:"):
            self._mark_file_finished("Already complete")
        elif line.startswith("SKIPPED:"):
            self._mark_file_finished("Skipped safely")
        elif line.startswith("NO SIZE BENEFIT:"):
            self._mark_file_finished("Original was smaller")
        elif "FAILED" in line or "NEEDS ATTENTION" in line:
            self._mark_file_finished("Needs attention")
        elif line.startswith("Encoded and validated:"):
            value = line.split(":", 1)[1].strip()
            self.receipt["compressed"] = int(value)
            self._show_receipt()
        elif line.startswith("Existing outputs verified:"):
            value = line.split(":", 1)[1].strip()
            self.receipt["complete"] = int(value)
            self._show_receipt()
        elif line.startswith("Efficient sources skipped:"):
            self.receipt["skipped"] += int(line.split(":", 1)[1].strip())
            self._show_receipt()
        elif line.startswith("No-size-benefit outputs discarded:"):
            self.receipt["skipped"] += int(line.split(":", 1)[1].strip())
            self._show_receipt()
        elif line.startswith("Other skipped:"):
            self.receipt["skipped"] += int(line.split(":", 1)[1].strip())
            self._show_receipt()
        elif line.startswith("Failed or needs attention:"):
            self.receipt["attention"] = int(line.split(":", 1)[1].strip())
            self._show_receipt()
        else:
            result_space = result_space_from_summary(line)
            if result_space:
                self.result_space_var.set(result_space)

    def _mark_file_finished(self, label: str) -> None:
        if not self.file_finished:
            self.file_finished = True
            self.completed_files += 1
            if label in {"Skipped safely", "Already complete", "Original was smaller"}:
                self.skipped_files += 1
            elif label == "Needs attention":
                self.attention_files += 1
            self._update_queue_summary()
        self.file_progress["value"] = 100
        self.file_text_var.set(label)
        self._update_overall_progress(completed=True)

    def _update_queue_summary(self) -> None:
        if self.total_files <= 0:
            return
        remaining = max(0, self.total_files - self.completed_files)
        summary = (
            f"{self.total_files} files queued  •  {self.completed_files} completed  •  "
            f"{remaining} remaining"
        )
        if self.skipped_files:
            summary += f"  •  {self.skipped_files} skipped safely"
        if self.attention_files:
            summary += f"  •  {self.attention_files} need attention"
        self.queue_summary_var.set(summary)

    def _update_overall_progress(self, completed: bool) -> None:
        if self.total_files <= 0:
            return
        completed_count = self.current_index if completed else max(0, self.current_index - 1)
        self.overall_progress["value"] = completed_count / self.total_files * 100

    def _job_finished(self, return_code: int) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            self.root.after(80, lambda: self._job_finished(return_code))
            return
        was_stopping = self.stopping
        self.process = None
        self.reader_thread = None
        self._set_running(False)
        if self.output_path and self.output_path.is_dir():
            self.open_button.configure(state="normal", bg=Palette.BLUE)
        if self.reader_error:
            self._set_status("Needs attention", Palette.RED)
            self.current_file_var.set("Compression stopped because the output reader failed.")
            self.file_text_var.set("Review activity")
            self._update_queue_summary()
            if not self.activity_visible:
                self._toggle_activity()
        elif was_stopping:
            self._set_status("Stopped", Palette.MUTED)
            self.current_file_var.set("Job stopped. Completed outputs remain available.")
            self.file_text_var.set("Stopped")
            self.queue_summary_var.set(
                f"Stopped after {self.completed_files} of {self.total_files} files"
            )
        elif self.empty_folder:
            self._set_status("No videos found", Palette.MUTED)
        elif return_code == 0:
            self.overall_progress["value"] = 100
            self._set_status("Complete", Palette.GREEN)
            self.current_file_var.set("Compression complete")
            self.file_text_var.set("Finished")
            self.queue_summary_var.set(
                f"Finished {self.completed_files} of {self.total_files} files"
            )
        else:
            self._set_status("Needs attention", Palette.RED)
            self.current_file_var.set("The job finished with an issue. Your originals are safe unless noted.")
            self.file_text_var.set("Review activity")
            self._update_queue_summary()
            if not self.activity_visible:
                self._toggle_activity()
        if self.delete_requested_for_job:
            title, message, needs_attention = removal_result_message(self.terminal_result)
            if needs_attention:
                self._set_status("Needs attention", Palette.RED)
                self.current_file_var.set("Original removal needs attention. Review Activity.")
                self.file_text_var.set("Review activity")
                if not self.activity_visible:
                    self._toggle_activity()
            messagebox.showinfo(title, message, parent=self.root)
        self.delete_requested_for_job = False
        self._select_keep_originals()
        self.stopping = False

    def _stop_job(self) -> None:
        process = self.process
        if process is None:
            return
        if not messagebox.askyesno(
            "Stop compression?",
            "The current encode will be asked to stop. Completed outputs remain available; "
            "an incomplete temporary file is kept for inspection. Originals already deleted "
            "for completed files stay deleted.",
            parent=self.root,
        ):
            return
        self.stopping = True
        self._set_status("Stopping...", Palette.RED)
        self.stop_button.configure(state="disabled")
        threading.Thread(target=self._terminate_process_tree, args=(process.pid,), daemon=True).start()

    def _terminate_process_tree(self, process_id: int) -> None:
        try:
            if os.name == "nt":
                process = self.process
                if process is not None:
                    try:
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                        process.wait(timeout=5)
                        return
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    **child_process_options(os.name),
                )
            elif self.process:
                os.killpg(process_id, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process_id, signal.SIGKILL)
        except OSError as error:
            self._queue_message("reader_error", f"Could not stop process: {error}")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(
            state=state,
            bg=Palette.MUTED if running else Palette.ORANGE,
        )
        self.browse_button.configure(state=state)
        self.folder_entry.configure(state=state)
        self.advanced_toggle.configure(state=state)
        self.output_entry.configure(state=state)
        self.output_button.configure(state=state)
        self.fps_check.state(["disabled"] if running else ["!disabled"])
        self.keep_originals_radio.state(["disabled"] if running else ["!disabled"])
        self.delete_originals_radio.state(["disabled"] if running else ["!disabled"])
        for control in self.scope_controls:
            control.state(["disabled"] if running else ["!disabled"])
        for button in self.mode_buttons:
            if button.cget("value") == "fast" and not self.fast_available:
                button.configure(state="disabled")
            else:
                button.configure(state=state)
        if not running:
            self._scope_changed()
        self.stop_button.configure(
            state="normal" if running else "disabled",
            bg=Palette.RED if running else Palette.MUTED,
        )

    def _reset_progress(self) -> None:
        self.stopping = False
        self.current_index = 0
        self.total_files = 0
        self.completed_files = 0
        self.skipped_files = 0
        self.attention_files = 0
        self.empty_folder = False
        self.receipt = {"compressed": 0, "complete": 0, "skipped": 0, "attention": 0}
        self.reader_error = None
        self.terminal_result = None
        self.file_finished = False
        self.overall_progress["value"] = 0
        self.file_progress["value"] = 0
        self.overall_text_var.set("Preparing queue")
        self.queue_summary_var.set("Counting videos in the folder...")
        self.file_text_var.set("Inspecting")
        self.result_count_var.set("0 compressed")
        self.result_space_var.set("Calculating compression result")
        self.open_button.configure(state="disabled", bg=Palette.MUTED)
        self.technical_log_path = None
        self.open_log_button.configure(state="disabled", bg=Palette.MUTED)
        self.activity_text.configure(state="normal")
        self.activity_text.delete("1.0", "end")
        self.activity_text.configure(state="disabled")
        self.activity_line_count = 0

    def _append_activity(self, text: str) -> None:
        self.activity_text.configure(state="normal")
        self.activity_text.insert("end", text)
        self.activity_line_count += text.count("\n")
        if self.activity_line_count > 240:
            self.activity_text.delete("1.0", "41.0")
            self.activity_line_count -= 40
        self.activity_text.see("end")
        self.activity_text.configure(state="disabled")

    def _show_receipt(self) -> None:
        self.result_count_var.set(
            "compressed {compressed} • complete {complete} • skipped {skipped} • attention {attention}".format(
                **self.receipt
            )
        )

    def _adapt_to_window(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        wraplength = max(300, event.width - 120)
        self.current_file_label.configure(wraplength=wraplength)
        self.queue_summary_label.configure(wraplength=wraplength)
        self.result_count_label.configure(wraplength=wraplength)
        self.result_space_label.configure(wraplength=wraplength)
        self._layout_action_bar(event.width)
        if event.width < 700:
            self.mode_row.grid_columnconfigure(0, weight=1, uniform="mode")
            self.mode_row.grid_columnconfigure(1, weight=0, uniform="")
            self.mode_row.grid_columnconfigure(2, weight=0, uniform="")
            for row, button in enumerate(self.mode_buttons):
                button.grid_configure(row=row, column=0, sticky="ew", padx=0, pady=(0, 6))
        else:
            for column in range(3):
                self.mode_row.grid_columnconfigure(column, weight=1, uniform="mode")
            for column, button in enumerate(self.mode_buttons):
                button.grid_configure(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0), pady=0)
        if event.width < 700:
            self.scope_controls[0].grid_configure(row=0, column=0, sticky="w", padx=0, pady=(0, 4))
            self.scope_controls[1].grid_configure(row=1, column=0, sticky="w", padx=0, pady=(0, 4))
            self.fps_check.grid_configure(row=2, column=0, sticky="w", padx=0)
        else:
            self.scope_controls[0].grid_configure(row=0, column=0, sticky="w", padx=(0, 14), pady=0)
            self.scope_controls[1].grid_configure(row=0, column=1, sticky="w", padx=(0, 14), pady=0)
            self.fps_check.grid_configure(row=0, column=2, sticky="e", padx=0, pady=0)
        if self.activity_visible:
            self.activity_text.configure(height=5 if event.height < 720 else 8)

    def _open_output(self) -> None:
        if not self.output_path or not self.output_path.is_dir():
            messagebox.showinfo("No results yet", "The output folder does not exist yet.", parent=self.root)
            return
        self._open_path(self.output_path, "Could not open folder")

    def _open_technical_log(self) -> None:
        if not self.technical_log_path or not self.technical_log_path.is_file():
            messagebox.showinfo("No technical log yet", "The technical log is not available yet.", parent=self.root)
            return
        self._open_path(self.technical_log_path, "Could not open technical log")

    def _open_path(self, path: Path, error_title: str) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as error:
            messagebox.showerror(error_title, str(error), parent=self.root)

    def _close_window(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno(
                "Close Espresso Compresso?",
                "Compression is running. Stop it and close the app?",
                parent=self.root,
            ):
                return
            self.stopping = True
            process_id = self.process.pid
            self._terminate_process_tree(process_id)
        self.root.destroy()


def enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def main() -> None:
    enable_windows_dpi_awareness()
    root = tk.Tk()
    app = CompressorApp(root)
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1].strip().strip('"')).expanduser()
        if candidate.is_dir():
            app.folder_var.set(str(candidate.resolve()))
            app.current_file_var.set("Folder ready - choose a mode and start")
    root.mainloop()


if __name__ == "__main__":
    main()
