import os
import sys
import time
import json
import shutil
import hashlib
import ctypes
from ctypes import wintypes
import threading
import subprocess
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import winreg
import psutil
import win32gui
import win32con
import win32api

# =====================================================================
# CẤU HÌNH HỆ THỐNG & HẰNG SỐ WIN32
# =====================================================================
APP_NAME = "WindowsAppTimeGuard"
WATCHDOG_EXE_NAME = "RuntimeBrokerGuard.exe"
REG_STATE_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\AppTimeGuard"
REG_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
DATA_DIR = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Microsoft", "AppTimeGuard")
STATE_FILE = os.path.join(DATA_DIR, "guard_state.dat")

# Hằng số Win32 Hotkey (Ctrl + Shift + L)
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_L = 0x4C
WM_HOTKEY = 0x0312
HOTKEY_ID = 9001


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def elevate_if_needed():
    """Tự động yêu cầu quyền Administrator nếu chưa có"""
    if not is_admin() and getattr(sys, 'frozen', False):
        params = " ".join([f'"{arg}"' for arg in sys.argv])
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit(0)


def normalize_folder(path: str) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def get_folder_hash(path: str) -> str:
    norm = normalize_folder(path)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def format_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


# =====================================================================
# LỚP LƯU TRỮ TRẠNG THÁI KÉP (REGISTRY + FILE HỆ THỐNG ẨN)
# =====================================================================
class StateManager:
    @staticmethod
    def _default_state():
        return {
            "target_folder": "",
            "limit_hours": 2,
            "is_active": False,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "usage_by_folder": {},  # {folder_hash: used_seconds_today}
            "main_exe_path": os.path.abspath(sys.argv[0]),
        }

    @classmethod
    def load(cls) -> dict:
        state = cls._default_state()
        raw_json = None

        # 1. Đọc từ Registry trước
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_STATE_PATH, 0, winreg.KEY_READ)
            raw_json, _ = winreg.QueryValueEx(key, "ConfigData")
            winreg.CloseKey(key)
        except Exception:
            pass

        # 2. Nếu Registry bị xóa, khôi phục từ File ẩn trong ProgramData
        if not raw_json and os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    raw_json = f.read()
            except Exception:
                pass

        if raw_json:
            try:
                loaded = json.loads(raw_json)
                state.update(loaded)
            except Exception:
                pass

        # Kiểm tra sang ngày mới -> Reset bộ đếm thời gian trong ngày
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("date") != today:
            state["date"] = today
            state["usage_by_folder"] = {}
            cls.save(state)

        return state

    @classmethod
    def save(cls, state: dict):
        raw_json = json.dumps(state, ensure_ascii=False)

        # 1. Ghi vào Registry
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_STATE_PATH)
            winreg.SetValueEx(key, "ConfigData", 0, winreg.REG_SZ, raw_json)
            winreg.CloseKey(key)
        except Exception:
            pass

        # 2. Ghi vào ProgramData
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                f.write(raw_json)
            # Đặt thuộc tính ẩn + hệ thống cho thư mục
            ctypes.windll.kernel32.SetFileAttributesW(DATA_DIR, 0x02 | 0x04)
        except Exception:
            pass


