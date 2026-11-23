"""
Синхронизация сохранений игр через облачную папку.
Копируется ТОЛЬКО папка сохранений, которую ты укажешь, а не вся игра.

Команды:
  save_claude_1.py            - открыть настройки (SETUP)
  save_claude_1.py run        - pull -> запуск игры -> ждать закрытия -> push (так работает ярлык)
  save_claude_1.py pull       - забрать сохранение из облака
  save_claude_1.py push       - отправить сохранение в облако
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime

# GUI application: discard all console output (also in source/Python mode).
try:
    _silent_stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _silent_stream
    sys.stderr = _silent_stream
except Exception:
    pass

# Prevent Windows from briefly creating a console for helper subprocesses.
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from pathlib import Path

BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
LOG = BASE / "sync_log.txt"
BACKUPS = BASE / "backups"
KEEP_BACKUPS = 3
ICON_FILE = BASE / "app_icon.ico"   # иконка приложения (копия иконки ярлыка/exe игры)
ICON_PNG = BASE / "app_icon.png"    # она же картинкой - для показа в окне
LOCAL_STATE = BASE / "sync_state.json"   # что считалось синхронизированным в прошлый раз
STATE_NAME = ".gss_state.json"      # такая же отметка лежит в облачной папке
CLOUD_WAIT_MAX = 45      # сколько секунд ждать, пока клиент облака докачает файлы
CLOUD_WAIT_STABLE = 6    # файлы не меняются столько секунд - считаем облако готовым
CLOUD_WAIT_POLL = 2      # как часто проверять
MAX_SIZE_MB = 2000  # защитный лимит для папок сохранений разных игр


def log(message: str) -> None:
    """Журнал с точным временем шагов (консоли нет). Файл sync_log.txt рядом с программой."""
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            LOG.write_text("", encoding="utf-8")
        now = datetime.now()
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{now:%d.%m %H:%M:%S}.{now.microsecond // 1000:03d}  {message}\n")
    except Exception:
        pass


# ---------- прогресс ----------
class Progress:
    """Полоса прогресса по числу скопированных файлов."""

    def __init__(self, total: int, label: str):
        self.total, self.done, self.label = total, 0, label
        self.draw()

    def draw(self) -> None:
        width = 30
        frac = self.done / self.total if self.total else 1
        filled = int(width * frac)
        bar = "#" * filled + "-" * (width - filled)
        print(f"\r{self.label} [{bar}] {int(frac * 100):3d}% ({self.done}/{self.total})",
              end="", flush=True)

    def step(self) -> None:
        self.done += 1
        self.draw()

    def finish(self) -> None:
        print()


def is_ignored(name: str) -> bool:
    """Служебные файлы и папки облачных клиентов (например .tmp.drivedownload от Google
    Drive): это не сохранения, их не синхронизируем и не кладём в резервные копии."""
    n = name.lower()
    return (n.startswith(".tmp.drive")
            or n in {"desktop.ini", "thumbs.db", ".ds_store", STATE_NAME})


def iter_files(folder: Path):
    """Все файлы папки, кроме служебных."""
    folder = Path(folder)
    if not folder.is_dir():
        return
    for p in folder.rglob("*"):
        if p.is_file() and not any(is_ignored(part) for part in p.relative_to(folder).parts):
            yield p


def count_files(folder: Path) -> int:
    return sum(1 for _ in iter_files(folder))


def fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------- вспомогательное ----------
def folder_size_mb(folder: Path) -> float:
    return sum(p.stat().st_size for p in iter_files(folder)) / 1024 / 1024


def latest_mtime(folder: Path) -> float:
    """Время изменения самого свежего файла в папке (0, если пусто)."""
    return max((p.stat().st_mtime for p in iter_files(folder)), default=0.0)


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S") if ts else "нет данных"


def check_save_dir(save: Path) -> bool:
    """Проверяет доступность и разумный размер выбранной папки сохранений."""
    if save.exists() and folder_size_mb(save) > MAX_SIZE_MB:
        print(f"В папке больше {MAX_SIZE_MB} МБ - для сохранений это слишком много. "
              f"Проверь путь: {save}")
        return False
    return True


def load_config() -> dict:
    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if "save_dir" not in cfg:
        print("Первый запуск - нужна настройка.\n")
        setup()
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        print()
    return cfg


def setup() -> None:
    print("=== Настройка (пути без кавычек) ===")
    print(r"Укажи папку сохранений выбранной игры (обычно она находится в AppData, Documents или рядом с игрой).")
    save_dir = input("Путь к папке сохранений (save): ").strip().strip('"')
    if not check_save_dir(Path(save_dir)):
        sys.exit(1)
    cloud_dir = input("Папка в облаке (например C:\\Users\\Имя\\OneDrive\\zelda_saves): ").strip().strip('"')
    game_exe = input("Путь к EXE-файлу игры для команды run (Enter - пропустить): ").strip().strip('"')
    CONFIG.write_text(
        json.dumps({"save_dir": save_dir, "cloud_dir": cloud_dir, "game_exe": game_exe},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Готово, config.json сохранён.")


# ---------- определение запущенной игры ----------
def _process_running_winapi(name: str) -> bool:
    """Мгновенная проверка по списку процессов через WinAPI (без запуска tasklist)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
    if snapshot is None or snapshot == ctypes.c_void_p(-1).value:
        raise OSError("snapshot failed")
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        target = name.lower()
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                return True
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        k32.CloseHandle(snapshot)


def list_process_names():
    """Имена запущенных процессов через WinAPI: мгновенно, без запуска tasklist.
    Возвращает set в нижнем регистре или None, если способ не сработал."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wintypes.BOOL
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        names = set()
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                names.add(entry.szExeFile.lower())
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        return names or None
    except Exception:
        return None


def process_running(name: str) -> bool:
    """Есть ли в системе запущенный процесс с таким именем."""
    names = list_process_names()
    if names is not None:
        return name.lower() in names
    try:                                   # запасной способ
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, creationflags=NO_CONSOLE
        ).stdout
        return name.lower() in out.lower()
    except Exception:
        return False


def lock_in_use(lock: Path) -> bool:
    """save.lock может оставаться на диске после игры; занят он, только если его держит игра."""
    if not lock.exists():
        return False
    try:
        with open(lock, "r+b"):
            return False
    except OSError:
        return True


def game_process_name(cfg: dict) -> str:
    """Имя процесса самой игры. Окно настроек (Configuration.exe) игрой не считается."""
    if cfg.get("process_name"):
        return cfg["process_name"]
    name = Path(cfg.get("game_exe") or "").name
    if not name or "config" in name.lower():
        return ""
    return name


def is_game_running(cfg: dict) -> bool:
    lock = Path(cfg["save_dir"]).parent / "save.lock"
    return (bool(game_process_name(cfg)) and process_running(game_process_name(cfg))) or lock_in_use(lock)


def game_running(cfg: dict) -> bool:
    if is_game_running(cfg):
        print("Игра ещё запущена. Закрой её полностью (проверь диспетчер задач) и повтори.")
        return True
    return False


# ---------- копирование ----------
def files_differ(src: Path, dst: Path) -> bool:
    """Быстрая проверка: отличается ли файл по размеру или времени изменения."""
    try:
        a, b = src.stat(), dst.stat()
        return a.st_size != b.st_size or a.st_mtime_ns != b.st_mtime_ns
    except OSError:
        return True


# Новый формат: 2026-11-11__19-03; прежний (с секундами) тоже распознаётся: 2026-11-11_19-03-40
BACKUP_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_{1,2}\d{2}-\d{2}(-\d{2})?(_\d+)?$")


def get_backup_dir(cfg: dict) -> Path:
    """Папка резервных копий: из настроек или 'backups' рядом с программой."""
    return Path(cfg.get("backup_dir") or BACKUPS)


def is_inside(child: Path, parent: Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def list_backups(backup_dir: Path) -> list:
    """Резервные копии от самой старой к самой новой (чужие папки не трогаем)."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []
    found = [p for p in backup_dir.iterdir() if p.is_dir() and BACKUP_NAME_RE.match(p.name)]

    def created(p: Path) -> float:
        info = p / "info.txt"
        return (info if info.exists() else p).stat().st_mtime

    return sorted(found, key=created)


