from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import smtplib
import ssl
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Iterable

import tkinter as tk
from tkinter import messagebox, ttk

import MetaTrader5 as mt5
import numpy as np


APP_NAME = "XAUUSD 多周期预警助手"
SYMBOL = "XAUUSD"
DEFAULT_TIMEFRAME = "M5"
TIMEFRAMES = {
    "M5": (mt5.TIMEFRAME_M5, 5 * 60),
    "M15": (mt5.TIMEFRAME_M15, 15 * 60),
    "H1": (mt5.TIMEFRAME_H1, 60 * 60),
}
TIMEFRAME_SECONDS = TIMEFRAMES[DEFAULT_TIMEFRAME][1]
BEIJING_TZ = timezone(timedelta(hours=8), name="北京时间")
PIVOT_N = 2
DEFAULT_MIN_WAVE_BARS = 5
DEFAULT_MIN_WAVE_RATIO = 0.004
DEFAULT_MAX_INTERNAL_RATIO = 0.5
CONSOLIDATION_BARS = 8
FIB_RATIO = 0.382
FETCH_BARS = 2000
POLL_SECONDS = 10

# MetaTrader5 扩展在运行时动态导入 numpy.core；保留显式引用，确保单文件打包完整收集 NumPy。
NUMPY_RUNTIME_VERSION = np.__version__


def app_data_dir() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home())
    path = base / "XAUUSDAlert"
    path.mkdir(parents=True, exist_ok=True)
    return path


CONFIG_FILE = app_data_dir() / "config.json"
STATE_FILE = app_data_dir() / "state.json"
LOG_FILE = app_data_dir() / "app.log"


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Signal:
    signal_id: str
    timeframe: str
    direction: str
    current_price: float
    take_profit: float
    fib_0382: float
    wave_min: float
    wave_max: float
    wave_start: int
    wave_end: int
    consolidation_start: int
    consolidation_end: int


@dataclass
class Settings:
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    security: str = "SSL"
    smtp_user: str = ""
    smtp_password: str = ""
    recipients: str = ""
    mt5_path: str = ""
    timeframe: str = DEFAULT_TIMEFRAME
    min_wave_bars: int = DEFAULT_MIN_WAVE_BARS
    min_wave_ratio: float = DEFAULT_MIN_WAVE_RATIO
    max_internal_ratio: float = DEFAULT_MAX_INTERNAL_RATIO


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect_secret(value: str) -> str:
    if not value:
        return ""
    if os.name != "nt":
        return base64.b64encode(value.encode("utf-8")).decode("ascii")
    in_blob, keepalive = _blob(value.encode("utf-8"))
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), "XAUUSDAlert", None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        del keepalive


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    raw = base64.b64decode(value)
    if os.name != "nt":
        return raw.decode("utf-8")
    in_blob, keepalive = _blob(raw)
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
        del keepalive


def load_settings() -> Settings:
    if not CONFIG_FILE.exists():
        return Settings()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        data["smtp_password"] = unprotect_secret(data.pop("password_encrypted", ""))
        allowed = Settings.__dataclass_fields__.keys()
        return Settings(**{key: value for key, value in data.items() if key in allowed})
    except Exception:
        return Settings()


def save_settings(settings: Settings) -> None:
    data = asdict(settings)
    password = data.pop("smtp_password")
    data["password_encrypted"] = protect_secret(password)
    temp = CONFIG_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CONFIG_FILE)


def load_state() -> dict:
    default = {"last_scanned_close_by_timeframe": {}, "sent_ids": []}
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        scanned = data.get("last_scanned_close_by_timeframe", {})
        if not isinstance(scanned, dict):
            scanned = {}
        scanned = {
            key: int(value)
            for key, value in scanned.items()
            if key in TIMEFRAMES
        }
        # 从旧版单一 M5 进度平滑迁移。
        if DEFAULT_TIMEFRAME not in scanned and data.get("last_scanned_close"):
            scanned[DEFAULT_TIMEFRAME] = int(data["last_scanned_close"])
        return {"last_scanned_close_by_timeframe": scanned, "sent_ids": list(data.get("sent_ids", []))[-5000:]}
    except Exception:
        return default


def save_state(state: dict) -> None:
    scanned = state.get("last_scanned_close_by_timeframe", {})
    state = {
        "last_scanned_close_by_timeframe": {
            key: int(value) for key, value in scanned.items() if key in TIMEFRAMES
        },
        "sent_ids": list(state.get("sent_ids", []))[-5000:],
    }
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(STATE_FILE)