# =====================================================================
# CƠ CHẾ TỰ KHỞI ĐỘNG & CHỐNG TẮT TASK MANAGER (WATCHDOG)
# =====================================================================
def setup_boot_persistence(enable: bool, exe_path: str):
    """Đăng ký tự động chạy ngầm khi khởi động lại máy"""
    cmd = f'"{exe_path}" --background'
    if enable:
        # 1. Registry Run
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH)
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
        except Exception:
            pass

        # 2. Windows Task Scheduler (Quyền cao nhất)
        try:
            subprocess.run(
                ["schtasks", "/Create", "/SC", "ONLOGON", "/TN", APP_NAME,
                 "/TR", cmd, "/RL", "HIGHEST", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception:
            pass
    else:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_PATH, 0, winreg.KEY_SET_VALUE)
            winreg.DeleteValue(key, APP_NAME)
            winreg.CloseKey(key)
        except Exception:
            pass

        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", APP_NAME, "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception:
            pass


def run_watchdog_mode(target_pid: int):
    """
    Chế độ tiến trình canh gác ngầm:
    Theo dõi tiến trình chính. Nếu bị End Task trong Task Manager, lập tức hồi sinh tiến trình chính.
    """
    while True:
        state = StateManager.load()
        if not state.get("is_active", False):
            # Người dùng đã bấm Stop hợp lệ trong giao diện
            sys.exit(0)

        if not psutil.pid_exists(target_pid):
            main_exe = state.get("main_exe_path", sys.executable)
            if os.path.exists(main_exe):
                try:
                    proc = subprocess.Popen(
                        [main_exe, "--background"],
                        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                    )
                    target_pid = proc.pid
                except Exception:
                    pass
        time.sleep(0.4)


# =====================================================================
# GIAO DIỆN CHÍNH & ĐỒNG HỒ HUD GÓC PHẢI MÀN HÌNH
# =====================================================================
class AppTimeLimiterGUI:
    def __init__(self, start_in_background=False):
        self.root = tk.Tk()
        self.root.title("Quản Lý Giới Hạn Thời Gian Ứng Dụng / Game")
        self.root.geometry("500x270")
        self.root.resizable(False, False)

        # Chặn nút X đóng cửa sổ khi đang chạy -> chỉ ẩn đi
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_window)

        self.state = StateManager.load()
        self.watchdog_proc = None
        self.overlay = None
        self.overlay_label = None

        self._build_main_ui()
        self._create_clickthrough_overlay()
        self._start_global_hotkey_listener()

        # Nếu khởi động lại máy hoặc bị kill rồi hồi sinh khi đang active
        if start_in_background or self.state.get("is_active", False):
            self.activate_protection(initial_boot=True)

        # Vòng lặp kiểm tra mỗi 1 giây
        self.root.after(1000, self._tick_second)

    def _build_main_ui(self):
        pad = {"padx": 16, "pady": 8}

        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)

        # 1. Chọn thư mục
        ttk.Label(frame, text="1. Đường dẫn thư mục Ứng dụng / Game cần giới hạn:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        path_frame = ttk.Frame(frame)
        path_frame.pack(fill="x", pady=(4, 10))

        self.folder_var = tk.StringVar(value=self.state.get("target_folder", ""))
        self.entry_folder = ttk.Entry(path_frame, textvariable=self.folder_var, font=("Segoe UI", 9))
        self.entry_folder.pack(side="left", fill="x", expand=True, padx=(0, 8))

        self.btn_browse = ttk.Button(path_frame, text="Chọn Thư Mục...", command=self.browse_folder)
        self.btn_browse.pack(side="right")

        # 2. Chỉnh thời gian sử dụng trong ngày (1 - 10 giờ)
        time_frame = ttk.Frame(frame)
        time_frame.pack(fill="x", pady=6)

        ttk.Label(time_frame, text="2. Tổng thời gian cho phép trong ngày (1 - 10 giờ):", font=("Segoe UI", 10, "bold")).pack(side="left")
        self.hours_var = tk.IntVar(value=int(self.state.get("limit_hours", 2)))
        self.spin_hours = ttk.Spinbox(
            time_frame, from_=1, to=10, textvariable=self.hours_var,
            width=6, font=("Segoe UI", 10, "bold"), state="readonly"
        )
        self.spin_hours.pack(side="right")

        # Thông tin trạng thái
        self.status_var = tk.StringVar(value="Trạng thái: Đang tạm dừng (Chưa kích hoạt)")
        ttk.Label(frame, textvariable=self.status_var, foreground="#005fb8", font=("Segoe UI", 9, "italic")).pack(anchor="w", pady=(6, 4))

        ttk.Label(
            frame,
            text="Phím tắt gọi lại bảng điều khiển khi đã ẩn: CTRL + SHIFT + L",
            foreground="#666666", font=("Segoe UI", 9)
        ).pack(anchor="w", pady=(0, 12))

        # 3. Nút Start & Stop
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=4)

        self.btn_start = tk.Button(
            btn_frame, text="START (Kích hoạt & Ẩn)", bg="#107c41", fg="white",
            font=("Segoe UI", 10, "bold"), height=2, command=self.on_start_clicked
        )
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.btn_stop = tk.Button(
            btn_frame, text="STOP (Dừng khóa)", bg="#c42b1c", fg="white",
            font=("Segoe UI", 10, "bold"), height=2, command=self.on_stop_clicked
        )
        self.btn_stop.pack(side="right", fill="x", expand=True, padx=(6, 0))

    def _create_clickthrough_overlay(self):
        """
        Tạo đồng hồ nhỏ cỡ chữ 11 ở góc phải phía trên màn hình.
        Khóa chết (Click-through hoàn toàn), không hiện trên Taskbar.
        """
        self.overlay = tk.Toplevel(self.root)
        self.overlay.overrideredirect(True)
        self.overlay.attributes("-topmost", True)
        self.overlay.attributes("-alpha", 0.88)
        self.overlay.configure(bg="#111111")

        self.overlay_label = tk.Label(
            self.overlay,
            text="Chương trình sẽ dừng hoạt động sau 00:00:00",
            font=("Segoe UI", 11, "bold"),
            fg="#ff4d4d",
            bg="#111111",
            padx=10,
            pady=4
        )
        self.overlay_label.pack()

        self.overlay.update_idletasks()
        self._position_overlay_top_right()
        self._make_window_clickthrough(self.overlay)
        self.overlay.withdraw()

    def _position_overlay_top_right(self):
        if not self.overlay:
            return
        self.overlay.update_idletasks()
        screen_w = self.overlay.winfo_screenwidth()
        win_w = self.overlay.winfo_reqwidth()
        win_h = self.overlay.winfo_reqheight()
        x = screen_w - win_w - 15
        y = 12
        self.overlay.geometry(f"{win_w}x{win_h}+{x}+{y}")

    def _make_window_clickthrough(self, widget):
        """Áp dụng cờ Win32 WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW"""
        widget.update_idletasks()
        hwnd = widget.winfo_id()
        parent = win32gui.GetParent(hwnd)
        target_hwnds = [h for h in (hwnd, parent) if h]

        for h in target_hwnds:
            try:
                ex_style = win32gui.GetWindowLong(h, win32con.GWL_EXSTYLE)
                ex_style |= (
                    win32con.WS_EX_LAYERED
                    | win32con.WS_EX_TRANSPARENT
                    | win32con.WS_EX_TOOLWINDOW
                    | win32con.WS_EX_TOPMOST
                    | 0x08000000  # WS_EX_NOACTIVATE (không bao giờ chiếm tiêu điểm)
                )
                ex_style &= ~win32con.WS_EX_APPWINDOW
                win32gui.SetWindowLong(h, win32con.GWL_EXSTYLE, ex_style)
                win32gui.SetWindowPos(
                    h, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_FRAMECHANGED
                )
            except Exception:
                pass

    def browse_folder(self):
        selected = filedialog.askdirectory(title="Chọn thư mục chứa Ứng dụng hoặc Game")
        if selected:
            self.folder_var.set(os.path.normpath(selected))

    def on_start_clicked(self):
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Lỗi đường dẫn", "Vui lòng chọn một thư mục hợp lệ chứa ứng dụng hoặc game!")
            return

        try:
            hours = int(self.hours_var.get())
            if not (1 <= hours <= 10):
                raise ValueError
        except ValueError:
            messagebox.showerror("Lỗi thời gian", "Giới hạn thời gian phải từ 1 đến 10 giờ!")
            return

        self.state["target_folder"] = os.path.normpath(folder)
        self.state["limit_hours"] = hours
        self.state["is_active"] = True
        self.state["main_exe_path"] = os.path.abspath(sys.argv[0])
        StateManager.save(self.state)

        self.activate_protection(initial_boot=False)

    def activate_protection(self, initial_boot=False):
        # Khóa chỉnh sửa trên UI
        self.entry_folder.configure(state="disabled")
        self.btn_browse.configure(state="disabled")
        self.spin_hours.configure(state="disabled")
        self.btn_start.configure(state="disabled")

        # Đăng ký tự chạy khi reset máy
        setup_boot_persistence(True, self.state.get("main_exe_path", os.path.abspath(sys.argv[0])))

        # Khởi chạy tiến trình canh gác chống tắt Task Manager
        self._ensure_watchdog_running()

        # Hiện đồng hồ HUD góc phải trên & Ẩn cửa sổ chính khỏi Taskbar
        self._update_overlay_text()
        self.overlay.deiconify()
        self._position_overlay_top_right()
        self._make_window_clickthrough(self.overlay)
        self.root.withdraw()

    def on_stop_clicked(self):
        self.state["is_active"] = False
        StateManager.save(self.state)

        # Hủy khởi động cùng Windows
        setup_boot_persistence(False, self.state.get("main_exe_path", ""))

        # Dừng tiến trình Watchdog
        self._terminate_watchdog()

        # Ẩn đồng hồ góc phải, mở khóa các nút cấu hình
        if self.overlay:
            self.overlay.withdraw()

        self.entry_folder.configure(state="normal")
        self.btn_browse.configure(state="normal")
        self.spin_hours.configure(state="readonly")
        self.btn_start.configure(state="normal")
        self.status_var.set("Trạng thái: Đã dừng khóa (Có thể chỉnh sửa hoặc thoát)")

    def on_close_window(self):
        """Nếu đang kích hoạt mà bấm X thì chỉ ẩn đi, không cho tắt"""
        if self.state.get("is_active", False):
            self.root.withdraw()
        else:
            self._terminate_watchdog()
            self.root.destroy()
            sys.exit(0)

    def show_main_window(self):
        """Được gọi khi ấn tổ hợp phím CTRL + SHIFT + L"""
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.focus_force()
        self.root.after(500, lambda: self.root.attributes("-topmost", False))

    # =====================================================================
    # QUẢN LÝ TIẾN TRÌNH CANH GÁC (CHỐNG END TASK)
    # =====================================================================
    def _ensure_watchdog_running(self):
        if not getattr(sys, 'frozen', False):
            # Chỉ nhân bản file EXE khi đã đóng gói thành EXE
            return

        if self.watchdog_proc and self.watchdog_proc.poll() is None:
            return

        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            watchdog_path = os.path.join(DATA_DIR, WATCHDOG_EXE_NAME)
            if not os.path.exists(watchdog_path):
                shutil.copy2(sys.executable, watchdog_path)

            self.watchdog_proc = subprocess.Popen(
                [watchdog_path, "--watchdog", str(os.getpid())],
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
            )
        except Exception:
            pass

    def _terminate_watchdog(self):
        if self.watchdog_proc:
            try:
                self.watchdog_proc.terminate()
            except Exception:
                pass
            self.watchdog_proc = None

        # Quét diệt mọi tiến trình watchdog còn sót
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if proc.info['name'] and proc.info['name'].lower() == WATCHDOG_EXE_NAME.lower():
                    proc.kill()
            except Exception:
                pass

    # =====================================================================
    # LOGIC QUÉT THƯ MỤC, ĐẾM NGƯỢC & KHÓA ỨNG DỤNG / GAME
    # =====================================================================
    def _get_remaining_seconds(self) -> int:
        folder = self.state.get("target_folder", "")
        limit_sec = int(self.state.get("limit_hours", 2)) * 3600
        if not folder:
            return limit_sec
        f_hash = get_folder_hash(folder)
        used_sec = int(self.state.get("usage_by_folder", {}).get(f_hash, 0))
        return max(0, limit_sec - used_sec)

    def _find_target_processes(self, target_folder: str):
        """Tìm tất cả các tiến trình có file thực thi nằm trong thư mục đích"""
        norm_target = normalize_folder(target_folder)
        if not norm_target:
            return []

        # Đảm bảo so khớp chính xác ranh giới thư mục
        target_prefix = norm_target if norm_target.endswith(os.sep) else norm_target + os.sep
        matched = []
        my_pid = os.getpid()

        for proc in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                if proc.info['pid'] == my_pid:
                    continue
                exe_path = proc.info.get('exe')
                if not exe_path:
                    continue
                norm_exe = normalize_folder(exe_path)
                if norm_exe.startswith(target_prefix) or norm_exe == norm_target:
                    matched.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return matched

    def _update_overlay_text(self):
        rem = self._get_remaining_seconds()
        text = f"Chương trình sẽ dừng hoạt động sau {format_hms(rem)}"
        if self.overlay_label:
            self.overlay_label.configure(text=text)
            self._position_overlay_top_right()

    def _tick_second(self):
        try:
            # Kiểm tra sang ngày mới
            today = datetime.now().strftime("%Y-%m-%d")
            if self.state.get("date") != today:
                self.state["date"] = today
                self.state["usage_by_folder"] = {}
                StateManager.save(self.state)

            if self.state.get("is_active", False):
                # Đảm bảo tiến trình canh gác luôn sống
                self._ensure_watchdog_running()

                # Giữ đồng hồ luôn nổi lên trên cùng và click-through
                if self.overlay:
                    self.overlay.lift()
                    self._make_window_clickthrough(self.overlay)

                target_folder = self.state.get("target_folder", "")
                running_procs = self._find_target_processes(target_folder)
                f_hash = get_folder_hash(target_folder)
                used_sec = int(self.state.get("usage_by_folder", {}).get(f_hash, 0))
                limit_sec = int(self.state.get("limit_hours", 2)) * 3600

                if running_procs:
                    if used_sec < limit_sec:
                        # Đang mở ứng dụng/game -> Đếm ngược
                        used_sec += 1
                        self.state.setdefault("usage_by_folder", {})[f_hash] = used_sec
                        # Lưu trạng thái bền vững
                        StateManager.save(self.state)

                    # Nếu đã hết thời gian -> Diệt toàn bộ tiến trình trong thư mục đó ngay lập tức
                    if used_sec >= limit_sec:
                        for p in running_procs:
                            try:
                                p.kill()
                            except Exception:
                                pass

                rem = max(0, limit_sec - used_sec)
                state_desc = "Đang chạy (Đếm ngược)" if running_procs else "Đang chờ mở ứng dụng"
                self.status_var.set(f"Trạng thái: Đang kích hoạt [{state_desc}] - Còn lại: {format_hms(rem)}")
                self._update_overlay_text()
        except Exception:
            pass
        finally:
            self.root.after(1000, self._tick_second)

    # =====================================================================
    # LẮNG NGHE TỔ HỢP PHÍM CTRL + SHIFT + L (WIN32 NATIVE)
    # =====================================================================
    def _start_global_hotkey_listener(self):
        def hotkey_thread():
            user32 = ctypes.windll.user32
            # Đăng ký CTRL + SHIFT + L
            user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_L)
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.root.after(0, self.show_main_window)
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        t = threading.Thread(target=hotkey_thread, daemon=True)
        t.start()

    def run(self):
        self.root.mainloop()


# =====================================================================
# ĐIỂM KHỞI CHẠY (ENTRY POINT)
# =====================================================================
if __name__ == "__main__":
    # 1. Nếu được gọi ở chế độ tiến trình canh gác ngầm
    if len(sys.argv) >= 3 and sys.argv[1] == "--watchdog":
        try:
            target_pid = int(sys.argv[2])
            run_watchdog_mode(target_pid)
        except Exception:
            sys.exit(0)

    # 2. Đảm bảo quyền Administrator để có thể kill mọi game và đăng ký Task Scheduler
    elevate_if_needed()

    # 3. Kiểm tra cờ chạy ngầm khi vừa khởi động máy hoặc hồi sinh
    bg_mode = "--background" in sys.argv
    app = AppTimeLimiterGUI(start_in_background=bg_mode)
    app.run()