def folder_files(folder: Path) -> dict:
    """{относительный путь: файл} для всех файлов папки (info.txt копии не считается)."""
    folder = Path(folder)
    return {
        p.relative_to(folder).as_posix(): p
        for p in iter_files(folder)
        if p.relative_to(folder).as_posix() != "info.txt"
    }


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def folders_identical(a: Path, b: Path) -> bool:
    """Одинаковы ли папки по набору файлов и их содержимому."""
    fa, fb = folder_files(a), folder_files(b)
    if fa.keys() != fb.keys():
        return False
    for rel, pa in fa.items():
        pb = fb[rel]
        if pa.stat().st_size != pb.stat().st_size or file_hash(pa) != file_hash(pb):
            return False
    return True


def make_backup(cfg: dict, source: Path, reason: str = "") -> bool:
    """Делает полную копию папки сохранения.
    - если файлы такие же, как в одной из уже имеющихся копий - копию не создаёт;
    - хранит максимум KEEP_BACKUPS копий: когда место закончилось, новая копия
      заменяет самую старую.
    Возвращает True, если копия создана."""
    source = Path(source)
    files = folder_files(source) if source.is_dir() else {}
    if not files:
        return False

    backup_dir = get_backup_dir(cfg)
    existing = list_backups(backup_dir)
    for old in existing:
        if folders_identical(source, old):
            log(f"Резервная копия не нужна: файлы совпадают с {old.name}")
            return False

    # имя копии = дата последнего сохранения
    saved_at = datetime.fromtimestamp(max(p.stat().st_mtime for p in files.values()))
    name = f"{saved_at:%Y-%m-%d__%H-%M}"
    target, n = backup_dir / name, 2
    while target.exists():
        target = backup_dir / f"{name}_{n}"
        n += 1

    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(source, target,
                        ignore=lambda d, names: [n for n in names if is_ignored(n)])
        (target / "info.txt").write_text(
            f"Дата последнего сохранения: {saved_at:%d.%m.%Y %H:%M:%S}\n"
            f"Резервная копия создана: {datetime.now():%d.%m.%Y %H:%M:%S}\n"
            f"Откуда: {source}\n"
            + (f"Причина: {reason}\n" if reason else ""),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(target, ignore_errors=True)    # не оставляем недописанную копию
        raise

    # Новая копия заменяет самую старую, если их уже больше KEEP_BACKUPS.
    all_backups = list_backups(backup_dir)
    for old in all_backups[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)
        log(f"Старая резервная копия заменена: {old.name}")
    log(f"Создана резервная копия: {target.name}")
    return True


def replace_dir(src: Path, dst: Path, progress: Progress = None) -> None:
    """Синхронизирует папки, копируя только новые и изменённые файлы."""
    src, dst = Path(src), Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    src_names = {p.name for p in src.iterdir() if not is_ignored(p.name)}

    # Удаляем элементы, которых больше нет в источнике (служебные файлы облака не трогаем).
    for item in list(dst.iterdir()):
        if item.name not in src_names and not is_ignored(item.name):
            shutil.rmtree(item) if item.is_dir() else item.unlink()

    for item in src.iterdir():
        if is_ignored(item.name):
            continue
        target = dst / item.name
        if item.is_dir():
            if target.exists() and not target.is_dir():
                target.unlink()
            replace_dir(item, target, progress)
        else:
            if target.is_dir():
                shutil.rmtree(target)
            if not target.exists() or files_differ(item, target):
                shutil.copy2(item, target)
            if progress:
                progress.step()


def notify_error(message: str) -> None:
    """Окно с предупреждением (консоли в этой версии нет, поэтому print не виден)."""
    print(message)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        apply_app_icon(root)
        root.attributes("-topmost", True)
        messagebox.showwarning("Game Save Sync", message, parent=root)
        root.destroy()
    except Exception:
        pass


class GameReturned(Exception):
    """После закрытия игры процесс появился снова (перезапуск игры) - отправлять рано."""


GAME_RETURNED = "game_returned"


class TaskProgress:
    """Счётчик, который заполняет рабочий поток, а читает окно прогресса.
    Через ask() рабочий поток может задать пользователю вопрос с выбором ответа."""

    def __init__(self):
        import threading
        self.total = 0
        self.done = 0
        self.status = "Подготовка..."
        self.close_after = 2            # через сколько секунд закрыть окно после завершения
        self.ready = threading.Event()  # окно создано (или графики нет)
        self.interactive = False
        self.question = None
        self._answered = threading.Event()
        self.answer = 0

    def set_total(self, total: int) -> None:
        self.total, self.done = total, 0

    def step(self) -> None:
        self.done += 1

    def ask(self, text: str, options: list, default: int = 0) -> int:
        """Показывает окно с вопросом и ждёт выбор. Без графики возвращает default."""
        self.ready.wait(timeout=10)
        if not self.interactive:
            return default
        self.answer = default
        self._answered.clear()
        self.question = (text, options, default)
        self._answered.wait()
        return self.answer


def choice_dialog(root, text: str, options: list, default: int = 0) -> int:
    """Окно с вопросом и кнопками-вариантами. Закрыть без выбора нельзя."""
    import tkinter as tk
    from tkinter import ttk
    dlg = tk.Toplevel(root)
    dlg.title("Game Save Sync")
    dlg.configure(bg="#111827")
    dlg.attributes("-topmost", True)
    dlg.resizable(False, False)
    dlg.protocol("WM_DELETE_WINDOW", lambda: None)
    result = {"value": default}

    def choose(index: int) -> None:
        result["value"] = index
        dlg.destroy()

    tk.Label(dlg, text=text, bg="#111827", fg="#f9fafb", font=("Segoe UI", 10),
             justify="left", wraplength=520).pack(padx=24, pady=(20, 12))
    for index, option in enumerate(options):
        ttk.Button(dlg, text=option, style="Close.TButton",
                   command=lambda i=index: choose(i)).pack(fill="x", padx=24, pady=4)
    tk.Label(dlg, text="", bg="#111827").pack(pady=4)
    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    dlg.geometry(f"+{(dlg.winfo_screenwidth() - w) // 2}+{(dlg.winfo_screenheight() - h) // 3}")
    dlg.grab_set()
    dlg.wait_window()
    return result["value"]


def run_with_progress_window(heading: str, task) -> bool:
    """Выполняет task(progress) в отдельном потоке и показывает окно с прогресс-баром.
    task возвращает итоговый текст. Возвращает True, если задача прошла без ошибок."""
    import threading

    progress = TaskProgress()
    result: dict = {}
    log(f"Окно '{heading}': запуск")

    def worker():
        try:
            result["text"] = task(progress)
        except GameReturned:
            result["returned"] = True
        except Exception as exc:
            result["error"] = exc
        finally:
            result["finished"] = True
            log(f"Окно '{heading}': задача завершена - {result.get('text') or result.get('error')}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        progress.interactive = True
        progress.ready.set()
    except Exception:           # нет графики - просто ждём окончания копирования
        progress.interactive = False
        progress.ready.set()
        thread.join()
        return "error" not in result

    bg, fg_sub = "#111827", "#9ca3af"
    root.title("Game Save Sync")
    apply_app_icon(root)
    root.configure(bg=bg)
    root.resizable(False, False)
    w, h = 540, 240
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}"
                  f"+{(root.winfo_screenheight() - h) // 3}")
    root.attributes("-topmost", True)    # чтобы окно появилось поверх всего после игры
    root.protocol("WM_DELETE_WINDOW",
                  lambda: root.destroy() if result.get("finished") else None)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Zelda.Horizontal.TProgressbar", troughcolor="#374151",
                    background="#22c55e", bordercolor="#374151",
                    lightcolor="#22c55e", darkcolor="#22c55e")
    style.configure("Close.TButton", font=("Segoe UI", 10), padding=8)

    frame = tk.Frame(root, bg=bg, padx=24, pady=20)
    frame.pack(fill="both", expand=True)
    tk.Label(frame, text=heading, bg=bg, fg="#f9fafb",
             font=("Segoe UI", 15, "bold")).pack(anchor="w")
    status_var = tk.StringVar(value=progress.status)
    tk.Label(frame, textvariable=status_var, bg=bg, fg=fg_sub, font=("Segoe UI", 10),
             wraplength=490, justify="left").pack(anchor="w", pady=(8, 12))
    bar = ttk.Progressbar(frame, style="Zelda.Horizontal.TProgressbar",
                          mode="determinate", length=490)
    bar.pack(fill="x")
    counter_var = tk.StringVar()
    tk.Label(frame, textvariable=counter_var, bg=bg, fg=fg_sub,
             font=("Segoe UI", 9)).pack(anchor="e", pady=(4, 0))

    bottom = tk.Frame(frame, bg=bg)
    bottom.pack(fill="x", side="bottom")
    hint_var = tk.StringVar()
    tk.Label(bottom, textvariable=hint_var, bg=bg, fg=fg_sub,
             font=("Segoe UI", 9)).pack(side="left")
    close_btn = ttk.Button(bottom, text="Закрыть", style="Close.TButton",
                           state="disabled", command=root.destroy)
    close_btn.pack(side="right")

    def countdown(seconds: int) -> None:
        if seconds <= 0:
            root.destroy()
            return
        hint_var.set(f"Окно закроется через {seconds} с")
        root.after(1000, countdown, seconds - 1)

    def finish() -> None:
        bar.configure(maximum=max(progress.total, 1), value=max(progress.total, 1))
        counter_var.set(f"{progress.total} / {progress.total}" if progress.total else "")
        close_btn.configure(state="normal")
        if "error" in result:
            status_var.set(f"Ошибка: {result['error']}")
        else:
            status_var.set(result.get("text") or "Готово.")
            countdown(progress.close_after)

    def poll() -> None:
        if progress.question is not None:              # рабочий поток задал вопрос
            text, options, default = progress.question
            progress.question = None
            progress.answer = choice_dialog(root, text, options, default)
            progress._answered.set()
        total, done = progress.total, progress.done
        if total:
            bar.configure(maximum=total, value=min(done, total))
            counter_var.set(f"{min(done, total)} / {total}")
        status_var.set(progress.status)
        if result.get("finished"):
            if result.get("returned"):
                root.destroy()              # игра запустилась снова - окно просто закрываем
            else:
                finish()
        else:
            root.after(50, poll)

    poll()
    root.update()
    log(f"Окно '{heading}': показано на экране")
    root.mainloop()
    return "error" not in result


