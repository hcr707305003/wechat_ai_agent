from __future__ import annotations

import ctypes
import difflib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DesktopToolError(RuntimeError):
    """Raised when a local desktop tool cannot complete an operation."""


@dataclass(slots=True, frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process_name: str = ""
    visible: bool = True
    minimized: bool = False


class DesktopToolbox:
    """Small, local Windows desktop toolbox.

    The toolbox deliberately does not use Computer Use or a remote desktop
    connector.  It talks to the local Win32 desktop and uses the same image
    files as the bridge's normal attachment delivery path.
    """

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)

    def list_windows(self) -> list[WindowInfo]:
        user32 = self._user32()
        if user32 is None:
            raise DesktopToolError("Windows desktop tools are unavailable")
        windows: list[WindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        def visit(hwnd_value: int, _extra: int) -> bool:
            hwnd = int(hwnd_value or 0)
            if not hwnd or not user32.IsWindow(hwnd):
                return True
            visible = bool(user32.IsWindowVisible(hwnd))
            if not visible:
                return True
            title = self._window_title(user32, hwnd)
            process_name = self._process_name(user32, hwnd)
            windows.append(
                WindowInfo(
                    hwnd,
                    title,
                    process_name,
                    visible=True,
                    minimized=bool(user32.IsIconic(hwnd)),
                )
            )
            return True

        callback = callback_type(visit)
        user32.EnumWindows(callback, 0)
        return windows

    def find_window(self, query: str) -> WindowInfo:
        windows = self.list_windows()
        matches = self.match_windows(query, windows)
        if matches:
            return matches[0]
        raise DesktopToolError(f"未找到可截图的应用窗口：{query}")

    def match_windows(
        self, query: str, windows: list[WindowInfo]
    ) -> list[WindowInfo]:
        """Return windows matching a human app name, strongest first."""
        needle = re.sub(r"\s+", "", str(query or "")).lower()
        if not needle:
            raise DesktopToolError("应用名称不能为空")
        aliases = {
            "浏览器": ("chrome", "edge", "firefox", "浏览器"),
            "谷歌浏览器": ("chrome",),
            "微软浏览器": ("edge",),
            "记事本": ("notepad", "记事本"),
            "代码": (
                "code",
                "code.exe",
                "vscode",
                "visual studio",
                "visual studio code",
                "code - insiders",
            ),
            "vscode": (
                "vscode",
                "code",
                "code.exe",
                "visual studio",
                "visual studio code",
                "code - insiders",
            ),
            "visualstudiocode": (
                "vscode",
                "code",
                "code.exe",
                "visual studio",
                "visual studio code",
                "code - insiders",
            ),
            "qq": ("qq", "qq.exe", "腾讯qq", "tim", "tim.exe"),
            "腾讯qq": ("qq", "qq.exe", "腾讯qq", "tim", "tim.exe"),
            # NetEase Cloud Music exposes a separate always-on-top desktop
            # lyrics window (usually titled "桌面歌词") in the same process.
            # Match the process as well as the localized product name so a
            # request such as "截图网易云" can find the real app window.
            "网易云": (
                "网易云",
                "网易云音乐",
                "cloudmusic",
                "cloudmusic.exe",
                "netease",
            ),
            "网易云音乐": (
                "网易云",
                "网易云音乐",
                "cloudmusic",
                "cloudmusic.exe",
                "netease",
            ),
            "cloudmusic": (
                "网易云",
                "网易云音乐",
                "cloudmusic",
                "cloudmusic.exe",
                "netease",
            ),
            "终端": ("powershell", "terminal", "命令提示符"),
        }
        terms = tuple(
            re.sub(r"\s+", "", term).lower()
            for term in aliases.get(needle, (needle,))
        )
        direct: list[WindowInfo] = []
        for window in windows:
            title = re.sub(r"\s+", "", window.title).lower()
            process = re.sub(r"\s+", "", window.process_name).lower()
            if any(term in title or term in process for term in terms):
                direct.append(window)
        if direct:
            # Do not let the optional Agent selector choose a lyric overlay
            # when the user named the music application itself. Keep the
            # overlay as a fallback only when it is the sole matching window.
            if needle in {"网易云", "网易云音乐", "cloudmusic", "netease"}:
                primary = [window for window in direct if not self._is_lyrics_window(window)]
                if primary:
                    direct = primary
            direct.sort(key=self._window_priority, reverse=True)
            # CloudMusic exposes its main surface as one top-level window
            # plus several empty child surfaces (sidebar, footer, etc.).
            # Returning every process match would invoke the Agent selector
            # and could select one of those non-capturable child surfaces.
            if needle in {"网易云", "网易云音乐", "cloudmusic", "netease"}:
                return direct[:1]
            return direct

        # Users usually know an app by a short form ("vsc", "photoshop",
        # "浏览器") or make a small typo.  Fall back to token/acronym fuzzy
        # matching after exact substring matching has failed.
        if len(needle) >= 3:
            ranked = sorted(
                [
                    (self._fuzzy_window_score(needle, window), window)
                    for window in windows
                ],
                key=lambda item: item[0],
                reverse=True,
            )
            if ranked:
                score = ranked[0][0]
                if score >= 0.58:
                    return [window for candidate_score, window in ranked if candidate_score >= score - 0.08]
        return []

    @staticmethod
    def _is_lyrics_window(window: WindowInfo) -> bool:
        value = re.sub(r"\s+", "", str(window.title or "")).lower()
        return any(term in value for term in ("桌面歌词", "歌词", "lyrics", "lyric"))

    @staticmethod
    def _window_priority(window: WindowInfo) -> tuple[int, int, int]:
        """Prefer a normal, larger app window over overlays and tiny tools."""
        try:
            import win32gui

            left, top, right, bottom = win32gui.GetWindowRect(int(window.hwnd))
            area = max(0, int(right - left)) * max(0, int(bottom - top))
        except Exception:
            area = 0
        return (
            0 if DesktopToolbox._is_lyrics_window(window) else 1,
            0 if window.minimized else 1,
            area,
        )

    @staticmethod
    def _fuzzy_window_score(needle: str, window: WindowInfo) -> float:
        """Score a short app query against a window title/process name."""
        candidates = [window.title, Path(window.process_name).stem]
        best = 0.0
        for candidate in candidates:
            raw = str(candidate or "").lower()
            compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", raw)
            if not compact:
                continue
            if needle in compact:
                return 1.0
            tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", raw)
            for token in tokens:
                best = max(best, difflib.SequenceMatcher(None, needle, token).ratio())
            latin_tokens = [token for token in tokens if re.fullmatch(r"[a-z0-9]+", token)]
            acronym = "".join(token[0] for token in latin_tokens)
            if acronym and (needle in acronym or acronym in needle):
                best = max(best, 0.9)
            best = max(
                best,
                difflib.SequenceMatcher(None, needle, compact).ratio(),
            )
        return best

    def foreground_window(self) -> WindowInfo:
        user32 = self._user32()
        if user32 is None:
            raise DesktopToolError("Windows desktop tools are unavailable")
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            raise DesktopToolError("当前没有前台应用窗口")
        title = self._window_title(user32, hwnd)
        return WindowInfo(hwnd, title, self._process_name(user32, hwnd))

    def capture_window(self, hwnd: int, prefix: str = "window") -> Path:
        rect = self.window_rect(hwnd)
        output = self._output_path(prefix)
        images: list[Any] = []
        user32 = self._user32()
        was_foreground = bool(
            user32 is not None and int(user32.GetForegroundWindow() or 0) == int(hwnd)
        )
        if not was_foreground:
            try:
                activated = self._capture_activated_screen(hwnd, rect)
            except DesktopToolError:
                activated = None
            if activated is not None:
                images.append(activated)
            # A minimized window reports the Win32 sentinel rectangle
            # (-32000,-32000,...) until it is restored.  Refresh the bounds
            # after activation so the remaining capture methods target the
            # actual main window rather than that off-screen placeholder.
            try:
                rect = self.window_rect(hwnd)
            except (DesktopToolError, AttributeError, OSError):
                pass
        expected_size = (rect[2] - rect[0], rect[3] - rect[1])
        try:
            # PrintWindow captures an obscured/minimized window when the
            # installed WeChat runtime provides its Win32 helper.
            import win32gui
            from wechatauto.utils.win32 import capture

            images.append(capture(int(hwnd), rect))
        except Exception:
            pass

        # Some GPU-rendered windows (网易云、QQ、浏览器等) return a black
        # bitmap through the default PrintWindow call.  Retry with
        # PW_RENDERFULLCONTENT before falling back to the visible desktop.
        try:
            rendered = self._capture_print_window(int(hwnd), rect)
        except Exception:
            rendered = None
        if rendered is not None:
            images.append(rendered)
        if was_foreground:
            try:
                time.sleep(0.25)
                images.append(self._grab_screen(rect))
            except DesktopToolError:
                pass
        for image in images:
            if self._is_usable_image(image, expected_size):
                image.save(output, format="PNG")
                return output
        raise DesktopToolError("应用窗口截图结果为空或全黑")

    def _capture_activated_screen(
        self, hwnd: int, rect: tuple[int, int, int, int]
    ) -> Any | None:
        """Temporarily show a GPU window so a real desktop capture can render it."""
        user32 = self._user32()
        if user32 is None:
            return None
        previous = int(user32.GetForegroundWindow() or 0)
        if not user32.ShowWindow(int(hwnd), 9):
            # ShowWindow returning False only means the previous visibility
            # state was false; continue as long as the handle is valid.
            if not user32.IsWindow(int(hwnd)):
                return None
        if not user32.SetForegroundWindow(int(hwnd)):
            return None
        try:
            capture_rect = rect
            try:
                refreshed_rect = self.window_rect(hwnd)
                if (
                    refreshed_rect[2] - refreshed_rect[0] >= 2
                    and refreshed_rect[3] - refreshed_rect[1] >= 2
                ):
                    capture_rect = refreshed_rect
            except (DesktopToolError, AttributeError, OSError):
                pass
            # GPU-rendered windows often show a toolbar/blank frame immediately
            # after activation.  Wait for the compositor, then sample a few
            # frames and use the latest usable one.
            time.sleep(0.8)
            frames = []
            for _ in range(3):
                try:
                    frame = self._grab_screen(capture_rect)
                except DesktopToolError:
                    frame = None
                if frame is not None and self._is_usable_image(
                    frame,
                    (
                        capture_rect[2] - capture_rect[0],
                        capture_rect[3] - capture_rect[1],
                    ),
                ):
                    frames.append(frame)
                time.sleep(0.2)
            return frames[-1] if frames else None
        finally:
            if previous and previous != int(hwnd) and user32.IsWindow(previous):
                user32.SetForegroundWindow(previous)

    @staticmethod
    def _is_usable_image(
        image: Any, expected_size: tuple[int, int] | None = None
    ) -> bool:
        try:
            width = int(image.width)
            height = int(image.height)
            if width < 2 or height < 2:
                return False
            if expected_size is not None:
                expected_width, expected_height = expected_size
                if width < expected_width * 0.75 or height < expected_height * 0.75:
                    return False
            extrema = image.convert("RGB").getextrema()
            return max(channel[1] for channel in extrema) > 8
        except Exception:
            return False

    @classmethod
    def _capture_print_window(
        cls, hwnd: int, rect: tuple[int, int, int, int]
    ) -> Any | None:
        """Capture a hidden GPU window with PW_RENDERFULLCONTENT."""
        import win32gui
        import win32ui
        from PIL import Image

        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        if width <= 1 or height <= 1:
            return None
        window_dc = win32gui.GetWindowDC(hwnd)
        source_dc = win32ui.CreateDCFromHandle(window_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        try:
            user32 = cls._user32()
            if user32 is None or not user32.PrintWindow(
                hwnd, memory_dc.GetSafeHdc(), 0x00000002
            ):
                return None
            info = bitmap.GetInfo()
            data = bitmap.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB",
                (int(info["bmWidth"]), int(info["bmHeight"])),
                data,
                "raw",
                "BGRX",
                0,
                1,
            )
        finally:
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, window_dc)
            win32gui.DeleteObject(bitmap.GetHandle())

    def capture_foreground(self, prefix: str = "foreground") -> Path:
        return self.capture_window(self.foreground_window().hwnd, prefix)

    def capture_desktop(self, prefix: str = "desktop") -> Path:
        output = self._output_path(prefix)
        try:
            from PIL import ImageGrab

            try:
                image = ImageGrab.grab(all_screens=True)
            except TypeError:
                image = ImageGrab.grab()
        except Exception as error:
            raise DesktopToolError(f"桌面截图失败：{type(error).__name__}") from error
        image.save(output, format="PNG")
        return output

    def combine_images(
        self,
        images: list[tuple[str, str | Path]],
        prefix: str = "screenshots",
        *,
        max_width: int = 1400,
    ) -> Path:
        """Compose several screenshots into one labeled image.

        The WeChat delivery drivers intentionally accept one image per atomic
        delivery.  A labeled contact sheet keeps a multi-window screenshot
        request atomic while preserving every captured window and its source
        name in the workbench history.
        """
        if not images:
            raise DesktopToolError("没有可合并的截图")
        try:
            from PIL import Image, ImageDraw
        except ImportError as error:  # pragma: no cover - dependency is bundled
            raise DesktopToolError("图片合并需要 Pillow") from error

        panels: list[tuple[str, Any]] = []
        for label, value in images:
            path = Path(value)
            if not path.is_file():
                raise DesktopToolError(f"截图文件不存在：{path}")
            try:
                image = Image.open(path).convert("RGB")
            except Exception as error:  # noqa: BLE001 - normalize image errors
                raise DesktopToolError(f"无法读取截图：{path.name}") from error
            if image.width > max_width:
                height = max(1, round(image.height * max_width / image.width))
                image = image.resize((max_width, height), Image.Resampling.LANCZOS)
            panels.append((str(label or "窗口"), image))

        header_height = 36
        gap = 12
        width = max(image.width for _label, image in panels)
        height = sum(header_height + image.height for _label, image in panels)
        height += gap * max(0, len(panels) - 1)
        canvas = Image.new("RGB", (width, height), "#f4f6fa")
        draw = ImageDraw.Draw(canvas)
        y = 0
        for index, (label, image) in enumerate(panels):
            draw.rectangle((0, y, width, y + header_height), fill="#273142")
            draw.text((14, y + 10), label, fill="#ffffff")
            y += header_height
            canvas.paste(image, ((width - image.width) // 2, y))
            y += image.height
            if index < len(panels) - 1:
                y += gap
        output = self._output_path(prefix)
        canvas.save(output, format="PNG")
        return output

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        user32 = self._user32()
        if user32 is None:
            raise DesktopToolError("Windows desktop tools are unavailable")
        rect = _RECT()
        if not user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            raise DesktopToolError("无法读取应用窗口区域")
        if rect.right <= rect.left or rect.bottom <= rect.top:
            raise DesktopToolError("应用窗口区域无效")
        return rect.left, rect.top, rect.right, rect.bottom

    def _output_path(self, prefix: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir / f"{prefix}-{int(time.time() * 1000)}.png"

    def _grab_screen(self, rect: tuple[int, int, int, int]) -> Any:
        try:
            from PIL import ImageGrab

            try:
                return ImageGrab.grab(bbox=rect, all_screens=True)
            except TypeError:
                return ImageGrab.grab(bbox=rect)
        except Exception as error:
            raise DesktopToolError(f"应用窗口截图失败：{type(error).__name__}") from error

    @staticmethod
    def _user32() -> Any | None:
        if os.name != "nt":
            return None
        # Use a private DLL handle.  Mutating ctypes.windll.user32 function
        # signatures is process-global and can collide with the companion
        # follower's own RECT structure.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        user32.IsWindow.restype = ctypes.c_bool
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowVisible.restype = ctypes.c_bool
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_bool
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = ctypes.c_bool
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        user32.PrintWindow.restype = ctypes.c_bool
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        return user32

    @staticmethod
    def _window_title(user32: Any, hwnd: int) -> str:
        length = max(256, int(user32.GetWindowTextLengthW(hwnd)) + 1)
        buffer = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, buffer, length)
        return str(buffer.value or "").strip()

    @staticmethod
    def _process_name(user32: Any, hwnd: int) -> str:
        try:
            import psutil

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return str(psutil.Process(int(pid.value)).name() or "")
        except Exception:
            return ""


class _RECT(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )
