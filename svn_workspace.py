import json
import locale
import os
import re
import shutil
import tempfile
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import quote, unquote
from xml.etree import ElementTree


def svn(directory, *args):
    environment = dict(os.environ, SVN_OUTPUT_ENCODING="UTF-8")
    result = subprocess.run(
        ["svn", "--non-interactive", *args], cwd=directory, env=environment,
        capture_output=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"SVN 退出码 {result.returncode}")
    return result.stdout


def target(path):
    # An empty peg revision preserves filenames containing @.
    return path + "@"


STATE_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SVN Workspace" / "workspaces.json"

LABELS = {"modified": "已修改", "added": "已添加", "deleted": "已删除",
          "replaced": "已替换", "unversioned": "未跟踪", "missing": "缺失",
          "conflicted": "冲突", "obstructed": "受阻", "incomplete": "不完整"}
CHANGE_COLORS = {"added": "#86e5a7", "modified": "#f5d477", "deleted": "#ffa0a9",
                 "renamed": "#90c4ff", "replaced": "#f5d477"}


VIEW_MODES = ("目录树", "文件列表")
CHANGE_ORDER = ("added", "modified", "deleted", "renamed", "replaced", "conflicted",
                "unversioned", "missing", "obstructed", "incomplete")


def read_status(directory):
    xml = ElementTree.fromstring(svn(directory, "status", "--xml", "--ignore-externals"))
    entries, moves = {}, {}
    for entry in xml.findall("./target/entry") + xml.findall("./changelist/entry"):
        status = entry.find("wc-status")
        path = entry.get("path").replace("\\", "/")
        item, props = status.get("item"), status.get("props")
        conflict = status.get("tree-conflicted") == "true" or props == "conflicted"
        if item == "normal" and props in {"none", "normal"} and not conflict:
            continue
        entries[path] = "conflicted" if conflict else (
            "modified" if item == "normal" and props == "modified" else item
        )
        destination = status.get("moved-to")
        if destination:
            moves[path] = destination.replace("\\", "/")
    return entries, moves


def working_copy_info(directory):
    entry = ElementTree.fromstring(svn(directory, "info", "--xml")).find("entry")
    return entry.findtext("url"), entry.findtext("repository/root")


def full_diff(directory, args, contents=None):
    options = ("--extensions", f"-U {len(contents.splitlines()) + 1}") if contents is not None else ()
    diff = svn(directory, "diff", "--internal-diff", *options, *args)
    if not contents or "\x00" in contents or re.search(r"^@@ ", diff, re.MULTILINE):
        return diff
    if not diff or re.search(r"^## ", diff, re.MULTILINE):
        lines = contents.splitlines()
        if lines:
            context = f"@@ -1,{len(lines)} +1,{len(lines)} @@\n"
            context += "".join(f" {line}\n" for line in lines)
            return context + ("\n" + diff if diff else "")
    return diff


def file_text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode(locale.getencoding(), errors="replace")


def working_diff(directory, path, moved):
    file = Path(directory) / path
    contents = file_text(file.read_bytes()) if file.is_file() else None
    options = () if moved else ("--show-copies-as-adds",)
    return full_diff(directory, (*options, "--", path), contents)


def historical_diff(directory, url, revision, action, kind):
    previous = int(revision) - 1
    contents = None
    if kind == "file" and action in {"M", "R"}:
        contents = svn(directory, "cat", "--", f"{url}@{revision}")
    return full_diff(directory, ("--notice-ancestry", "--show-copies-as-adds",
                                  "--old", f"{url}@{previous}", "--new", f"{url}@{revision}"), contents)


def conflict_info(directory, path):
    root = Path(directory).resolve()
    working = root / path
    entry = ElementTree.fromstring(svn(directory, "info", "--xml", "--", target(path))).find("entry")
    texts = {}
    kinds = set()
    notes = []
    names = {"text": "文件内容冲突", "property": "SVN 属性冲突", "tree": "目录结构冲突"}
    operations = {"update": "更新", "merge": "合并", "switch": "切换"}
    for item in entry.findall("conflict") + entry.findall("tree-conflict"):
        kind = "tree" if item.tag == "tree-conflict" else item.get("type")
        kinds.add(kind)
        operation = item.get("operation", "")
        notes.append(f"{names.get(kind, 'SVN 冲突')} · {operations.get(operation, operation)}")
        if kind == "tree":
            actions = {"add": "新增", "edit": "修改", "delete": "删除", "replace": "替换"}
            reasons = {"edit": "已修改", "delete": "已删除", "missing": "缺失",
                       "obstruction": "路径受阻", "moved-away": "已移走",
                       "moved-here": "有文件移入", "unversioned": "未纳入版本控制"}
            action, reason = item.get("action", ""), item.get("reason", "")
            notes.append(f"传入操作：{actions.get(action, action)}，本地状态：{reasons.get(reason, reason)}")
        for version in item.findall("version"):
            label = "原仓库版本" if version.get("side") == "source-left" else "传入仓库版本"
            repo_path = version.get("path-in-repos") or "/"
            revision = version.get("revision", "?")
            state = "（无此路径）" if version.get("kind") == "none" else ""
            notes.append(f"{label} r{revision} · {repo_path}{state}")
        for field, label in (("prev-base-file", "共同基线"), ("prev-wc-file", "本地版本"),
                             ("cur-base-file", "传入版本"), ("prop-file", "属性冲突详情")):
            value = item.findtext(field)
            if value:
                texts[label] = Path(value) if Path(value).is_absolute() else root / value
    has_incoming = "传入版本" in texts
    if "text" in kinds and "本地版本" not in texts and working.is_file():
        texts["本地版本"] = working
    if working.is_file():
        texts["当前工作文件"] = working

    previews = {}
    for label, file in texts.items():
        try:
            if not file.resolve().is_relative_to(root):
                continue
            data = file.read_bytes()
            content = file_text(data)
            previews[label] = content if content is not None else f"二进制文件 · {len(data):,} 字节\n{file}"
        except OSError as error:
            previews[label] = str(error)
    return kinds, "\n".join(notes), previews, has_incoming


def read_log(directory, workspace_url, path=None, repository_target=None):
    args = ("log", "--xml", "--verbose")
    if path is None:
        args += ("-l", "100")
    revision_range = "HEAD:1"
    log_target = target(workspace_url)
    fallback = None
    if repository_target:
        url, peg = repository_target
        log_target = f"{url}@{peg}"
        fallback = f"{peg}:1"
    elif path is not None:
        info = ElementTree.fromstring(svn(directory, "info", "--xml", "--", target(path))).find("entry")
        url = info.findtext("url")
        revision = info.get("revision", "")
        copied_from = info.findtext("wc-info/copy-from-url")
        if copied_from:
            revision = info.findtext("wc-info/copy-from-rev")
            log_target = f"{copied_from}@{revision}"
            revision_range = f"{revision}:1"
        elif info.findtext("wc-info/schedule") == "add" or not revision.isdigit():
            return {}
        else:
            log_target = f"{url}@{revision}"
            if info.findtext("wc-info/schedule") == "delete":
                revision_range = f"{revision}:1"
            else:
                fallback = f"{revision}:1"

    try:
        output = svn(directory, *args, "-r", revision_range, "--", log_target)
    except RuntimeError as error:
        if not fallback or not re.search(r"E(?:160013|170000|195012|200009)", str(error)):
            raise
        output = svn(directory, *args, "-r", fallback, "--", log_target)
    xml = ElementTree.fromstring(output)
    return {entry.get("revision"): entry for entry in xml.findall("logentry")}


def incoming_revision_count(directory):
    entry = ElementTree.fromstring(svn(directory, "info", "--xml")).find("entry")
    base = int(entry.get("revision"))
    url = entry.findtext("url")
    output = svn(directory, "log", "--xml", "--quiet", "-r", f"HEAD:{base}", "--", target(url))
    log = ElementTree.fromstring(output)
    return sum(int(revision.get("revision")) > base for revision in log.findall("logentry"))


def history_merge_args(directory, path, revisions, restore, expected_url=None):
    entry = ElementTree.fromstring(svn(directory, "info", "--xml", "--", target(path))).find("entry")
    url = entry.findtext("url")
    root = entry.findtext("wc-info/wcroot-abspath")
    if not root or Path(root).resolve() != Path(directory).resolve():
        raise RuntimeError("目标路径不属于当前工作副本。")
    if expected_url and unquote(url).rstrip("/") != unquote(expected_url).rstrip("/"):
        raise RuntimeError("目标路径的仓库地址与历史记录不同，可能已切换分支。")
    revision = entry.get("revision", "")
    if not revision.isdigit() or entry.findtext("wc-info/schedule") not in (None, "normal"):
        raise RuntimeError("目标必须是当前工作副本中已有的版本化路径。")
    base = int(revision)
    if max(revisions) > base:
        raise RuntimeError(f"目标当前在 r{base}。请先拉取更新，再回滚较新的版本。")
    local = Path(directory) / path
    is_file = entry.get("kind") == "file"
    cwd = local.parent if is_file else local
    if is_file and os.path.normcase(local.name) != os.path.normcase(unquote(url.rsplit("/", 1)[-1])):
        raise RuntimeError("本地文件名与仓库文件名不同，请从其所在目录的历史回滚。")
    if restore and revisions[0] == base:
        return cwd, ()
    selection = ("-r", f"{base}:{revisions[0]}") if restore else (
        "-c", ",".join(f"-{revision}" for revision in revisions)
    )
    args = ("merge", "--accept", "postpone", *selection, "--", f"{url}@{base}")
    # SVN infers the matching file in its parent directory, including names with @.
    return cwd, args if is_file else (*args, ".")


