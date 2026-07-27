from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import socket
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from typing import Any, Callable

UI_API = 1
INTEGRATION_REVISION = 4

_MODULE_ROOT = Path(__file__).resolve().parent
if str(_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MODULE_ROOT))

from chatgpt_exporter.app import ExporterApplication  # noqa: E402
from chatgpt_exporter.floating_panel import (  # noqa: E402
    AMBER,
    BLUE,
    GREEN,
    PURPLE,
    RED,
    PanelButton,
    TooltipManager,
)
from chatgpt_exporter.i18n import LANGUAGES, TRANSLATIONS  # noqa: E402


class _ErrorOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


class DynamicConversationExporterPanel(tk.Frame):
    """Full v1.5 SEMANTIC_I18N exporter embedded into AutomationPlatform.

    The original exporter backend (ExporterApplication), semantic i18n tables,
    recovery scrolling, DOM bridge, live pair persistence and 3D PanelButton
    implementation are reused directly. This class only replaces the standalone
    Tk root/window shell with an AutomationPlatform-owned Frame.
    """

    def __init__(self, master: tk.Misc, services: Any) -> None:
        self.services = services
        self.platform_root = Path(services.root)
        self.module_root = Path(__file__).resolve().parent
        self.theme = getattr(services, "theme", {}) or {}
        super().__init__(master, bg=self.c("bg", "#08111b"))

        self.position_path = self.module_root / "panel_position.json"
        self.config = self._load_config()
        self.language = str(self.config.get("default_language", "ru"))
        if self.language not in LANGUAGES:
            self.language = "ru"
        self.tooltips_enabled = bool(self.config.get("tooltips_enabled", True))
        self._load_preferences()

        self.formats = {
            name: bool(self.config.get("default_formats", {}).get(name, True))
            for name in ("html", "md", "txt", "json")
        }
        self.state: dict[str, Any] = {
            "status": "connecting",
            "phase": self.tr("connecting"),
            "busy": False,
            "progress": 0,
            "messages_total": 0,
            "pairs_total": 0,
            "pairs_complete": 0,
            "pairs_missing": 0,
            "pairs_dom": 0,
            "exported_pairs": 0,
            "overlay_enabled": bool(self.config.get("overlay_enabled", True)),
            "last_error": "",
            "last_output_directory": "",
        }
        self.pages: list[dict[str, Any]] = []
        self.app: ExporterApplication | None = None
        self.worker_thread: threading.Thread | None = None
        self.worker_error: BaseException | None = None
        self._poll_job: str | None = None
        self._pulse_job: str | None = None
        self._pulse_phase = False
        self._shutting_down = False
        self._handlers_configured = False

        top = self.winfo_toplevel()
        self.tooltip = TooltipManager(top, lambda key: self.tr(f"tip_{key}"), enabled=self.tooltips_enabled)

        self._build_ui()
        self._refresh_all()
        self._schedule_poll()
        self._schedule_pulse()
        self.ensure_backend(auto=True)

    # ------------------------------------------------------------------
    # Common/theme/i18n
    # ------------------------------------------------------------------
    def c(self, key: str, fallback: str) -> str:
        return str(self.theme.get(key, fallback))

    def tr(self, key: str) -> str:
        table = TRANSLATIONS.get(self.language, TRANSLATIONS["ru"])
        return table.get(key, TRANSLATIONS["ru"].get(key, key))

    def trf(self, key: str, **values: Any) -> str:
        text = self.tr(key)
        try:
            return text.format(**values)
        except (KeyError, ValueError):
            return text

    def _localized_event_message(self, item: dict[str, Any], fallback: str = "") -> str:
        key = str(item.get("message_key") or item.get("error_key") or "")
        args = item.get("message_args") or {}
        if key:
            values = args if isinstance(args, dict) else {}
            text = self.trf(key, **values)
            details = str(item.get("details") or "").strip()
            return f"{text} · {details}" if details else text
        return str(item.get("message") or item.get("error") or fallback)

    # ------------------------------------------------------------------
    # Config/preferences/logging/backend
    # ------------------------------------------------------------------
    def _load_config(self) -> dict[str, Any]:
        path = self.module_root / "config.json"
        data: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                data = raw
        except Exception:
            data = {}
        data["cdp_url"] = str(getattr(self.services, "cdp_url", "http://127.0.0.1:9222"))
        data.setdefault("allowed_domains", ["chatgpt.com", "chat.openai.com"])
        data.setdefault("output_directory", "exports")
        data.setdefault("default_language", "ru")
        data.setdefault("tooltips_enabled", True)
        return data

    def _load_preferences(self) -> None:
        try:
            if not self.position_path.exists():
                return
            data = json.loads(self.position_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return
            lang = str(data.get("language", self.language))
            if lang in LANGUAGES:
                self.language = lang
            self.tooltips_enabled = bool(data.get("tooltips_enabled", self.tooltips_enabled))
        except Exception:
            pass

    def _save_preferences(self) -> None:
        try:
            data: dict[str, Any] = {}
            if self.position_path.exists():
                try:
                    old = json.loads(self.position_path.read_text(encoding="utf-8-sig"))
                    if isinstance(old, dict):
                        data.update(old)
                except Exception:
                    pass
            data["language"] = self.language
            data["tooltips_enabled"] = self.tooltips_enabled
            self.position_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _configure_logging(self) -> None:
        if self._handlers_configured:
            return
        logs = self.module_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("chatgpt_exporter")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        marker = str(self.module_root.resolve())
        for handler in logger.handlers:
            if getattr(handler, "_automationplatform_module", None) == marker:
                self._handlers_configured = True
                return

        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        run_log = logs / f"run_{stamp}.log"

        for path, level, error_only in (
            (run_log, logging.DEBUG, False),
            (logs / "exporter.log", logging.INFO, False),
            (logs / "errors.log", logging.ERROR, True),
        ):
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setLevel(level)
            handler.setFormatter(formatter)
            if error_only:
                handler.addFilter(_ErrorOnlyFilter())
            handler._automationplatform_module = marker  # type: ignore[attr-defined]
            logger.addHandler(handler)
        (logs / "LAST_RUN.txt").write_text(str(run_log), encoding="utf-8")
        self._handlers_configured = True

    def _cdp_online(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 9222), timeout=0.18):
                return True
        except OSError:
            return False

    def ensure_backend(self, *, auto: bool = False) -> None:
        if self._shutting_down:
            return
        if self.worker_thread is not None and self.worker_thread.is_alive() and self.app is not None:
            if not auto:
                self.app.submit_command("refresh_pages")
                self.app.submit_command("get_state")
            return
        if auto and not self._cdp_online():
            self.state["status"] = "idle"
            self.state["phase"] = self.tr("error_connect_chrome")
            self._refresh_all()
            return

        self._configure_logging()
        self.worker_error = None
        self.app = ExporterApplication(base_dir=self.module_root, config=dict(self.config))
        app = self.app

        def worker() -> None:
            if sys.platform == "win32":
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass
            try:
                asyncio.run(app.run())
            except BaseException as exc:  # noqa: BLE001
                self.worker_error = exc
                logging.getLogger("chatgpt_exporter.embedded").exception("Embedded browser worker stopped")
                try:
                    app.ui_queue.put({"type": "error", "error_key": "error_connect_chrome", "details": str(exc)})
                except Exception:
                    pass

        self.state["status"] = "connecting"
        self.state["phase"] = self.trf("service_connecting", url=self.config.get("cdp_url", "http://127.0.0.1:9222"))
        self._refresh_all()
        self.worker_thread = threading.Thread(target=worker, name="DCE-Embedded-BrowserWorker", daemon=True)
        self.worker_thread.start()

    def _send(self, action: str, **payload: Any) -> None:
        if self._shutting_down:
            return
        if self.app is None or self.worker_thread is None or not self.worker_thread.is_alive():
            self.ensure_backend(auto=False)
        if self.app is None:
            self._show_message(self.tr("error_connect_chrome"), error=True)
            return
        self.app.submit_command(action, **payload)

    def _start_shared_chrome(self) -> None:
        launcher = self.platform_root / "START_CHROME_DEBUG.cmd"
        if not launcher.exists():
            messagebox.showerror("Dynamic Conversation Exporter", f"Не найден общий Chrome launcher:\n{launcher}", parent=self.winfo_toplevel())
            return
        self._show_message("Chrome Debug → https://chatgpt.com/", error=False)
        self.services.run_process(
            [str(launcher), "https://chatgpt.com/"],
            on_done=lambda _rc, _out: self.after(900, lambda: self.ensure_backend(auto=False)),
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        bg = self.c("bg", "#08111b")
        panel = self.c("panel", "#0d1a28")
        panel2 = self.c("panel2", "#102235")
        text = self.c("text", "#edf8ff")
        muted = self.c("muted", "#8eabbc")
        line = self.c("line", "#24445d")
        accent = self.c("accent", "#38dfb5")

        wrap = tk.Frame(self, bg=bg)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=bg, bd=0, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.inner_window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.inner_window, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

        head = tk.Frame(self.inner, bg=bg)
        head.pack(fill="x", pady=(2, 10))
        self.title_label = tk.Label(head, text=self.tr("app_title"), bg=bg, fg=text, font=("Segoe UI Semibold", 20))
        self.title_label.pack(side="left")
        self.service_dot = tk.Label(head, text="●", bg=bg, fg="#777f8b", font=("Segoe UI", 13, "bold"))
        self.service_dot.pack(side="left", padx=(10, 5))
        self.count_label = tk.Label(head, text="✓ 0/0", bg=bg, fg="#9fd4ff", font=("Consolas", 10, "bold"))
        self.count_label.pack(side="left")
        tk.Button(
            head,
            text="CHROME + CHATGPT",
            command=self._start_shared_chrome,
            bg=self.c("accent2", "#44aeea"), fg="#06111a", bd=0, padx=12, pady=6,
            font=("Segoe UI Semibold", 8), cursor="hand2",
        ).pack(side="right")
        self.reconnect_button = tk.Button(
            head,
            text="CONNECT",
            command=lambda: self.ensure_backend(auto=False),
            bg=panel2, fg=text, activebackground=accent, activeforeground="#06111a",
            bd=0, padx=12, pady=6, font=("Segoe UI Semibold", 8), cursor="hand2",
        )
        self.reconnect_button.pack(side="right", padx=(0, 7))

        status_card = tk.Frame(self.inner, bg=panel, highlightthickness=1, highlightbackground=line, padx=12, pady=10)
        status_card.pack(fill="x", pady=(0, 8))
        self.status_label = tk.Label(status_card, text=self.tr("connecting"), bg=panel, fg="#a8d9ff", anchor="w", font=("Consolas", 9, "bold"))
        self.status_label.pack(fill="x")
        self.phase_label = tk.Label(status_card, text="", bg=panel, fg=muted, anchor="w", justify="left", wraplength=900, font=("Segoe UI", 8))
        self.phase_label.pack(fill="x", pady=(3, 0))

        lang_frame = tk.Frame(self.inner, bg=bg)
        lang_frame.pack(fill="x", pady=(0, 7))
        self.lang_title = tk.Label(lang_frame, text=self.tr("language"), bg=bg, fg=muted, font=("Segoe UI", 8, "bold"))
        self.lang_title.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.lang_buttons: dict[str, tk.Button] = {}
        for idx, code in enumerate(("ru", "en", "de", "pl", "it", "uk"), 1):
            b = tk.Button(
                lang_frame,
                text={"ru": "RU", "en": "EN", "de": "DE", "pl": "PL", "it": "IT", "uk": "UA"}[code],
                command=lambda c=code: self.set_language(c),
                relief="flat", bd=0, padx=7, pady=4, font=("Segoe UI", 8, "bold"), cursor="hand2",
            )
            b.grid(row=0, column=idx, sticky="ew", padx=1)
            lang_frame.grid_columnconfigure(idx, weight=1)
            self.lang_buttons[code] = b
            self.tooltip.bind(b, "language")

        page_frame = tk.Frame(self.inner, bg=bg)
        page_frame.pack(fill="x", pady=(0, 7))
        self.page_button = tk.Menubutton(
            page_frame, text=f"{self.tr('page')}: —", bg=panel2, fg=text,
            activebackground="#24445d", activeforeground="#fff", relief="flat", bd=0,
            anchor="w", padx=9, pady=6, font=("Segoe UI", 8), cursor="hand2",
        )
        self.page_menu = tk.Menu(self.page_button, tearoff=False, bg=panel2, fg=text, activebackground="#36536a", activeforeground="#fff")
        self.page_button.configure(menu=self.page_menu)
        self.page_button.pack(side="left", fill="x", expand=True)
        self.tooltip.bind(self.page_button, "page")
        self._small_button(page_frame, "↗", lambda: self._send("activate_page"), "activate_page").pack(side="right", padx=(5, 0))
        self._small_button(page_frame, "⟳", lambda: self._send("refresh_pages"), "refresh_tabs").pack(side="right", padx=(5, 0))

        self.progress = tk.Canvas(self.inner, height=9, bg="#0d1116", highlightbackground="#333d49", highlightthickness=1, bd=0)
        self.progress.pack(fill="x", pady=(0, 8))
        self.progress.bind("<Configure>", lambda _e: self._draw_progress(), add="+")

        self.stats_frame = tk.Frame(self.inner, bg=panel2, highlightbackground=line, highlightthickness=1)
        self.stats_frame.pack(fill="x", pady=(0, 8))
        self.stats_labels: dict[str, tk.Label] = {}
        self.stats_titles: dict[str, tk.Label] = {}
        for idx, name in enumerate(("messages", "pairs", "saved", "yellow", "red")):
            cell = tk.Frame(self.stats_frame, bg=panel2)
            cell.grid(row=0, column=idx, sticky="ew", padx=2, pady=7)
            title = tk.Label(cell, text="", bg=panel2, fg=muted, font=("Segoe UI", 7))
            title.pack()
            value = tk.Label(cell, text="0", bg=panel2, fg=text, font=("Consolas", 11, "bold"))
            value.pack()
            self.stats_titles[name] = title
            self.stats_labels[name] = value
            self.stats_frame.grid_columnconfigure(idx, weight=1)

        controls = tk.Frame(self.inner, bg=bg)
        controls.pack(fill="x")
        self.buttons: dict[str, PanelButton] = {}
        specs: list[tuple[str, Callable[[], None], str]] = [
            ("live", self.live_export_or_stop, "live_export"),
            ("repeat", self.repeat_scroll_or_stop, "repeat"),
            ("stop", lambda: self._send("stop"), "stop"),
            ("overlay", lambda: self._send("toggle_overlay"), "overlay"),
            ("clear", lambda: self._send("clear_marks"), "clear"),
            ("open", lambda: self._send("open_output"), "open"),
            ("refresh", lambda: self._send("reload_page"), "refresh"),
            ("logs", lambda: self._send("open_logs"), "logs"),
            ("tips", self.toggle_tooltips, "tooltips"),
        ]
        for idx, (name, command, tip) in enumerate(specs):
            button = self._make_button(controls, command, tip)
            row = idx // 2
            col = idx % 2
            if name == "tips":
                button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
            else:
                button.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 4, 4 if col == 0 else 0), pady=3)
            self.buttons[name] = button
        controls.grid_columnconfigure(0, weight=1)
        controls.grid_columnconfigure(1, weight=1)

        self._separator(self.inner)
        self.format_header = tk.Label(self.inner, text=self.tr("formats"), bg=bg, fg=muted, font=("Segoe UI", 8, "bold"), anchor="w")
        self.format_header.pack(fill="x")
        self.tooltip.bind(self.format_header, "formats")
        self.format_frame = tk.Frame(self.inner, bg=bg)
        self.format_frame.pack(fill="x", pady=(4, 2))
        self.format_buttons: dict[str, PanelButton] = {}
        for idx, fmt in enumerate(("html", "md", "txt", "json")):
            b = self._make_button(self.format_frame, lambda f=fmt: self.toggle_format(f), "formats", height=36)
            b.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=(0 if idx % 2 == 0 else 4, 4 if idx % 2 == 0 else 0), pady=2)
            self.format_buttons[fmt] = b
        all_button = self._make_button(self.format_frame, self.toggle_all_formats, "formats", height=36)
        all_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.format_buttons["all"] = all_button
        self.format_frame.grid_columnconfigure(0, weight=1)
        self.format_frame.grid_columnconfigure(1, weight=1)

        self._separator(self.inner)
        self.details = tk.Text(
            self.inner, height=4, width=1, bg=panel2, fg="#dce6f2", insertbackground="#dce6f2",
            relief="flat", bd=0, padx=8, pady=7, wrap="word", font=("Consolas", 8), state="disabled",
        )
        self.details.pack(fill="x")
        self.message_label = tk.Label(self.inner, text="", bg=bg, fg="#8fd8ad", justify="left", anchor="w", wraplength=900, font=("Segoe UI", 8))
        self.message_label.pack(fill="x", pady=(6, 2))

    def _on_mousewheel(self, event: tk.Event) -> None:
        try:
            if self.winfo_ismapped():
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass

    def _make_button(self, parent: tk.Widget, command: Callable[[], None], tip: str, height: int = 40) -> PanelButton:
        return PanelButton(parent, command, self.tooltip, tip, height=height, base_font_size=9, bold=True)

    def _small_button(self, parent: tk.Widget, text: str, command: Callable[[], None], tip: str) -> tk.Button:
        button = tk.Button(
            parent, text=text, command=command, bg="#252a32", fg="#d8e0ea",
            activebackground="#3a414d", activeforeground="#fff", relief="flat", bd=0,
            padx=9, pady=4, font=("Segoe UI", 9, "bold"), cursor="hand2",
        )
        self.tooltip.bind(button, tip)
        return button

    def _separator(self, parent: tk.Widget) -> None:
        line = tk.Frame(parent, bg="#0a0d11", height=3)
        line.pack(fill="x", pady=6)
        tk.Frame(line, bg="#3e4a58", height=1).pack(fill="x", side="top")
        tk.Frame(line, bg="#11161c", height=1).pack(fill="x", side="bottom")

    # ------------------------------------------------------------------
    # Original v1.5 user actions
    # ------------------------------------------------------------------
    def selected_formats(self) -> list[str]:
        return [name for name, selected in self.formats.items() if selected]

    def toggle_format(self, fmt: str) -> None:
        self.formats[fmt] = not self.formats.get(fmt, False)
        self._refresh_formats()

    def toggle_all_formats(self) -> None:
        target = not all(self.formats.values())
        for name in self.formats:
            self.formats[name] = target
        self._refresh_formats()

    def set_language(self, language: str) -> None:
        if language not in LANGUAGES:
            return
        self.language = language
        self._save_preferences()
        self._send("set_language", language=language)
        self._refresh_all()

    def _require_formats(self) -> list[str] | None:
        formats = self.selected_formats()
        if not formats:
            self._show_message(self.tr("choose_format"), error=True)
            return None
        return formats

    def live_export_or_stop(self) -> None:
        if bool(self.state.get("busy")):
            self._send("stop")
            return
        formats = self._require_formats()
        if formats:
            self._send("live_export", formats=formats)

    def repeat_scroll_or_stop(self) -> None:
        if bool(self.state.get("busy")):
            self._send("stop")
            return
        formats = self._require_formats()
        if formats:
            self._send("repeat_scroll", formats=formats)

    def toggle_tooltips(self) -> None:
        self.tooltips_enabled = not self.tooltips_enabled
        self.tooltip.set_enabled(self.tooltips_enabled)
        self._save_preferences()
        self._refresh_all()
        self._show_message(self.tr("tips_enabled_notice") if self.tooltips_enabled else self.tr("tips_disabled_notice"), error=False)

    # ------------------------------------------------------------------
    # Queue/state rendering (ported from original FloatingControlPanel)
    # ------------------------------------------------------------------
    def _schedule_poll(self) -> None:
        if self._shutting_down:
            return
        self._poll_job = self.after(60, self._poll_queue)

    def _poll_queue(self) -> None:
        if self._shutting_down:
            return
        app = self.app
        if app is not None:
            for _ in range(160):
                try:
                    item = app.ui_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_message(item)
        self._schedule_poll()

    def _handle_message(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "telemetry":
            data = item.get("data") or {}
            if isinstance(data, dict):
                self.state.update(data)
                self._refresh_all()
            return
        if kind == "pages":
            self.pages = list(item.get("pages") or [])
            self._refresh_pages()
            return
        if kind == "service":
            status = str(item.get("status") or "")
            self.state["status"] = status
            self._show_message(self._localized_event_message(item), error=status == "error")
            self._refresh_all()
            return
        if kind == "live_session":
            self.state["last_output_directory"] = str(item.get("output_directory") or "")
            self.state["exported_pairs"] = int(item.get("complete_pairs") or 0)
            self._refresh_all()
            return
        if kind == "pair_saved":
            self.state["exported_pairs"] = int(item.get("complete_pairs") or self.state.get("exported_pairs") or 0)
            self.state["last_output_directory"] = str(item.get("output_directory") or self.state.get("last_output_directory") or "")
            self._refresh_all()
            return
        if kind == "export_saved":
            self.state["last_output_directory"] = str(item.get("output_directory") or self.state.get("last_output_directory") or "")
            self.state["exported_pairs"] = int(item.get("complete_pairs") or self.state.get("exported_pairs") or 0)
            self._show_message(self.trf("saved_notice", n=item.get("complete_pairs", 0), path=item.get("output_directory", "")), error=False)
            self._refresh_all()
            return
        if kind == "error":
            self.state["status"] = "error"
            self.state["last_error"] = self._localized_event_message(item, self.tr("error"))
            self._show_message(self.state["last_error"], error=True)
            self._refresh_all()
            return
        if kind == "command_result":
            if not item.get("ok", True):
                error = self._localized_event_message(item, self.tr("error_command"))
                self.state["last_error"] = error
                self._show_message(error, error=True)
            else:
                action = str(item.get("action") or "")
                feedback = {
                    "refresh_pages": "tabs_refreshed",
                    "select_page": "page_activated",
                    "activate_page": "page_activated",
                    "reload_page": "page_reloaded",
                    "open_logs": "logs_opened",
                }.get(action)
                if feedback:
                    self._show_message(self.tr(feedback), error=False)
                if action not in {"refresh_pages", "open_logs", "open_output"}:
                    self._send("get_state")

    def _refresh_pages(self) -> None:
        self.page_menu.delete(0, "end")
        if not self.pages:
            self.page_button.configure(text=f"{self.tr('page')}: {self.tr('no_page')}")
            self.page_menu.add_command(label=self.tr("no_tabs"), state="disabled")
            return
        active = next((page for page in self.pages if page.get("active")), self.pages[-1])
        title = str(active.get("title") or active.get("url") or "")
        self.page_button.configure(text=f"{self.tr('page')}: {title[:90]}")
        for page in self.pages:
            label = str(page.get("title") or page.get("url") or "")[:110]
            prefix = "✓ " if page.get("active") else "   "
            page_id = str(page.get("page_id", ""))
            self.page_menu.add_command(label=prefix + label, command=lambda pid=page_id: self._select_page(pid))

    def _select_page(self, page_id: str) -> None:
        if not page_id:
            return
        self._show_message(self.tr("activating_page"), error=False)
        self._send("select_page", page_id=page_id)

    def _refresh_all(self) -> None:
        status = str(self.state.get("status") or "idle")
        phase = str(self.state.get("phase") or "")
        busy = bool(self.state.get("busy"))
        total = int(self.state.get("pairs_total") or 0)
        missing = int(self.state.get("pairs_missing") or 0)
        yellow = int(self.state.get("pairs_dom") or 0)
        messages = int(self.state.get("messages_total") or 0)
        saved = int(self.state.get("exported_pairs") or 0)
        progress = int(self.state.get("progress") or 0)
        overlay = bool(self.state.get("overlay_enabled", True))

        status_key = f"status_{status}"
        status_text = self.tr(status_key) if status_key in TRANSLATIONS.get(self.language, {}) else status.upper()
        self.title_label.configure(text=self.tr("app_title"))
        self.count_label.configure(text=f"✓ {saved}/{total}")
        self.status_label.configure(text=f"{status_text} · {progress}%", fg="#ff8f8f" if status == "error" else "#a8d9ff")
        self.phase_label.configure(text=phase)
        self.service_dot.configure(fg="#ff5252" if status == "error" else "#ffbf55" if status == "connecting" else "#39d98a" if status in {"ready", "exported", "idle", "stopped"} else "#777f8b")
        self.reconnect_button.configure(text="CONNECTED" if self.worker_thread is not None and self.worker_thread.is_alive() else "CONNECT")

        self.lang_title.configure(text=self.tr("language"))
        for code, button in self.lang_buttons.items():
            button.configure(bg="#315f78" if code == self.language else "#252a32", fg="#fff" if code == self.language else "#d8e0ea", activebackground="#416f8a")

        labels = {
            "messages": self.tr("messages"),
            "pairs": self.tr("pairs"),
            "saved": self.tr("saved"),
            "yellow": self.tr("yellow"),
            "red": self.tr("red"),
        }
        for name, text in labels.items():
            self.stats_titles[name].configure(text=text)
        self.stats_labels["messages"].configure(text=str(messages))
        self.stats_labels["pairs"].configure(text=str(total))
        self.stats_labels["saved"].configure(text=str(saved), fg="#74f0ad")
        self.stats_labels["yellow"].configure(text=str(yellow), fg="#ffd16a")
        self.stats_labels["red"].configure(text=str(missing), fg="#ff7d88" if missing else "#e9f4ff")

        exporting = busy and status == "exporting"
        self.buttons["live"].set_texts(self.tr("live_export"), self.tr("live_export_stop"))
        self.buttons["live"].set_running(exporting)
        self.buttons["repeat"].set_texts(self.tr("repeat"), self.tr("repeat_stop"))
        self.buttons["repeat"].set_running(exporting)
        self.buttons["stop"].set_mode(self.tr("stop"), RED)
        self.buttons["stop"].set_enabled(busy)
        self.buttons["overlay"].set_mode(self.tr("overlay_on") if overlay else self.tr("overlay_off"), GREEN if overlay else "#4a5059")
        self.buttons["clear"].set_mode(self.tr("clear"), "#4b515b")
        self.buttons["open"].set_mode(self.tr("open"), BLUE)
        self.buttons["refresh"].set_mode(self.tr("refresh"), PURPLE)
        self.buttons["logs"].set_mode(self.tr("logs"), "#3d5367")
        self.buttons["tips"].set_mode(self.tr("tips_on") if self.tooltips_enabled else self.tr("tips_off"), GREEN if self.tooltips_enabled else "#4a5059")

        self.format_header.configure(text=self.tr("formats"))
        self._refresh_formats()
        self._draw_progress()
        self._refresh_pages()

        error = str(self.state.get("last_error") or "")
        output = str(self.state.get("last_output_directory") or "")
        details = [
            f"{self.tr('full_pairs')}: {int(self.state.get('pairs_complete') or 0)}   {self.tr('saved')}: {saved}   {self.tr('yellow')}: {yellow}   {self.tr('red')}: {missing}",
            f"{self.tr('format_line')}: {', '.join(name.upper() for name in self.selected_formats()) or self.tr('none')}",
        ]
        if output:
            details.append(f"{self.tr('folder')}: {output}")
        if error:
            details.append(f"{self.tr('error')}: {error}")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(details))
        self.details.configure(state="disabled")

    def _refresh_formats(self) -> None:
        for fmt in ("html", "md", "txt", "json"):
            enabled = self.formats.get(fmt, False)
            self.format_buttons[fmt].set_mode(f"{'✓ ' if enabled else ''}{fmt.upper()}", GREEN if enabled else None)
        all_selected = all(self.formats.values())
        any_selected = any(self.formats.values())
        label = self.tr("all") if all_selected else self.tr("all_on") if not any_selected else self.tr("all_partial")
        self.format_buttons["all"].set_mode(label, BLUE if all_selected else AMBER if any_selected else None)

    def _draw_progress(self) -> None:
        try:
            self.progress.delete("all")
            width = max(10, self.progress.winfo_width())
            height = max(5, self.progress.winfo_height())
            progress = max(0, min(100, int(self.state.get("progress") or 0)))
            fill_width = width * progress / 100
            self.progress.create_rectangle(0, 0, width, height, fill="#111821", outline="")
            if fill_width > 0:
                for x in range(int(fill_width)):
                    self.progress.create_line(x, 0, x, height, fill=PanelButton._blend("#238954", "#68ffb5", x / max(1, fill_width)))
        except tk.TclError:
            pass

    def _schedule_pulse(self) -> None:
        if self._shutting_down:
            return
        self._pulse_phase = not self._pulse_phase
        for button in list(self.buttons.values()) + list(self.format_buttons.values()):
            button.set_pulse(self._pulse_phase)
        self._pulse_job = self.after(520, self._schedule_pulse)

    def _show_message(self, message: str, error: bool = False) -> None:
        if message:
            self.message_label.configure(text=message[:650], fg="#ff8f8f" if error else "#8fd8ad")

    # ------------------------------------------------------------------
    # AutomationPlatform lifecycle
    # ------------------------------------------------------------------
    def on_show(self) -> None:
        if self._shutting_down:
            return
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.ensure_backend(auto=True)
        if self.app is not None and self.worker_thread is not None and self.worker_thread.is_alive():
            self.app.submit_command("refresh_pages")
            self.app.submit_command("get_state")

    def on_hide(self) -> None:
        self.tooltip.hide()

    def on_shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.tooltip.hide()
        self._save_preferences()
        for job in (self._poll_job, self._pulse_job):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        if self.app is not None:
            try:
                self.app.request_stop()
            except Exception:
                pass
        if self.worker_thread is not None and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=3.0)