# ---------- сравнение сохранений по содержимому ----------
def file_digest(path: Path) -> str:
    """Отпечаток файла. Для очень больших файлов - размер, начало и конец."""
    path = Path(path)
    size = path.stat().st_size
    if size <= 256 * 1024 * 1024:
        return file_hash(path)
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(4 * 1024 * 1024))
        f.seek(max(0, size - 4 * 1024 * 1024))
        h.update(f.read())
    return h.hexdigest()


def tree_digest(folder: Path) -> str:
    """Один отпечаток на всю папку сохранений (пустая папка -> пустая строка).
    Не зависит от времени изменения файлов и часов компьютера."""
    folder = Path(folder)
    files = sorted(iter_files(folder), key=lambda p: p.relative_to(folder).as_posix())
    if not files:
        return ""
    h = hashlib.sha256()
    for path in files:
        h.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        h.update(file_digest(path).encode() + b"\0")
    return h.hexdigest()


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_cloud_state(cloud: Path):
    data = read_json(Path(cloud) / STATE_NAME)
    return data if isinstance(data, dict) else None


def write_cloud_state(cloud: Path, digest: str) -> None:
    Path(cloud).mkdir(parents=True, exist_ok=True)
    write_json_atomic(Path(cloud) / STATE_NAME, {
        "digest": digest, "device": platform.node(),
        "saved_at": datetime.now().isoformat(timespec="seconds")})


def read_local_state(cfg: dict):
    """Отпечаток, который на этом компьютере считался синхронизированным с облаком."""
    data = read_json(LOCAL_STATE)
    if (isinstance(data, dict) and data.get("save_dir") == str(cfg["save_dir"])
            and data.get("cloud_dir") == str(cfg["cloud_dir"])):
        return data
    return None


def write_local_state(cfg: dict, digest: str) -> None:
    write_json_atomic(LOCAL_STATE, {
        "digest": digest, "save_dir": str(cfg["save_dir"]), "cloud_dir": str(cfg["cloud_dir"]),
        "synced_at": datetime.now().isoformat(timespec="seconds")})


def wait_for_cloud(cloud: Path, p: TaskProgress):
    """Клиент облака может ещё докачивать файлы с другого компьютера. Пока файлы в облачной
    папке не совпадают с отметкой .gss_state.json - ждём. Если файлы перестали меняться,
    а отметка старая, значит их правили вручную: отметке не верим.
    Возвращает (отметка, отпечаток папки, готово ли облако)."""
    start, last_digest, stable_since = time.time(), None, time.time()
    while True:
        state, digest = read_cloud_state(cloud), tree_digest(cloud)
        if not state or state.get("digest") == digest:
            return state, digest, True
        if digest != last_digest:
            last_digest, stable_since = digest, time.time()
        elif time.time() - stable_since >= CLOUD_WAIT_STABLE:
            log("Отметка в облаке устарела (файлы правили вручную) - игнорирую её")
            return None, digest, True
        left = CLOUD_WAIT_MAX - (time.time() - start)
        if left <= 0:
            log("Облако не досинхронизировалось: файлы не совпадают с отметкой")
            return state, digest, False
        p.status = f"Жду, пока облако закончит синхронизацию... {int(left)} с"
        time.sleep(CLOUD_WAIT_POLL)


def plan_sync(local_digest: str, cloud_digest: str, local_state) -> str:
    """Что делать с сохранениями. Сравнивает с состоянием последней синхронизации:
    same - одинаковые, pull - в облаке новее, push - на этом ПК новее,
    conflict - менялись и тут и там, unknown - истории нет, а файлы разные."""
    if local_digest == cloud_digest:
        return "same"
    if not cloud_digest:
        return "push"
    if not local_digest:
        return "pull"
    if local_state is None:
        return "unknown"
    base = local_state.get("digest")
    local_changed, cloud_changed = local_digest != base, cloud_digest != base
    if cloud_changed and not local_changed:
        return "pull"
    if local_changed and not cloud_changed:
        return "push"
    return "conflict"


def describe_side(state, folder: Path) -> str:
    if state and state.get("saved_at"):
        try:
            when = datetime.fromisoformat(state["saved_at"]).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            when = str(state["saved_at"])
        return when + (f", {state['device']}" if state.get("device") else "")
    ts = latest_mtime(folder)
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "нет данных"


