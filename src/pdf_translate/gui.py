import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .pipeline import BACKENDS, Cancelled, Options, run

BACKEND_LABELS = {
    "agy": "Antigravity CLI (agy)",
    "claude-cli": "Claude Code (claude)",
    "anthropic": "Claude API（API キー）",
    "dummy": "ダミー（レイアウト確認のみ）",
}
DEFAULT_MODEL = "（既定のモデル）"
STATIC_MODELS = {
    "claude-cli": [("opus", "Opus（最新）"), ("sonnet", "Sonnet（最新）"), ("haiku", "Haiku（最新）")],
    "anthropic": [
        ("claude-opus-5-5", "Claude Opus 5.5"),
        ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
        ("claude-haiku-4-5", "Claude Haiku 4.5"),
        ("claude-fable-5-1", "Claude Fable 5.1"),
    ],
}


def list_agy_models() -> list[tuple[str, str]]:
    from .translate import NO_WINDOW, find_cli

    exe = find_cli("agy")
    if not exe:
        return []
    proc = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, creationflags=NO_WINDOW)
    models = []
    for line in proc.stdout.splitlines():
        model_id, _, label = line.partition("\t")
        if model_id.strip() and " " not in model_id.strip():
            models.append((model_id.strip(), label.strip() or model_id.strip()))
    return models


STAGE_LABELS = {"body": "本文を翻訳中", "toc": "しおりを翻訳中", "layout": "ページを組版中"}