def format_bj(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(BEIJING_TZ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def are_contiguous(bars: list[Bar], timeframe_seconds: int = TIMEFRAME_SECONDS) -> bool:
    return all(
        bars[index].time - bars[index - 1].time == timeframe_seconds
        for index in range(1, len(bars))
    )


def find_pivots(
    bars: list[Bar], n: int = PIVOT_N, timeframe_seconds: int = TIMEFRAME_SECONDS
) -> list[tuple[int, int]]:
    """返回按时间排列的 (索引, 类型)，类型 1=Pivot High，-1=Pivot Low。"""
    pivots: list[tuple[int, int]] = []
    if n < 1 or len(bars) < n * 2 + 1:
        return pivots
    for index in range(n, len(bars) - n):
        current = bars[index]
        left = bars[index - n : index]
        right = bars[index + 1 : index + n + 1]
        if not are_contiguous(bars[index - n : index + n + 1], timeframe_seconds):
            continue
        is_low = current.low < min(item.low for item in left) and current.low < min(
            item.low for item in right
        )
        is_high = current.high > max(item.high for item in left) and current.high > max(
            item.high for item in right
        )
        # 同一根外包K线可能同时成为高低枢轴；保留两者使其成为保守的波段分隔点。
        if is_low:
            pivots.append((index, -1))
        if is_high:
            pivots.append((index, 1))
    return pivots


def maximum_internal_move(wave: list[Bar], direction: int) -> float:
    """上涨返回历史最高点后的最大回撤；下跌返回历史最低点后的最大反弹。"""
    if len(wave) < 2:
        return 0.0
    maximum = 0.0
    if direction == 1:
        running_high = wave[0].high
        for item in wave[1:]:
            maximum = max(maximum, running_high - item.low)
            running_high = max(running_high, item.high)
    else:
        running_low = wave[0].low
        for item in wave[1:]:
            maximum = max(maximum, item.high - running_low)
            running_low = min(running_low, item.low)
    return max(0.0, maximum)


def find_completed_waves(
    bars: list[Bar],
    min_wave_bars: int = DEFAULT_MIN_WAVE_BARS,
    min_wave_ratio: float = DEFAULT_MIN_WAVE_RATIO,
    max_internal_ratio: float = DEFAULT_MAX_INTERNAL_RATIO,
    pivot_n: int = PIVOT_N,
    timeframe_seconds: int = TIMEFRAME_SECONDS,
) -> Iterable[tuple[int, int, int]]:
    """筛选相邻枢轴形成的合格波段，返回（方向, 起点索引, 极值终点索引）。"""
    pivots = find_pivots(bars, pivot_n, timeframe_seconds)
    for (start, start_type), (end, end_type) in zip(pivots, pivots[1:]):
        if start == end or start_type == end_type:
            continue
        direction = 1 if start_type == -1 and end_type == 1 else -1
        wave = bars[start : end + 1]
        if len(wave) < min_wave_bars:
            continue

        if direction == 1:
            wave_min = bars[start].low
            wave_max = bars[end].high
            amplitude = wave_max - wave_min
            ratio_ok = wave_min > 0 and amplitude / wave_min >= min_wave_ratio
        else:
            wave_max = bars[start].high
            wave_min = bars[end].low
            amplitude = wave_max - wave_min
            ratio_ok = wave_max > 0 and amplitude / wave_max >= min_wave_ratio
        if amplitude <= 0 or not ratio_ok:
            continue
        if maximum_internal_move(wave, direction) > amplitude * max_internal_ratio:
            continue
        yield direction, start, end


def detect_signals(
    bars: list[Bar],
    current_price: float,
    after_close: int,
    already_sent: set[str],
    min_wave_bars: int = DEFAULT_MIN_WAVE_BARS,
    min_wave_ratio: float = DEFAULT_MIN_WAVE_RATIO,
    max_internal_ratio: float = DEFAULT_MAX_INTERNAL_RATIO,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> list[Signal]:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"不支持的周期：{timeframe}")
    timeframe_seconds = TIMEFRAMES[timeframe][1]
    results: list[Signal] = []
    for direction, start, end in find_completed_waves(
        bars, min_wave_bars, min_wave_ratio, max_internal_ratio, PIVOT_N, timeframe_seconds
    ):
        confirmation_index = end + PIVOT_N
        consolidation_start = end + PIVOT_N + 1
        consolidation_end = consolidation_start + CONSOLIDATION_BARS - 1
        if consolidation_end >= len(bars):
            continue
        if not are_contiguous(bars[start : consolidation_end + 1], timeframe_seconds):
            continue
        end_close_time = bars[consolidation_end].time + timeframe_seconds
        if end_close_time <= after_close:
            continue

        consolidation = bars[consolidation_start : consolidation_end + 1]
        wave_min = bars[start].low if direction == 1 else bars[end].low
        wave_max = bars[end].high if direction == 1 else bars[start].high
        price_range = wave_max - wave_min
        if price_range <= 0:
            continue

        if direction == 1:
            fib = wave_max - price_range * FIB_RATIO
            valid = all(item.low >= fib for item in consolidation)
            take_profit = fib + price_range
            direction_name = "多头"
        else:
            fib = wave_min + price_range * FIB_RATIO
            valid = all(item.high <= fib for item in consolidation)
            # 空头止盈向下延伸一个完整波段：min + (max-min)*0.382 - (max-min)
            take_profit = fib - price_range
            direction_name = "空头"

        signal_id = f"{SYMBOL}:{timeframe}:{direction}:{bars[start].time}:{bars[end].time}"
        if valid and signal_id not in already_sent:
            results.append(
                Signal(
                    signal_id=signal_id,
                    timeframe=timeframe,
                    direction=direction_name,
                    current_price=current_price,
                    take_profit=take_profit,
                    fib_0382=fib,
                    wave_min=wave_min,
                    wave_max=wave_max,
                    wave_start=bars[start].time,
                    wave_end=bars[confirmation_index].time + timeframe_seconds,
                    consolidation_start=bars[consolidation_start].time,
                    consolidation_end=end_close_time,
                )
            )
    return results


def recipients_from_text(value: str) -> list[str]:
    normalized = value.replace("；", ";").replace("，", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def send_email(settings: Settings, subject: str, plain: str, html: str | None = None) -> None:
    recipients = recipients_from_text(settings.recipients)
    if not settings.smtp_host or not settings.smtp_user or not recipients:
        raise ValueError("请完整填写 SMTP 服务器、发件账号和收件邮箱")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = ", ".join(recipients)
    message.set_content(plain)
    if html:
        message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    timeout = 20
    if settings.security == "SSL":
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout, context=context) as server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout) as server:
            server.ehlo()
            if settings.security == "STARTTLS":
                server.starttls(context=context)
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)