def ask_which_save(p: TaskProgress, local: Path, cloud: Path, state, context: str) -> str:
    """Спрашивает, какое сохранение оставить: 'cloud' или 'local'."""
    local_when, cloud_when = describe_side(None, local), describe_side(state, cloud)
    default = 0 if latest_mtime(cloud) > latest_mtime(local) else 1
    text = (f"{context}\n\nЭтот компьютер: {local_when}\nОблако: {cloud_when}\n\n"
            "Какое сохранение оставить? Второе не пропадёт: оно будет в резервной копии.")
    answer = p.ask(text, [f"Взять из облака ({cloud_when})",
                          f"Оставить с этого компьютера ({local_when})"], default)
    return "cloud" if answer == 0 else "local"


def take_from_cloud(cfg: dict, p: TaskProgress, local: Path, cloud: Path) -> None:
    p.status = "Делаю резервную копию..."
    make_backup(cfg, local, "перед загрузкой из облака")
    p.status = "Скачиваю сохранение из облака..."
    p.set_total(count_files(cloud))
    replace_dir(cloud, local, p)
    write_local_state(cfg, tree_digest(local))


def send_to_cloud(cfg: dict, p: TaskProgress, local: Path, cloud: Path,
                  backup_cloud: bool = False) -> None:
    if backup_cloud:
        p.status = "Делаю резервную копию сохранения из облака..."
        make_backup(cfg, cloud, "облако перед заменой")
    p.status = "Отправляю сохранение в облако..."
    p.set_total(count_files(local))
    replace_dir(local, cloud, p)
    digest = tree_digest(local)
    write_cloud_state(cloud, digest)          # отметку пишем последней
    write_local_state(cfg, digest)


def sync_before_game(cfg: dict, p: TaskProgress) -> str:
    """Перед игрой: берём из облака более свежее сохранение (например, с ноутбука)."""
    local, cloud = Path(cfg["save_dir"]), Path(cfg["cloud_dir"])
    p.status = "Проверяю облако..."
    state, cloud_digest, consistent = wait_for_cloud(cloud, p)
    if not consistent:
        p.close_after = 5
        return ("Облако ещё синхронизируется, поэтому игра запущена с сохранением этого "
                "компьютера. Когда закроешь игру, программа спросит, какое сохранение "
                "оставить, если в облаке появится другое.")
    local_digest = tree_digest(local)
    plan = plan_sync(local_digest, cloud_digest, read_local_state(cfg))
    log(f"Проверка перед игрой: {plan}")

    if plan == "same":
        write_local_state(cfg, local_digest)
        p.close_after = 0
        return "Сохранения совпадают."
    if plan == "push":
        p.close_after = 1
        return "На этом компьютере сохранение новее облачного - оно отправится после игры."
    if plan == "pull":
        take_from_cloud(cfg, p, local, cloud)
        return f"Готово: скачано свежее сохранение из облака ({describe_side(state, cloud)})."

    context = ("Сохранения на этом компьютере и в облаке отличаются." if plan == "unknown"
               else "С последней синхронизации сохранение менялось и здесь, и в облаке.")
    if ask_which_save(p, local, cloud, state, context) == "cloud":
        take_from_cloud(cfg, p, local, cloud)
        return "Взято сохранение из облака. Прежнее лежит в резервной копии."
    send_to_cloud(cfg, p, local, cloud, backup_cloud=True)
    return "Оставлено сохранение этого компьютера, облако обновлено. Прежнее облачное лежит в резервной копии."


def sync_after_game(cfg: dict, p: TaskProgress, note: str) -> str:
    """После игры: отправляем сохранение в облако, не затирая более новое с другого компьютера."""
    local, cloud = Path(cfg["save_dir"]), Path(cfg["cloud_dir"])
    p.status = "Проверяю облако..."
    forced = False
    for _ in range(5):
        state, cloud_digest, consistent = wait_for_cloud(cloud, p)
        if consistent:
            break
        answer = p.ask("Облако ещё не досинхронизировалось (клиент облака докачивает файлы с другого "
                       "компьютера). Если отправить сейчас, можно затереть более новое сохранение.\n\n"
                       "Что сделать?",
                       ["Подождать ещё", "Отправить всё равно (заменит облако)", "Не отправлять сейчас"], 2)
        if answer == 1:
            forced = True
            break
        if answer == 2:
            return ("Отправка отложена: сохранение осталось на этом компьютере "
                    "(копия есть в резервных копиях)." + note)
    else:
        return "Облако так и не досинхронизировалось, отправка отложена." + note

    local_digest = tree_digest(local)
    plan = "push" if forced else plan_sync(local_digest, cloud_digest, read_local_state(cfg))
    log(f"Проверка после игры: {plan}")
    done_text = ("Готово! Файлы лежат в папке облака. Подожди, пока клиент облака закончит "
                 "загрузку (значок в трее без стрелок).")

    if plan == "same":
        write_local_state(cfg, local_digest)
        return "В облаке уже актуальная версия, отправлять нечего." + note
    if plan == "pull":
        take_from_cloud(cfg, p, local, cloud)
        return "Игра ничего не изменила, а в облаке появилось более свежее сохранение - оно скачано." + note
    if plan == "push":
        send_to_cloud(cfg, p, local, cloud, backup_cloud=forced)
        return done_text + note

    context = ("Пока шла игра, в облаке появилось другое сохранение (например, с другого компьютера)."
               if plan == "conflict" else "Сохранения на этом компьютере и в облаке отличаются.")
    if ask_which_save(p, local, cloud, state, context) == "cloud":
        take_from_cloud(cfg, p, local, cloud)
        return "Взято сохранение из облака. Сохранение этой игры лежит в резервной копии." + note
    send_to_cloud(cfg, p, local, cloud, backup_cloud=True)
    return done_text + " Прежнее облачное сохранение лежит в резервной копии." + note


def pull(cfg: dict) -> bool:
    local = Path(cfg["save_dir"])
    if not check_save_dir(local):
        notify_error("Проверь путь к папке сохранений в настройках (должна быть папка save).")
        return False
    if game_running(cfg):
        notify_error("Игра ещё запущена. Закрой её полностью и повтори.")
        return False
    return run_with_progress_window("Проверка облака", lambda p: sync_before_game(cfg, p))


def push(cfg: dict, settle: bool = False) -> bool:
    """settle=True: игра только что закрылась - окно появляется сразу,
    а короткая пауза (пока игра дописывает файлы) и все проверки идут внутри окна."""
    local, cloud = Path(cfg["save_dir"]), Path(cfg["cloud_dir"])
    returned = {"flag": False}

    def task(p: TaskProgress) -> str:
        p.status = "Игра закрыта. Проверяю сохранение..." if settle else "Проверяю сохранение..."
        if settle:
            # Игра могла просто перезапуститься (обновление, лаунчер): несколько секунд
            # убеждаемся, что процесс не появился снова.
            deadline = time.time() + float(cfg.get("close_grace_seconds", 5))
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                if is_game_running(cfg):
                    returned["flag"] = True
                    raise GameReturned()
                p.status = f"Игра закрыта. Проверяю, что она не запустилась снова... {int(left) + 1} с"
                time.sleep(0.25)
        if not check_save_dir(local):
            raise RuntimeError("Проверь путь к папке сохранений в настройках "
                               "(должна быть папка save).")
        # процесс уже завершён; если игра ещё отпускает файлы - ждём до 15 секунд
        for _ in range(30):
            if not is_game_running(cfg):
                break
            p.status = "Жду, пока игра полностью завершится..."
            time.sleep(0.5)
        else:
            raise RuntimeError("Игра ещё запущена, сохранение не отправлено. "
                               "Закрой игру полностью и запусти программу ещё раз.")
        p.status = "Делаю резервную копию..."
        note = (" Резервная копия создана." if make_backup(cfg, local, "после игры")
                else " Файлы не изменились - новая копия не нужна.")
        return sync_after_game(cfg, p, note)

    ok = run_with_progress_window("Отправка в облако", task)
    return GAME_RETURNED if returned["flag"] else ok


