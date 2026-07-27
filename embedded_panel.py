from __future__ import annotations

import json
import os
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Any

UI_API = 1


class DynamicConversationExporterPanel(tk.Frame):
    """AutomationPlatform embedded panel.

    This class intentionally never creates Tk() or Toplevel(). AutomationPlatform
    owns the one application window and passes its workspace as `master`.
    """

    def __init__(self, master: tk.Misc, services: Any) -> None:
        self.services = services
        self.platform_root = Path(services.root)
        self.module_root = Path(__file__).resolve().parent
        self.theme = getattr(services, "theme", {}) or {}
        bg = self.theme.get("bg", "#08111b")
        super().__init__(master, bg=bg)

        self.log_text: tk.Text | None = None
        self.status_labels: dict[str, tk.Label] = {}
        self._build()
        self.refresh_status()

    def c(self, key: str, fallback: str) -> str:
        return str(self.theme.get(key, fallback))

    def _build(self) -> None:
        bg = self.c("bg", "#08111b")
        panel = self.c("panel", "#0d1a28")
        card = self.c("card", "#13283a")
        text = self.c("text", "#edf8ff")
        muted = self.c("muted", "#8eabbc")
        accent = self.c("accent", "#38dfb5")
        accent2 = self.c("accent2", "#44aeea")
        line = self.c("line", "#24445d")

        head = tk.Frame(self, bg=bg)
        head.pack(fill="x", pady=(4, 16))
        tk.Label(head, text="Dynamic Conversation Exporter", bg=bg, fg=text, font=("Segoe UI Semibold", 20)).pack(anchor="w")
        tk.Label(
            head,
            text="Встроенная панель модуля • общий Python • общий Chrome Profile • общий CDP :9222",
            bg=bg,
            fg=muted,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(3, 0))

        cards = tk.Frame(self, bg=bg)
        cards.pack(fill="x")
        for idx, (key, title) in enumerate(
            (
                ("module", "MODULE"),
                ("cdp", "CDP DEBUG"),
                ("exports", "EXPORTS"),
                ("engine", "ENGINE"),
            )
        ):
            box = tk.Frame(cards, bg=card, padx=14, pady=12, highlightthickness=1, highlightbackground=line)
            box.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 7, 0))
            cards.columnconfigure(idx, weight=1)
            tk.Label(box, text=title, bg=card, fg=muted, font=("Segoe UI", 8)).pack(anchor="w")
            label = tk.Label(box, text="…", bg=card, fg=accent, font=("Segoe UI Semibold", 10))
            label.pack(anchor="w", pady=(4, 0))
            self.status_labels[key] = label

        actions = tk.Frame(self, bg=panel, padx=14, pady=14, highlightthickness=1, highlightbackground=line)
        actions.pack(fill="x", pady=(16, 12))
        tk.Label(actions, text="БЫСТРЫЕ ДЕЙСТВИЯ", bg=panel, fg=muted, font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(0, 10))
        row1 = tk.Frame(actions, bg=panel)
        row1.pack(fill="x")
        self._button(row1, "CHROME + CHATGPT", self.start_chatgpt_debug, accent2).pack(side="left", padx=(0, 7))
        self._button(row1, "ПРОВЕРИТЬ ОКРУЖЕНИЕ", self.check_environment, accent2).pack(side="left", padx=7)
        self._button(row1, "ЗАПУСТИТЬ ЭКСПОРТ", self.start_export, accent).pack(side="left", padx=7)
        self._button(row1, "ОБНОВИТЬ СТАТУС", self.refresh_status, panel).pack(side="left", padx=7)

        row2 = tk.Frame(actions, bg=panel)
        row2.pack(fill="x", pady=(9, 0))
        self._button(row2, "ЭКСПОРТЫ", lambda: self.services.open_folder(self.module_root / "exports"), panel).pack(side="left", padx=(0, 7))
        self._button(row2, "ЛОГИ МОДУЛЯ", lambda: self.services.open_folder(self.module_root / "logs"), panel).pack(side="left", padx=7)
        self._button(row2, "ПАПКА МОДУЛЯ", lambda: self.services.open_folder(self.module_root), panel).pack(side="left", padx=7)
        self._button(row2, "ПОСЛЕДНИЙ ЭКСПОРТ", self.open_latest_export, panel).pack(side="left", padx=7)

        info = tk.Frame(self, bg=card, padx=14, pady=12, highlightthickness=1, highlightbackground=line)
        info.pack(fill="x", pady=(0, 12))
        tk.Label(info, text="ИНТЕГРАЦИЯ", bg=card, fg=muted, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.integration_text = tk.Label(info, text="", bg=card, fg=text, justify="left", anchor="w", font=("Consolas", 9))
        self.integration_text.pack(fill="x", pady=(6, 0))

        tk.Label(self, text="ЖИВОЙ ВЫВОД МОДУЛЯ", bg=bg, fg=muted, font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self.log_text = tk.Text(self, bg="#050b11", fg=text, insertbackground=accent, bd=0, font=("Consolas", 9), wrap="word", height=15)
        self.log_text.pack(fill="both", expand=True, pady=(7, 0))
        self._append("Embedded UI loaded. No second AutomationPlatform window is created.")

    def _button(self, master: tk.Misc, text: str, command: Any, color: str) -> tk.Button:
        bg = color if color.startswith("#") else self.c("panel2", "#102235")
        fg = "#051019" if bg in (self.c("accent", "#38dfb5"), self.c("accent2", "#44aeea")) else self.c("text", "#edf8ff")
        return tk.Button(
            master,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=self.c("accent", "#38dfb5"),
            activeforeground="#051019",
            bd=0,
            padx=13,
            pady=8,
            font=("Segoe UI Semibold", 9),
            cursor="hand2",
        )

    def _append(self, text: str) -> None:
        if self.log_text is None:
            return
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")

    def _run_script(self, name: str, *, on_done: Any | None = None) -> None:
        path = self.module_root / name
        if not path.exists():
            messagebox.showerror("Dynamic Conversation Exporter", f"Не найден файл:\n{path}")
            return
        self._append(f"> {name}")

        def output(line: str) -> None:
            self._append(line)

        def done(rc: int, output_text: str) -> None:
            self._append(f"[DONE] exit={rc}")
            self.refresh_status()
            if on_done:
                on_done(rc, output_text)

        self.services.run_process([str(path)], cwd=self.module_root, on_output=output, on_done=done)

    def start_chatgpt_debug(self) -> None:
        launcher = self.platform_root / "START_CHROME_DEBUG.cmd"
        if not launcher.exists():
            messagebox.showerror("Dynamic Conversation Exporter", f"Не найден общий Chrome launcher:\n{launcher}")
            return
        self._append("> shared Chrome Debug → https://chatgpt.com/")
        self.services.run_process([str(launcher), "https://chatgpt.com/"], on_output=self._append, on_done=lambda _rc, _out: self.refresh_status())

    def check_environment(self) -> None:
        self._run_script("04_CHECK_ENVIRONMENT.cmd")

    def start_export(self) -> None:
        """Run the module engine without opening another AutomationPlatform shell.

        Existing exporter scripts are kept for backwards compatibility. The platform
        passes AUTOMATION_PLATFORM_EMBEDDED=1 so future exporter backends can suppress
        any legacy standalone desktop launcher while retaining this embedded panel.
        """
        script = "03_RUN_EXPORTER.cmd"
        if not (self.module_root / script).exists():
            script = "00_START_ALL.cmd"
        self._run_script(script)

    def open_latest_export(self) -> None:
        export_dir = self.module_root / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        files = [p for p in export_dir.rglob("*") if p.is_file()]
        if not files:
            messagebox.showinfo("Dynamic Conversation Exporter", "Экспортов пока нет.")
            return
        latest = max(files, key=lambda p: p.stat().st_mtime)
        try:
            os.startfile(str(latest))
        except OSError:
            self.services.open_folder(latest.parent)

    def refresh_status(self) -> None:
        module_json = self.module_root / "module.json"
        version = "installed"
        if module_json.exists():
            try:
                version = str(json.loads(module_json.read_text(encoding="utf-8-sig")).get("version", "installed"))
            except Exception:
                pass

        cdp_online = False
        try:
            import socket

            with socket.create_connection(("127.0.0.1", 9222), timeout=0.18):
                cdp_online = True
        except OSError:
            pass

        exports = self.module_root / "exports"
        count = len([p for p in exports.rglob("*") if p.is_file()]) if exports.exists() else 0
        engine = "READY" if (self.module_root / "03_RUN_EXPORTER.cmd").exists() else "CHECK"

        ok = self.c("ok", "#4ce4a9")
        warning = self.c("warning", "#ffbf55")
        self.status_labels["module"].configure(text=f"v{version}", fg=ok)
        self.status_labels["cdp"].configure(text="ONLINE" if cdp_online else "OFFLINE", fg=ok if cdp_online else warning)
        self.status_labels["exports"].configure(text=str(count), fg=ok)
        self.status_labels["engine"].configure(text=engine, fg=ok if engine == "READY" else warning)

        self.integration_text.configure(
            text=(
                f"Platform root : {self.platform_root}\n"
                f"Module path   : {self.module_root}\n"
                f"Python        : {self.services.python_exe}\n"
                f"ChromeProfile : {self.services.chrome_profile}\n"
                f"CDP           : {self.services.cdp_url}"
            )
        )

    def on_show(self) -> None:
        self.refresh_status()
