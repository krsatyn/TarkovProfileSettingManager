"""Tarkov Profile Manager — offline settings backup/switcher. Python 3.10+."""
from __future__ import annotations

import datetime as dt
import json
import random
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import hashlib
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from PIL import Image, ImageTk

APP_NAME = "Tarkov Profile Manager"
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ASSETS = BASE_DIR / "assets"
APP_ID = "tarkov-profile-manager"
SUPPORTED = {".ini", ".json"}
MAX_FILE = 15 * 1024 * 1024
PROCESS_NAMES = {"escapefromtarkov.exe", "escapefromtarkov_be.exe"}


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def app_home():
    root = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(root) / "TarkovProfileManager"


def default_settings():
    roaming = os.environ.get("APPDATA")
    if roaming:
        return Path(roaming) / "Battlestate Games" / "Escape from Tarkov" / "Settings"
    return Path.home() / "AppData" / "Roaming" / "Battlestate Games" / "Escape from Tarkov" / "Settings"


def safe_name(name):
    name = name.strip()
    if not name or len(name) > 80 or any(c in name for c in "<>:\"/\\|?*\r\n\t") or name in {".", ".."}:
        raise ValueError("Имя должно содержать 1–80 символов, без запрещённых символов Windows.")
    return name


def file_names(directory):
    """Only flat, regular, non-symlink INI/JSON files are settings candidates."""
    if not directory.is_dir():
        return []
    return sorted([p.name for p in directory.iterdir()
                   if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUPPORTED
                   and p.stat().st_size <= MAX_FILE], key=str.casefold)