# ---------- запуск игры ----------
def clean_child_environment() -> dict:
    """Окружение для игры без служебных путей приложения (PyInstaller _MEI...).
    Иначе игра может загрузить оттуда DLL и не даст приложению удалить свою временную папку."""
    env = os.environ.copy()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bad = os.path.normcase(os.path.abspath(meipass))
        keep = [part for part in env.get("PATH", "").split(os.pathsep)
                if part and not os.path.normcase(os.path.abspath(part)).startswith(bad)]
        env["PATH"] = os.pathsep.join(keep)
    for key in list(env):
        if key.upper().startswith(("_MEIPASS", "_PYI", "PYI_", "_PYINSTALLER")):
            env.pop(key, None)
    return env


def run(cfg: dict) -> None:
    exe = Path(cfg.get("game_exe") or "")
    if not exe.is_file():
        print("Путь к EXE-файлу игры не указан или неверный. Открой настройки и выбери исполняемый файл игры.")
        sys.exit(1)
    log("=== Запуск программы ===")
    if not pull(cfg):
        sys.exit(1)

    process_name = game_process_name(cfg)
    # Запущенный файл и есть игра? Тогда следим и за его процессом.
    # Если запущен лаунчер/окно настроек, он может жить дольше игры - его игнорируем.
    own_process = exe.name.lower() == process_name.lower()
    start_timeout = 60 if own_process else 300

    log(f"Запускаю {exe.name}; слежу за процессом {process_name}")
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=NO_CONSOLE,
                            env=clean_child_environment())

    started, seen = time.time(), False
    while True:
        running = (own_process and proc.poll() is None) or is_game_running(cfg)
        if running:
            if not seen:
                seen = True
                names = list_process_names() or set()
                log("Процесс игры запущен. Похожие процессы: "
                    + (", ".join(sorted(n for n in names if "savage" in n)) or "не найдены"))
        elif seen:
            log("Игра закрыта - открываю окно отправки")
            if push(cfg, settle=True) == GAME_RETURNED:
                log("Игра запустилась снова (перезапуск) - продолжаю следить")
                time.sleep(0.2)
                continue
            break                                   # игра закрыта, сохранение отправлено
        elif time.time() - started > start_timeout:
            log("Процесс игры так и не появился")
            push(cfg, settle=True)
            break
        time.sleep(0.2)

    log("=== Программа завершена ===")


# ---------- иконка приложения (копируется с ярлыка / exe игры) ----------
ICON_EXTENSIONS = {".ico", ".exe", ".dll", ".lnk", ".url",
                   ".png", ".jpg", ".jpeg", ".bmp", ".gif"}


def ps_quote(text: str) -> str:
    """Строка для PowerShell в одинарных кавычках."""
    return "'" + str(text).replace("'", "''") + "'"


def run_powershell(script: str, timeout: int = 40) -> str:
    """Запускает PowerShell (только Windows). Скрипт передаётся в Unicode, вывод читаем как UTF-8."""
    import base64
    if os.name != "nt":
        raise RuntimeError("Копирование иконки работает только в Windows.")
    script = ("try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\n"
              "$ErrorActionPreference='Stop'\n") + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_CONSOLE, timeout=timeout,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "ошибка PowerShell").strip()[:300])
    return done.stdout.strip()


def parse_icon_location(text: str):
    """'C:\\path\\game.exe,2' -> ('C:\\path\\game.exe', 2). Пустая строка -> ('', 0)."""
    text = os.path.expandvars((text or "").strip().strip('"'))
    if "," in text:
        path, _, index = text.rpartition(",")
        try:
            return path.strip().strip('"'), int(index.strip())
        except ValueError:
            pass
    return text, 0


def icon_source_from_url_file(path: Path) -> str:
    """У ярлыков .url (Steam, Epic и др.) иконка указана строкой IconFile=..."""
    for encoding in ("utf-8", "cp1251"):
        try:
            for line in Path(path).read_text(encoding=encoding).splitlines():
                if line.strip().lower().startswith("iconfile="):
                    return os.path.expandvars(line.split("=", 1)[1].strip().strip('"'))
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def resolve_icon_source(source: str):
    """Определяет, откуда брать картинку. Возвращает (путь к файлу, номер иконки)."""
    src = Path(source.strip().strip('"'))
    ext = src.suffix.lower()
    if ext == ".lnk":                                   # обычный ярлык Windows
        out = run_powershell(
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(%s)\n"
            "Write-Output $s.IconLocation\nWrite-Output $s.TargetPath" % ps_quote(src))
        lines = out.splitlines() + ["", ""]
        icon_path, index = parse_icon_location(lines[0])
        target = lines[1].strip()
        for candidate in (icon_path, target):
            if candidate and Path(candidate).is_file():
                return Path(candidate), (index if candidate == icon_path else 0)
        raise RuntimeError("у ярлыка нет иконки и не найден файл, на который он указывает")
    if ext == ".url":                                   # интернет-ярлык (Steam и т.п.)
        icon_path = icon_source_from_url_file(src)
        if icon_path and Path(icon_path).is_file():
            return Path(icon_path), 0
        raise RuntimeError("в этом .url-ярлыке не указан файл иконки")
    if ext in ICON_EXTENSIONS:
        return src, 0
    raise RuntimeError(f"неподдерживаемый тип файла: {ext or 'без расширения'}")