def signal_email(signal: Signal) -> tuple[str, str, str]:
    subject = f"【XAUUSD {signal.timeframe}预警】{signal.direction}信号"
    fields = [
        ("品种", SYMBOL),
        ("周期", signal.timeframe),
        ("开仓方向", signal.direction),
        ("当前现价", f"{signal.current_price:.2f}"),
        ("止盈价格", f"{signal.take_profit:.2f}"),
        ("0.382价格", f"{signal.fib_0382:.2f}"),
        ("波段极值", f"min={signal.wave_min:.2f}，max={signal.wave_max:.2f}"),
        ("波段开始时间（北京时间）", format_bj(signal.wave_start)),
        ("波段结束时间（北京时间）", format_bj(signal.wave_end)),
        ("整理段开始时间（北京时间）", format_bj(signal.consolidation_start)),
        ("整理段结束时间（北京时间）", format_bj(signal.consolidation_end)),
    ]
    plain = f"XAUUSD {signal.timeframe}交易信号\n\n" + "\n".join(
        f"{key}：{value}" for key, value in fields
    )
    rows = "".join(
        f"<tr><td style='padding:8px;border:1px solid #ddd'><b>{key}</b></td>"
        f"<td style='padding:8px;border:1px solid #ddd'>{value}</td></tr>"
        for key, value in fields
    )
    html = (
        f"<html><body><h2>XAUUSD {signal.timeframe}交易信号</h2>"
        f"<table style='border-collapse:collapse'>{rows}</table>"
        "<p style='color:#666'>本邮件由本地预警程序自动生成，仅作策略提示。</p></body></html>"
    )
    return subject, plain, html


