from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

UI_API = 1
INTEGRATION_REVISION = 5
MODULE_VERSION = "1.6.0"

MODULE_ROOT = Path(__file__).resolve().parent
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from chatgpt_exporter.app import ExporterApplication  # noqa: E402
from chatgpt_exporter.floating_panel import (  # noqa: E402
    FloatingControlPanel,
    LANGUAGES,
    TRANSLATIONS,
)


class _ErrorOnlyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.ERROR


def _load_config(base_dir: Path, cdp_url: str) -> dict[str, Any]:
    path = base_dir / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config.json root must be an object")
    data["cdp_url"] = cdp_url
    return data


def _configure_module_logging(base_dir: Path) -> Path:
    """Configure exporter loggers without clearing AutomationPlatform root handlers."""
    logs = base_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    run_log = logs / f"run_{stamp}.log"
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("chatgpt_exporter")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    marker = str(base_dir.resolve())
    if not any(getattr(h, "_ap_dce_marker", None) == marker for h in logger.handlers):
        run_handler = logging.FileHandler(run_log, encoding="utf-8")
        run_handler.setLevel(logging.DEBUG)
        run_handler.setFormatter(formatter)
        run_handler._ap_dce_marker = marker  # type: ignore[attr-defined]
        logger.addHandler(run_handler)

        combined = logging.FileHandler(logs / "exporter.log", encoding="utf-8")
        combined.setLevel(logging.INFO)
        combined.setFormatter(formatter)
        combined._ap_dce_marker = marker  # type: ignore[attr-defined]
        logger.addHandler(combined)

        errors = logging.FileHandler(logs / "errors.log", encoding="utf-8")
        errors.setLevel(logging.ERROR)
        errors.addFilter(_ErrorOnlyFilter())
        errors.setFormatter(formatter)
        errors._ap_dce_marker = marker  # type: ignore[attr-defined]
        logger.addHandler(errors)

    (logs / "LAST_RUN.txt").write_text(str(run_log), encoding="utf-8")
    return run_log