def is_ico(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x00\x00\x01\x00"
    except OSError:
        return False


ICON_SCRIPT_HEAD = r"""
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace W -Name N -MemberDefinition '[DllImport("shell32.dll", CharSet=CharSet.Unicode)] public static extern uint ExtractIconEx(string f, int i, IntPtr[] large, IntPtr[] small, uint n);'
$src = @SRC@
$ico = @ICO@
$png = @PNG@
"""
ICON_SCRIPT_EXE = r"""
$large = New-Object IntPtr[] 1
$small = New-Object IntPtr[] 1
$n = [W.N]::ExtractIconEx($src, @INDEX@, $large, $small, 1)
if ($n -gt 0 -and $large[0] -ne [IntPtr]::Zero) { $icon = [System.Drawing.Icon]::FromHandle($large[0]) }
else { $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($src) }
$fs = [System.IO.File]::Create($ico); $icon.Save($fs); $fs.Close()
$icon.ToBitmap().Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
"""
ICON_SCRIPT_IMAGE = r"""
$img = [System.Drawing.Image]::FromFile($src)
$bmp = New-Object System.Drawing.Bitmap($img, 64, 64)
$img.Dispose()
$icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
$fs = [System.IO.File]::Create($ico); $icon.Save($fs); $fs.Close()
$bmp.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
"""
ICON_SCRIPT_ICO_PNG = r"""
$icon = New-Object System.Drawing.Icon($src, 64, 64)
$icon.ToBitmap().Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
"""


def build_icon_script(kind: str, src: Path, index: int, ico: Path, png: Path) -> str:
    body = {"exe": ICON_SCRIPT_EXE, "image": ICON_SCRIPT_IMAGE, "ico": ICON_SCRIPT_ICO_PNG}[kind]
    text = ICON_SCRIPT_HEAD + body
    for key, value in (("@SRC@", ps_quote(src)), ("@ICO@", ps_quote(ico)),
                       ("@PNG@", ps_quote(png)), ("@INDEX@", str(int(index)))):
        text = text.replace(key, value)
    return text


def update_app_icon(source: str) -> None:
    """Копирует иконку с ярлыка игры (.lnk/.url), exe или картинки в app_icon.ico/.png
    рядом с программой. Дальше эту иконку получают окна приложения и ярлык на рабочем столе."""
    src, index = resolve_icon_source(source)
    ext = src.suffix.lower()
    tmp_ico, tmp_png = BASE / "app_icon.new.ico", BASE / "app_icon.new.png"
    for f in (tmp_ico, tmp_png):
        f.unlink(missing_ok=True)
    try:
        if ext == ".ico":                       # готовая иконка: копируем как есть (все размеры)
            shutil.copyfile(src, tmp_ico)
            try:
                run_powershell(build_icon_script("ico", src, 0, tmp_ico, tmp_png))
            except Exception:
                pass                            # картинка для окна не обязательна
        elif ext in (".exe", ".dll"):
            run_powershell(build_icon_script("exe", src, index, tmp_ico, tmp_png))
        else:
            run_powershell(build_icon_script("image", src, 0, tmp_ico, tmp_png))
        if not tmp_ico.exists() or not is_ico(tmp_ico):
            raise RuntimeError("не удалось получить иконку из выбранного файла")
        os.replace(tmp_ico, ICON_FILE)
        if tmp_png.exists():
            os.replace(tmp_png, ICON_PNG)
        elif ICON_PNG.exists():
            ICON_PNG.unlink()
        log(f"Иконка приложения обновлена из {src}")
    finally:
        for f in (tmp_ico, tmp_png):
            f.unlink(missing_ok=True)


def set_app_user_model_id() -> None:
    """Чтобы в панели задач было видно иконку приложения, а не значок Python."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GameSaveSync.App")
        except Exception:
            pass


def apply_app_icon(root) -> None:
    """Ставит иконку приложения на окно (и на все следующие окна и диалоги)."""
    if os.name == "nt" and ICON_FILE.exists():
        try:
            root.iconbitmap(default=str(ICON_FILE))
        except Exception:
            pass


SHORTCUT_SUFFIX = "gss"


def safe_filename(name: str) -> str:
    """Убирает символы, которые Windows не разрешает в имени файла."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name or "")
    return re.sub(r"\s+", " ", name).strip(" .")


def game_display_name(cfg: dict) -> str:
    """Название игры для ярлыка. Приоритет: game_name из config.json,
    имя ярлыка игры (.lnk/.url), имя exe-файла игры."""
    from pathlib import PureWindowsPath          # понимает и \ и / на любой системе
    custom = safe_filename(cfg.get("game_name") or "")
    if custom:
        return custom
    for key in ("icon_source", "game_exe"):
        text = (cfg.get(key) or "").strip().strip('"')
        if text and PureWindowsPath(text).suffix.lower() in (".lnk", ".url"):
            name = safe_filename(PureWindowsPath(text).stem)
            if name:
                return name
    exe = (cfg.get("game_exe") or "").strip().strip('"')
    if exe:
        name = safe_filename(PureWindowsPath(exe).stem.replace("_", " "))
        if name:
            return name
    return "Game Save Sync"


def shortcut_name(cfg: dict) -> str:
    """Имя файла ярлыка: сначала название игры, потом в скобках (gss)."""
    return f"{game_display_name(cfg)} ({SHORTCUT_SUFFIX})"


def create_desktop_shortcut(name: str = "Game Save Sync") -> Path:
    """Создаёт на рабочем столе ярлык с иконкой игры. Ярлык запускает команду run:
    синхронизация сохранения, запуск игры, отправка в облако после выхода.
    name - имя ярлыка без .lnk, например 'Savage Wind (gss)'."""
    if getattr(sys, "frozen", False):
        target, arguments = sys.executable, "run"
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")      # без чёрного окна
        target = str(pythonw if pythonw.exists() else sys.executable)
        arguments = f'"{Path(__file__).resolve()}" run'
    out = run_powershell(
        "$d=[Environment]::GetFolderPath('Desktop')\n"
        f"$p=Join-Path $d {ps_quote(safe_filename(name) + '.lnk')}\n"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p)\n"
        f"$s.TargetPath={ps_quote(target)}\n"
        f"$s.Arguments={ps_quote(arguments)}\n"
        f"$s.WorkingDirectory={ps_quote(BASE)}\n"
        f"$s.IconLocation={ps_quote(str(ICON_FILE) + ',0')}\n"
        "$s.Save()\nWrite-Output $p")
    return Path(out.splitlines()[-1].strip())


