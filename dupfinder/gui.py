"""데스크톱 화면(tkinter). 검사·관리는 뒤에서 돌리고 화면은 항상 조작할 수 있게 한다(R5-I4)."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from .actions import QUARANTINE, TRASH, ActionError, ActionRunner, auto_select, cleanup_partials, plan, summarize
from .config import (DEFAULT_APP_DIR, DEFAULT_EXCLUDE_DIR_NAMES, DEFAULT_EXCLUDE_PATHS, DEFAULT_QUARANTINE_DIR,
                     EXT_PRESETS, Limits, ScanConfig, parse_exts)
from .engine import AskRequest, Engine, PHASE_FULL, PHASE_PARTIAL, PHASE_WALK
from .models import Issue, Reason, ScanResult
from .report import export_groups, export_issues, fmt_size, fmt_time
from .store import RunMarker, clear_records
from .winutil import long_path

try:
    from PIL import Image, ImageTk
except Exception:  # noqa: BLE001
    Image = ImageTk = None

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".htm", ".ini", ".cfg", ".py", ".js",
            ".ts", ".css", ".yaml", ".yml", ".bat", ".ps1", ".sql", ".java", ".c", ".cpp", ".h", ".cs"}


# ---------------------------------------------------------------- 가상 스크롤 표(R5-I2)

class VirtualTable(ttk.Frame):
    """보이는 줄만 그리는 표. 행이 수백만 개여도 화면에는 수십 줄만 존재한다."""

    def __init__(self, master, columns, get_count, get_row, on_click=None, on_select=None, on_double=None,
                 rowheight=24):
        super().__init__(master)
        self.get_count, self.get_row = get_count, get_row
        self.on_click, self.on_select, self.on_double = on_click, on_select, on_double
        self.rowheight = rowheight
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for cid, head, width, anchor, stretch in columns:
            self.tree.heading(cid, text=head)
            self.tree.column(cid, width=width, minwidth=40, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("grp", background="#e8eef7")
        self.tree.tag_configure("base", foreground="#555555")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.offset, self.visible, self.sel = 0, 20, None
        t = self.tree
        t.bind("<Configure>", self._on_configure)
        t.bind("<ButtonRelease-1>", lambda e: self.refresh() if t.identify_region(e.x, e.y) == "separator" else None,
               add="+")
        t.bind("<MouseWheel>", self._on_wheel)
        t.bind("<Button-1>", self._on_button)
        t.bind("<Double-1>", self._on_double)
        for key in ("<Up>", "<Down>", "<Prior>", "<Next>", "<Home>", "<End>", "<space>"):
            t.bind(key, self._on_key)

    def refresh(self, keep_sel=True):
        n = self.get_count()
        self.offset = max(0, min(self.offset, max(0, n - self.visible)))
        if not keep_sel or (self.sel is not None and self.sel >= n):
            self.sel = None
        t = self.tree
        t.delete(*t.get_children())
        end = min(n, self.offset + self.visible)
        for i in range(self.offset, end):
            vals, tags = self.get_row(i)
            t.insert("", "end", iid=str(i), values=vals, tags=tags)
        if self.sel is not None and self.offset <= self.sel < end:
            t.selection_set(str(self.sel))
        self.vsb.set(*(0.0, 1.0) if n == 0 else (self.offset / n, end / n))

    def _yview(self, *args):
        n = self.get_count()
        if args[0] == "moveto":
            self.offset = int(float(args[1]) * n)
        elif args[0] == "scroll":
            step = int(args[1]) * (self.visible if args[2] == "pages" else 1)
            self.offset += step
        self.refresh()

    def _on_configure(self, ev):
        vis = max(1, (ev.height - self.rowheight - 4) // self.rowheight)
        if vis != self.visible:
            self.visible = vis
            self.refresh()

    def _on_wheel(self, ev):
        self.offset -= int(ev.delta / 120) * 3
        self.refresh()
        return "break"

    def _row_at(self, y):
        iid = self.tree.identify_row(y)
        return int(iid) if iid else None

    def _on_button(self, ev):
        self.tree.focus_set()
        i = self._row_at(ev.y)
        if i is None:
            return None
        col = self.tree.identify_column(ev.x)
        cid = self.tree["columns"][int(col[1:]) - 1] if col and col != "#0" else ""
        self._select(i)
        if self.on_click:
            self.on_click(i, cid)
        self.refresh()
        return "break"

    def _on_double(self, ev):
        i = self._row_at(ev.y)
        if i is not None and self.on_double:
            self.on_double(i)
            self.refresh()
        return "break"

    def _on_key(self, ev):
        n = self.get_count()
        if n == 0:
            return "break"
        cur = self.sel if self.sel is not None else self.offset
        k = ev.keysym
        if k == "space":
            if self.on_click and self.sel is not None:
                self.on_click(self.sel, "chk")
            self.refresh()
            return "break"
        cur = {"Up": cur - 1, "Down": cur + 1, "Prior": cur - self.visible, "Next": cur + self.visible,
               "Home": 0, "End": n - 1}.get(k, cur)
        cur = max(0, min(n - 1, cur))
        if cur < self.offset:
            self.offset = cur
        elif cur >= self.offset + self.visible:
            self.offset = cur - self.visible + 1
        self._select(cur)
        self.refresh()
        return "break"

    def _select(self, i):
        self.sel = i
        if self.on_select:
            self.on_select(i)


# ---------------------------------------------------------------- 질문 창(Ⓔ)

class AskDialog(tk.Toplevel):
    """정해진 시간 안에 답이 없으면 기본 동작을 고른다."""

    def __init__(self, master, req: AskRequest):
        super().__init__(master)
        self.req = req
        self.remaining = int(req.timeout)
        self.title("확인이 필요합니다")
        self.resizable(False, False)
        self.transient(master)
        ttk.Label(self, text=req.question, justify="left", wraplength=480).pack(padx=16, pady=(16, 8), anchor="w")
        self.count = ttk.Label(self, foreground="#a33")
        self.count.pack(padx=16, anchor="w")
        bar = ttk.Frame(self)
        bar.pack(padx=16, pady=16, anchor="e")
        for opt in req.options:
            b = ttk.Button(bar, text=opt + (" (기본)" if opt == req.default else ""),
                           command=lambda o=opt: self._done(o))
            b.pack(side="left", padx=4)
        self.protocol("WM_DELETE_WINDOW", lambda: self._done(req.default))
        self._tick()
        self.lift()
        self.grab_set()

    def _tick(self):
        if self.req._ev.is_set():  # 엔진 쪽에서 이미 결정(취소 등)
            self.destroy()
            return
        if self.remaining <= 0:
            self._done(self.req.default)
            return
        self.count.config(text=f"{self.remaining}초 안에 고르지 않으면 '{self.req.default}'로 진행합니다.")
        self.remaining -= 1
        self.after(1000, self._tick)

    def _done(self, choice):
        self.req.reply(choice)
        try:
            self.grab_release()
        finally:
            self.destroy()


# ---------------------------------------------------------------- 폴더 목록 상자

class FolderList(ttk.LabelFrame):
    def __init__(self, master, title, height=4):
        super().__init__(master, text=title, padding=6)
        self.lb = tk.Listbox(self, height=height, activestyle="none")
        self.lb.grid(row=0, column=0, rowspan=2, sticky="nsew")
        self.add_btn = ttk.Button(self, text="추가…", command=self._add)
        self.add_btn.grid(row=0, column=1, padx=(6, 0), sticky="n")
        self.del_btn = ttk.Button(self, text="빼기", command=self._remove)
        self.del_btn.grid(row=1, column=1, padx=(6, 0), sticky="n")
        self.columnconfigure(0, weight=1)

    def _add(self):
        d = filedialog.askdirectory(parent=self, mustexist=True)
        if d:
            d = os.path.normpath(d)
            if d not in self.get():
                self.lb.insert("end", d)

    def _remove(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)

    def get(self) -> list[str]:
        return list(self.lb.get(0, "end"))

    def set_enabled(self, on: bool):
        st = "normal" if on else "disabled"
        self.lb.config(state=st)
        self.add_btn.config(state=st)
        self.del_btn.config(state=st)


# ---------------------------------------------------------------- 본 화면

class App(tk.Tk):
    def __init__(self, app_dir: str = DEFAULT_APP_DIR):
        super().__init__()
        self.app_dir = app_dir
        self.title("중복 파일 관리")
        self.geometry("1180x760")
        self.minsize(900, 600)
        self._setup_fonts()
        self.events: queue.Queue = queue.Queue()
        self.engine: Engine | None = None
        self.scan_token = None
        self.runner: ActionRunner | None = None
        self.busy = None              # None | "scan" | "action"
        self.cancel_deadline = None
        self.result: ScanResult | None = None
        self.groups = []
        self.selected: set[str] = set()
        self.expanded: set[int] = set()
        self.rows: list[tuple[int, int]] = []
        self.issues: list[Issue] = []
        self.preview_token = 0
        self.preview_img = None
        self._build()
        self.after(100, self._poll)
        self.after(300, self._startup_checks)

    def _setup_fonts(self):
        try:
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family="맑은 고딕", size=10)
        except tk.TclError:
            pass
        f = tkfont.nametofont("TkDefaultFont")
        self.rowheight = int(f.metrics("linespace") * 1.5)
        self.char_px = max(1, f.measure("가나다abc012") // 9)
        ttk.Style(self).configure("Treeview", rowheight=self.rowheight)

    # ---- 화면 구성

    def _build(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self._build_settings()
        self._build_results()
        self._build_issues()
        self._build_bottom()

    def _build_settings(self):
        f = ttk.Frame(self.nb, padding=10)
        self.nb.add(f, text="검사 설정")
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        mode = ttk.LabelFrame(f, text="비교 방식", padding=6)
        mode.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.mode = tk.StringVar(value="all")
        ttk.Radiobutton(mode, text="모든 파일끼리 비교", value="all", variable=self.mode,
                        command=self._mode_changed).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(mode, text="기준 폴더와 대상 폴더 비교 (기준 폴더 파일은 건드리지 않음)", value="compare",
                        variable=self.mode, command=self._mode_changed).pack(side="left")

        self.roots = FolderList(f, "검사 폴더 (비교 모드에서는 대상 폴더)")
        self.roots.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.bases = FolderList(f, "기준 폴더 (비교 모드 전용)")
        self.bases.grid(row=1, column=1, sticky="nsew")
        self.prefer = FolderList(f, "우선 보존 폴더 (이 폴더의 파일을 원본으로 남김)", height=3)
        self.prefer.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=(8, 0))

        q = ttk.LabelFrame(f, text="격리 폴더 (옮긴 파일 보관, 검사에서 항상 제외)", padding=6)
        q.grid(row=2, column=1, sticky="nsew", pady=(8, 0))
        self.qdir = tk.StringVar(value=DEFAULT_QUARANTINE_DIR)
        ttk.Entry(q, textvariable=self.qdir).pack(side="left", fill="x", expand=True)
        ttk.Button(q, text="찾아보기…", command=self._pick_qdir).pack(side="left", padx=(6, 0))

        ex = ttk.LabelFrame(f, text="제외 경로 (한 줄에 하나)", padding=6)
        ex.grid(row=3, column=0, sticky="nsew", padx=(0, 6), pady=(8, 0))
        self.excl = tk.Text(ex, height=7, width=40)
        self.excl.pack(fill="both", expand=True)
        self.excl.insert("1.0", "\n".join(DEFAULT_EXCLUDE_PATHS))

        opt = ttk.LabelFrame(f, text="필터와 한도", padding=6)
        opt.grid(row=3, column=1, sticky="nsew", pady=(8, 0))
        self.excl_names = tk.StringVar(value=", ".join(DEFAULT_EXCLUDE_DIR_NAMES))
        self.min_kb = tk.StringVar(value="1")
        self.max_mb = tk.StringVar(value="")
        self.time_min = tk.StringVar(value="")
        rows = [("제외할 폴더 이름 (쉼표로 구분)", self.excl_names), ("최소 크기 (KB)", self.min_kb),
                ("최대 크기 (MB, 비우면 제한 없음)", self.max_mb), ("전체 시간 한도 (분, 비우면 무제한)", self.time_min)]
        for i, (label, var) in enumerate(rows):
            ttk.Label(opt, text=label).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(opt, textvariable=var, width=28).grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=2)

        r = len(rows)
        ttk.Separator(opt).grid(row=r, column=0, columnspan=2, sticky="ew", pady=6)
        kind = ttk.Frame(opt)
        kind.grid(row=r + 1, column=0, columnspan=2, sticky="w")
        ttk.Label(kind, text="파일 종류").pack(side="left", padx=(0, 8))
        self.ext_mode = tk.StringVar(value="all")
        for text, val in (("모두", "all"), ("이것만 검사", "include"), ("이것은 빼고 검사", "exclude")):
            ttk.Radiobutton(kind, text=text, value=val, variable=self.ext_mode,
                            command=self._ext_mode_changed).pack(side="left", padx=(0, 8))
        self.exts = tk.StringVar(value="")
        self.ext_entry = ttk.Entry(opt, textvariable=self.exts)
        self.ext_entry.grid(row=r + 2, column=0, columnspan=2, sticky="ew", pady=(4, 2))
        pre = ttk.Frame(opt)
        pre.grid(row=r + 3, column=0, columnspan=2, sticky="w")
        self.ext_buttons = [ttk.Button(pre, text=name, width=9, command=lambda e=exts: self._add_preset(e))
                            for name, exts in EXT_PRESETS]
        self.ext_buttons.append(ttk.Button(pre, text="비우기", width=8, command=lambda: self.exts.set("")))
        for b in self.ext_buttons:
            b.pack(side="left", padx=(0, 4))
        self._ext_mode_changed()
        opt.columnconfigure(1, weight=1)

        rec = ttk.LabelFrame(f, text="남기는 기록", padding=6)
        rec.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.use_cache = tk.BooleanVar(value=True)
        ttk.Checkbutton(rec, text="해시 캐시 사용 (다시 검사할 때 빨라지지만, 검사한 파일의 경로가 저장됨)",
                        variable=self.use_cache).pack(side="left")
        self.clear_btn = ttk.Button(rec, text="저장된 기록 지우기…", command=self._clear_records)
        self.clear_btn.pack(side="right")
        ttk.Label(rec, text=f"저장 위치: {self.app_dir}", foreground="#666").pack(side="right", padx=8)
        f.rowconfigure(1, weight=1)
        f.rowconfigure(3, weight=1)
        self._mode_changed()

    def _build_results(self):
        f = ttk.Frame(self.nb, padding=(10, 8))
        self.nb.add(f, text="결과")
        self.banner = ttk.Label(f, text="아직 검사하지 않았습니다.", foreground="#333")
        self.banner.pack(fill="x")
        self.summary = ttk.Label(f, text="")
        self.summary.pack(fill="x", pady=(2, 6))

        tools = ttk.Frame(f)
        tools.pack(fill="x", pady=(0, 6))
        ttk.Button(tools, text="자동 선택 다시 적용", command=self._reapply_auto).pack(side="left")
        ttk.Button(tools, text="모두 펼치기", command=lambda: self._expand_all(True)).pack(side="left", padx=4)
        ttk.Button(tools, text="모두 접기", command=lambda: self._expand_all(False)).pack(side="left")
        ttk.Button(tools, text="결과 CSV로 내보내기…", command=self._export_groups).pack(side="right")

        body = ttk.Panedwindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        cols = [("grp", "묶음", 70, "w", False), ("chk", "처리", 44, "center", False),
                ("path", "경로", 260, "w", True), ("size", "크기", 80, "e", False),
                ("ctime", "생성일", 135, "center", False), ("mtime", "수정일", 135, "center", False),
                ("role", "구분", 44, "center", False)]
        self.table = VirtualTable(body, cols, lambda: len(self.rows), self._row_values, self._row_click,
                                  self._row_select, self._row_double, self.rowheight)
        body.add(self.table, weight=4)
        pv = ttk.LabelFrame(body, text="미리보기", padding=6)
        body.add(pv, weight=1)
        self.pv_img = ttk.Label(pv, anchor="center")
        self.pv_img.pack(fill="x")
        self.pv_text = tk.Text(pv, height=12, width=30, wrap="char", state="disabled", font="TkDefaultFont")
        self.pv_text.pack(fill="both", expand=True, pady=(6, 0))

        acts = ttk.Frame(f)
        acts.pack(fill="x", pady=(8, 0))
        self.act_buttons = [
            ttk.Button(acts, text="선택한 파일 → 휴지통", command=lambda: self._do_action(TRASH)),
            ttk.Button(acts, text="선택한 파일 → 격리 폴더", command=lambda: self._do_action(QUARANTINE)),
            ttk.Button(acts, text="현재 묶음을 무시 목록에 추가", command=self._do_ignore),
        ]
        for b in self.act_buttons:
            b.pack(side="left", padx=(0, 6))
        ttk.Label(acts, text="처리 칸 클릭·스페이스: 선택 바꾸기 · 더블클릭: 펼치기/탐색기",
                  foreground="#666").pack(side="left", padx=8)

    def _build_issues(self):
        f = ttk.Frame(self.nb, padding=(10, 8))
        self.nb.add(f, text="미처리 목록")
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 6))
        self.issue_summary = ttk.Label(top, text="")
        self.issue_summary.pack(side="left")
        ttk.Button(top, text="CSV로 내보내기…", command=self._export_issues).pack(side="right")
        cols = [("path", "경로", 560, "w", True), ("kind", "종류", 60, "center", False),
                ("phase", "단계", 90, "center", False), ("cls", "구분", 60, "center", False),
                ("reason", "사유", 320, "w", True)]
        self.issue_table = VirtualTable(f, cols, lambda: len(self.issues), self._issue_values,
                                        rowheight=self.rowheight)
        self.issue_table.pack(fill="both", expand=True)

    def _build_bottom(self):
        f = ttk.Frame(self, padding=(10, 8))
        f.pack(fill="x")
        self.phase_lbl = ttk.Label(f, text="대기 중", width=16)
        self.phase_lbl.grid(row=0, column=0, sticky="w")
        self.pbar = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.pbar.grid(row=0, column=1, sticky="ew", padx=8)
        self.pct_lbl = ttk.Label(f, text="", width=8)
        self.pct_lbl.grid(row=0, column=2)
        self.start_btn = ttk.Button(f, text="검사 시작", command=self._start_scan)
        self.start_btn.grid(row=0, column=3, padx=(8, 4))
        self.cancel_btn = ttk.Button(f, text="취소", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=4)
        self.detail_lbl = ttk.Label(f, text="", foreground="#555")
        self.detail_lbl.grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))
        f.columnconfigure(1, weight=1)

    # ---- 설정

    def _mode_changed(self):
        self.bases.set_enabled(self.mode.get() == "compare")

    def _ext_mode_changed(self):
        st = "disabled" if self.ext_mode.get() == "all" else "normal"
        self.ext_entry.config(state=st)
        for b in self.ext_buttons:
            b.config(state=st)

    def _add_preset(self, exts: str):
        """버튼의 확장자를 입력란에 덧붙인다(이미 있는 것은 그대로)."""
        self.exts.set(", ".join(parse_exts(self.exts.get() + " " + exts)))

    def _pick_qdir(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self.qdir.set(os.path.normpath(d))

    def _make_config(self) -> ScanConfig:
        def num(var, label, allow_empty):
            s = var.get().strip()
            if not s and allow_empty:
                return None
            try:
                v = float(s)
            except ValueError:
                raise ValueError(f"{label}에는 숫자를 넣으세요.") from None
            if v < 0:
                raise ValueError(f"{label}은 0 이상이어야 합니다.")
            return v
        min_kb = num(self.min_kb, "최소 크기", False)
        max_mb = num(self.max_mb, "최대 크기", True)
        tmin = num(self.time_min, "시간 한도", True)
        limits = Limits(time_limit=tmin * 60 if tmin else None)
        cfg = ScanConfig(
            roots=self.roots.get(), base_roots=self.bases.get(), mode=self.mode.get(),
            exclude_paths=[ln.strip() for ln in self.excl.get("1.0", "end").splitlines() if ln.strip()],
            exclude_dir_names=[s.strip() for s in self.excl_names.get().split(",") if s.strip()],
            min_size=int(min_kb * 1024), max_size=int(max_mb * 1024 * 1024) if max_mb else None,
            app_dir=self.app_dir, quarantine_dir=self.qdir.get().strip() or DEFAULT_QUARANTINE_DIR,
            use_cache=self.use_cache.get(), ext_mode=self.ext_mode.get(),
            extensions=parse_exts(self.exts.get()), limits=limits)
        cfg.validate()
        return cfg

    def _clear_records(self):
        if self.busy:
            messagebox.showinfo("기록 지우기", "검사나 정리 작업이 끝난 뒤에 지울 수 있습니다.", parent=self)
            return
        if not messagebox.askyesno(
                "기록 지우기",
                "다음 기록을 지웁니다. 되돌릴 수 없습니다.\n\n"
                "· 해시 캐시 (검사한 파일의 경로·해시)\n"
                "· 작업 기록 (휴지통·격리 폴더로 옮긴 파일 목록)\n\n"
                "격리 폴더에 옮겨 둔 파일은 지우지 않습니다. 계속할까요?", parent=self):
            return
        include_ignore = False
        if os.path.isfile(os.path.join(self.app_dir, "ignore.json")):
            include_ignore = messagebox.askyesno(
                "기록 지우기", "무시 목록도 지울까요?\n(지우면 무시했던 묶음이 다음 검사에 다시 나타납니다)",
                default="no", parent=self)
        removed, failed = clear_records(self.app_dir, include_ignore)
        msg = f"파일 {len(removed)}개를 지웠습니다." if removed else "지울 기록이 없었습니다."
        if failed:
            messagebox.showwarning("기록 지우기", msg + "\n\n지우지 못한 파일:\n" + "\n".join(failed[:10]), parent=self)
        else:
            messagebox.showinfo("기록 지우기", msg, parent=self)

    # ---- 검사

    def _start_scan(self):
        if self.busy:
            return
        try:
            cfg = self._make_config()
        except ValueError as e:
            messagebox.showwarning("설정 확인", str(e), parent=self)
            return
        self.cfg = cfg
        token = object()  # 이 검사에서 온 소식만 받기 위한 표
        self.scan_token = token
        self.engine = Engine(cfg, on_event=lambda k, d: self.events.put((token, k, d)), interactive=True)
        eng = self.engine
        self._set_busy("scan")
        self.result = None
        self._show_progress(PHASE_WALK, 0.0, "검사를 준비하는 중…")
        threading.Thread(target=self._run_engine, args=(eng, token), daemon=True).start()

    def _run_engine(self, eng, token):
        try:
            eng.run()
        except Exception as e:  # noqa: BLE001
            self.events.put((token, "crash", repr(e)))

    def _cancel(self):
        if self.busy == "scan" and self.engine:
            self.engine.cancel()
            self.cancel_deadline = time.monotonic() + Limits().cancel_response
            self.detail_lbl.config(text="취소하는 중…")
        elif self.busy == "action" and self.runner:
            self.runner.cancel()
            self.detail_lbl.config(text="지금 처리 중인 파일을 마치고 멈춥니다…")
        self.cancel_btn.config(state="disabled")

    def _poll(self):
        try:
            for _ in range(500):
                src, kind, data = self.events.get_nowait()
                if src is not self.scan_token and (src is None or src is not self.runner):
                    continue  # 분리된 이전 작업의 늦은 소식은 무시
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        if self.busy == "scan" and self.cancel_deadline and time.monotonic() > self.cancel_deadline:
            # R5-F1: 2초 안에 엔진이 멈추지 않으면 화면 쪽에서 작업을 분리한다
            self.engine = None
            self.scan_token = None
            self.cancel_deadline = None
            self._set_busy(None)
            self._show_progress("취소됨", 0.0, "엔진이 제때 응답하지 않아 작업을 분리했습니다. 결과는 표시하지 않습니다.")
        self.after(100, self._poll)

    def _handle_event(self, kind, data):
        if kind == "progress":
            self._on_progress(data)
        elif kind == "ask":
            AskDialog(self, data)
        elif kind == "log":
            pass
        elif kind == "done":
            self.cancel_deadline = None
            self._on_done(data)
        elif kind == "crash":
            self.cancel_deadline = None
            self._set_busy(None)
            messagebox.showerror("오류", f"검사 중 예기치 않은 오류: {data}", parent=self)
        elif kind == "action_progress":
            done, total, path = data
            self._show_progress("처리 중", done / total if total else 1.0,
                                f"{done:,}/{total:,}  {self._short(path)}")
        elif kind == "action_done":
            self._on_action_done(*data)

    def _on_progress(self, p):
        ph = p["phase"]
        if ph == PHASE_WALK:
            tot = p["dirs_done"] + p["dirs_pending"]
            frac = p["dirs_done"] / tot if tot else 0.0
            txt = f"폴더 {p['dirs_done']:,}/{tot:,} · 파일 {p['found']:,}개 발견"
        elif ph == PHASE_PARTIAL:
            frac = p["tasks_done"] / p["tasks_total"] if p["tasks_total"] else 1.0
            txt = f"파일 {p['tasks_done']:,}/{p['tasks_total']:,}"
        elif ph == PHASE_FULL:
            frac = p["bytes_done"] / p["bytes_total"] if p["bytes_total"] else 1.0
            txt = (f"파일 {p['tasks_done']:,}/{p['tasks_total']:,} · "
                   f"{fmt_size(p['bytes_done'])}/{fmt_size(p['bytes_total'])}")
        else:
            frac, txt = 1.0, ""
        el = int(p["elapsed"])
        txt += f" · 경과 {el // 60}분 {el % 60:02d}초 · 미처리 {p['issues']:,}건 · 동시 처리 {p['workers']}"
        if p["current"]:
            txt += f" · {self._short(p['current'])}"
        self._show_progress(ph, frac, txt)

    def _show_progress(self, phase, frac, detail):
        self.phase_lbl.config(text=phase)
        self.pbar["value"] = max(0, min(1000, int(frac * 1000)))
        self.pct_lbl.config(text=f"{frac * 100:.0f}%")
        self.detail_lbl.config(text=detail)

    @staticmethod
    def _short(p, n=90):
        return p if len(p) <= n else "…" + p[-(n - 1):]

    def _on_done(self, r: ScanResult):
        self._set_busy(None)
        self.result = r
        self.groups = r.groups
        self.issues = list(r.issues)
        self.expanded = set(range(len(self.groups)))
        self.selected = auto_select(self.groups, self.prefer.get(), r.mode)
        self._rebuild_rows()
        c = r.counts
        mark = "" if r.complete else "  ⚠ 검사가 끝나지 않은 결과입니다."
        self.banner.config(text=f"종료 사유: {r.reason.value} — {r.detail}{mark}",
                           foreground="#333" if r.complete else "#a33")
        self._show_progress(r.reason.value, 1.0 if r.complete else self.pbar["value"] / 1000,
                            f"찾은 파일 {c['found']:,} = 처리 {c['processed']:,} + 건너뜀 {c['skipped']:,} + 실패 "
                            f"{c['failed']:,} + 미처리 {c['unprocessed']:,} · 하드링크 {c['hardlinks']:,} · 캐시 사용 "
                            f"{c['cache_hits']:,} · 시작 후 생성되어 제외 {c['created_after']:,} · "
                            + (f"확장자로 제외 {c['ext_filtered']:,} · " if c['ext_filtered'] else "") +
                            f"{int(r.elapsed)}초")
        self._refresh_issue_summary()
        self.nb.select(1)
        if r.reason == Reason.LIMIT or r.reason == Reason.ERROR or r.reason == Reason.ENVIRONMENT:
            messagebox.showwarning("검사 중단", f"{r.reason.value}: {r.detail}\n\n그때까지 확인된 중복 묶음은 결과 탭에,"
                                               " 검사하지 못한 항목은 '미처리 목록' 탭에 있습니다.", parent=self)

    def _set_busy(self, what):
        self.busy = what
        self.start_btn.config(state="disabled" if what else "normal")
        self.cancel_btn.config(state="normal" if what else "disabled")
        for b in self.act_buttons + [self.clear_btn]:
            b.config(state="disabled" if what else "normal")

    # ---- 결과 표

    def _rebuild_rows(self):
        rows = []
        for gi, g in enumerate(self.groups):
            rows.append((gi, -1))
            if gi in self.expanded:
                rows.extend((gi, fi) for fi in range(len(g.files)))
        self.rows = rows
        self.table.refresh()
        self._refresh_summary()

    def _row_values(self, i):
        gi, fi = self.rows[i]
        g = self.groups[gi]
        if fi < 0:
            arrow = "▼" if gi in self.expanded else "▶"
            return ((f"{arrow} {gi + 1}", "", f"파일 {len(g.files)}개 · 확보 가능 {fmt_size(g.reclaimable)}",
                     fmt_size(g.size), "", "", ""), ("grp",))
        e = g.files[fi]
        chk = "🔒" if e.is_base else ("☑" if e.path in self.selected else "☐")
        width = int(self.table.tree.column("path", "width"))
        shown = self._short(e.path, max(20, width // max(1, self.char_px)))  # 파일 이름이 보이게 앞쪽을 줄임
        return (("", chk, shown, fmt_size(e.size), fmt_time(e.ctime), fmt_time(e.mtime_ns / 1e9),
                 "기준" if e.is_base else ("대상" if self.result and self.result.mode == "compare" else "")),
                ("base",) if e.is_base else ())

    def _row_click(self, i, col):
        gi, fi = self.rows[i]
        if fi < 0:
            if col in ("grp", "chk"):
                self._toggle_group(gi)
            return
        if col == "chk":
            self._toggle_file(gi, fi)

    def _row_double(self, i):
        gi, fi = self.rows[i]
        if fi < 0:
            self._toggle_group(gi)
        else:
            p = self.groups[gi].files[fi].path
            subprocess.Popen(["explorer", "/select,", p])

    def _toggle_group(self, gi):
        self.expanded.symmetric_difference_update({gi})
        self._rebuild_rows()

    def _toggle_file(self, gi, fi):
        g = self.groups[gi]
        e = g.files[fi]
        if e.is_base:
            self.detail_lbl.config(text="기준 폴더 파일은 처리할 수 없습니다.")
            return
        if e.path in self.selected:
            self.selected.discard(e.path)
        else:
            others = [x for x in g.files if x is not e]
            if all(x.path in self.selected for x in others):  # R4-4
                self.detail_lbl.config(text="묶음마다 최소 1개는 남겨야 합니다.")
                return
            self.selected.add(e.path)
        self._refresh_summary()

    def _expand_all(self, on):
        self.expanded = set(range(len(self.groups))) if on else set()
        self._rebuild_rows()

    def _reapply_auto(self):
        if self.result:
            self.selected = auto_select(self.groups, self.prefer.get(), self.result.mode)
            self.table.refresh()
            self._refresh_summary()

    def _refresh_summary(self):
        copies = sum(len(g.files) - 1 for g in self.groups)
        recl = sum(g.reclaimable for g in self.groups)
        items, _ = plan(self.groups, self.selected)
        n, b = summarize(items)
        self.summary.config(text=f"중복 묶음 {len(self.groups):,}개 · 사본 {copies:,}개 · 확보 가능 {fmt_size(recl)}"
                                 f"    |    선택: {n:,}개 파일 · {fmt_size(b)}")

    def _row_select(self, i):
        gi, fi = self.rows[i]
        if fi >= 0:
            self._preview(self.groups[gi].files[fi].path)

    # ---- 미리보기(R5-I3)

    def _preview(self, path):
        self.preview_token += 1
        tok = self.preview_token
        ext = os.path.splitext(path)[1].lower()
        self._set_preview(None, "불러오는 중…")
        box: dict = {}

        def work():
            try:
                if ext in IMAGE_EXT and Image is not None:
                    im = Image.open(long_path(path))
                    im.thumbnail((260, 260))
                    box["img"] = im.convert("RGB") if im.mode not in ("RGB", "RGBA") else im.copy()
                elif ext in TEXT_EXT:
                    with open(long_path(path), "rb") as fh:
                        raw = fh.read(4096)
                    try:
                        box["text"] = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        box["text"] = raw.decode("cp949", errors="replace")
                else:
                    box["text"] = "미리보기를 지원하지 않는 형식입니다."
            except Exception as ex:  # noqa: BLE001
                box["text"] = f"미리보기 불가: {ex}"
        threading.Thread(target=work, daemon=True).start()
        deadline = time.monotonic() + Limits().preview_timeout

        def check():
            if tok != self.preview_token:
                return
            if "img" in box:
                self.preview_img = ImageTk.PhotoImage(box["img"])
                self._set_preview(self.preview_img, path)
            elif "text" in box:
                self._set_preview(None, box["text"])
            elif time.monotonic() > deadline:
                self._set_preview(None, "미리보기 불가(3초 안에 불러오지 못함)")
            else:
                self.after(100, check)
        self.after(100, check)

    def _set_preview(self, img, text):
        self.pv_img.config(image=img or "")
        self.pv_text.config(state="normal")
        self.pv_text.delete("1.0", "end")
        self.pv_text.insert("1.0", text)
        self.pv_text.config(state="disabled")

    # ---- 관리

    def _do_action(self, action):
        if self.busy or not self.groups:
            return
        items, problems = plan(self.groups, self.selected)
        n, b = summarize(items)
        if n == 0:
            messagebox.showinfo("처리", "선택한 파일이 없습니다.", parent=self)
            return
        qroot = self.qdir.get().strip() or DEFAULT_QUARANTINE_DIR
        where = "휴지통" if action == TRASH else f"격리 폴더({qroot})"
        sample = "\n".join(e.path for _, r, _ in items for e in r[:1])
        sample = "\n".join(sample.splitlines()[:8])
        msg = (f"{n:,}개 파일({fmt_size(b)})을 {where}(으)로 옮깁니다.\n"
               f"묶음마다 원본 1개 이상은 남기며, 실행 직전에 내용을 다시 확인합니다.\n\n예시:\n{sample}")
        if problems:
            msg += f"\n\n건너뛸 묶음 {len(problems)}개(모든 파일이 선택됨)"
        if not messagebox.askyesno("실행 전 확인", msg, parent=self):  # R4-5
            return
        runner = ActionRunner(self.app_dir, Limits(),
                              on_progress=lambda d, t, p: self.events.put((runner, "action_progress", (d, t, p))))
        try:
            runner.preflight(items, action, qroot)
        except ActionError as e:
            messagebox.showwarning("시작하지 않음", str(e), parent=self)
            return
        self.runner = runner
        self._set_busy("action")

        def work():
            try:
                res = runner.run(items, action, qroot)
                err = None
            except Exception as e:  # noqa: BLE001
                res, err = [], str(e)
            self.events.put((runner, "action_done", (action, res, err)))
        threading.Thread(target=work, daemon=True).start()

    def _on_action_done(self, action, results, err):
        self._set_busy(None)
        self.runner = None
        if err:
            messagebox.showerror("처리 오류", err, parent=self)
        ok = {r.path for r in results if r.ok}
        bad = [r for r in results if not r.ok]
        for r in bad:
            self.issues.append(Issue(r.path, "파일", "관리", f"{action}: {r.detail}", True))
        self._drop_paths(ok)
        self._refresh_issue_summary()
        self._show_progress("처리 완료", 1.0, f"{action}: 성공 {len(ok):,}개 · 실패/건너뜀 {len(bad):,}개 "
                                               f"(작업 기록: {os.path.join(self.app_dir, 'logs')})")
        if bad:
            messagebox.showwarning("일부 처리 안 됨", f"성공 {len(ok):,}개, 처리 안 됨 {len(bad):,}개.\n"
                                                  "사유는 '미처리 목록' 탭에서 볼 수 있습니다.", parent=self)

    def _do_ignore(self):
        if self.busy or not self.groups:
            return
        sel = self.table.sel
        if sel is None:
            messagebox.showinfo("무시 목록", "무시할 묶음(또는 그 안의 파일)을 먼저 고르세요.", parent=self)
            return
        gi, _ = self.rows[sel]
        g = self.groups[gi]
        if not messagebox.askyesno("무시 목록에 추가", f"이 묶음(파일 {len(g.files)}개)을 앞으로 결과에서 빼겠습니까?\n"
                                                      f"{g.files[0].path}", parent=self):
            return
        ActionRunner(self.app_dir).ignore([g])
        self.groups.pop(gi)
        self._after_groups_changed()

    def _drop_paths(self, paths):
        if not paths:
            return
        keep = []
        for g in self.groups:
            g.files = [e for e in g.files if e.path not in paths]
            ok = len(g.files) >= 2 and (self.result.mode != "compare" or
                                         (any(e.is_base for e in g.files) and any(not e.is_base for e in g.files)))
            if ok:
                keep.append(g)
        self.groups = keep
        self.selected -= paths
        self._after_groups_changed()

    def _after_groups_changed(self):
        self.expanded = set(range(len(self.groups)))
        valid = {e.path for g in self.groups for e in g.files}
        self.selected &= valid
        self.table.sel = None
        self._rebuild_rows()

    # ---- 미처리 목록

    def _issue_values(self, i):
        x = self.issues[i]
        return (x.path, x.kind, x.phase, "오류" if x.failed else "건너뜀", x.reason), ()

    def _refresh_issue_summary(self):
        nf = sum(1 for x in self.issues if x.failed)
        self.issue_summary.config(text=f"전체 {len(self.issues):,}건 · 오류 {nf:,}건 · 건너뜀 {len(self.issues) - nf:,}건")
        self.issue_table.refresh(keep_sel=False)

    # ---- 내보내기

    def _export_groups(self):
        if not self.groups:
            return
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile="중복파일목록.csv")
        if p:
            export_groups(p, self.groups, self.selected)
            self.detail_lbl.config(text=f"저장함: {p}")

    def _export_issues(self):
        if not self.issues:
            return
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                         initialfile="미처리목록.csv")
        if p:
            export_issues(p, self.issues)
            self.detail_lbl.config(text=f"저장함: {p}")

    # ---- 시작 시 점검

    def _startup_checks(self):
        cleanup_partials(DEFAULT_QUARANTINE_DIR)  # R5-G5
        m = RunMarker(self.app_dir)
        info = m.leftover()
        if info:  # R5-F4
            messagebox.showinfo(
                "지난 검사가 비정상 종료됨",
                f"지난번 검사({info.get('started', '?')})가 끝나기 전에 프로그램이 종료되었습니다.\n"
                "이어서 하기는 지원하지 않습니다. 새로 검사하면 이미 계산한 해시는 캐시에서 다시 씁니다.", parent=self)
            m.finish()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    App().mainloop()