class _EmbeddedFloatingControlPanel(FloatingControlPanel):
    """The original v1.6 FloatingControlPanel rendered inside a provided Frame.

    `_build_ui`, queue handling, translations, buttons, status rendering and all
    exporter actions are inherited from the working ZIP unchanged. Only native
    top-level-window behavior is adapted to the AutomationPlatform host window.
    """

    def __init__(
        self,
        host: tk.Frame,
        base_dir: Path,
        config: dict[str, Any],
        command_sender: Callable[..., None],
        ui_queue: queue.Queue[dict[str, Any]],
        on_hide_request: Callable[[], None],
    ) -> None:
        # Mirror the original v1.6 constructor state exactly up to Tk window creation.
        self.base_dir = base_dir
        self.config = config
        self.send = command_sender
        self.ui_queue = ui_queue
        self.on_close_callback = on_hide_request
        self._embedded_hide_callback = on_hide_request
        self.logger = logging.getLogger("chatgpt_exporter.panel")
        self.position_path = base_dir / "panel_position.json"
        self.width = int(config.get("panel_width", 510))
        self.alpha = float(config.get("panel_alpha", 0.97))
        self.pinned = bool(config.get("panel_topmost", True))
        self.virtual_desktops_all = bool(config.get("panel_all_virtual_desktops", True))
        self.collapsed = False
        self.tooltips_enabled = bool(config.get("tooltips_enabled", True))
        self.language = str(config.get("default_language", "ru"))
        if self.language not in LANGUAGES:
            self.language = "ru"
        self._saved_xy = (int(config.get("panel_start_x", 40)), int(config.get("panel_start_y", 80)))
        self._load_preferences()
        self.pages: list[dict[str, Any]] = []
        self.formats = {
            name: bool(config.get("default_formats", {}).get(name, True))
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
            "overlay_enabled": True,
            "last_error": "",
            "last_output_directory": "",
        }
        self._pulse_phase = False
        self._dragging = False
        self._drag_root_x = 0
        self._drag_root_y = 0
        self._drag_mouse_x = 0
        self._drag_mouse_y = 0
        self._flash_job: str | None = None

        # Crucial difference from standalone mode: no tk.Tk(), no Toplevel().
        self.root = host
        top = host.winfo_toplevel()
        self.tooltip = self._make_tooltip(top)

        # The original _build_ui uses bind_all only for moving its standalone
        # frameless window. Suppress those global move bindings in embedded mode,
        # while reusing every visual/widget construction line from v1.6.
        original_bind_all = host.bind_all
        try:
            host.bind_all = lambda *_a, **_k: None  # type: ignore[method-assign]
            self._build_ui()
        finally:
            host.bind_all = original_bind_all  # type: ignore[method-assign]

        self._refresh_all()
        self.root.after(60, self._poll_queue)
        self.root.after(520, self._schedule_pulse)
        self.send("set_language", language=self.language)
        self.send("get_state")

    def _make_tooltip(self, top: tk.Misc):
        # TooltipManager is imported by floating_panel and used by the working UI.
        from chatgpt_exporter.floating_panel import TooltipManager

        return TooltipManager(top, lambda key: self.tr(f"tip_{key}"), enabled=self.tooltips_enabled)

    def run(self) -> None:
        # AutomationPlatform owns mainloop().
        return

    def _bind_drag(self, widget: tk.Widget) -> None:
        # A module must not drag the whole AutomationPlatform window merely because
        # the standalone panel header is draggable. Keep the original visual header.
        self.tooltip.bind(widget, "move")

    def _start_move(self, _event: tk.Event) -> str:
        return "break"

    def _do_move(self, _event: tk.Event) -> str:
        return "break"

    def _end_move(self, _event: tk.Event) -> str:
        return "break"

    def _load_geometry(self) -> None:
        return

    def _apply_toolwindow_style(self) -> None:
        return

    def _save_geometry(self) -> None:
        """Persist exporter preferences, never overwrite AutomationPlatform geometry."""
        try:
            existing: dict[str, Any] = {}
            if self.position_path.exists():
                try:
                    raw = json.loads(self.position_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        existing.update(raw)
                except Exception:
                    pass
            existing.update(
                {
                    "collapsed": self.collapsed,
                    "pinned": self.pinned,
                    "virtual_desktops_all": self.virtual_desktops_all,
                    "tooltips_enabled": self.tooltips_enabled,
                    "language": self.language,
                }
            )
            self.position_path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except (OSError, tk.TclError):
            pass

    def toggle_pin(self) -> None:
        self.pinned = not self.pinned
        try:
            self.root.winfo_toplevel().attributes("-topmost", self.pinned)
        except tk.TclError:
            pass
        self._refresh_header_buttons()
        self._save_geometry()

    def _apply_virtual_desktop_state(self) -> None:
        # In embedded mode VD applies to the AutomationPlatform host window.
        if os.name != "nt":
            self.virtual_desktops_all = False
            self._refresh_header_buttons()
            return
        try:
            from pyvda import AppView  # type: ignore

            view = AppView(hwnd=self._hwnd())
            view.pin() if self.virtual_desktops_all else view.unpin()
            self.virtual_desktops_all = bool(view.is_pinned())
        except Exception:
            self.virtual_desktops_all = False
        self._refresh_header_buttons()
        self._save_geometry()

    def toggle_collapse(self) -> None:
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.body.pack_forget()
            self.collapse_button.configure(text="+")
        else:
            self.body.pack(fill="both", expand=True, padx=6, pady=(5, 7))
            self.collapse_button.configure(text="−")
        self._save_geometry()

    def close(self) -> None:
        # The original X button becomes "leave this module view". The backend is
        # intentionally kept alive because AutomationPlatform caches module panels.
        self.tooltip.hide()
        self._save_geometry()
        self._embedded_hide_callback()


class DynamicConversationExporterPanel(tk.Frame):
    """AutomationPlatform adapter around the exact working v1.6 panel/backend."""

    def __init__(self, master: tk.Misc, services: Any) -> None:
        super().__init__(master, bg="#090b0e")
        self.services = services
        self.module_root = MODULE_ROOT
        self.config = _load_config(self.module_root, str(services.cdp_url))
        self.run_log = _configure_module_logging(self.module_root)
        self.logger = logging.getLogger("chatgpt_exporter.embedded")
        self.logger.info("=== Dynamic Conversation Exporter v1.6.0 / embedded ===")
        self.logger.info("Module root: %s", self.module_root)
        self.logger.info("CDP: %s", self.config["cdp_url"])

        self.app = ExporterApplication(base_dir=self.module_root, config=self.config)
        self._worker_errors: list[BaseException] = []
        self._shutdown = False

        def worker() -> None:
            if sys.platform == "win32":
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass
            try:
                asyncio.run(self.app.run())
            except BaseException as exc:  # noqa: BLE001
                self._worker_errors.append(exc)
                self.logger.exception("Browser worker stopped")
                try:
                    self.app.ui_queue.put(
                        {"type": "error", "error_key": "error_connect_chrome", "details": str(exc)}
                    )
                except Exception:
                    pass

        self.worker_thread = threading.Thread(
            target=worker, name="ChatGPTExporterBrowserWorker", daemon=True
        )
        self.worker_thread.start()

        self.panel = _EmbeddedFloatingControlPanel(
            host=self,
            base_dir=self.module_root,
            config=self.config,
            command_sender=self.app.submit_command,
            ui_queue=self.app.ui_queue,
            on_hide_request=lambda: self.services.show_page("home"),
        )

    def on_show(self) -> None:
        if self._shutdown:
            return
        try:
            self.panel.tooltip.set_enabled(self.panel.tooltips_enabled)
            self.app.submit_command("refresh_pages")
            self.app.submit_command("get_state")
        except Exception:
            self.logger.exception("on_show refresh failed")

    def on_hide(self) -> None:
        try:
            self.panel.tooltip.hide()
        except Exception:
            pass

    def on_shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self.panel._save_geometry()
            self.panel.tooltip.hide()
        except Exception:
            pass
        try:
            self.app.request_stop()
        except Exception:
            pass
        if self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)