# ---------- графический интерфейс ----------
def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if os.name == "nt":
        try:
            import ctypes
            console = ctypes.windll.kernel32.GetConsoleWindow()
            if console:
                ctypes.windll.user32.ShowWindow(console, 0)
        except Exception:
            pass

    config = {}
    if CONFIG.exists():
        try:
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            config = {}

    LANG = {"ru": {
        "window": "Game Save Sync", "subtitle": "Синхронизация сохранений игр",
        "sections": "РАЗДЕЛЫ", "setup": "⚙  НАСТРОЙКА", "paths": "Настройка путей",
        "description": "Укажи папку сохранений игры, облачную папку и EXE-файл игры.",
        "save": "1. Папка сохранений (save)", "cloud": "2. Папка облака (например OneDrive)",
        "game": "3. Исполняемый файл игры (.exe)",
        "backup": "4. Папка резервных копий (необязательно, максимум 3 копии)",
        "browse": "Обзор…", "save_title": "Выбери папку сохранений (save)",
        "cloud_title": "Выбери облачную папку", "game_title": "Выбери EXE-файл игры",
        "backup_title": "Выбери папку для резервных копий",
        "hint": "Важно: выбирай именно папку save, внутри которой находится save_0, а не корневую папку игры.",
        "save_btn": "СОХРАНИТЬ НАСТРОЙКИ", "error": "Ошибка",
        "need_save": "Укажи существующую папку сохранений.",
        "wrong_save_title": "Неверная папка",
        "wrong_save": "Выбрана папка игры. Укажи именно папку с файлами сохранений.",
        "too_big": "Папка больше {limit} МБ. Проверь, что выбрана именно папка сохранений.",
        "no_cloud_title": "Не указано облако", "no_cloud": "Выбери папку облака.",
        "create_title": "Создать папку?", "create": "Облачная папка пока не существует. Создать её?",
        "no_game": "Файл игры не найден.", "wrong_backup_title": "Неверная папка копий",
        "wrong_backup": "Папка резервных копий не должна лежать внутри папки сохранений или облачной папки. Выбери другое место.",
        "done_title": "Готово",
        "done": "Настройки сохранены. Чтобы играть с синхронизацией, запускай игру через ярлык: "
                "его можно создать кнопкой «Создать ярлык на рабочем столе».",
        "save_error": "Ошибка сохранения", "language": "Язык / Language",
        "hotkeys": "Вставка и копирование работают с Ctrl на любой раскладке клавиатуры.",
        "icon": "5. Ярлык игры или картинка для иконки (необязательно)",
        "icon_title": "Выбери ярлык игры, exe или картинку",
        "icon_hint": "Иконка с ярлыка игры (.lnk, .url), exe или картинки станет иконкой этого "
                     "приложения. Если поле пустое, иконка берётся из exe-файла игры.",
        "no_icon": "Файл для иконки не найден.",
        "icon_error": "Настройки сохранены, но иконку получить не удалось: {err}",
        "no_icon_source": "Сначала выбери ярлык игры или exe-файл игры.",
        "shortcut_btn": "СОЗДАТЬ ЯРЛЫК НА РАБОЧЕМ СТОЛЕ",
        "shortcut_done": "Ярлык создан:\n{path}",
        "shortcut_error": "Не удалось создать ярлык: {err}"
    }, "en": {
        "window": "Game Save Sync", "subtitle": "Game save synchronization",
        "sections": "SECTIONS", "setup": "⚙  SETUP", "paths": "Path settings",
        "description": "Select the game save folder, cloud folder, and the game executable.",
        "save": "1. Save folder (save)", "cloud": "2. Cloud folder (for example, OneDrive)",
        "game": "3. Game executable (.exe)",
        "backup": "4. Backup folder (optional, up to 3 backups)",
        "browse": "Browse…", "save_title": "Select the save folder",
        "cloud_title": "Select the cloud folder", "game_title": "Select the game executable",
        "backup_title": "Select the backup folder",
        "hint": "Important: select the save folder containing save_0, not the game's root folder.",
        "save_btn": "SAVE SETTINGS", "error": "Error",
        "need_save": "Select an existing save folder.",
        "wrong_save_title": "Invalid folder",
        "wrong_save": "This looks like the game installation folder. Select the folder containing the save files.",
        "too_big": "The folder is larger than {limit} MB. Make sure you selected the save folder.",
        "no_cloud_title": "Cloud folder missing", "no_cloud": "Select a cloud folder.",
        "create_title": "Create folder?", "create": "The cloud folder does not exist. Create it?",
        "no_game": "The game file was not found.", "wrong_backup_title": "Invalid backup folder",
        "wrong_backup": "The backup folder must not be inside the save or cloud folder. Choose another location.",
        "done_title": "Done",
        "done": "Settings saved. To play with synchronization, start the game with the shortcut: "
                "you can create it with the \"Create desktop shortcut\" button.",
        "save_error": "Could not save settings", "language": "Language / Язык",
        "hotkeys": "Paste and copy shortcuts work with Ctrl in any keyboard layout.",
        "icon": "5. Game shortcut or image for the icon (optional)",
        "icon_title": "Select a game shortcut, exe or image",
        "icon_hint": "The icon of the game shortcut (.lnk, .url), exe or image becomes the icon "
                     "of this app. If empty, the icon is taken from the game's exe.",
        "no_icon": "The icon source file was not found.",
        "icon_error": "Settings saved, but the icon could not be copied: {err}",
        "no_icon_source": "First select the game shortcut or the game executable.",
        "shortcut_btn": "CREATE DESKTOP SHORTCUT",
        "shortcut_done": "Shortcut created:\n{path}",
        "shortcut_error": "Could not create the shortcut: {err}"
    }}

    root = tk.Tk()
    root.title("Game Save Sync")
    apply_app_icon(root)
    root.geometry("820x900")
    root.minsize(720, 820)
    root.configure(bg="#111827")

    lang_var = tk.StringVar(value=config.get("language", "ru") if config.get("language") in ("ru", "en") else "ru")
    def tr(key):
        return LANG[lang_var.get()].get(key, LANG["ru"].get(key, key))

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#111827")
    style.configure("Panel.TFrame", background="#1f2937")
    style.configure("TLabel", background="#111827", foreground="#e5e7eb", font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"), foreground="#f9fafb")
    style.configure("Sub.TLabel", foreground="#9ca3af")
    style.configure("TEntry", fieldbackground="#374151", foreground="#f9fafb",
                    insertcolor="#ffffff", padding=8)
    style.configure("TButton", font=("Segoe UI", 10), padding=9)
    style.configure("Accent.TButton", background="#2563eb", foreground="white",
                    font=("Segoe UI", 10, "bold"))
    style.map("Accent.TButton", background=[("active", "#1d4ed8")])
    style.configure("Nav.TButton", background="#1f2937", foreground="#e5e7eb",
                    padding=12, anchor="w")

    save_var = tk.StringVar(value=config.get("save_dir", ""))
    cloud_var = tk.StringVar(value=config.get("cloud_dir", ""))
    game_var = tk.StringVar(value=config.get("game_exe", ""))
    backup_var = tk.StringVar(value=config.get("backup_dir", ""))
    icon_var = tk.StringVar(value=config.get("icon_source", ""))

    outer = ttk.Frame(root, padding=22)
    outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer)
    header.pack(fill="x", pady=(0, 18))
    title_row = ttk.Frame(header)
    title_row.pack(anchor="w")
    avatar_label = ttk.Label(title_row)
    avatar_label.pack(side="left", padx=(0, 12))
    ttk.Label(title_row, text="Game Save Sync", style="Title.TLabel").pack(side="left")
    subtitle = ttk.Label(header, style="Sub.TLabel")
    subtitle.pack(anchor="w", pady=(4, 0))

    language_row = ttk.Frame(header)
    language_row.pack(anchor="e", pady=(0, 4))
    language_label = ttk.Label(language_row)
    language_label.pack(side="left", padx=(0, 8))
    language_display = tk.StringVar()
    language_box = ttk.Combobox(language_row, textvariable=language_display,
                                values=("Русский", "English"), state="readonly", width=12)
    language_box.pack(side="left")

    body = ttk.Frame(outer)
    body.pack(fill="both", expand=True)
    nav = ttk.Frame(body, style="Panel.TFrame", padding=10)
    nav.pack(side="left", fill="y", padx=(0, 16))
    content = ttk.Frame(body)
    content.pack(side="left", fill="both", expand=True)
    setup_page = ttk.Frame(content)

    nav_title = ttk.Label(nav, background="#1f2937", foreground="#9ca3af",
                          font=("Segoe UI", 9, "bold"))
    nav_title.pack(anchor="w", padx=8, pady=(5, 12))
    nav_button = ttk.Button(nav, style="Nav.TButton",
                            command=lambda: show_page("setup"))
    nav_button.pack(fill="x", pady=4)
    ttk.Separator(nav).pack(fill="x", pady=15)

    page_heading = ttk.Label(setup_page, style="Title.TLabel")
    page_heading.pack(anchor="w")
    description = ttk.Label(setup_page, style="Sub.TLabel", wraplength=560)
    description.pack(anchor="w", pady=(5, 18))

    def bind_layout_independent_shortcuts(entry):
        # Windows virtual-key codes identify physical Latin keys regardless of active layout.
        def shortcut(event):
            if not (event.state & 0x4):  # Ctrl
                return
            key = event.keycode
            if key == 86:  # V
                try:
                    value = root.clipboard_get()
                    if entry.selection_present():
                        entry.delete("sel.first", "sel.last")
                    entry.insert("insert", value)
                except tk.TclError:
                    pass
                return "break"
            if key == 67:  # C
                try:
                    root.clipboard_clear()
                    root.clipboard_append(entry.selection_get())
                except tk.TclError:
                    pass
                return "break"
            if key == 88:  # X
                try:
                    selected = entry.selection_get()
                    root.clipboard_clear()
                    root.clipboard_append(selected)
                    entry.delete("sel.first", "sel.last")
                except tk.TclError:
                    pass
                return "break"
            if key == 65:  # A
                entry.select_range(0, "end")
                entry.icursor("end")
                return "break"
        entry.bind("<KeyPress>", shortcut, add="+")

    def add_path_row(parent, label_key, variable, browse_command):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=8)
        label = ttk.Label(row)
        label.pack(anchor="w", pady=(0, 5))
        line = ttk.Frame(row)
        line.pack(fill="x")
        entry = ttk.Entry(line, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        bind_layout_independent_shortcuts(entry)
        browse = ttk.Button(line, command=browse_command)
        browse.pack(side="left", padx=(8, 0))
        return label, browse

    def browse_save():
        chosen = filedialog.askdirectory(title=tr("save_title"))
        if chosen: save_var.set(chosen)

    def browse_cloud():
        chosen = filedialog.askdirectory(title=tr("cloud_title"))
        if chosen: cloud_var.set(chosen)

    def browse_game():
        chosen = filedialog.askopenfilename(
            title=tr("game_title"),
            filetypes=[("Executable files", "*.exe"), ("All files", "*.*")]
        )
        if chosen: game_var.set(chosen)

    def browse_backup():
        chosen = filedialog.askdirectory(title=tr("backup_title"))
        if chosen: backup_var.set(chosen)

    def browse_icon():
        chosen = filedialog.askopenfilename(
            title=tr("icon_title"),
            filetypes=[("Shortcuts, icons, images, exe",
                        "*.lnk *.url *.ico *.exe *.png *.jpg *.jpeg *.bmp *.gif"),
                       ("All files", "*.*")]
        )
        if chosen: icon_var.set(chosen)

    rows = [
        add_path_row(setup_page, "save", save_var, browse_save),
        add_path_row(setup_page, "cloud", cloud_var, browse_cloud),
        add_path_row(setup_page, "game", game_var, browse_game),
        add_path_row(setup_page, "backup", backup_var, browse_backup),
        add_path_row(setup_page, "icon", icon_var, browse_icon),
    ]
    icon_hint = ttk.Label(setup_page, style="Sub.TLabel", wraplength=560)
    icon_hint.pack(anchor="w", pady=(0, 4))
    hint = ttk.Label(setup_page, style="Sub.TLabel", wraplength=560)
    hint.pack(anchor="w", pady=(12, 8))
    hotkey_hint = ttk.Label(setup_page, style="Sub.TLabel", wraplength=560)
    hotkey_hint.pack(anchor="w", pady=(0, 10))
    shortcut_button = ttk.Button(setup_page, command=lambda: make_shortcut())
    shortcut_button.pack(anchor="w", pady=(0, 10))

    avatar = {}

    def refresh_avatar():
        try:
            if ICON_PNG.exists():
                image = tk.PhotoImage(file=str(ICON_PNG))
                if 0 < image.width() < 64:
                    image = image.zoom(max(1, 64 // image.width()))
                avatar["image"] = image            # держим ссылку, иначе картинка пропадёт
                avatar_label.configure(image=image)
            else:
                avatar_label.configure(image="")
        except tk.TclError:
            avatar_label.configure(image="")

    def make_shortcut():
        t = LANG[lang_var.get()]
        try:
            game_text = game_var.get().strip().strip('"')
            source = icon_var.get().strip().strip('"') or game_text
            if not source:
                raise RuntimeError(t["no_icon_source"])
            root.configure(cursor="watch"); root.update_idletasks()
            update_app_icon(source)                # берём свежую иконку
            apply_app_icon(root); refresh_avatar()
            name = shortcut_name({"game_name": config.get("game_name", ""),
                                  "icon_source": icon_var.get(), "game_exe": game_text})
            path = create_desktop_shortcut(name)
            messagebox.showinfo(t["done_title"], t["shortcut_done"].format(path=path))
        except Exception as exc:
            messagebox.showerror(t["error"], t["shortcut_error"].format(err=exc))
        finally:
            root.configure(cursor="")

    def refresh_language(*_):
        t = LANG[lang_var.get()]
        root.title(t["window"])
        subtitle.configure(text=t["subtitle"])
        language_label.configure(text=t["language"])
        nav_title.configure(text=t["sections"])
        nav_button.configure(text=t["setup"])
        page_heading.configure(text=t["paths"])
        description.configure(text=t["description"])
        for (label, browse), key in zip(rows, ("save", "cloud", "game", "backup", "icon")):
            label.configure(text=t[key])
            browse.configure(text=t["browse"])
        hint.configure(text=t["hint"])
        hotkey_hint.configure(text=t["hotkeys"])
        icon_hint.configure(text=t["icon_hint"])
        shortcut_button.configure(text=t["shortcut_btn"])
        save_button.configure(text=t["save_btn"])

    def show_page(which):
        setup_page.pack_forget()
        if which == "setup":
            setup_page.pack(fill="both", expand=True)

    def save_setup():
        t = LANG[lang_var.get()]
        save = Path(save_var.get().strip().strip('"'))
        cloud = Path(cloud_var.get().strip().strip('"'))
        game = Path(game_var.get().strip().strip('"')) if game_var.get().strip() else None

        if not save.is_dir():
            messagebox.showerror(t["error"], t["need_save"]); return
        try:
            if folder_size_mb(save) > MAX_SIZE_MB:
                messagebox.showerror(t["error"], t["too_big"].format(limit=MAX_SIZE_MB)); return
        except OSError as exc:
            messagebox.showerror(t["error"], str(exc)); return
        if not cloud_var.get().strip():
            messagebox.showerror(t["no_cloud_title"], t["no_cloud"]); return
        if not cloud.exists() and not messagebox.askyesno(t["create_title"], t["create"]):
            return
        if game is not None and not game.is_file():
            messagebox.showerror(t["error"], t["no_game"]); return
        backup_text = backup_var.get().strip().strip('"')
        if backup_text and (is_inside(Path(backup_text), save) or is_inside(Path(backup_text), cloud)):
            messagebox.showerror(t["wrong_backup_title"], t["wrong_backup"]); return
        icon_text = icon_var.get().strip().strip('"')
        if icon_text and not Path(icon_text).is_file():
            messagebox.showerror(t["error"], t["no_icon"]); return
        try:
            cloud.mkdir(parents=True, exist_ok=True)
            if backup_text:
                Path(backup_text).mkdir(parents=True, exist_ok=True)
            data = dict(config)
            data.update({
                "save_dir": str(save), "cloud_dir": str(cloud),
                "game_exe": str(game) if game else "",
                "backup_dir": backup_text, "language": lang_var.get(),
                "icon_source": icon_text
            })
            CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

            # Копируем иконку с ярлыка игры (или из exe), если источник есть.
            icon_note = ""
            source = icon_text or (str(game) if game else "")
            if source:
                try:
                    root.configure(cursor="watch"); root.update_idletasks()
                    update_app_icon(source)
                    apply_app_icon(root)
                except Exception as exc:
                    log(f"Иконку получить не удалось: {exc}")
                    icon_note = "\n\n" + t["icon_error"].format(err=exc)
                finally:
                    root.configure(cursor="")
            messagebox.showinfo(t["done_title"], t["done"] + icon_note)
            root.destroy()
        except Exception as exc:
            messagebox.showerror(t["save_error"], str(exc))

    save_button = ttk.Button(setup_page, style="Accent.TButton", command=save_setup)
    save_button.pack(anchor="w")
    language_box.bind("<<ComboboxSelected>>",
                      lambda _e: lang_var.set("ru" if language_display.get() == "Русский" else "en"))
    lang_var.trace_add("write", refresh_language)
    language_display.set("Русский" if lang_var.get() == "ru" else "English")
    refresh_language()
    refresh_avatar()
    show_page("setup")
    root.mainloop()


def has_config() -> bool:
    try:
        return "save_dir" in json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return False


def main() -> None:
    # Запуск самого приложения (без аргументов) всегда открывает настройки (SETUP).
    # Синхронизацию и запуск игры выполняет ярлык: он запускает программу с командой run.
    set_app_user_model_id()
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "setup"

    if cmd in ("setup", "gui", "auto"):
        launch_gui()
    elif cmd in ("run", "pull", "push"):
        if not has_config():          # настройки ещё нет - сначала открываем SETUP
            launch_gui()
            return
        cfg = load_config()
        {"run": run, "pull": pull, "push": push}[cmd](cfg)
    else:
        print("Команда: setup | run | pull | push")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as exc:
        try:
            import tkinter.messagebox as _messagebox
            _messagebox.showerror("Ошибка", str(exc))
        except Exception:
            pass