class Monitor:
    def __init__(self, settings_provider: Callable[[], Settings], log: Callable[[str], None]):
        self.settings_provider = settings_provider
        self.log = log
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.server_offset_seconds = 0

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="MT5Monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _initialize_mt5(self, settings: Settings) -> None:
        kwargs = {"path": settings.mt5_path} if settings.mt5_path.strip() else {}
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 连接失败：{mt5.last_error()}。请先打开并登录 MT5。")
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(f"MT5 中无法选择 {SYMBOL}：{mt5.last_error()}")

    def _refresh_server_offset(self) -> None:
        """校正部分经纪商把服务器墙上时间直接写入 epoch 的情况。"""
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None or not tick.time:
            return
        difference = int(tick.time) - int(time.time())
        # 仅在行情足够新、差值接近 15 分钟整倍数时采信，避免休市旧 tick 误判。
        rounded = round(difference / 900) * 900
        if abs(difference) <= 14 * 3600 and abs(difference - rounded) <= 300:
            self.server_offset_seconds = rounded

    def _closed_bars(self, timeframe: str = DEFAULT_TIMEFRAME) -> list[Bar]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"不支持的周期：{timeframe}")
        mt5_timeframe, timeframe_seconds = TIMEFRAMES[timeframe]
        rates = mt5.copy_rates_from_pos(SYMBOL, mt5_timeframe, 0, FETCH_BARS)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"读取 {SYMBOL} {timeframe} K线失败：{mt5.last_error()}")
        self._refresh_server_offset()
        now = int(time.time())
        result = [
            Bar(
                int(row["time"]) - self.server_offset_seconds,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
            )
            for row in rates
            if int(row["time"]) - self.server_offset_seconds + timeframe_seconds <= now
        ]
        result.sort(key=lambda item: item.time)
        return result

    @staticmethod
    def _current_price() -> float:
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            raise RuntimeError(f"读取 {SYMBOL} 现价失败：{mt5.last_error()}")
        for value in (tick.last, tick.bid, tick.ask):
            if value and value > 0:
                return float(value)
        raise RuntimeError("MT5 返回的 XAUUSD 现价无效")

    def _run(self) -> None:
        state = load_state()
        last_error_text = ""
        active_timeframe = ""
        try:
            self._initialize_mt5(self.settings_provider())
            self.log("已连接 MT5，固定品种 XAUUSD，可选周期 M5 / M15 / H1。")
            while not self.stop_event.is_set():
                try:
                    settings = self.settings_provider()
                    timeframe = settings.timeframe
                    if timeframe not in TIMEFRAMES:
                        raise ValueError(f"不支持的周期：{timeframe}")
                    timeframe_seconds = TIMEFRAMES[timeframe][1]
                    if timeframe != active_timeframe:
                        self.log(f"当前扫描周期：{timeframe}。")
                        active_timeframe = timeframe
                    bars = self._closed_bars(timeframe)
                    if not bars:
                        raise RuntimeError(f"没有可用的已收盘 {timeframe} K线")
                    if self.server_offset_seconds:
                        offset_hours = self.server_offset_seconds / 3600
                        offset_text = f"{offset_hours:+g}小时"
                    else:
                        offset_text = "UTC"
                    latest_close = bars[-1].time + timeframe_seconds
                    scanned = state["last_scanned_close_by_timeframe"]
                    last_scanned_close = int(scanned.get(timeframe, 0))
                    if last_scanned_close == 0:
                        scanned[timeframe] = latest_close
                        save_state(state)
                        self.log(
                            f"{timeframe} 首次启动基准已设为 {format_bj(latest_close)}，不补发历史信号。"
                            f"MT5服务器时间偏移：{offset_text}。"
                        )
                    elif latest_close > last_scanned_close:
                        sent = set(state["sent_ids"])
                        signals = detect_signals(
                            bars,
                            self._current_price(),
                            last_scanned_close,
                            sent,
                            settings.min_wave_bars,
                            settings.min_wave_ratio,
                            settings.max_internal_ratio,
                            timeframe,
                        )
                        for signal in signals:
                            subject, plain, html = signal_email(signal)
                            send_email(self.settings_provider(), subject, plain, html)
                            state["sent_ids"].append(signal.signal_id)
                            save_state(state)
                            self.log(
                                f"已发送 {timeframe} {signal.direction} 信号：现价 {signal.current_price:.2f}，"
                                f"止盈 {signal.take_profit:.2f}。"
                            )
                        scanned[timeframe] = latest_close
                        save_state(state)
                        if not signals:
                            self.log(
                                f"已检查 {timeframe} 新收盘 K线 {format_bj(latest_close)}，无有效信号。"
                            )
                    last_error_text = ""
                except Exception as exc:
                    error_text = str(exc)
                    if error_text != last_error_text:
                        self.log(f"监控异常：{error_text}")
                        last_error_text = error_text
                self.stop_event.wait(POLL_SECONDS)
        except Exception as exc:
            self.log(str(exc))
        finally:
            mt5.shutdown()
            self.log("监控已停止。")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("820x780")
        self.minsize(760, 700)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.events: queue.Queue[str] = queue.Queue()
        self.settings = load_settings()
        self.monitor = Monitor(self._settings_from_form_threadsafe, self._queue_log)
        self.vars: dict[str, tk.StringVar] = {}
        self._build_ui()
        self._load_form(self.settings)
        self.after(150, self._drain_events)
        self._log_ui(f"配置和运行记录目录：{app_data_dir()}")

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        container = ttk.Frame(self, padding=14)
        container.pack(fill="both", expand=True)

        status_frame = ttk.Frame(container)
        status_frame.pack(fill="x", pady=(0, 10))
        ttk.Label(status_frame, text="XAUUSD 多周期预警助手", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.status_label = ttk.Label(status_frame, text="● 已停止", foreground="#a33")
        self.status_label.pack(side="right")

        form = ttk.LabelFrame(container, text="邮件与 MT5 配置", padding=12)
        form.pack(fill="x")
        labels = [
            ("行情周期", "timeframe"),
            ("SMTP服务器", "smtp_host"),
            ("端口", "smtp_port"),
            ("加密方式", "security"),
            ("发件邮箱/用户名", "smtp_user"),
            ("邮箱授权码/密码", "smtp_password"),
            ("收件邮箱（逗号分隔）", "recipients"),
            ("MT5 terminal64.exe 路径（可留空）", "mt5_path"),
        ]
        for row, (label, key) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=5)
            variable = tk.StringVar()
            self.vars[key] = variable
            if key == "security":
                widget = ttk.Combobox(form, textvariable=variable, values=("SSL", "STARTTLS", "无"), state="readonly")
            elif key == "timeframe":
                widget = ttk.Combobox(
                    form, textvariable=variable, values=tuple(TIMEFRAMES), state="readonly"
                )
            else:
                show = "●" if key == "smtp_password" else ""
                widget = ttk.Entry(form, textvariable=variable, show=show)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
        form.columnconfigure(1, weight=1)

        filters = ttk.LabelFrame(container, text="波段过滤参数", padding=10)
        filters.pack(fill="x", pady=(10, 0))
        filter_fields = [
            ("K 最少K线数", "min_wave_bars", "5"),
            ("R 最小相对幅度", "min_wave_ratio", "0.004"),
            ("M 最大内部回撤/反弹倍数", "max_internal_ratio", "0.5"),
        ]
        for column, (label, key, example) in enumerate(filter_fields):
            block = ttk.Frame(filters)
            block.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 10, 0))
            ttk.Label(block, text=label).pack(anchor="w")
            variable = tk.StringVar(value=example)
            self.vars[key] = variable
            ttk.Entry(block, textvariable=variable).pack(fill="x", pady=(4, 0))
            filters.columnconfigure(column, weight=1)

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=10)
        ttk.Button(buttons, text="保存配置", command=self._save).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="发送测试邮件", command=self._test_email).pack(side="left", padx=(0, 8))
        self.start_button = ttk.Button(buttons, text="开始监控", command=self._start)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="停止监控", command=self._stop, state="disabled")
        self.stop_button.pack(side="left")

        rules = ttk.LabelFrame(container, text="策略口径", padding=10)
        rules.pack(fill="x", pady=(0, 10))
        rule_text = (
            "仅用 XAUUSD 所选周期（M5/M15/H1）的已收盘K线，不混用周期。N固定为2："
            "本K线高/低点严格高于/低于左右各2根，"
            "形成 Pivot High / Pivot Low；相邻低→高为上涨候选，相邻高→低为下跌候选。\n"
            "候选需同时满足 K、R、M 三项过滤。终点枢轴右侧第2根收盘时确认，右侧第3根开始计8根整理K线。"
            "多头：任一 low < 上涨波段0.382则作废；空头：任一 high > 下跌波段0.382则作废。"
            "止盈严格按需求公式计算；单波段仅邮件一次。所有邮件时间均为北京时间。"
        )
        ttk.Label(rules, text=rule_text, wraplength=750, justify="left").pack(anchor="w")

        log_frame = ttk.LabelFrame(container, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=12, state="disabled", wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _load_form(self, settings: Settings) -> None:
        for key, variable in self.vars.items():
            variable.set(str(getattr(settings, key)))

    def _form_settings(self) -> Settings:
        try:
            port = int(self.vars["smtp_port"].get().strip())
            min_wave_bars = int(self.vars["min_wave_bars"].get().strip())
            min_wave_ratio = float(self.vars["min_wave_ratio"].get().strip())
            max_internal_ratio = float(self.vars["max_internal_ratio"].get().strip())
        except ValueError as exc:
            raise ValueError("端口、K、R、M 必须填写有效数字") from exc
        if not 1 <= port <= 65535:
            raise ValueError("SMTP 端口应在 1 到 65535 之间")
        if min_wave_bars < 2:
            raise ValueError("K 最少K线数不能小于 2")
        if not 0 <= min_wave_ratio <= 1:
            raise ValueError("R 最小相对幅度应在 0 到 1 之间")
        if not 0 <= max_internal_ratio <= 1:
            raise ValueError("M 最大内部回撤/反弹倍数应在 0 到 1 之间")
        timeframe = self.vars["timeframe"].get().strip()
        if timeframe not in TIMEFRAMES:
            raise ValueError("行情周期只能选择 M5、M15 或 H1")
        return Settings(
            smtp_host=self.vars["smtp_host"].get().strip(),
            smtp_port=port,
            security=self.vars["security"].get().strip(),
            smtp_user=self.vars["smtp_user"].get().strip(),
            smtp_password=self.vars["smtp_password"].get(),
            recipients=self.vars["recipients"].get().strip(),
            mt5_path=self.vars["mt5_path"].get().strip(),
            timeframe=timeframe,
            min_wave_bars=min_wave_bars,
            min_wave_ratio=min_wave_ratio,
            max_internal_ratio=max_internal_ratio,
        )

    def _settings_from_form_threadsafe(self) -> Settings:
        # 监控线程只读取已保存的不可变快照，避免跨线程访问 Tk。
        return self.settings

    def _save(self, silent: bool = False) -> bool:
        try:
            self.settings = self._form_settings()
            save_settings(self.settings)
            if not silent:
                self._log_ui("配置已保存，邮箱密码已由 Windows 当前用户加密。")
            return True
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return False

    def _test_email(self) -> None:
        if not self._save(silent=True):
            return
        self._log_ui("正在发送测试邮件……")
        settings = self.settings

        def worker() -> None:
            try:
                now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
                send_email(settings, "【XAUUSD预警】测试邮件", f"邮件配置测试成功。\n北京时间：{now}")
                self._queue_log("测试邮件发送成功。")
            except Exception as exc:
                self._queue_log(f"测试邮件发送失败：{exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _start(self) -> None:
        if not self._save(silent=True):
            return
        if not self.settings.smtp_host or not self.settings.smtp_user or not recipients_from_text(self.settings.recipients):
            messagebox.showerror(APP_NAME, "请先完整填写并保存邮件配置。", parent=self)
            return
        self.monitor.start()
        self.status_label.configure(text="● 监控中", foreground="#18794e")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._log_ui("正在启动监控……")

    def _stop(self) -> None:
        self.monitor.stop()
        self.status_label.configure(text="● 已停止", foreground="#a33")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _queue_log(self, message: str) -> None:
        timestamp = datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        try:
            with LOG_FILE.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            pass
        self.events.put(line)

    def _log_ui(self, message: str) -> None:
        timestamp = datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
        self._append_log(f"[{timestamp}] {message}")

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                self._append_log(self.events.get_nowait())
        except queue.Empty:
            pass
        if not self.monitor.running and str(self.stop_button["state"]) == "normal":
            self.status_label.configure(text="● 已停止", foreground="#a33")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
        self.after(150, self._drain_events)

    def _on_close(self) -> None:
        self.monitor.stop()
        self.destroy()


def main() -> None:
    try:
        App().mainloop()
    except Exception as exc:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, str(exc), APP_NAME, 0x10)
        else:
            raise


if __name__ == "__main__":
    main()