def open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opening the user's own output
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.result: Path | None = None

        root.title("PDF 日本語翻訳")
        root.minsize(640, 520)
        frm = ttk.Frame(root, padding=12)
        frm.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.pages_var = tk.StringVar()
        self.only_var = tk.BooleanVar(value=False)
        self.backend_var = tk.StringVar(value=BACKEND_LABELS["agy"])
        self.model_var = tk.StringVar()
        self.key_var = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        self.workers_var = tk.IntVar(value=4)
        self.scale_var = tk.DoubleVar(value=0.85)

        r = 0
        ttk.Label(frm, text="翻訳する PDF").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.input_var).grid(row=r, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="選択…", command=self.pick_input).grid(row=r, column=2)
        r += 1
        ttk.Label(frm, text="保存先").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.output_var).grid(row=r, column=1, sticky="ew", padx=6)
        ttk.Button(frm, text="変更…", command=self.pick_output).grid(row=r, column=2)
        r += 1
        ttk.Label(frm, text="ページ").grid(row=r, column=0, sticky="w")
        pages = ttk.Frame(frm)
        pages.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        ttk.Entry(pages, textvariable=self.pages_var, width=24).pack(side="left")
        ttk.Label(pages, text="空欄で全ページ（例: 1-20,25,30-）").pack(side="left", padx=6)
        r += 1
        ttk.Checkbutton(frm, text="選んだページだけを出力する", variable=self.only_var).grid(
            row=r, column=1, columnspan=2, sticky="w", padx=6)
        r += 1
        ttk.Label(frm, text="翻訳エンジン").grid(row=r, column=0, sticky="w")
        combo = ttk.Combobox(frm, textvariable=self.backend_var, state="readonly",
                             values=[BACKEND_LABELS[b] for b in BACKENDS])
        combo.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        combo.bind("<<ComboboxSelected>>", lambda _e: self.update_fields())
        r += 1
        ttk.Label(frm, text="モデル").grid(row=r, column=0, sticky="w")
        self.model_combo = ttk.Combobox(frm, textvariable=self.model_var, state="readonly")
        self.model_combo.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        r += 1
        self.key_label = ttk.Label(frm, text="API キー")
        self.model_choices: list[tuple[str, str]] = []
        self.agy_models: list[tuple[str, str]] | None = None
        self.key_label.grid(row=r, column=0, sticky="w")
        self.key_entry = ttk.Entry(frm, textvariable=self.key_var, show="•")
        self.key_entry.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        r += 1
        opts = ttk.Frame(frm)
        opts.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        ttk.Label(opts, text="同時リクエスト数").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=32, width=4, textvariable=self.workers_var).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="文字サイズ（原文比）").pack(side="left")
        ttk.Spinbox(opts, from_=0.5, to=1.2, increment=0.05, width=5, textvariable=self.scale_var).pack(
            side="left", padx=4)
        r += 1

        buttons = ttk.Frame(frm)
        buttons.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        self.start_btn = ttk.Button(buttons, text="翻訳を開始", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(buttons, text="中止", command=self.request_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(buttons, text="結果を開く", command=self.open_result, state="disabled")
        self.open_btn.pack(side="right")
        r += 1
        self.status = ttk.Label(frm, text="PDF を選んでください")
        self.status.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.bar = ttk.Progressbar(frm, mode="determinate")
        self.bar.grid(row=r, column=0, columnspan=3, sticky="ew", pady=4)
        r += 1
        self.log_text = tk.Text(frm, height=10, state="disabled", wrap="word")
        self.log_text.grid(row=r, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(r, weight=1)

        self.update_fields()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if sys.platform == "darwin":
            root.createcommand("::tk::mac::Quit", self.quit)
        root.after(100, self.poll)

    @property
    def backend(self) -> str:
        label = self.backend_var.get()
        return next(k for k, v in BACKEND_LABELS.items() if v == label)

    def update_fields(self) -> None:
        backend = self.backend
        # only the Claude API needs a key; the CLIs use their own sign-in
        uses_key = backend == "anthropic"
        self.key_entry.configure(state="normal" if uses_key else "disabled")
        self.key_label.configure(text="API キー" if uses_key else "API キー（不要）")
        if backend == "agy" and self.agy_models is None:
            self.set_model_choices([], loading=True)

            def work():
                try:
                    models = list_agy_models()
                except Exception:  # noqa: BLE001 - fall back to the CLI default model
                    models = []
                self.events.put(("agy_models", models))

            threading.Thread(target=work, daemon=True).start()
        elif backend == "agy":
            self.set_model_choices(self.agy_models or [])
        else:
            self.set_model_choices(STATIC_MODELS.get(backend, []), disabled=backend == "dummy")

    def set_model_choices(self, models: list[tuple[str, str]], loading: bool = False, disabled: bool = False) -> None:
        self.model_choices = models
        if loading:
            self.model_combo.configure(values=[], state="disabled")
            self.model_var.set("モデル一覧を取得中…")
            return
        self.model_combo.configure(values=[DEFAULT_MODEL] + [label for _, label in models],
                                   state="disabled" if disabled else "readonly")
        self.model_var.set(DEFAULT_MODEL)

    @property
    def model(self) -> str | None:
        label = self.model_var.get()
        return next((mid for mid, lbl in self.model_choices if lbl == label), None)

    def pick_input(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("PDF", "*.pdf")])
        if path:
            self.input_var.set(path)
            self.output_var.set(str(Path(path).with_suffix(".ja.pdf")))
            self.status.configure(text="準備完了")

    def pick_output(self) -> None:
        initial = Path(self.output_var.get() or "translated.ja.pdf")
        path = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
                                            initialdir=initial.parent, initialfile=initial.name)
        if path:
            self.output_var.set(path)

    def log(self, msg: str) -> None:
        self.events.put(("log", msg))

    def append_log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_running(self, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.cancel_btn.configure(state="normal" if running else "disabled")

    def ensure_agy(self) -> bool:
        from .agy import install_agy, login_command
        from .translate import find_cli

        if find_cli("agy"):
            return True
        if not messagebox.askyesno(
            "Antigravity CLI が見つかりません",
            "翻訳に使う Antigravity CLI (agy) がインストールされていません。\n"
            "公式のインストーラーを実行してインストールしますか？",
        ):
            return False

        def work():
            try:
                exe = install_agy(self.log)
                self.events.put(("agy_installed", exe))
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", f"インストールに失敗しました: {e}"))

        self.status.configure(text="Antigravity CLI をインストール中…")
        self.bar.configure(mode="indeterminate")
        self.bar.start()
        self.set_running(True)
        self.cancel_btn.configure(state="disabled")
        self.login_command = login_command
        threading.Thread(target=work, daemon=True).start()
        return False

    def start(self) -> None:
        src = Path(self.input_var.get())
        if not src.is_file():
            messagebox.showerror("エラー", "翻訳する PDF を選んでください。")
            return
        if self.backend == "agy" and not self.ensure_agy():
            return
        if self.backend == "anthropic":
            if not self.key_var.get().strip():
                messagebox.showerror("エラー", "Claude API の API キーを入力してください。")
                return
            os.environ["ANTHROPIC_API_KEY"] = self.key_var.get().strip()
        try:
            opts = Options(
                input=src,
                output=Path(self.output_var.get()) if self.output_var.get() else None,
                pages=self.pages_var.get() or None,
                only_selected=self.only_var.get(),
                backend=self.backend,
                model=self.model,
                workers=int(self.workers_var.get()),
                font_scale=float(self.scale_var.get()),
            )
        except (ValueError, tk.TclError) as e:
            messagebox.showerror("エラー", f"設定値が正しくありません: {e}")
            return

        self.cancel.clear()
        self.result = None
        self.open_btn.configure(state="disabled")
        self.set_running(True)
        self.status.configure(text="開始しています…")
        self.bar.configure(mode="determinate", value=0)

        def work():
            try:
                out = run(opts, self.log, lambda s, d, t: self.events.put(("progress", (s, d, t))), self.cancel)
                self.events.put(("done", out))
            except Cancelled:
                self.events.put(("cancelled", None))
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def quit(self) -> None:
        from .translate import kill_running_cli

        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "終了の確認",
                "翻訳中です。中止して終了しますか？\n翻訳済みの部分は保存されているので、次回は続きから翻訳します。",
            ):
                return
        self.cancel.set()
        kill_running_cli()
        self.root.destroy()
        # translation threads may still be unwinding; exit now so the app never lingers without a window
        os._exit(0)

    def request_cancel(self) -> None:
        from .translate import kill_running_cli

        self.cancel.set()
        kill_running_cli()
        self.cancel_btn.configure(state="disabled")
        self.status.configure(text="中止しています（実行中のリクエストの完了を待っています）…")

    def open_result(self) -> None:
        if self.result and self.result.exists():
            open_file(self.result)

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "progress":
                    stage, done, total = payload
                    self.bar.configure(maximum=max(total, 1), value=done)
                    self.status.configure(text=f"{STAGE_LABELS.get(stage, stage)}… {done}/{total}")
                elif kind == "done":
                    self.result = payload
                    self.set_running(False)
                    self.open_btn.configure(state="normal")
                    self.status.configure(text=f"完了しました: {payload}")
                elif kind == "cancelled":
                    self.set_running(False)
                    self.status.configure(text="中止しました。途中までの翻訳は保存されているので、再開すると続きから翻訳します。")
                elif kind == "agy_models":
                    self.agy_models = payload
                    if self.backend == "agy":
                        self.set_model_choices(payload)
                elif kind == "agy_installed":
                    self.agy_models = None
                    self.update_fields()
                    self.bar.stop()
                    self.bar.configure(mode="determinate", value=0)
                    self.set_running(False)
                    self.status.configure(text="Antigravity CLI をインストールしました")
                    if messagebox.askyesno(
                        "サインイン",
                        "続けて Antigravity にサインインする必要があります。\n"
                        "サインイン用のウィンドウを開きますか？\n"
                        "サインインが終わったら、そのウィンドウを閉じて「翻訳を開始」を押してください。",
                    ):
                        subprocess.Popen(self.login_command(payload))
                elif kind == "error":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                    self.set_running(False)
                    self.status.configure(text="エラーで停止しました")
                    self.append_log(f"ERROR: {payload}")
                    messagebox.showerror("エラー", payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def _fix_tcl_paths() -> None:
    # standalone Python builds (e.g. uv-managed) may not locate their bundled Tcl/Tk scripts
    lib = Path(sys.base_prefix) / "lib"
    for var, name in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        if var not in os.environ and (lib / name / ("init.tcl" if var == "TCL_LIBRARY" else "tk.tcl")).exists():
            os.environ[var] = str(lib / name)


def main() -> None:
    if not getattr(sys, "frozen", False):
        _fix_tcl_paths()
    root = tk.Tk()
    App(root)
    root.mainloop()
