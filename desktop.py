"""CapFlux desktop shell.

Chạy backend FastAPI trong một thread nền và hiển thị giao diện trong cửa sổ
native (WebView2 trên Windows) thay vì mở trình duyệt.

    python desktop.py          # chạy thử từ source (cần: pip install pywebview)
    build_exe.bat              # đóng gói thành CapFlux.exe

Hai chế độ:
  * source : dữ liệu (data/, config.json) nằm cạnh code như cũ.
  * frozen : dữ liệu nằm ở %APPDATA%\\CapFlux để cập nhật/cài đặt lại
             không làm mất danh sách phòng.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP_NAME = "CapFlux"
FROZEN = getattr(sys, "frozen", False)

# Thư mục chứa tài nguyên đi kèm (chỉ đọc) và thư mục chứa file .exe.
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

if FROZEN:
    USER_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
else:
    USER_DIR = EXE_DIR
USER_DIR.mkdir(parents=True, exist_ok=True)

LOCK_PORT = 47653          # chặn mở 2 bản cùng lúc (2 bản sẽ giết ffmpeg của nhau)
PREFERRED_PORT = 5000


def _setup_logging_streams() -> None:
    """App dạng --windowed không có stdout/stderr -> ghi ra file log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = open(USER_DIR / "capflux.log", "a", buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def _ensure_hidden_console() -> None:
    """Tạo một console ẩn cho app dạng --windowed (chỉ Windows, chỉ khi chưa có).

    Không có console thì: (1) mỗi ffmpeg/yt-dlp con bật ra một cửa sổ đen,
    (2) CTRL_BREAK_EVENT mà main.py dùng để dừng ffmpeg êm sẽ không gửi được.
    Có console ẩn dùng chung thì con kế thừa nó, không popup, tín hiệu hoạt
    động giống hệt khi chạy từ CMD.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
        if k32.GetConsoleWindow():
            return
        if k32.AllocConsole():
            hwnd = k32.GetConsoleWindow()
            if hwnd:
                u32.ShowWindow(hwnd, 0)     # SW_HIDE
    except Exception:
        pass


def _setup_environment() -> None:
    os.environ.setdefault("TOOL_DATA_DIR", str(USER_DIR / "data"))
    os.environ.setdefault("TOOL_CONFIG_PATH", str(USER_DIR / "config.json"))
    os.environ.setdefault("PYTHONUTF8", "1")
    if FROZEN:
        os.environ.setdefault("STATIC_DIR", str(RES_DIR / "web"))

    # ffmpeg.exe / yt-dlp.exe: ưu tiên thư mục bin\ cạnh file .exe.
    bin_dir = EXE_DIR / "bin"
    if bin_dir.is_dir():
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _acquire_single_instance() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def _free_port(preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 20)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _folder_dialog(window, initial: str) -> str:
    import webview
    kind = (webview.FileDialog.FOLDER if hasattr(webview, "FileDialog")
            else webview.FOLDER_DIALOG)
    res = window.create_file_dialog(kind, directory=initial or "")
    if not res:
        return ""
    return res[0] if isinstance(res, (list, tuple)) else str(res)


def main() -> int:
    _setup_logging_streams()
    if FROZEN:
        _ensure_hidden_console()
    _setup_environment()

    lock = _acquire_single_instance()
    if lock is None:
        _message("CapFlux đang chạy rồi. Hãy tìm cửa sổ CapFlux có sẵn (hoặc khay hệ thống/Task Manager).")
        return 0

    import uvicorn
    import webview

    import main as backend          # import sau khi đã set biến môi trường

    port = _free_port(PREFERRED_PORT)
    config = uvicorn.Config(backend.app, host="127.0.0.1", port=port,
                            log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None       # không ở main thread
    thread = threading.Thread(target=server.run, name="capflux-server", daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    if not _wait_ready(base_url + "/api/channels"):
        _message("Không khởi động được máy chủ nội bộ của CapFlux.\n"
                 f"Xem file log: {USER_DIR / 'capflux.log'}")
        server.should_exit = True
        return 1

    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(
        APP_NAME, base_url, width=1360, height=860, min_size=(1000, 640),
        text_select=True, confirm_close=False,
    )
    backend.FOLDER_PICKER_HOOK = lambda initial: _folder_dialog(window, initial)

    def on_closing():
        active = sum(1 for r in list(backend.recordings.values())
                     if r.get("status") in ("recording", "starting"))
        if active and not window.create_confirmation_dialog(
                APP_NAME, f"Đang có {active} phòng đang quay. Thoát sẽ dừng các bản quay này.\n\nThoát?"):
            return False
        return True

    window.events.closing += on_closing

    try:
        webview.start(private_mode=False, storage_path=str(USER_DIR / "webview"))
    finally:
        # lifespan của FastAPI sẽ dừng ffmpeg/yt-dlp và lưu state (tối đa ~10s)
        server.should_exit = True
        thread.join(timeout=20)
        lock.close()
    return 0


def _message(text: str) -> None:
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, 0x40)
            return
    except Exception:
        pass
    print(text)


if __name__ == "__main__":
    sys.exit(main())