def is_game_running():
    if sys.platform != "win32":
        return False
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=5, creationflags=flags, errors="replace")
        return any(re.match(r'^"?(' + "|".join(re.escape(x) for x in PROCESS_NAMES) + r')"?,', line.strip(), re.I)
                   for line in res.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        # On failed inspection, user should manually ensure game is closed.
        return None


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".tmp-", suffix=".json", delete=False) as f:
        tmp = Path(f.name)
        json.dump(obj, f, ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as file:
        for part in iter(lambda: file.read(1024 * 1024), b''):
            h.update(part)
    return h.digest()


class Store:
    def __init__(self):
        self.home = app_home()
        self.profiles = self.home / "profiles"
        self.backups = self.home / "backups"
        self.config_file = self.home / "config.json"
        self.profiles.mkdir(parents=True, exist_ok=True)
        self.backups.mkdir(parents=True, exist_ok=True)
        try:
            self.cfg = json.loads(self.config_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            self.cfg = {}
        self.cfg.setdefault("settings_path", str(default_settings()))

    @property
    def settings(self):
        return Path(self.cfg["settings_path"]).expanduser()

    def set_settings(self, path):
        self.cfg["settings_path"] = str(path)
        atomic_json(self.config_file, self.cfg)

    def list_profiles(self):
        result = []
        for folder in self.profiles.iterdir():
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
                files = self.valid_profile_files(folder, manifest)
                result.append((folder, manifest, files))
            except (OSError, ValueError, KeyError):
                continue
        return sorted(result, key=lambda x: x[1].get("updated_at", ""), reverse=True)

    @staticmethod
    def valid_profile_files(folder, manifest):
        if manifest.get("format") != APP_ID or manifest.get("version") != 1:
            raise ValueError("Неизвестный формат профиля.")
        safe_name(manifest["name"])
        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > 100:
            raise ValueError("Профиль не содержит допустимых файлов.")
        if len(files) != len(set(x.casefold() for x in files if isinstance(x, str))):
            raise ValueError("Повторяющиеся имена файлов.")
        for fn in files:
            if not isinstance(fn, str) or fn != Path(fn).name or not re.fullmatch(r"[^/\\:]+\.(?:ini|json)", fn, re.I):
                raise ValueError("Недопустимое имя файла профиля.")
            p = folder / "settings" / fn
            if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_FILE:
                raise ValueError(f"Повреждён или отсутствует файл: {fn}")
        return files

    def create_from_settings(self, name, chosen):
        name = safe_name(name)
        if not chosen:
            raise ValueError("Нужно выбрать хотя бы один файл.")
        available = set(file_names(self.settings))
        if not set(chosen).issubset(available):
            raise ValueError("Один из файлов настроек отсутствует или не поддерживается.")
        folder = self.profiles / uuid.uuid4().hex
        folder.mkdir()
        try:
            (folder / "settings").mkdir()
            for fn in chosen:
                shutil.copy2(self.settings / fn, folder / "settings" / fn)
            manifest = {"format": APP_ID, "version": 1, "name": name, "created_at": now(),
                        "updated_at": now(), "files": list(chosen)}
            atomic_json(folder / "manifest.json", manifest)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def rename(self, folder, manifest, new_name):
        manifest["name"] = safe_name(new_name)
        manifest["updated_at"] = now()
        atomic_json(folder / "manifest.json", manifest)

    def export(self, folder, manifest, destination_root):
        """Export the exact original INI/JSON files in a plain named folder."""
        files = self.valid_profile_files(folder, manifest)
        destination_root = Path(destination_root).expanduser().resolve()
        if not destination_root.is_dir():
            raise FileNotFoundError('Папка для экспорта не существует.')
        base = safe_name(manifest['name'])
        destination = destination_root / base
        index = 2
        while destination.exists():
            destination = destination_root / f'{base} ({index})'
            index += 1
        staging = destination_root / ('.tpm-writing-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for fn in files:
                source, target = folder / 'settings' / fn, staging / fn
                shutil.copy2(source, target)
                if source.stat().st_size != target.stat().st_size or sha256_file(source) != sha256_file(target):
                    raise IOError('Проверка копирования не прошла: ' + fn)
            staging.rename(destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return destination

    def import_folder(self, source, name=None):
        source = Path(source).expanduser().resolve()
        if not source.is_dir() or source.is_symlink():
            raise ValueError('Выберите папку с файлами настроек.')
        files = file_names(source)
        if not files:
            raise ValueError('Нет файлов .ini или .json в выбранной папке.')
        if len(files) > 100:
            raise ValueError('Слишком много файлов настроек.')
        name = safe_name(name or source.name)
        folder = self.profiles / uuid.uuid4().hex
        folder.mkdir()
        try:
            (folder / 'settings').mkdir()
            for fn in files:
                shutil.copy2(source / fn, folder / 'settings' / fn)
                if sha256_file(source / fn) != sha256_file(folder / 'settings' / fn):
                    raise IOError('Проверка копирования не прошла: ' + fn)
            manifest = {'format': APP_ID, 'version': 1, 'name': name,
                        'created_at': now(), 'updated_at': now(), 'files': files}
            atomic_json(folder / 'manifest.json', manifest)
            self.valid_profile_files(folder, manifest)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return folder

    def make_backup(self):
        if not self.settings.is_dir():
            raise FileNotFoundError("Каталог настроек игры не найден.")
        chosen = file_names(self.settings)
        if not chosen:
            return None
        dest = self.backups / (dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_before_apply")
        dest.mkdir()
        try:
            for fn in chosen:
                shutil.copy2(self.settings / fn, dest / fn)
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return dest

    def apply(self, folder, manifest):
        files = self.valid_profile_files(folder, manifest)
        if not self.settings.is_dir():
            raise FileNotFoundError("Папка настроек игры не существует. Выберите её вручную.")
        previous = set(file_names(self.settings))
        backup = self.make_backup()
        try:
            for fn in files:
                source = folder / "settings" / fn
                target = self.settings / fn
                with tempfile.NamedTemporaryFile(dir=self.settings, prefix=".tpm-", delete=False) as tmp:
                    temp = Path(tmp.name)
                try:
                    shutil.copyfile(source, temp)
                    os.replace(temp, target)
                finally:
                    temp.unlink(missing_ok=True)
        except Exception:
            # Attempt automatic rollback for any files that existed before apply.
            for fn in files:
                if fn not in previous:
                    try:
                        (self.settings / fn).unlink(missing_ok=True)
                    except OSError:
                        pass
            if backup:
                for old in backup.iterdir():
                    try:
                        shutil.copy2(old, self.settings / old.name)
                    except OSError:
                        pass
            raise
        return backup


MEMES = [
    ("КОТАРКОВ", "Лут не вынес, зато настройки сохранил."),
    ("ЧВК МЯУ", "Вышел в рейд. Вернулся в главное меню."),
    ("FPS В КАРМАНЕ", "Минус тени, плюс девять жизней."),
    ("ПРОФИЛЬ СОХРАНЁН", "Этот кот теперь отвечает за твой конфиг."),
    ("КОТ-ПРАПОР", "Тащи настройки. Остальное найдём."),
    ("МЯУШРУТ ПОСТРОЕН", "От настроек до рейда — один клик."),
    ("СТРАХОВКА ОТ КОТА", "Бэкап на месте. Мурчим спокойно."),
    ("КОТ НЕ КЕМПЕР", "Просто уже третий час выбирает графику."),
    ("ПРОФИЛЬ: ЛЕГЕНДА", "Когда скилл не сохранился, но бинды — да."),
    ("МЯУТАЛЬНЫЙ КОНТРОЛЬ", "Главное не потерять settings.ini."),
]


class CatMemePanel(tk.Frame):
    """Маскот из фотографии пользователя, одинаковый для всех мемов."""

    def __init__(self, master, bg="#1a2230"):
        super().__init__(master, bg=bg, highlightbackground="#35465b", highlightthickness=1)
        self.index = random.randrange(len(MEMES))
        image = Image.open(ASSETS / "cat.png").convert("RGB")
        image.thumbnail((248, 220), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image)
        panel = tk.Label(self, image=self.photo, bg=bg, cursor="hand2")
        panel.pack(pady=(8, 6))
        panel.bind("<Button-1>", lambda _event: self.next_meme())
        self.title_var = tk.StringVar()
        self.quote_var = tk.StringVar()
        tk.Label(self, textvariable=self.title_var, bg=bg, fg="#e8bd75",
                 font=("Segoe UI Semibold", 10)).pack(pady=(0, 2))
        tk.Label(self, textvariable=self.quote_var, bg=bg, fg="#c9d3e0", justify="center",
                 wraplength=245, font=("Segoe UI", 9)).pack(padx=8, pady=(0, 8))
        self._update_quote()

    def _update_quote(self):
        title, quote = MEMES[self.index]
        self.title_var.set(title)
        self.quote_var.set(quote)

    def next_meme(self):
        self.index = (self.index + 1) % len(MEMES)
        self._update_quote()


class SharedFolderStorage:
    SUBFOLDER = 'Tarkov Profiles'

    def __init__(self, store):
        self.store = store

    def configured_path(self):
        value = self.store.cfg.get('shared_folder', '')
        return Path(value).expanduser() if value else None

    def set_path(self, path):
        path = Path(path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError('Общая папка недоступна.')
        self.store.cfg['shared_folder'] = str(path)
        atomic_json(self.store.config_file, self.store.cfg)
        return self.folder()

    def folder(self):
        root = self.configured_path()
        if root is None:
            raise ValueError('Сначала выберите общую папку Google Drive.')
        if not root.is_dir():
            raise FileNotFoundError('Общая папка недоступна. Проверьте Google Drive для компьютера.')
        target = root / self.SUBFOLDER
        target.mkdir(exist_ok=True)
        if target.is_symlink() or not target.is_dir():
            raise ValueError('Каталог общих профилей недоступен.')
        return target

    def list_profiles(self):
        result = []
        for entry in self.folder().iterdir():
            if entry.is_dir() and not entry.is_symlink():
                files = file_names(entry)
                if files:
                    result.append({'name': entry.name, 'path': entry, 'files': len(files),
                                   'modified': dt.datetime.fromtimestamp(entry.stat().st_mtime).strftime('%d.%m.%Y %H:%M')})
        return sorted(result, key=lambda x: x['name'].casefold())

    def upload(self, store, folder, manifest):
        return store.export(folder, manifest, self.folder())

    def download(self, store, path):
        base = self.folder().resolve()
        path = Path(path)
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != base:
            raise ValueError('Профиль не принадлежит общей папке.')
        return store.import_folder(path, path.name)


class App(tk.Tk):
    BG = "#10151e"
    PANEL = "#1a2230"
    TEXT = "#e9edf4"
    MUTED = "#a9b6c9"
    ACCENT = "#e3b96b"

    def __init__(self):
        super().__init__()
        self.store = Store()
        self.title(APP_NAME + "  •  CAT EDITION 4.0")
        self._set_app_icon()
        self.geometry("1080x760")
        self.minsize(610, 520)
        self.configure(bg=self.BG)
        self._style()
        self._build()
        self.refresh()
        self.after(180, self.show_welcome)

    def _set_app_icon(self):
        try:
            self.iconbitmap(str(ASSETS / "cat.ico"))
        except tk.TclError:
            image = Image.open(ASSETS / "cat.png").convert("RGBA")
            image.thumbnail((64, 64))
            self.icon_image = ImageTk.PhotoImage(image)
            self.iconphoto(True, self.icon_image)

    def _style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TFrame", background=self.BG)
        st.configure("Panel.TFrame", background=self.PANEL)
        st.configure("TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 10))
        st.configure("Panel.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        st.configure("Muted.TLabel", background=self.BG, foreground=self.MUTED, font=("Segoe UI", 9))
        st.configure("Header.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI Semibold", 21))
        st.configure("TButton", background="#283448", foreground=self.TEXT, padding=(12, 9),
                     font=("Segoe UI", 10), borderwidth=0)
        st.map("TButton", background=[("active", "#3a4b64")], foreground=[("disabled", "#68758a")])
        st.configure("Accent.TButton", background=self.ACCENT, foreground="#141923", font=("Segoe UI Semibold", 10))
        st.map("Accent.TButton", background=[("active", "#f5ce89")])
        st.configure("Treeview", background=self.PANEL, foreground=self.TEXT, fieldbackground=self.PANEL,
                     borderwidth=0, rowheight=35, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background="#283448", foreground=self.MUTED,
                     font=("Segoe UI Semibold", 9), padding=8)
        st.map("Treeview", background=[("selected", "#42536a")], foreground=[("selected", "#ffffff")])
        st.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10))
        st.map("TCheckbutton", background=[("active", self.PANEL)], foreground=[("active", self.TEXT)])

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        ttk.Label(outer, text='TARKOV / PROFILE MANAGER', style='Header.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(outer, text='Оригинальные файлы настроек • общая папка • снежный барс',
                  style='Muted.TLabel').grid(row=1, column=0, sticky='w', pady=(0, 10))
        top = ttk.Frame(outer)
        top.grid(row=2, column=0, sticky='ew', pady=(0, 10))
        top.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar()
        tk.Entry(top, textvariable=self.path_var, state='readonly', bg='#202a38',
                 fg=self.TEXT, readonlybackground='#202a38', relief='flat').grid(row=0, column=0, sticky='ew', ipady=8)
        ttk.Button(top, text='Папка игры', command=self.choose_folder).grid(row=0, column=1, padx=(7, 0))
        ttk.Button(top, text='Открыть', command=self.open_settings).grid(row=0, column=2, padx=(7, 0))
        topactions = ttk.Frame(top)
        topactions.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(9, 0))
        for i in range(4):
            topactions.columnconfigure(i, weight=1)
        ttk.Button(topactions, text='+ Сохранить', style='Accent.TButton', command=self.save_current).grid(row=0, column=0, sticky='ew', padx=(0, 5))
        ttk.Button(topactions, text='Импорт папки', command=self.import_profile).grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(topactions, text='Общая папка', command=self.open_drive_window).grid(row=0, column=2, sticky='ew', padx=5)
        ttk.Button(topactions, text='Обновить', command=self.refresh).grid(row=0, column=3, sticky='ew', padx=(5, 0))
        self.body = ttk.Frame(outer)
        self.body.grid(row=3, column=0, sticky='nsew')
        self.body.columnconfigure(0, weight=3)
        self.body.columnconfigure(1, weight=1)
        self.body.rowconfigure(0, weight=1)
        left = ttk.Frame(self.body, style='Panel.TFrame', padding=8)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 9))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.left = left
        self.tree = ttk.Treeview(left, columns=('name', 'files', 'date'), show='headings', selectmode='browse')
        for col, label in [('name', 'ПРОФИЛЬ'), ('files', 'ФАЙЛОВ'), ('date', 'ИЗМЕНЁН')]:
            self.tree.heading(col, text=label)
        self.tree.column('name', width=210, minwidth=90)
        self.tree.column('files', width=65, minwidth=55, anchor='center', stretch=False)
        self.tree.column('date', width=150, minwidth=100, stretch=False)
        self.tree.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(left, command=self.tree.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind('<<TreeviewSelect>>', lambda _e: self.show_details())
        right = ttk.Frame(self.body, padding=(6, 0, 0, 0))
        right.grid(row=0, column=1, sticky='nsew')
        self.right = right
        right.columnconfigure(0, weight=1)
        self.cat_panel = CatMemePanel(right, bg=self.PANEL)
        self.cat_panel.grid(row=0, column=0, sticky='ew')
        ttk.Button(right, text='Другой мем', command=self.cat_panel.next_meme).grid(row=1, column=0, sticky='ew', pady=(4, 8))
        ttk.Label(right, text='Управление профилем', font=('Segoe UI Semibold', 11)).grid(row=2, column=0, sticky='w')
        self.detail = tk.Text(right, height=4, wrap='word', bg=self.BG, fg=self.MUTED, relief='flat',
                              borderwidth=0, font=('Segoe UI', 9), state='disabled')
        self.detail.grid(row=3, column=0, sticky='ew', pady=(5, 7))
        actions = ttk.Frame(right)
        actions.grid(row=4, column=0, sticky='ew')
        for i in range(2):
            actions.columnconfigure(i, weight=1)
        actionspec = [('Применить', self.apply_profile), ('Экспорт', self.export_profile),
                      ('Переименовать', self.rename_profile), ('Удалить', self.delete_profile),
                      ('Резервные копии', self.open_backups)]
        for i, (label, callback) in enumerate(actionspec):
            ttk.Button(actions, text=label, command=callback,
                       style='Accent.TButton' if i == 0 else 'TButton').grid(
                           row=i // 2, column=i % 2, sticky='ew', padx=2, pady=3)
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, style='Muted.TLabel', wraplength=580).grid(
            row=4, column=0, sticky='ew', pady=(9, 0))
        self._narrow = None
        self.bind('<Configure>', self._on_resize)

    def _on_resize(self, event):
        if event.widget is not self:
            return
        narrow = event.width < 880
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow:
            self.body.columnconfigure(1, weight=0)
            self.body.rowconfigure(1, weight=0)
            self.left.grid_configure(row=0, column=0, padx=0)
            self.right.grid_configure(row=1, column=0, sticky='ew', pady=(6, 0))
            self.cat_panel.grid_remove()
            self.right.grid_columnconfigure(0, weight=1)
        else:
            self.body.columnconfigure(1, weight=1)
            self.body.rowconfigure(1, weight=0)
            self.left.grid_configure(row=0, column=0, padx=(0, 9))
            self.right.grid_configure(row=0, column=1, sticky='nsew', pady=0)
            self.cat_panel.grid()

    def show_welcome(self):
        """Full, UNCROPPED first image, scaled proportionally to available space."""
        win = tk.Toplevel(self)
        win.title('Tarkov Profile Manager')
        win.configure(bg='#090a0d')
        win.transient(self)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(830, int(sw * .88)), min(810, int(sh * .85))
        win.geometry(f'{w}x{h}+{max(0, (sw-w)//2)}+{max(0, (sh-h)//2)}')
        win.minsize(320, 300)
        win.grab_set()
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)
        picture = tk.Label(win, bg='#090a0d', bd=0)
        picture.grid(row=0, column=0, sticky='nsew', padx=12, pady=(12, 5))
        ttk.Label(win, text='TARKOV PROFILE MANAGER', font=('Segoe UI Semibold', 16),
                  background='#090a0d', foreground=self.TEXT).grid(row=1, column=0, pady=(4, 5))
        ttk.Button(win, text='Продолжить', style='Accent.TButton', command=win.destroy).grid(
            row=2, column=0, pady=(3, 14))
        original = Image.open(ASSETS / 'splash.png').convert('RGB')
        cache = {'size': (0, 0)}
        def redraw(event):
            size = (max(1, event.width), max(1, event.height))
            if abs(size[0]-cache['size'][0]) < 8 and abs(size[1]-cache['size'][1]) < 8:
                return
            cache['size'] = size
            image = original.copy()
            image.thumbnail(size, Image.Resampling.LANCZOS)
            picture.photo = ImageTk.PhotoImage(image)
            picture.configure(image=picture.photo)
        picture.bind('<Configure>', redraw)
        win.bind('<Return>', lambda _e: win.destroy())
        win.bind('<Escape>', lambda _e: win.destroy())
        win.protocol('WM_DELETE_WINDOW', win.destroy)

    def open_drive_window(self):
        win = tk.Toplevel(self)
        win.title('Общая папка Google Drive')
        win.geometry('790x560')
        win.minsize(490, 360)
        win.configure(bg=self.BG)
        win.transient(self)
        storage = SharedFolderStorage(self.store)
        wrap = ttk.Frame(win, padding=12)
        wrap.pack(fill='both', expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(2, weight=1)
        ttk.Label(wrap, text='ОБЩИЕ ПРОФИЛИ', font=('Segoe UI Semibold', 15)).grid(row=0, column=0, sticky='w')
        current = tk.StringVar(value=str(storage.configured_path() or 'Папка ещё не выбрана'))
        status = tk.StringVar(value='Укажите синхронизируемую общую папку.')
        top = ttk.Frame(wrap)
        top.grid(row=1, column=0, sticky='ew', pady=(8, 7))
        top.columnconfigure(0, weight=1)
        ttk.Label(top, textvariable=current, style='Muted.TLabel').grid(row=0, column=0, sticky='ew')
        listing = ttk.Treeview(wrap, columns=('name', 'count', 'date'), show='headings', selectmode='browse')
        for col, label in [('name','ПАПКА ПРОФИЛЯ'),('count','ФАЙЛОВ'),('date','ИЗМЕНЁН')]:
            listing.heading(col, text=label)
        listing.column('name', width=330, minwidth=100)
        listing.column('count', width=70, minwidth=55, stretch=False)
        listing.column('date', width=155, minwidth=90, stretch=False)
        listing.grid(row=2, column=0, sticky='nsew')
        shared_items = {}
        def refresh():
            try:
                listing.delete(*listing.get_children())
                shared_items.clear()
                items = storage.list_profiles()
                for item in items:
                    iid = listing.insert('', 'end', values=(item['name'], item['files'], item['modified']))
                    shared_items[iid] = item
                status.set(f'Общих профилей: {len(items)}. Синхронизацию выполняет Google Drive для компьютера.')
            except (OSError, ValueError) as exc:
                status.set(str(exc))
        def choose():
            path = filedialog.askdirectory(parent=win, title='Выберите общую папку Google Drive')
            if path:
                try:
                    storage.set_path(path)
                    current.set(str(storage.configured_path()))
                    refresh()
                except (OSError, ValueError) as exc:
                    messagebox.showerror('Общая папка', str(exc), parent=win)
        def open_folder():
            try:
                self.open_dir(storage.folder())
            except (OSError, ValueError) as exc:
                messagebox.showerror('Общая папка', str(exc), parent=win)
        ttk.Button(top, text='Выбрать', command=choose).grid(row=0, column=1, padx=(7, 0))
        ttk.Button(top, text='Открыть', command=open_folder).grid(row=0, column=2, padx=(7, 0))
        actions = ttk.Frame(wrap)
        actions.grid(row=3, column=0, sticky='ew', pady=(12, 8))
        for i in range(3):
            actions.columnconfigure(i, weight=1)
        def upload():
            item = self.selected()
            if not item:
                return
            try:
                folder, manifest, _ = item
                destination = storage.upload(self.store, folder, manifest)
                refresh()
                messagebox.showinfo('Экспорт', f'Создана папка:\n{destination}\n\n'
                                    'Все файлы сохранены в исходном формате. Дождитесь синхронизации.', parent=win)
            except (OSError, ValueError) as exc:
                messagebox.showerror('Экспорт', str(exc), parent=win)
        def download():
            selection = listing.selection()
            if not selection:
                messagebox.showinfo('Импорт', 'Выберите папку профиля.', parent=win)
                return
            try:
                storage.download(self.store, shared_items[selection[0]]['path'])
                self.refresh()
                messagebox.showinfo('Импорт', 'Профиль импортирован в локальную библиотеку.', parent=win)
            except (OSError, ValueError) as exc:
                messagebox.showerror('Импорт', str(exc), parent=win)
        ttk.Button(actions, text='Экспорт выбранного', style='Accent.TButton', command=upload).grid(row=0, column=0, sticky='ew', padx=(0, 5))
        ttk.Button(actions, text='Импорт выбранного', command=download).grid(row=0, column=1, sticky='ew', padx=5)
        ttk.Button(actions, text='Обновить', command=refresh).grid(row=0, column=2, sticky='ew', padx=(5, 0))
        ttk.Label(wrap, textvariable=status, style='Muted.TLabel').grid(row=4, column=0, sticky='ew')
        ttk.Label(wrap, text='Профили передаются обычными папками с оригинальными .ini и .json.',
                  style='Muted.TLabel').grid(row=5, column=0, sticky='w', pady=(5, 0))
        if storage.configured_path() is not None:
            refresh()

    def report_error(self, exc):
        messagebox.showerror("Ошибка", str(exc), parent=self)

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Профиль не выбран", "Выберите профиль из списка.", parent=self)
            return None
        return self.items.get(sel[0])

    def refresh(self):
        self.path_var.set(str(self.store.settings))
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.items = {}
        for folder, manifest, files in self.store.list_profiles():
            when = manifest.get("updated_at", "")[:16].replace("T", "  ")
            iid = self.tree.insert("", "end", values=(manifest["name"], len(files), when))
            self.items[iid] = (folder, manifest, files)
        self.show_details()
        available = file_names(self.store.settings)
        self.status_var.set(f"Найдено файлов настроек: {len(available)}   •   Профилей: {len(self.items)}   •   Профили хранятся в {self.store.profiles}")

    def show_details(self):
        sel = self.tree.selection()
        item = self.items.get(sel[0]) if sel else None
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if item:
            _folder, manifest, files = item
            self.detail.insert("end", f"{manifest['name']}\n\nФайлы настроек:\n" + "\n".join("• " + f for f in files))
        else:
            self.detail.insert("end", "Выберите профиль слева для просмотра его содержимого и применения.")
        self.detail.configure(state="disabled")

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self, title="Укажите папку Settings в Escape from Tarkov",
                                       initialdir=str(self.store.settings.parent if self.store.settings.parent.exists() else Path.home()))
        if path:
            self.store.set_settings(Path(path))
            self.refresh()

    def open_dir(self, path):
        try:
            Path(path).mkdir(parents=True, exist_ok=True) if path == self.store.backups else None
            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            self.report_error(exc)

    def open_settings(self):
        if self.store.settings.exists():
            self.open_dir(self.store.settings)
        else:
            messagebox.showwarning("Каталог не найден", "Выберите существующий каталог настроек игры.", parent=self)

    def open_backups(self):
        self.open_dir(self.store.backups)

    def save_current(self):
        files = file_names(self.store.settings)
        if not files:
            messagebox.showwarning("Нет настроек", "Не найдены файлы .ini или .json в указанной папке.", parent=self)
            return
        name = simpledialog.askstring("Создать профиль", "Название профиля:", parent=self)
        if name is None:
            return
        try:
            safe_name(name)
        except ValueError as exc:
            self.report_error(exc)
            return
        win = tk.Toplevel(self)
        win.title("Файлы нового профиля")
        win.configure(bg=self.PANEL)
        win.geometry("450x" + str(min(570, 145 + 33 * len(files))))
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text="Какие настройки сохранить?", style="Panel.TLabel",
                  font=("Segoe UI Semibold", 12)).pack(anchor="w", padx=18, pady=(16, 10))
        frame = ttk.Frame(win, style="Panel.TFrame")
        frame.pack(fill="both", expand=True, padx=18)
        values = []
        for fn in files:
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(frame, text=fn, variable=v).pack(anchor="w", pady=3)
            values.append((fn, v))
        buttons = ttk.Frame(win, style="Panel.TFrame")
        buttons.pack(fill="x", padx=18, pady=15)
        def confirm():
            try:
                chosen = [fn for fn, val in values if val.get()]
                self.store.create_from_settings(name, chosen)
                win.destroy()
                self.refresh()
                self.cat_panel.next_meme()
            except Exception as exc:
                self.report_error(exc)
        ttk.Button(buttons, text="Сохранить профиль", style="Accent.TButton", command=confirm).pack(side="left")
        ttk.Button(buttons, text="Отмена", command=win.destroy).pack(side="right")

    def rename_profile(self):
        item = self.selected()
        if not item:
            return
        folder, manifest, _files = item
        name = simpledialog.askstring("Переименовать", "Новое название:", initialvalue=manifest["name"], parent=self)
        if name is not None:
            try:
                self.store.rename(folder, manifest, name)
                self.refresh()
            except Exception as exc:
                self.report_error(exc)

    def delete_profile(self):
        item = self.selected()
        if item and messagebox.askyesno("Удалить профиль", f"Удалить «{item[1]['name']}»?\nНастройки игры не изменятся.", parent=self):
            try:
                shutil.rmtree(item[0])
                self.refresh()
            except Exception as exc:
                self.report_error(exc)

    def export_profile(self):
        item = self.selected()
        if not item:
            return
        destination = filedialog.askdirectory(parent=self, title='Куда экспортировать папку профиля?')
        if destination:
            try:
                folder, manifest, _files = item
                output = self.store.export(folder, manifest, destination)
                messagebox.showinfo('Экспорт завершён', f'Оригинальные файлы сохранены в папке:\n{output}', parent=self)
            except (OSError, ValueError) as exc:
                self.report_error(exc)

    def import_profile(self):
        source = filedialog.askdirectory(parent=self, title='Выберите папку с файлами настроек .ini / .json')
        if source:
            try:
                self.store.import_folder(source)
                self.refresh()
                messagebox.showinfo('Импорт завершён', 'Настройки импортированы в профили.', parent=self)
            except (OSError, ValueError) as exc:
                self.report_error(exc)

    def apply_profile(self):
        item = self.selected()
        if not item:
            return
        running = is_game_running()
        if running is True:
            messagebox.showwarning("Игра запущена", "Закройте Escape from Tarkov перед применением профиля.", parent=self)
            return
        if running is None and not messagebox.askyesno("Не удалось проверить игру",
                "Не удалось определить, запущен ли Tarkov. Вы уверены, что игра закрыта?", parent=self):
            return
        folder, manifest, files = item
        if not messagebox.askyesno("Применить профиль",
                f"Применить «{manifest['name']}»?\n\nБудут заменены файлы: {', '.join(files)}.\n"
                "Перед заменой будет создана резервная копия текущих настроек.\n"
                "Другие файлы настроек останутся без изменений.", parent=self):
            return
        try:
            backup = self.store.apply(folder, manifest)
            msg = "Настройки применены. Запустите игру для проверки."
            if backup:
                msg += f"\n\nРезервная копия:\n{backup}"
            messagebox.showinfo("Готово", msg, parent=self)
            self.refresh()
            self.cat_panel.next_meme()
        except Exception as exc:
            self.report_error(exc)


if __name__ == "__main__":
    App().mainloop()