def log_date(value):
    if not value:
        return "未记录"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


class Workspace(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SVN 工作台")
        self.geometry("1120x720")
        self.minsize(780, 540)
        self.configure(bg="#172433")
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background="#172433", foreground="#e1eaf3",
                        fieldbackground="#101925", bordercolor="#344558",
                        lightcolor="#344558", darkcolor="#344558",
                        insertcolor="#e1eaf3", troughcolor="#101925")
        style.configure("TButton", background="#26384c", padding=(8, 5))
        style.map("TButton", background=[("active", "#35506c")],
                  foreground=[("disabled", "#8190a0")])
        style.configure("TNotebook.Tab", background="#26384c", padding=(14, 5))
        style.map("TNotebook.Tab", background=[("selected", "#295377"), ("active", "#35506c")])
        style.configure("Treeview", background="#101925", fieldbackground="#101925",
                        rowheight=26)
        style.configure("Directory.Treeview", indent=28)
        style.configure("Treeview.Heading", background="#26384c")
        style.map("Treeview", background=[("selected", "#295377")])
        self.directory = None
        self.workspaces = {}
        self.checked = set()
        self.directory_filter = ()
        self.change_sort = "path"
        self.change_reverse = False
        self.entries = {}
        self.moves = {}
        self.repository_url = ""
        self.repository_root = ""
        self.status_ready = False
        self.hunks = []
        self.hunk_index = -1
        self.diff_content = ""
        self.color_diff = False
        self.jobs = queue.Queue()
        self.running = False
        self.reading_diff = False
        self.pending_diff = None
        self.pending_history = None
        self.pending_history_rollback = None
        self.pending_conflict = None
        self.pending_focus_refresh = False
        self.window_active = None
        self.conflict_path = None
        self.buttons = []

        header = tk.Frame(self, bg="#172433", padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="SVN 工作台", bg="#172433", fg="white",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.location = tk.StringVar(value="请选择 SVN 工作副本")
        self.workspace_picker = ttk.Combobox(header, state="readonly", width=44)
        self.workspace_picker.set("请选择 SVN 工作副本")
        self.workspace_picker.pack(side="left", padx=(16, 0), fill="x", expand=True)
        self.workspace_picker.bind("<<ComboboxSelected>>", self.switch_workspace)
        self.rename_button = ttk.Button(header, text="重命名", command=self.rename_workspace)
        self.rename_button.pack(side="left", padx=(8, 0))
        location_bar = ttk.Frame(self, padding=(18, 4))
        location_bar.pack(fill="x")
        self.metadata_button = ttk.Button(location_bar, text="打开 .svn", command=self.open_metadata)
        self.metadata_button.pack(side="right", padx=(8, 0))
        ttk.Label(location_bar, textvariable=self.location).pack(side="left", fill="x", expand=True)

        toolbar = ttk.Frame(self, padding=(12, 8))
        toolbar.pack(fill="x")
        self.last_refresh_label = tk.StringVar(value="上次刷新：—")
        self.incoming_label = tk.StringVar(value="待拉取：—")
        ttk.Label(toolbar, textvariable=self.last_refresh_label).pack(side="right", padx=(8, 0))
        for name, callback in [
            ("打开工作区", self.open_directory), ("刷新", self.refresh),
            ("拉取更新", self.update_copy), ("检查更新", self.check_updates),
            ("提交历史", self.history),
            ("加入版本控制", self.add_files),
        ]:
            button = ttk.Button(toolbar, text=name, command=callback)
            button.pack(side="left", padx=(0, 6))
            self.buttons.append(button)
            if callback == self.check_updates:
                ttk.Label(toolbar, textvariable=self.incoming_label).pack(side="left", padx=(0, 8))

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, padding=(12, 4))
        right = ttk.Frame(panes, padding=(4, 4, 12, 4))
        panes.add(left, weight=2)
        panes.add(right, weight=4)

        self.pages = ttk.Notebook(left)
        self.pages.pack(fill="both", expand=True)
        self.changes_page = ttk.Frame(self.pages)
        self.history_page = ttk.Frame(self.pages, padding=6)
        self.pages.add(self.changes_page, text="变更")
        self.pages.add(self.history_page, text="历史")
        self.conflict_page = ttk.Frame(self.pages, padding=6)
        self.pages.add(self.conflict_page, text="冲突 (0)")
        self.build_history()
        self.build_conflict_list()

        lists = ttk.Panedwindow(self.changes_page, orient="vertical")
        lists.pack(fill="both", expand=True)
        staged = ttk.Frame(lists)
        changed = ttk.Frame(lists)
        lists.add(changed, weight=2)
        lists.add(staged, weight=1)

        def size_change_list(event):
            if event.height > 1:
                lists.unbind("<Configure>")
                lists.after_idle(lambda: lists.sashpos(0, int(lists.winfo_height() * 0.57)))

        lists.bind("<Configure>", size_change_list)
        self.staged_title = tk.StringVar(value="待提交 (0)")
        ttk.Label(staged, textvariable=self.staged_title).pack(anchor="w", pady=5)
        commit_bar = ttk.Frame(staged)
        commit_bar.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Label(commit_bar, text="提交说明").pack(anchor="w")
        self.message = self.make_text(commit_bar, width=30, height=3, wrap="word")
        self.message.pack(fill="x", pady=4)
        self.commit_button = ttk.Button(commit_bar, text="提交勾选项", command=self.commit)
        self.commit_button.pack(anchor="e")
        self.staged_tree = self.make_tree(staged, "移出")
        change_header = ttk.Frame(changed)
        change_header.pack(fill="x", pady=(0, 4))
        ttk.Label(change_header, text="全部修改").pack(side="left")
        self.view_mode = tk.StringVar(value=VIEW_MODES[0])

        search_row = ttk.Frame(changed)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="搜索").pack(side="left", padx=(0, 6))
        self.clear_directory_button = ttk.Button(
            search_row, text="显示全部", command=lambda: self.set_directory_filter(()))
        self.clear_directory_button.pack(side="right", padx=(4, 0))
        self.directory_filter_label = tk.StringVar(value="筛选目录")
        self.directory_filter_button = ttk.Button(
            search_row, textvariable=self.directory_filter_label, command=self.choose_directories)
        self.directory_filter_button.pack(side="right", padx=(4, 0))
        self.search = tk.StringVar()
        ttk.Button(search_row, text="清除", command=lambda: self.search.set("")).pack(side="right", padx=(4, 0))
        ttk.Entry(search_row, textvariable=self.search, width=6).pack(side="left", fill="x", expand=True)
        self.tree = self.make_tree(changed, "提交", folders=True)
        self.configure_change_view()
        self.search.trace_add("write", lambda *_: self.draw_tree(self.tree))

        self.preview_area = ttk.Frame(right)
        self.preview_area.pack(fill="both", expand=True)
        self.preview_title = tk.StringVar(value="差异预览")
        preview_toolbar = ttk.Frame(self.preview_area)
        preview_toolbar.pack(fill="x")
        ttk.Button(preview_toolbar, text="下一处", command=lambda: self.jump_diff(1)).pack(side="right")
        ttk.Button(preview_toolbar, text="上一处", command=lambda: self.jump_diff(-1)).pack(side="right")
        self.split_diff = tk.BooleanVar(value=True)
        ttk.Checkbutton(preview_toolbar, text="并排", variable=self.split_diff,
                        command=self.paint_diff).pack(side="right", padx=4)
        ttk.Label(preview_toolbar, textvariable=self.preview_title).pack(side="left", fill="x", expand=True)

        self.single_frame = ttk.Frame(self.preview_area)
        self.single_frame.pack(fill="both", expand=True)
        self.output = self.make_text(self.single_frame)
        self.pack_output(self.single_frame, self.output)
        self.split_frame = ttk.Panedwindow(self.preview_area, orient="horizontal")
        self.split_outputs = []
        for label in ("修改前", "修改后"):
            pane = ttk.Frame(self.split_frame)
            self.split_frame.add(pane, weight=1)
            ttk.Label(pane, text=label).pack(anchor="w")
            output = self.make_text(pane)
            self.pack_output(pane, output, self.scroll_split)
            self.split_outputs.append(output)
        for output in (self.output, *self.split_outputs):
            output.configure(state="disabled")
            output.tag_configure("added", foreground="#9ee7b5", background="#203d33")
            output.tag_configure("removed", foreground="#ffb4b9", background="#482d39")
            output.tag_configure("hunk", foreground="#94c7ff")
        for output in self.split_outputs:
            output.bind("<MouseWheel>", self.wheel_split)

        self.build_conflict_panel(right)
        self.status = tk.StringVar(value="就绪")
        self.pages.bind("<<NotebookTabChanged>>", self.page_changed)
        ttk.Label(self, textvariable=self.status, padding=(12, 6)).pack(fill="x")
        self.set_buttons()
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.poll_timer = self.after(80, self.poll)
        self.restore_workspaces()

    def make_text(self, parent, **options):
        options.setdefault("wrap", "none")
        return tk.Text(parent, bg="#101925", fg="#e1eaf3", insertbackground="#e1eaf3",
                       selectbackground="#295377", relief="flat",
                       padx=8, pady=6, **options)

    def pack_output(self, frame, output, yview=None):
        vertical = ttk.Scrollbar(frame, orient="vertical", command=yview or output.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=output.xview)
        output.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set,
                         font=("Consolas", 11))
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        output.pack(fill="both", expand=True)

    def check_window_focus(self):
        active = self.focus_displayof() is not None
        if active and self.window_active is False and self.directory and not self.grab_current():
            self.pending_focus_refresh = True
        self.window_active = active

    def page_changed(self, event=None):
        page = self.pages.select()
        if page == str(self.conflict_page):
            self.preview_area.pack_forget()
            self.conflict_panel.pack(fill="both", expand=True)
            if not self.conflict_list.selection():
                items = self.conflict_list.get_children()
                if items:
                    self.conflict_list.selection_set(items[0])
            else:
                self.conflict_selected()
        else:
            self.conflict_panel.pack_forget()
            self.preview_area.pack(fill="both", expand=True)
            if page == str(self.history_page) and self.repository_url and not self.history_loaded:
                if not self.running or self.reading_diff:
                    self.history()

    def set_buttons(self):
        self.directory_filter_button.configure(state="normal" if self.status_ready else "disabled")
        self.clear_directory_button.configure(state="normal" if self.directory_filter else "disabled")
        self.metadata_button.configure(
            state="normal" if self.directory and (Path(self.directory) / ".svn").is_dir() else "disabled")
        self.workspace_picker.configure(state="disabled" if self.running else "readonly")
        self.rename_button.configure(state="normal" if self.directory and not self.running else "disabled")
        self.commit_button.configure(
            state="normal" if self.status_ready and self.checked and not self.running
            and all(self.can_commit(path) for path in self.checked) else "disabled")
        for index, button in enumerate(self.buttons):
            enabled = not self.running and (index == 0 or self.directory)
            button.configure(state="normal" if enabled else "disabled")

    def run(self, description, action, done, on_error=None, *, reading_diff=False):
        if self.running:
            return
        self.running = True
        self.reading_diff = reading_diff
        self.status.set(description)
        self.set_buttons()

        def worker():
            try:
                self.jobs.put((done, on_error, action(), None))
            except Exception as error:
                self.jobs.put((done, on_error, None, str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            done, on_error, value, error = self.jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            self.running = False
            self.reading_diff = False
            self.set_buttons()
            self.status.set(f"操作失败 · {error}" if error else "就绪")
            if error:
                if on_error:
                    on_error(error)
                else:
                    messagebox.showerror("SVN 操作失败", error, parent=self)
            else:
                done(value)
            if self.pending_history_rollback is not None and not self.running:
                request, self.pending_history_rollback = self.pending_history_rollback, None
                if self.pages.select() == str(self.history_page):
                    self.history_rollback(*request)
            elif self.pending_history is not None and not self.running:
                request, self.pending_history = self.pending_history, None
                if self.pages.select() == str(self.history_page):
                    self.history(*request)
            elif self.pending_conflict is not None and not self.running:
                path, self.pending_conflict = self.pending_conflict, None
                if self.pages.select() == str(self.conflict_page):
                    self.load_conflict(path)
            elif self.pending_diff is not None and not self.running:
                tree, self.pending_diff = self.pending_diff, None
                self.preview(tree)
        self.check_window_focus()
        if self.pending_focus_refresh and self.window_active and not self.running and not self.grab_current():
            self.pending_focus_refresh = False
            self.refresh(keep_output=True, preserve_selection=True)
        self.poll_timer = self.after(80, self.poll)

    def open_directory(self):
        folder = filedialog.askdirectory(parent=self)
        if folder:
            folder = Path(folder)
            if folder.name.casefold() == ".svn":
                folder = folder.parent
            self.run("正在识别工作副本…",
                     lambda: svn(folder, "info", "--show-item", "wc-root").strip(),
                     self.load_directory)

    def open_metadata(self):
        if not self.directory:
            return
        metadata = Path(self.directory) / ".svn"
        try:
            os.startfile(metadata)
        except OSError as error:
            messagebox.showerror("无法打开 .svn", str(error), parent=self)

    def workspace_label(self, directory):
        name = self.workspaces[directory]["name"] or os.path.basename(directory)
        return f"{name or directory}  ·  {directory}"

    def restore_workspaces(self):
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            self.status.set(f"无法读取工作区设置 · {error}")
            return
        if not isinstance(data, dict) or not isinstance(data.get("workspaces"), dict):
            self.status.set("工作区设置格式无效")
            return
        for directory, record in data["workspaces"].items():
            if not os.path.isabs(directory) or not isinstance(record, dict):
                continue
            checked = record.get("checked")
            directories = record.get("directories")
            self.workspaces[directory] = {
                key: record.get(key) if isinstance(record.get(key), str) else ""
                for key in ("name", "query", "draft", "last_refresh", "view_mode", "sort_column")
            }
            self.workspaces[directory]["checked"] = {
                path for path in checked if isinstance(path, str)
            } if isinstance(checked, list) else set()
            self.workspaces[directory]["sort_reverse"] = record.get("sort_reverse") is True
            self.workspaces[directory]["directories"] = [
                path for path in directories if isinstance(path, str)
            ] if isinstance(directories, list) else []
        self.update_workspace_picker()
        last = data.get("active")
        if isinstance(last, str) and last in self.workspaces:
            if os.path.isdir(last):
                self.load_directory(last)
            else:
                self.status.set(f"上次打开的目录不可用 · {last}")

    def save_workspaces(self):
        if self.directory:
            self.workspaces[self.directory].update(
                checked=self.checked, query=self.search.get(), draft=self.message.get("1.0", "end-1c"),
                directories=list(self.directory_filter), view_mode=self.view_mode.get(),
                sort_column=self.change_sort, sort_reverse=self.change_reverse)
        records = {root: dict(record, checked=sorted(record["checked"]))
                   for root, record in self.workspaces.items()}
        temporary = None
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=STATE_PATH.parent) as stream:
                temporary = Path(stream.name)
                json.dump({"active": self.directory, "workspaces": records}, stream, ensure_ascii=False)
            temporary.replace(STATE_PATH)
        except OSError as error:
            self.status.set(f"无法保存工作区设置 · {error}")
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def close_window(self):
        if self.running:
            if not messagebox.askokcancel("退出", "SVN 操作仍在进行。确定退出？", parent=self):
                return
        self.save_workspaces()
        self.after_cancel(self.poll_timer)
        self.destroy()

    def update_workspace_picker(self):
        self.workspace_picker.configure(values=[self.workspace_label(root) for root in self.workspaces])
        if self.directory:
            self.workspace_picker.set(self.workspace_label(self.directory))

    def rename_workspace(self):
        name = simpledialog.askstring("工作区别名", "输入名称，留空使用目录名。",
                                      initialvalue=self.workspaces[self.directory]["name"], parent=self)
        if name is not None:
            self.workspaces[self.directory]["name"] = name.strip()
            self.update_workspace_picker()
            self.save_workspaces()

    def switch_workspace(self, event=None):
        index = self.workspace_picker.current()
        if not self.running and index >= 0:
            self.load_directory(list(self.workspaces)[index])

    def load_directory(self, directory):
        directory = os.path.normpath(directory)
        key = os.path.normcase(directory)
        directory = next((root for root in self.workspaces if os.path.normcase(root) == key),
                         directory)
        if self.directory == directory:
            self.refresh()
            return
        self.save_workspaces()
        self.directory = directory
        record = self.workspaces.setdefault(directory, dict(name="", checked=set(), query="", draft=""))
        self.checked = record["checked"]
        self.display_refresh_time(record.get("last_refresh", ""))
        self.incoming_label.set("待拉取：—")
        self.directory_filter = tuple(record.get("directories", ()))
        mode = record.get("view_mode")
        sort_column = record.get("sort_column")
        self.change_sort = sort_column if sort_column in {"path", "state"} else "path"
        self.change_reverse = record.get("sort_reverse") is True
        if mode in {"按文件路径", "按修改类型"}:
            self.change_sort = "state" if mode == "按修改类型" else "path"
            mode = VIEW_MODES[1]
        self.view_mode.set(mode if mode in VIEW_MODES else VIEW_MODES[0])
        self.update_directory_filter_label()
        self.update_workspace_picker()
        self.location.set(directory)
        self.entries = {}
        self.moves = {}
        self.status_ready = False
        self.repository_url = self.repository_root = ""
        self.pending_diff = None
        self.pending_conflict = None
        self.sync_conflicts()
        self.configure_change_view()
        self.search.set(record["query"])
        self.message.delete("1.0", "end")
        self.message.insert("1.0", record["draft"])
        self.clear_history()
        self.pages.select(0)
        self.draw_tree(self.staged_tree)
        self.count()
        self.render("差异预览", "")
        self.refresh()
        self.save_workspaces()

    def refresh(self, keep_output=False, preserve_selection=False):
        if self.running or not self.directory:
            return
        directory = self.directory
        self.status_ready = False

        def read():
            return read_status(directory), working_copy_info(directory)

        def show(result):
            (entries, moves), (self.repository_url, self.repository_root) = result
            self.moves = moves
            self.location.set(f"{directory}  ·  {self.repository_url}")
            self.show_status(entries, keep_output=keep_output, preserve_selection=preserve_selection)
            self.mark_refreshed()

        self.run("正在读取变更…", read, show)

    def display_refresh_time(self, value):
        self.last_refresh_label.set(f"上次刷新：{value or '—'}")

    def mark_refreshed(self):
        value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.workspaces[self.directory]["last_refresh"] = value
        self.display_refresh_time(value)
        self.save_workspaces()

    def show_status(self, entries, keep_output=False, preserve_selection=False):
        self.entries = entries
        self.status_ready = True
        self.sync_conflicts()
        self.checked.intersection_update(entries)
        for source, destination in self.moves.items():
            if self.checked.intersection({source, destination}):
                self.checked.update(path for path in (source, destination) if path in entries)
        if not keep_output:
            self.render("差异预览", "")
        active_tree = None
        for tree in (self.tree, self.staged_tree):
            if not preserve_selection:
                tree.selection_remove(tree.selection())
            self.draw_tree(tree)
            if preserve_selection and self.preview_title.get() in tree.selection():
                active_tree = tree
        self.count()
        if active_tree is not None:
            self.preview(active_tree)

    def make_tree(self, frame, action, folders=False):
        columns = ("check", "state", "path")
        tree = ttk.Treeview(frame, columns=columns, height=6 if folders else 3,
                            show="tree headings" if folders else "headings")
        if folders:
            tree.configure(style="Directory.Treeview", displaycolumns=("check", "state"))
            tree.heading("#0", text="文件 / 目录")
            tree.column("#0", width=230)
        for key, name, width in [("check", action, 50), ("state", "状态", 105), ("path", "路径", 230)]:
            if key not in columns:
                continue
            tree.heading(key, text=name)
            tree.column(key, width=width, stretch=(key == "path"),
                        anchor="center" if key == "check" else "w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        if folders:
            horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
            tree.configure(xscrollcommand=horizontal.set)
            horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        for change, color in CHANGE_COLORS.items():
            tree.tag_configure(change, foreground=color)
        tree.bind("<Button-1>", lambda event: self.toggle(event, tree))
        tree.bind("<Double-Button-1>", self.change_stage_on_double_click)
        tree.bind("<<TreeviewSelect>>", lambda event: self.preview(tree))
        tree.bind("<Button-3>", lambda event: self.file_menu(event, tree))
        tree.bind("<space>", lambda event: self.toggle_selection(tree))
        return tree

    def change_view_mode(self):
        mode = VIEW_MODES[1] if self.view_mode.get() == VIEW_MODES[0] else VIEW_MODES[0]
        self.view_mode.set(mode)
        self.configure_change_view()
        self.draw_tree(self.tree, reveal_directories=True)
        self.save_workspaces()

    def configure_change_view(self):
        self.update_sort_headings()
        self.tree.xview_moveto(0)

    def update_sort_headings(self):
        flat = self.view_mode.get() == VIEW_MODES[1]
        path_heading = "文件路径" if flat else "文件 / 目录"
        state_heading = "修改类型" if flat else "状态"
        if flat:
            direction = " ▼" if self.change_reverse else " ▲"
            if self.change_sort == "path":
                path_heading += direction
            else:
                state_heading += direction
        self.tree.heading("#0", text=path_heading + "  ⇄", command=self.change_view_mode)
        self.tree.heading("state", text=state_heading,
                          command=(lambda: self.sort_changes("state")) if flat else "")

    def sort_changes(self, column):
        self.change_reverse = self.change_sort == column and not self.change_reverse
        self.change_sort = column
        self.update_sort_headings()
        self.draw_tree(self.tree)
        self.save_workspaces()

    def update_directory_filter_label(self):
        count = len(self.directory_filter)
        self.directory_filter_label.set(f"筛选目录 ({count})" if count else "筛选目录")

    def set_directory_filter(self, directories):
        self.directory_filter = tuple(sorted(set(directories), key=str.casefold))
        self.update_directory_filter_label()
        self.draw_tree(self.tree, reveal_directories=True)
        self.set_buttons()
        self.save_workspaces()

    def choose_directories(self):
        directories = set(self.directory_filter)
        root = Path(self.directory)
        for path in self.entries:
            parts = path.split("/")
            for index in range(1, len(parts)):
                directories.add("/".join(parts[:index]))
            if path != "." and (root / path).is_dir():
                directories.add(path)
        if not directories:
            self.status.set("当前没有可筛选的变更目录。")
            return
        choices = sorted(directories, key=str.casefold)
        dialog = tk.Toplevel(self, bg="#172433")
        dialog.title("筛选目录")
        dialog.transient(self)
        ttk.Label(dialog, text="点击选择一个或多个目录，所选目录包含其子目录。",
                  padding=8).pack(anchor="w")
        frame = ttk.Frame(dialog, padding=(8, 0))
        frame.pack(fill="both", expand=True)
        listing = tk.Listbox(frame, width=54, height=min(len(choices), 14),
                             selectmode="multiple", exportselection=False,
                             bg="#101925", fg="#e1eaf3", selectbackground="#295377")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        listing.pack(side="left", fill="both", expand=True)
        for index, path in enumerate(choices):
            listing.insert("end", path)
            if path in self.directory_filter:
                listing.selection_set(index)

        def apply():
            self.set_directory_filter(choices[index] for index in listing.curselection())
            dialog.destroy()

        controls = ttk.Frame(dialog, padding=8)
        controls.pack(fill="x")
        ttk.Button(controls, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(controls, text="应用", command=apply).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.wait_visibility()
        dialog.grab_set()
        listing.focus_set()

    def in_directory_filter(self, path):
        return not self.directory_filter or any(
            path == folder or path.startswith(folder + "/") for folder in self.directory_filter
        )

    def draw_tree(self, tree, reveal_directories=False):
        staged = tree is self.staged_tree
        mode = self.view_mode.get()
        nested = not staged and mode == VIEW_MODES[0]
        query = self.search.get().strip().casefold()
        sources = {destination: source for source, destination in self.moves.items()
                   if self.can_commit(source) and self.can_commit(destination)}
        hidden = set(sources.values())
        locations = {}
        for path in self.entries:
            if path in hidden:
                continue
            if staged:
                if path in self.checked:
                    locations[path] = path
                continue
            source = sources.get(path)
            if query not in path.casefold() and (not source or query not in source.casefold()):
                continue
            if self.in_directory_filter(path):
                locations[path] = path
            elif source and self.in_directory_filter(source):
                locations[path] = source
        nodes_by_location = {location: path for path, location in locations.items()}
        reverse_paths = not staged and not nested and self.change_sort == "path" and self.change_reverse
        paths = sorted(locations, key=lambda path: locations[path].casefold(), reverse=reverse_paths)
        if not staged and not nested and self.change_sort == "state":
            order = {change: index for index, change in enumerate(CHANGE_ORDER)}
            paths.sort(key=lambda path: order.get("renamed" if path in sources else self.entries[path], len(order)),
                       reverse=self.change_reverse)
        keep = set()
        for index, path in enumerate(paths):
            location = locations[path]
            parent = ""
            if nested:
                parts = location.split("/")
                for depth, part in enumerate(parts[:-1], 1):
                    prefix = "/".join(parts[:depth])
                    folder = nodes_by_location.get(prefix, f"/{prefix}")
                    selected_branch = any(folder == prefix or folder.startswith(prefix + "/")
                                          for folder in self.directory_filter)
                    keep.add(folder)
                    if not tree.exists(folder):
                        tree.insert(parent, "end", iid=folder, text=part, open=bool(query) or selected_branch)
                    else:
                        if tree.parent(folder) != parent:
                            tree.move(folder, parent, "end")
                        if query or (reveal_directories and selected_branch):
                            tree.item(folder, open=True)
                    parent = folder
            source = sources.get(path)
            label = f"{source} → {path}" if source else path
            change = "renamed" if source else self.entries[path]
            state = "已重命名 / 移动" if source else LABELS.get(change, change)
            tags = (change,) if change in CHANGE_COLORS else ()
            box = "×" if staged else ("☑" if path in self.checked else "☐")
            values = (box if staged or self.can_commit(path) or path in self.checked else "·", state, label)
            name = label if source or not nested else path.rsplit("/", 1)[-1]
            keep.add(path)
            position = "end" if nested else index
            if tree.exists(path):
                tree.item(path, text=name, values=values, tags=tags)
                if not nested or tree.parent(path) != parent:
                    tree.move(path, parent, position)
            else:
                tree.insert(parent, position, iid=path, text=name, values=values, tags=tags)

        def prune(parent):
            for row in tree.get_children(parent):
                if row in keep:
                    prune(row)
                else:
                    tree.delete(row)

        prune("")

    def can_commit(self, path):
        return self.entries.get(path) in {"modified", "added", "deleted", "replaced"}

    def count(self):
        hidden = {source for source, dest in self.moves.items()
                  if source in self.checked and dest in self.checked}
        count = len(self.checked - hidden)
        self.staged_title.set(f"待提交 ({count})")
        blocked = sum(not self.can_commit(path) for path in self.checked)
        status = f"{len(self.entries)} 条路径有变更，待提交 {count} 项"
        self.status.set(f"{status}，{blocked} 条待提交路径需要处理" if blocked else status)
        self.set_buttons()

    def commit_group(self, paths):
        group = {path for path in paths if self.can_commit(path)}
        for source, destination in self.moves.items():
            if group.intersection({source, destination}):
                if self.can_commit(source) and self.can_commit(destination):
                    group.update({source, destination})
                else:
                    group.difference_update({source, destination})
        return group

    def set_checked(self, paths, checked):
        if (self.running and not self.reading_diff) or not self.status_ready:
            return
        group = self.commit_group(paths)
        if checked:
            self.checked.update(group)
        else:
            group = set(paths)
            for source, destination in self.moves.items():
                if group.intersection({source, destination}):
                    group.update({source, destination})
            self.checked.difference_update(group)
        self.draw_tree(self.tree)
        self.draw_tree(self.staged_tree)
        self.count()

    def toggle(self, event, tree):
        path = tree.identify_row(event.y)
        if path not in self.entries:
            return
        bounds = tree.bbox(path, "check")
        if not bounds or not bounds[0] <= event.x < bounds[0] + bounds[2]:
            return
        if self.can_commit(path) or path in self.checked:
            self.set_checked([path], path not in self.checked)
        elif self.entries[path] == "unversioned":
            tree.selection_set(path)
            self.status.set("未跟踪文件需先加入版本控制，再勾选提交。")
        return "break"

    def change_stage_on_double_click(self, event):
        tree = event.widget
        if tree.identify_region(event.x, event.y) not in {"tree", "cell"}:
            return
        path = tree.identify_row(event.y)
        if tree is self.staged_tree:
            bounds = tree.bbox(path, "check") if path else ()
            if path in self.checked and not (bounds and bounds[0] <= event.x < bounds[0] + bounds[2]):
                self.set_checked([path], False)
            return "break"
        if not self.can_commit(path) or tree.get_children(path):
            return
        self.set_checked([path], True)
        return "break"

    def toggle_selection(self, tree):
        paths = tree.selection()
        self.set_checked(paths, tree is self.tree and any(path not in self.checked for path in paths))
        return "break"

    def file_menu(self, event, tree):
        if tree is self.tree and tree.identify_region(event.x, event.y) == "heading":
            if tree.identify_column(event.x) == "#0" and self.view_mode.get() == VIEW_MODES[1]:
                menu = tk.Menu(tree, tearoff=False)
                menu.add_command(label="按文件路径排序", command=lambda: self.sort_changes("path"))
                try:
                    menu.tk_popup(event.x_root, event.y_root)
                finally:
                    menu.grab_release()
            return
        if (self.running and not self.reading_diff) or not self.status_ready:
            return
        row = tree.identify_row(event.y)
        if not row:
            return
        paths = tree.selection() if row in tree.selection() else (row,)
        menu = tk.Menu(self, tearoff=False, bg="#172433", fg="#e1eaf3",
                       activebackground="#295377", activeforeground="#ffffff")
        if tree is self.tree and (tree.get_children(row) or row.startswith("/")):
            prefix = row.lstrip("/")
            if not self.in_directory_filter(prefix):
                prefix = next((source for source, destination in self.moves.items()
                               if destination == prefix), prefix)
            menu.add_command(label="只看此目录", command=lambda: self.set_directory_filter([prefix]))
            group = [path for path in self.entries if path == prefix or path.startswith(prefix + "/")]
            menu.add_command(label="勾选此目录的变更", command=lambda: self.set_checked(group, True))
            menu.add_command(label="移出此目录的变更", command=lambda: self.set_checked(group, False))
        elif any(self.can_commit(path) or path in self.checked for path in paths):
            if tree is self.tree:
                menu.add_command(label="勾选所选项", command=lambda: self.set_checked(paths, True))
            menu.add_command(label="移出所选项", command=lambda: self.set_checked(paths, False))
        if any(self.entries.get(path) == "unversioned" for path in paths):
            menu.add_command(label="加入版本控制", command=lambda: self.add_files(paths))
        if row in self.entries and self.entries[row] != "unversioned":
            menu.add_command(label="SVN 查看日志（Show Log）", command=lambda: self.history(row))
            log_index = menu.index("end")
        else:
            log_index = None
        menu.add_command(label="打开所在目录", command=lambda: self.open_location(row.lstrip("/")))
        if self.entries.get(row) == "conflicted":
            menu.add_command(label="打开工作文件", command=lambda: self.open_location(row, open_file=True))
            menu.add_command(label="查看解决方案", command=lambda: self.select_conflict(row))
        rollback = self.rollback_targets(paths)
        if rollback:
            menu.add_command(label=f"回滚所选修改（{len(rollback)} 条路径）…",
                             command=lambda: self.revert(rollback))
        if self.reading_diff:
            if log_index is None:
                menu.destroy()
                return
            for index in range(menu.index("end") + 1):
                if index != log_index:
                    menu.entryconfigure(index, state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def open_location(self, path, open_file=False):
        location = Path(self.directory) / path
        if not open_file:
            while not location.is_dir() and location != location.parent:
                location = location.parent
        try:
            os.startfile(location)
        except OSError as error:
            messagebox.showerror("打开失败", str(error), parent=self)

    def rollback_targets(self, paths):
        eligible = {"modified", "deleted", "missing", "added", "replaced"}
        selected = {path for path in paths if self.entries.get(path) in eligible}
        for source, destination in self.moves.items():
            pair = {source, destination}
            if selected.intersection(pair):
                if all(self.entries.get(path) in eligible for path in pair):
                    selected.update(pair)
                else:
                    selected.difference_update(pair)
        return tuple(sorted(selected))

    def revert(self, paths):
        directory = self.directory
        lines = [f"{LABELS.get(self.entries[path], self.entries[path])}  {path}" for path in paths]
        if not self.confirm_paths(
            lines, "回滚所选修改", "将撤销这些路径的本地 SVN 修改。此操作可能丢失本地改动。",
            "确认回滚",
        ):
            return

        def failed(error):
            self.render("回滚失败", error)
            self.refresh(keep_output=True)

        self.run("正在回滚修改…", lambda: svn(directory, "revert", "--depth", "empty", "--",
                                               *(target(path) for path in paths)),
                 lambda value: self.after_operation("回滚结果", value), failed)

    def build_conflict_list(self):
        self.conflict_summary = tk.StringVar(value="当前没有待处理的冲突。")
        ttk.Label(self.conflict_page, textvariable=self.conflict_summary).pack(anchor="w", pady=(2, 8))
        frame = ttk.Frame(self.conflict_page)
        frame.pack(fill="both", expand=True)
        self.conflict_list = ttk.Treeview(frame, columns=("path",), show="headings", selectmode="browse")
        self.conflict_list.heading("path", text="待解决路径")
        self.conflict_list.column("path", width=295)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.conflict_list.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.conflict_list.xview)
        self.conflict_list.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.conflict_list.pack(fill="both", expand=True)
        self.conflict_list.bind("<<TreeviewSelect>>", self.conflict_selected)

    def build_conflict_panel(self, parent):
        self.conflict_panel = ttk.Frame(parent)
        self.conflict_title = tk.StringVar(value="冲突处理")
        ttk.Label(self.conflict_panel, textvariable=self.conflict_title).pack(anchor="w", pady=(3, 5))
        self.conflict_actions = ttk.Frame(self.conflict_panel)
        self.conflict_actions.pack(side="bottom", fill="x", pady=6)
        self.conflict_versions = ttk.Notebook(self.conflict_panel)
        self.conflict_versions.pack(fill="both", expand=True)

    def sync_conflicts(self):
        paths = sorted(path for path, state in self.entries.items() if state == "conflicted")
        selected = self.conflict_list.selection()
        self.conflict_list.delete(*self.conflict_list.get_children())
        for path in paths:
            self.conflict_list.insert("", "end", iid=path, values=(path,))
        self.pages.tab(self.conflict_page, text=f"冲突 ({len(paths)})")
        self.conflict_summary.set(f"有 {len(paths)} 项冲突需要处理。选择路径查看方案。"
                                  if paths else "当前没有待处理的冲突。")
        self.conflict_path = None
        if self.pages.select() == str(self.conflict_page) and paths:
            path = selected[0] if selected and selected[0] in paths else paths[0]
            self.conflict_list.selection_set(path)
        else:
            self.show_conflict_panel("冲突处理", self.conflict_summary.get())

    def select_conflict(self, path):
        if self.conflict_list.exists(path):
            self.pages.select(self.conflict_page)
            self.conflict_list.selection_set(path)
            self.conflict_list.see(path)

    def conflict_selected(self, event=None):
        selected = self.conflict_list.selection()
        if selected and selected[0] != self.conflict_path and self.pages.select() == str(self.conflict_page):
            self.load_conflict(selected[0])

    def load_conflict(self, path):
        if self.entries.get(path) != "conflicted" or self.conflict_list.selection() != (path,):
            return
        self.conflict_path = path
        self.show_conflict_panel(path, "正在读取冲突详情…")
        if self.running:
            self.pending_conflict = path
            return
        directory = self.directory

        def show(result):
            if self.conflict_list.selection() != (path,):
                return
            kinds, notes, previews, has_incoming = result
            if kinds == {"text"}:
                guidance = ("先比较本地版本、传入版本和共同基线。可保留任一方的完整内容，"
                            "也可编辑当前工作文件，保存合并结果后选择「保留当前文件」。")
            elif "tree" in kinds:
                guidance = ("先核对新增、删除、移动或重命名后的正确路径。可交给 TortoiseSVN "
                            "处理目录结构冲突，或在手动调整并确认目录结构后标记解决。")
            else:
                guidance = ("打开「属性冲突详情」核对各方的属性值，使用 TortoiseSVN 编辑属性。"
                            "确认文件和属性均已整理正确后，再标记解决。")
            self.show_conflict_panel(path, notes + "\n\n" + guidance, previews)
            controls = ttk.Frame(self.conflict_actions)
            controls.pack(side="bottom", fill="x", pady=(0, 5))
            if (Path(directory) / path).is_file():
                ttk.Button(self.conflict_actions, text="打开工作文件",
                           command=lambda: self.open_location(path, open_file=True)).pack(side="left", padx=(0, 6))
            ttk.Button(self.conflict_actions, text="用 TortoiseSVN 处理",
                       command=lambda: self.open_conflict_editor(path)).pack(side="left")
            if kinds == {"text"} and has_incoming:
                for title, option in (("保留本地版本", "mine-full"), ("接受传入版本", "theirs-full")):
                    ttk.Button(controls, text=title, command=lambda option=option: self.resolve_conflict(path, option)
                               ).pack(side="left", padx=(0, 6))
            ttk.Button(controls, text="保留当前文件并标记解决" if kinds == {"text"} else "标记已手动解决",
                       command=lambda: self.resolve_conflict(path, "working")).pack(side="left")

        self.run("正在读取冲突详情…", lambda: conflict_info(directory, path), show,
                 lambda error: self.show_conflict_panel(path, f"无法读取 SVN 冲突详情。\n\n{error}")
                 if self.conflict_list.selection() == (path,) else None)

    def show_conflict_panel(self, title, notes, previews=None):
        self.conflict_title.set(title)
        for pane in self.conflict_versions.winfo_children():
            pane.destroy()
        for control in self.conflict_actions.winfo_children():
            control.destroy()
        for name, value in {"处理方案": notes, **(previews or {})}.items():
            pane = ttk.Frame(self.conflict_versions)
            self.conflict_versions.add(pane, text=name)
            output = self.make_text(pane, wrap="word" if name == "处理方案" else "none")
            self.pack_output(pane, output)
            output.insert("1.0", value)
            output.configure(state="disabled")

    def open_conflict_editor(self, path):
        svn_exe = shutil.which("svn")
        executable = Path(svn_exe).with_name("TortoiseProc.exe") if svn_exe else None
        if not executable or not executable.is_file():
            program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            executable = program_files / "TortoiseSVN" / "bin" / "TortoiseProc.exe"
        if not executable.is_file():
            messagebox.showinfo("打开冲突编辑器", "未找到 TortoiseSVN 冲突编辑器。", parent=self)
            return
        try:
            subprocess.Popen([str(executable), "/command:conflicteditor",
                              f"/path:{Path(self.directory) / path}"])
        except OSError as error:
            messagebox.showerror("打开冲突编辑器失败", str(error), parent=self)
        else:
            self.status.set("在冲突编辑器完成操作后，点击「刷新」重新检查冲突。")

    def resolve_conflict(self, path, option):
        if self.running or self.entries.get(path) != "conflicted":
            return
        messages = {
            "working": "确认已处理内容、属性或路径冲突，并保留当前工作副本的状态？",
            "mine-full": "确认使用更新前的完整本地版本？该文件收到的传入修改将被丢弃。",
            "theirs-full": "确认使用完整传入版本？该文件的本地修改将被丢弃。",
        }
        if not messagebox.askyesno("确认解决冲突", f"{messages[option]}\n\n{path}", parent=self):
            return
        directory = self.directory

        def failed(error):
            messagebox.showerror("解决冲突失败", error, parent=self)
            self.refresh(keep_output=True)

        self.run("正在解决冲突…",
                 lambda: svn(directory, "resolve", "--accept", option, "--depth", "empty", "--", target(path)),
                 lambda value: self.after_operation("冲突处理结果", value), failed)

    def render(self, title, content, color_diff=False):
        self.preview_title.set(title)
        self.diff_content = content
        self.color_diff = color_diff
        self.paint_diff()

    def paint_diff(self):
        split = self.color_diff and self.split_diff.get()
        self.hunks = []
        self.hunk_index = -1
        if split:
            self.single_frame.pack_forget()
            self.split_frame.pack(fill="both", expand=True)
            outputs = self.split_outputs
        else:
            self.split_frame.pack_forget()
            self.single_frame.pack(fill="both", expand=True)
            outputs = [self.output]
        for output in outputs:
            output.configure(state="normal")
            output.delete("1.0", "end")

        if not self.color_diff:
            self.output.insert("end", self.diff_content)
        elif split:
            self.paint_split()
        else:
            in_hunk = False
            in_change = False
            for line in self.diff_content.splitlines(keepends=True):
                tag = ""
                if line.startswith(("@@", "##")):
                    tag, in_hunk = "hunk", True
                elif line.startswith(("Index: ", "Property changes on:")):
                    in_hunk = False
                elif in_hunk:
                    tag = {"-": "removed", "+": "added"}.get(line[:1], "")
                changed = tag in {"removed", "added"}
                if changed and not in_change:
                    self.hunks.append(self.output.index("end-1c"))
                in_change = changed
                self.output.insert("end", line, (tag,))
        for output in outputs:
            output.configure(state="disabled")

    def paint_split(self):
        old, new = self.split_outputs
        removed, added, context = [], [], []

        def flush_context():
            if context:
                text = "".join(context)
                for output in (old, new):
                    output.insert("end", text)
                context.clear()

        def flush_changes():
            rows = max(len(removed), len(added))
            if not rows:
                return
            self.hunks.append(old.index("end-1c"))
            for output, lines, tag in ((old, removed, "removed"), (new, added, "added")):
                output.insert("end", "".join(lines), (tag,))
                output.insert("end", "\n" * (rows - len(lines)))
                lines.clear()

        in_hunk = False
        for line in self.diff_content.splitlines():
            if line.startswith(("@@", "##")):
                flush_context()
                flush_changes()
                for output in (old, new):
                    output.insert("end", line + "\n", ("hunk",))
                in_hunk = True
                continue
            if line.startswith(("Index: ", "Property changes on:")):
                in_hunk = False
            if in_hunk and line.startswith("-"):
                flush_context()
                removed.append(line[1:] + "\n")
            elif in_hunk and line.startswith("+"):
                flush_context()
                added.append(line[1:] + "\n")
            else:
                flush_changes()
                text = line[1:] if in_hunk and line.startswith(" ") else line
                context.append(text + "\n")
        flush_changes()
        flush_context()

    def jump_diff(self, step):
        if not self.hunks:
            return
        if self.hunk_index == -1:
            self.hunk_index = 0 if step > 0 else len(self.hunks) - 1
        else:
            self.hunk_index = (self.hunk_index + step) % len(self.hunks)
        index = self.hunks[self.hunk_index]
        outputs = self.split_outputs if self.split_diff.get() else [self.output]
        for output in outputs:
            output.yview(index)

    def scroll_split(self, *args):
        for output in self.split_outputs:
            output.yview(*args)

    def wheel_split(self, event):
        self.scroll_split("scroll", -int(event.delta / 120) * 3, "units")
        return "break"

    def preview(self, tree):
        if self.pages.select() != str(self.changes_page):
            return
        selection = tree.selection()
        if not selection or selection[0] not in self.entries or not self.status_ready:
            return
        if self.running:
            self.pending_diff = tree
            return
        path = selection[0]
        if self.entries[path] == "unversioned":
            self.render(path, "先将该项目纳入版本控制，即可预览 SVN 差异。")
            return
        directory = self.directory
        moved = path in self.moves.values()
        def show(value, is_diff=True):
            if self.pages.select() == str(self.changes_page) and tree.selection() == selection:
                self.render(path, value or "没有可显示的文本差异。", is_diff and bool(value))

        self.run("正在读取差异…",
                 lambda: working_diff(directory, path, moved),
                 show, lambda error: show(error, False), reading_diff=True)

    def check_updates(self):
        directory = self.directory
        self.incoming_label.set("待拉取：检查中…")

        def show(count):
            self.incoming_label.set(f"待拉取：{count} 条")

        def failed(error):
            self.incoming_label.set("待拉取：查询失败")
            messagebox.showerror("检查更新失败", error, parent=self)

        self.run("正在检查待拉取提交…", lambda: incoming_revision_count(directory), show, failed)

    def update_copy(self):
        directory = self.directory

        def update():
            output = svn(directory, "update", "--accept", "postpone")
            return output, read_status(directory)

        def show(result):
            output, (entries, self.moves) = result
            conflicts = sorted(path for path, state in entries.items() if state == "conflicted")
            details = "\n".join(conflicts)
            if conflicts:
                output += f"\n需要处理的冲突\n{details}\n\n请在「冲突」标签页选择文件查看解决方案。"
            self.pages.select(0)
            self.render("更新结果", output or "工作副本已更新。")
            self.show_status(entries, keep_output=True)
            self.mark_refreshed()
            self.check_updates()
            if conflicts:
                self.select_conflict(conflicts[0])

        def failed(error):
            self.incoming_label.set("待拉取：—")
            self.render("更新失败", error)
            self.refresh(keep_output=True)

        self.run("正在更新工作副本…", update, show, failed)

    def build_history(self):
        self.history_title = tk.StringVar(value="提交历史")
        toolbar = ttk.Frame(self.history_page)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="工作区历史", command=self.history).pack(side="right")
        ttk.Label(toolbar, textvariable=self.history_title).pack(side="left", fill="x", expand=True)
        self.history_query = tk.StringVar()
        search_bar = ttk.Frame(self.history_page)
        search_bar.pack(fill="x", pady=4)
        ttk.Label(search_bar, text="搜索历史").pack(side="left", padx=(0, 6))
        ttk.Entry(search_bar, textvariable=self.history_query).pack(side="left", fill="x", expand=True)
        self.history_query.trace_add("write", lambda *_: self.filter_history())
        history_list = ttk.Frame(self.history_page)
        history_list.pack(fill="both", expand=True)
        self.history_rows = ttk.Treeview(history_list, columns=("revision", "author", "date", "message"),
                                         show="headings", selectmode="extended", height=7)
        for key, title, width in (("revision", "版本", 56), ("author", "提交者", 96),
                                  ("date", "提交日期", 145), ("message", "提交说明", 220)):
            self.history_rows.heading(key, text=title)
            self.history_rows.column(key, width=width, minwidth=width, stretch=(key == "message"))
        vertical = ttk.Scrollbar(history_list, orient="vertical", command=self.history_rows.yview)
        horizontal = ttk.Scrollbar(history_list, orient="horizontal", command=self.history_rows.xview)
        self.history_rows.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        history_list.grid_rowconfigure(0, weight=1)
        history_list.grid_columnconfigure(0, weight=1)
        self.history_rows.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.history_rows.bind("<<TreeviewSelect>>", self.show_revision)
        self.history_rows.bind("<Button-3>", self.history_revision_menu)
        files_header = ttk.Frame(self.history_page)
        files_header.pack(fill="x", pady=(8, 4))
        ttk.Label(files_header, text="版本涉及的文件").pack(side="left")
        ttk.Button(files_header, text="查看文件历史", command=self.selected_file_history).pack(side="right")
        files = ttk.Frame(self.history_page)
        files.pack(fill="both", expand=True)
        self.history_files = ttk.Treeview(files, columns=("action", "path"), show="headings",
                                          selectmode="browse", height=6)
        self.history_files.heading("action", text="状态")
        self.history_files.heading("path", text="仓库路径")
        self.history_files.column("action", width=60, stretch=False)
        self.history_files.column("path", width=290)
        scrollbar = ttk.Scrollbar(files, orient="vertical", command=self.history_files.yview)
        self.history_files.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.history_files.pack(fill="both", expand=True)
        self.history_files.bind("<<TreeviewSelect>>", self.revision_diff)
        self.history_files.bind("<Double-Button-1>", self.selected_file_history)
        self.history_files.bind("<Button-3>", self.history_file_menu)
        self.history_rollback_target = None
        self.revisions = {}
        self.history_loaded = False
        self.revision_paths = {}
        self.active_revision = ""

    def clear_history(self):
        self.history_rollback_target = None
        self.history_loaded = False
        self.revisions.clear()
        self.revision_paths.clear()
        self.active_revision = ""
        self.history_rows.delete(*self.history_rows.get_children())
        self.history_files.delete(*self.history_files.get_children())
        self.history_title.set("提交历史")
        self.history_query.set("")

    def history(self, path=None, repository_target=None):
        if not self.repository_url or (self.running and not self.reading_diff):
            return
        directory, url = self.directory, self.repository_url
        self.pending_diff = None
        self.clear_history()
        self.history_loaded = True
        if repository_target:
            scoped_url = repository_target[0]
            scoped_path = self.local_history_path(scoped_url)
            if scoped_path is not None:
                self.history_rollback_target = (scoped_path, scoped_url)
        else:
            self.history_rollback_target = (path or ".", url if not path else None)
        title = path or "工作区历史"
        self.history_title.set(title)
        self.pages.select(self.history_page)
        self.render("SVN 查看日志", f"{title}\n\n正在读取提交记录…")
        if self.reading_diff:
            self.pending_history = (path, repository_target)
            return

        def show(entries):
            self.revisions = entries
            self.filter_history()
            if not entries:
                self.render("SVN 查看日志", f"{title}\n\n该路径没有可查看的提交记录。")

        def failed(error):
            self.render("无法读取 SVN 日志", f"{title}\n\n{error}")

        self.run("正在读取 SVN 日志…", lambda: read_log(directory, url, path, repository_target), show, failed)

    def filter_history(self):
        query = self.history_query.get().strip().casefold()
        for row in self.history_rows.get_children():
            self.history_rows.delete(row)
        self.history_files.delete(*self.history_files.get_children())
        self.revision_paths.clear()
        self.active_revision = ""
        for revision, entry in self.revisions.items():
            author = entry.findtext("author", "")
            date = log_date(entry.findtext("date"))
            message = entry.findtext("msg", "")
            if query not in f"r{revision} {author} {date} {message}".casefold():
                continue
            headline = next(iter(message.splitlines()), "")
            self.history_rows.insert("", "end", iid=revision,
                                     values=(f"r{revision}", author or "未记录", date, headline or "无提交说明"))
        rows = self.history_rows.get_children()
        if rows:
            self.history_rows.selection_set(rows[0])
        elif self.revisions:
            self.render("SVN 查看日志", "没有与搜索条件匹配的提交。")

    def show_revision(self, event=None):
        selected = self.history_rows.selection()
        if not selected:
            return
        focused = self.history_rows.focus()
        self.active_revision = focused if focused in selected else selected[0]
        entry = self.revisions[self.active_revision]
        self.revision_paths.clear()
        self.history_files.delete(*self.history_files.get_children())
        labels = {"A": "新增", "D": "删除", "M": "修改", "R": "替换"}
        for index, path in enumerate(entry.findall("./paths/path")):
            row = str(index)
            self.revision_paths[row] = path
            self.history_files.insert("", "end", iid=row,
                                      values=(labels.get(path.get("action"), path.get("action")), path.text))
        author = entry.findtext("author") or "未记录"
        date = log_date(entry.findtext("date"))
        message = entry.findtext("msg") or "无提交说明"
        self.render(f"r{self.active_revision} · 提交详情",
                    f"提交者  {author}\n提交日期  {date}\n\n提交说明\n{message}"
                    "\n\n选择历史页下方的文件可查看版本差异。")

    def local_history_path(self, url):
        root_url = unquote(self.repository_url).rstrip("/")
        path_url = unquote(url).rstrip("/")
        if path_url == root_url:
            return "."
        if not path_url.startswith(root_url + "/"):
            return None
        relative = path_url[len(root_url) + 1:]
        parts = relative.split("/")
        if any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
            return None
        return relative

    def show_history_menu(self, event, commands):
        menu = tk.Menu(self, tearoff=False, bg="#172433", fg="#e1eaf3",
                       activebackground="#295377", activeforeground="#ffffff")
        for label, command in commands:
            menu.add_command(label=label, command=command)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def history_revision_menu(self, event):
        row = self.history_rows.identify_row(event.y)
        scope = self.history_rollback_target
        if not row or scope is None or (self.running and not self.reading_diff):
            return
        selected = self.history_rows.selection()
        if row not in selected:
            self.history_rows.selection_set(row)
            selected = (row,)
        self.history_rows.focus(row)
        revisions = tuple(sorted(map(int, selected), reverse=True))
        label = "撤销此版本的更改…" if len(revisions) == 1 else "撤销所选版本的更改…"
        commands = [(label, lambda: self.history_rollback(scope, revisions, False))]
        if len(revisions) == 1:
            commands.append(("回滚到此版本…", lambda: self.history_rollback(scope, revisions, True)))
        self.show_history_menu(event, commands)

    def history_file_menu(self, event):
        row = self.history_files.identify_row(event.y)
        if not row or (self.running and not self.reading_diff):
            return
        change = self.revision_paths[row]
        url = self.repository_root.rstrip("/") + quote(change.text, safe="/")
        local_path = self.local_history_path(url)
        revision = int(self.active_revision)
        if local_path is not None and change.get("action") in {"M", "R"}:
            self.show_history_menu(event, [
                ("撤销此版本对该路径的更改…",
                 lambda: self.history_rollback((local_path, url), (revision,), False)),
            ])

    def history_rollback(self, scope, revisions, restore):
        if self.reading_diff:
            self.pending_history_rollback = (scope, revisions, restore)
            return
        if self.running:
            return
        directory = self.directory
        path, expected_url = scope
        title = "回滚到此版本" if restore else "撤销历史版本更改"

        def preview():
            cwd, args = history_merge_args(directory, path, revisions, restore, expected_url)
            output = svn(cwd, args[0], "--dry-run", *args[1:]) if args else ""
            return cwd, args, output

        def confirm(result):
            cwd, args, output = result
            if not output.strip():
                messagebox.showinfo(title, "SVN 预览没有发现可回滚的更改。", parent=self)
                return
            versions = ", ".join(f"r{revision}" for revision in revisions)
            lines = [f"目标路径  {Path(directory) / path}",
                     f"目标版本  {versions}" if restore else f"撤销版本  {versions}",
                     "", *output.strip().splitlines()]
            if self.confirm_paths(lines, title, "确认后将在当前工作副本执行反向合并。",
                                  "确认回滚"):
                def apply():
                    if history_merge_args(directory, path, revisions, restore, expected_url) != (cwd, args):
                        raise RuntimeError("工作副本版本已变化，请重新预览回滚。")
                    return svn(cwd, *args)

                self.run("正在回滚历史版本…", apply, complete, failed)

        def complete(output):
            self.pages.select(self.changes_page)
            self.after_operation("历史回滚结果", output)

        def failed(error):
            self.render("历史回滚失败", error)
            self.refresh(keep_output=True)

        self.run("正在预览历史回滚…", preview, confirm,
                 lambda error: messagebox.showerror("无法预览历史回滚", error, parent=self))

    def selected_file_history(self, event=None):
        selection = self.history_files.selection()
        if not selection:
            return
        change = self.revision_paths[selection[0]]
        path = change.text
        url = self.repository_root.rstrip("/") + quote(path, safe="/")
        peg = int(self.active_revision) - (change.get("action") == "D")
        self.history(path, (url, peg))
        return "break"

    def revision_diff(self, event=None):
        selected = self.history_files.selection()
        if not selected or self.running:
            return
        change = self.revision_paths[selected[0]]
        revision = self.active_revision
        path = change.text
        url = self.repository_root.rstrip("/") + quote(path, safe="/")
        directory = self.directory
        action, kind = change.get("action"), change.get("kind")

        def show(value):
            if self.active_revision == revision and self.history_files.selection() == selected:
                self.render(f"r{revision} · {path}", value or "没有可显示的文本差异。", bool(value))
            elif self.history_files.selection():
                self.revision_diff()

        self.run("正在读取版本差异…",
                 lambda: historical_diff(directory, url, revision, action, kind),
                 show, reading_diff=True)

    def add_files(self, selection=None):
        paths = [path for path in (self.tree.selection() if selection is None else selection)
                 if self.entries.get(path) == "unversioned"]
        if not paths:
            messagebox.showinfo("纳入版本控制", "请先在列表中选中未跟踪项目。", parent=self)
            return
        directory = self.directory
        self.run("正在添加…", lambda: svn(directory, "add", "--depth", "empty", "--",
                                          *(target(path) for path in paths)),
                 lambda value: self.refresh())

    def commit_targets(self, selected, entries, moves):
        allowed = {"modified", "added", "deleted", "replaced"}
        paths = set(selected)
        for source, destination in moves.items():
            if paths.intersection({source, destination}):
                paths.update({source, destination})
        for path in list(paths):
            parts = path.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if entries.get(parent) in {"added", "replaced"}:
                    paths.add(parent)
        if any(entries.get(path) not in allowed for path in paths):
            raise RuntimeError("勾选项或关联路径的状态已变化，请刷新后核对提交范围。")
        return paths

    def commit(self):
        if self.running or not self.status_ready or not self.checked:
            return
        summary = self.message.get("1.0", "end-1c").strip()
        if not summary:
            self.message.focus_set()
            self.status.set("请输入提交说明")
            return
        selected = set(self.checked)
        directory = self.directory
        try:
            paths = self.commit_targets(selected, self.entries, self.moves)
        except RuntimeError as error:
            self.commit_error(str(error))
            return
        lines = []
        for path in sorted(paths):
            state = self.entries[path]
            extra = " · 所需父目录" if path not in selected else ""
            if state in {"added", "deleted", "replaced"} and (
                (Path(directory) / path).is_dir() or any(p.startswith(path + "/") for p in self.entries)
            ):
                extra += " · 目录操作可能涉及其子树"
            lines.append(f"{LABELS.get(state, state)}  {path}{extra}")
        if not self.confirm_commit(lines):
            return
        self.save_workspaces()

        def submit():
            entries, moves = read_status(directory)
            if self.commit_targets(selected, entries, moves) != paths or any(
                entries.get(path) != expected[path] for path in paths
            ) or {key: value for key, value in moves.items() if key in paths or value in paths} != confirmed_moves:
                raise RuntimeError("提交范围已变化，请刷新后重新确认。")
            return svn(directory, "commit", "--depth", "empty", "-m", summary, "--",
                       *(target(path) for path in sorted(paths)))

        expected = {path: self.entries[path] for path in paths}
        confirmed_moves = {key: value for key, value in self.moves.items() if key in paths or value in paths}
        self.run("正在提交…", submit, self.commit_success, self.commit_error)

    def confirm_commit(self, lines):
        return self.confirm_paths(lines, "核对提交范围", f"即将提交 {len(lines)} 条路径", "确认提交")

    def confirm_paths(self, lines, title, description, action):
        dialog = tk.Toplevel(self, bg="#172433")
        dialog.title(title)
        dialog.transient(self)
        ttk.Label(dialog, text=f"当前工作区 · {self.workspace_label(self.directory)}",
                  padding=8).pack(anchor="w")
        ttk.Label(dialog, text=description, padding=8).pack(anchor="w")
        frame = ttk.Frame(dialog, padding=8)
        frame.pack(fill="both", expand=True)
        contents = self.make_text(frame, width=70, height=min(14, len(lines)))
        self.pack_output(frame, contents)
        contents.insert("1.0", "\n".join(lines))
        contents.configure(state="disabled")
        controls = ttk.Frame(dialog, padding=8)
        controls.pack(fill="x")
        accepted = tk.BooleanVar(dialog, value=False)

        def confirm():
            accepted.set(True)
            dialog.destroy()

        ttk.Button(controls, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(controls, text=action, command=confirm).pack(side="right", padx=8)
        dialog.bind("<Escape>", lambda event: dialog.destroy())
        dialog.wait_visibility()
        dialog.grab_set()
        dialog.wait_window()
        return accepted.get()

    def commit_success(self, value):
        self.checked.clear()
        self.message.delete("1.0", "end")
        self.save_workspaces()
        self.render("提交结果", value)
        self.refresh(keep_output=True)

    def commit_error(self, error):
        if re.search(r"E(?:155011|160028)", error):
            error += "\n\n服务器上的部分路径已更新。请先更新工作副本并检查冲突，再重新提交。"
        self.render("提交失败", error + "\n\n已保留勾选和提交说明。")
        messagebox.showerror("提交失败", error, parent=self)

    def after_operation(self, title, value):
        self.render(title, value.strip() or "操作完成")
        self.refresh(keep_output=True)


if __name__ == "__main__":
    Workspace().mainloop()
