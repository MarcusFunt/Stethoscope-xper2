"""
XIAO MG24 Sense USB-CDC audio recorder GUI
- Sends REC,<sr>,<n> to the board
- Receives DATA,<n> header then <n> int16 samples (little-endian)
- Plots waveform, can play audio and save WAV

Dependencies: pip install dearpygui pyserial numpy sounddevice
Optional (for saving WAV without external libs we use stdlib wave)
"""

import sys
import time
import math
import threading
import struct
import wave
from typing import Optional
import queue

import numpy as np
import serial
from serial.tools import list_ports

import dearpygui.dearpygui as dpg

# ----------------- Serial helpers -----------------

DEVICE_MAX_SR = 8000

def list_serial_ports():
    ports = list_ports.comports()
    # Prefer CDC ACM/USB devices first
    ports_sorted = sorted(ports, key=lambda p: ("USB" not in (p.description or ""), p.device))
    return [f"{p.device} — {p.description}".strip() for p in ports_sorted], [p.device for p in ports_sorted]


def open_serial(port: str, timeout=2.0) -> serial.Serial:
    ser = serial.Serial(port=port, baudrate=115200, timeout=timeout)
    # Give the device a moment to enumerate; some cores reset on open
    time.sleep(0.25)
    # Flush any startup text like READY\n
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    return ser


def rec_once(ser: serial.Serial, sr: int, seconds: float) -> np.ndarray:
    n = int(round(sr * seconds))
    cmd = f"REC,{sr},{n}\n".encode()
    # Discard any stray bytes from previous runs before issuing the command.
    # Without this, stale binary audio from an earlier failed capture could
    # appear before the ACK/DATA headers and trigger "Unexpected header"
    # errors on the host side.
    ser.reset_input_buffer()
    ser.write(cmd)
    ser.flush()

    # Expect: DATA,<n>\n
    header = ""
    ack_seen = False
    # Allow the device at least the requested recording duration plus a
    # little slack to deliver the DATA header. The previous fixed attempt
    # counter caused a false timeout for long captures (e.g. 10 s) because
    # we would give up before the device finished sampling.
    wait_budget = max(5.0, seconds + 2.0)
    wait_deadline = time.monotonic() + wait_budget
    while True:
        raw = ser.readline()
        if raw == b"":
            if time.monotonic() > wait_deadline:
                if ack_seen:
                    raise RuntimeError("Device did not send DATA header after ACK.")
                raise RuntimeError("Device did not send DATA header (only blank lines).")
            continue

        stripped = raw.strip()
        if stripped == b"":
            if time.monotonic() > wait_deadline:
                if ack_seen:
                    raise RuntimeError("Device did not send DATA header after ACK.")
                raise RuntimeError("Device did not send DATA header (only blank lines).")
            continue
        if stripped.endswith(b"ACK"):
            ack_seen = True
            if time.monotonic() > wait_deadline:
                raise RuntimeError("Device did not send DATA header after ACK.")
            continue
        idx = stripped.find(b"DATA,")
        if idx != -1:
            header_bytes = stripped[idx:]
            try:
                header = header_bytes.decode("ascii")
            except UnicodeDecodeError:
                pass
            else:
                break

        preview = stripped.decode("ascii", errors="replace")
        raise RuntimeError(f"Unexpected header from device: {preview!r}")

    try:
        n_declared = int(header.split(",", 1)[1])
    except Exception:
        raise RuntimeError(f"Malformed DATA header: {header!r}")

    if n_declared != n:
        # not fatal; we'll honor the device's count
        n = n_declared

    # Read exactly n int16 little-endian samples
    byte_count = n * 2
    buf = bytearray(byte_count)
    mv = memoryview(buf)
    got = 0
    while got < byte_count:
        chunk = ser.readinto(mv[got:])
        if not chunk:
            raise TimeoutError("Timed out while reading audio bytes from device")
        got += chunk

    # Optionally read trailing DONE line, but don't block if it's not there yet
    ser.timeout = 0
    _ = ser.readline()  # best-effort
    ser.timeout = 2.0

    data = np.frombuffer(buf, dtype=np.int16)
    return data


# ----------------- Dear PyGui app -----------------

class App:
    def __init__(self):
        self.ser: Optional[serial.Serial] = None
        self.ports_labels, self.ports_devices = list_serial_ports()
        self.current_samples: Optional[np.ndarray] = None
        self.current_sr: int = DEVICE_MAX_SR
        self._record_thread: Optional[threading.Thread] = None
        self._recording: bool = False
        self._result_queue: "queue.Queue[tuple[Optional[np.ndarray], int, Optional[Exception]]]" = queue.Queue()
        self._record_start_time: float = 0.0
        self._record_duration: float = 0.0
        self._record_pulse: float = 0.0
        self._post_record_flash_until: float = 0.0
        self._post_record_flash_duration: float = 0.0
        self._post_record_flash_color = (110, 150, 210, 255)
        self._connected: bool = False
        self._connected_port_label: str = ""
        self._conn_pulse: float = 0.0
        self._last_frame_time: float = time.perf_counter()
        self._record_color_idle = (110, 150, 210, 255)
        self._record_color_success = (120, 210, 150, 255)
        self._record_color_error = (230, 140, 90, 255)
        self._record_color_recording = (120, 200, 255, 255)
        self._conn_color_connected = (110, 200, 150, 255)
        self._conn_color_disconnected = (200, 90, 90, 255)

        dpg.create_context()
        self._apply_theme()

        with dpg.window(
            tag="main",
            label="XIAO MG24 Sense — USB CDC Audio",
            width=1100,
            height=720,
            no_resize=False,
        ):
            dpg.add_text("Capture audio from the XIAO MG24 Sense in style.", color=(190, 205, 230, 255))
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True, horizontal_spacing=32):
                with dpg.group():
                    dpg.add_text("Device", color=(149, 182, 255, 255))
                    with dpg.group(horizontal=True, horizontal_spacing=8):
                        dpg.add_text("●", tag="conn_indicator", color=(200, 90, 90, 255))
                        dpg.add_text("Disconnected", tag="conn_label")
                with dpg.group():
                    dpg.add_text("Session", color=(149, 182, 255, 255))
                    with dpg.group(horizontal=True, horizontal_spacing=10):
                        dpg.add_loading_indicator(
                            tag="record_spinner",
                            radius=5.5,
                            style=1,
                            color=(130, 190, 255, 220),
                            secondary_color=(80, 120, 200, 180),
                            show=False,
                        )
                        dpg.add_text("Idle", tag="record_label")
                        dpg.add_text("●", tag="record_indicator", color=(110, 150, 210, 255))
                    dpg.add_progress_bar(tag="record_progress", default_value=0.0, width=260, overlay="0%", show=False)
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True, horizontal_spacing=18):
                with dpg.child_window(width=340, autosize_y=True, border=False):
                    dpg.add_text("Connection", color=(149, 182, 255, 255))
                    dpg.add_separator()
                    dpg.add_combo(self.ports_labels, width=-1, tag="port_combo", label="Serial port")
                    with dpg.group(horizontal=True, horizontal_spacing=10):
                        dpg.add_button(label="Refresh", callback=self.on_refresh_ports, width=150)
                        dpg.add_button(label="Connect", callback=self.on_connect, width=150)
                    dpg.add_button(label="Disconnect", callback=self.on_disconnect, width=-1)

                    dpg.add_spacer(height=10)
                    dpg.add_text("Recording", color=(149, 182, 255, 255))
                    dpg.add_separator()
                    dpg.add_input_int(
                        tag="sr",
                        default_value=DEVICE_MAX_SR,
                        min_value=4000,
                        max_value=DEVICE_MAX_SR,
                        width=-1,
                        label="Sample rate (Hz)",
                    )
                    dpg.add_input_float(
                        tag="dur",
                        default_value=2.0,
                        min_value=0.1,
                        max_value=30.0,
                        width=-1,
                        format="%.2f",
                        label="Duration (s)",
                    )
                    dpg.add_button(label="Record", callback=self.on_record, tag="record_btn", width=-1)

                with dpg.child_window(autosize_x=True, autosize_y=True, border=False):
                    dpg.add_text("Waveform", color=(149, 182, 255, 255))
                    dpg.add_separator()
                    with dpg.plot(label="", height=-1, width=-1, anti_aliased=True):
                        dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="xaxis")
                        yaxis = dpg.add_plot_axis(dpg.mvYAxis, label="Amplitude", tag="yaxis")
                        dpg.add_line_series([], [], parent=yaxis, tag="series")

                    dpg.add_spacer(height=6)
                    with dpg.group(horizontal=True, horizontal_spacing=12):
                        dpg.add_button(label="Play", callback=self.on_play, width=100)
                        dpg.add_button(label="Save WAV", callback=self.on_save, width=120)
                        dpg.add_button(label="Clear", callback=self.on_clear, width=100)

            dpg.add_spacer(height=12)
            with dpg.child_window(height=90, autosize_x=True, border=False):
                dpg.add_text("Status", color=(149, 182, 255, 255))
                dpg.add_separator()
                dpg.add_text(tag="status", default_value="Idle", wrap=520)

        dpg.create_viewport(
            title="XIAO MG24 Sense — Audio GUI",
            width=1180,
            height=760,
            resizable=True,
            min_width=980,
            min_height=640,
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main", True)

        self._set_connection_state(False)
        self._set_record_visual_idle()

    # ---------- UI callbacks ----------

    def _apply_theme(self):
        with dpg.theme(tag="app_theme"):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_Text, (230, 235, 245, 255))
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (28, 32, 40, 255))
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (30, 34, 45, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (45, 55, 72, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (36, 40, 52, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (65, 90, 140, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (80, 110, 165, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (65, 90, 140, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (90, 120, 180, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (110, 150, 210, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, (110, 150, 210, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, (140, 180, 235, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 8)
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, 8)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 12, 8)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 6)

            with dpg.theme_component(dpg.mvPlot):
                dpg.add_theme_color(dpg.mvPlotCol_PlotBg, (22, 26, 34, 255))
                dpg.add_theme_color(dpg.mvPlotCol_FrameBg, (30, 34, 45, 255))
                dpg.add_theme_color(dpg.mvPlotCol_Line, (110, 190, 255, 255))

        dpg.bind_theme("app_theme")

    def set_status(self, txt: str):
        dpg.set_value("status", txt)

    def _set_connection_state(self, connected: bool, port_label: str = ""):
        self._connected = connected
        self._connected_port_label = port_label
        if connected:
            label = f"Connected ({port_label})" if port_label else "Connected"
            color = self._conn_color_connected
        else:
            label = "Disconnected"
            color = self._conn_color_disconnected
        dpg.set_value("conn_label", label)
        dpg.configure_item("conn_indicator", color=color)

    def _set_record_visual_idle(self):
        dpg.configure_item("record_spinner", show=False)
        dpg.configure_item("record_progress", show=False)
        dpg.set_value("record_label", "Idle")
        dpg.configure_item("record_indicator", color=self._record_color_idle)
        self._post_record_flash_color = self._record_color_idle

    def _set_record_visual_error(self):
        dpg.configure_item("record_spinner", show=False)
        dpg.configure_item("record_progress", show=False)
        dpg.set_value("record_label", "Error")
        dpg.configure_item("record_indicator", color=self._record_color_error)
        self._post_record_flash_color = self._record_color_error

    def _set_record_visual_success(self):
        dpg.configure_item("record_spinner", show=False)
        dpg.configure_item("record_progress", show=False)
        dpg.set_value("record_label", "Captured")
        dpg.configure_item("record_indicator", color=self._record_color_success)
        self._post_record_flash_color = self._record_color_success

    def _set_recording_enabled(self, enabled: bool):
        dpg.configure_item("record_btn", enabled=enabled)

    def _clamp_sample_rate(self, sr: int) -> int:
        if sr < 4000:
            sr = 4000
        if sr > DEVICE_MAX_SR:
            sr = DEVICE_MAX_SR
        return sr

    def on_refresh_ports(self):
        labels, devs = list_serial_ports()
        self.ports_labels, self.ports_devices = labels, devs
        dpg.configure_item("port_combo", items=labels)
        self.set_status("Ports refreshed.")

    def on_connect(self):
        if self.ser:
            self.set_status("Already connected.")
            return
        idx = dpg.get_value("port_combo")
        display_label = ""
        if isinstance(idx, int):
            # Dear PyGui returns index for combos
            if idx < 0 or idx >= len(self.ports_devices):
                self.set_status("Select a serial port first.")
                return
            port = self.ports_devices[idx]
            if 0 <= idx < len(self.ports_labels):
                display_label = self.ports_labels[idx]
        else:
            # Older DPG may return label; map it
            label = idx
            try:
                port = self.ports_devices[self.ports_labels.index(label)]
            except Exception:
                self.set_status("Select a serial port first.")
                return
            display_label = label

        try:
            self.ser = open_serial(port)
            if not display_label:
                display_label = port
            self._set_connection_state(True, display_label)
            self.set_status(f"Connected to {port}.")
        except Exception as e:
            self.set_status(f"Failed to open {port}: {e}")
            self.ser = None
            self._set_connection_state(False)

    def on_disconnect(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.set_status("Disconnected.")
        self._set_connection_state(False)

    def on_record(self):
        if self._recording:
            self.set_status("Recording already in progress.")
            return
        if not self.ser:
            self.set_status("Not connected.")
            return

        try:
            requested_sr = int(dpg.get_value("sr"))
        except (TypeError, ValueError):
            self.set_status("Sample rate must be a number.")
            return

        try:
            dur = float(dpg.get_value("dur"))
        except (TypeError, ValueError):
            self.set_status("Duration must be a number.")
            return

        sr = self._clamp_sample_rate(requested_sr)
        if requested_sr != sr:
            dpg.set_value("sr", sr)

        if not math.isfinite(dur) or dur <= 0:
            dur = max(dur if math.isfinite(dur) else 0.0, 0.1)
            dpg.set_value("dur", dur)
            self.set_status("Duration must be greater than zero.")
            return

        self.current_sr = sr
        self._recording = True
        self._set_recording_enabled(False)
        self._record_start_time = time.perf_counter()
        self._record_duration = max(dur, 0.0)
        self._record_pulse = 0.0
        self._post_record_flash_until = 0.0
        self._post_record_flash_duration = 0.0
        self._post_record_flash_color = self._record_color_recording
        dpg.configure_item("record_spinner", show=True)
        dpg.configure_item("record_progress", show=True)
        dpg.set_value("record_progress", 0.0)
        dpg.configure_item("record_progress", overlay="0%")
        dpg.set_value("record_label", "Recording…")
        dpg.configure_item("record_indicator", color=self._record_color_recording)
        if sr != requested_sr:
            dpg.set_value("sr", sr)
            status_msg = f"Recording {dur:.2f}s at {sr} Hz (device limit)."
        else:
            status_msg = f"Recording {dur:.2f}s at {sr} Hz…"
        self.set_status(status_msg)

        thread = threading.Thread(target=self._record_worker, args=(sr, dur), daemon=True)
        self._record_thread = thread
        thread.start()

    def on_play(self):
        if self.current_samples is None:
            self.set_status("Nothing to play. Record first.")
            return
        try:
            import sounddevice as sd
            sd.stop()
            sd.play(self.current_samples.astype(np.int16), self.current_sr)
            # Don't block UI; let it play in background
            self.set_status("Playing…")
        except Exception as e:
            self.set_status(f"Playback error: {e} (install 'sounddevice'?)")

    def on_save(self):
        if self.current_samples is None:
            self.set_status("Nothing to save. Record first.")
            return
        # Simple timestamped filename
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"xiao_mg24_audio_{ts}.wav"
        try:
            with wave.open(fname, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # int16
                wf.setframerate(self.current_sr)
                wf.writeframes(self.current_samples.astype(np.int16).tobytes())
            self.set_status(f"Saved {fname}")
        except Exception as e:
            self.set_status(f"Save error: {e}")

    def on_clear(self):
        self.current_samples = None
        dpg.set_value("series", [[], []])
        self.set_status("Cleared.")
        self._set_record_visual_idle()
        self._post_record_flash_until = 0.0
        self._post_record_flash_duration = 0.0

    def run(self):
        while dpg.is_dearpygui_running():
            self._update_animation()
            dpg.render_dearpygui_frame()
            self._drain_queue()
        dpg.destroy_context()

    # ---------- Background helpers ----------

    def _record_worker(self, sr: int, dur: float):
        ser = self.ser
        if ser is None:
            self._result_queue.put((None, sr, RuntimeError("Serial port disconnected.")))
            return
        try:
            data = rec_once(ser, sr, dur)
        except Exception as exc:
            self._result_queue.put((None, sr, exc))
            return
        self._result_queue.put((data, sr, None))

    def _drain_queue(self):
        while True:
            try:
                data, sr, exc = self._result_queue.get_nowait()
            except queue.Empty:
                break
            self._finish_recording(data, sr, exc)

    def _update_animation(self):
        now = time.perf_counter()
        dt = now - self._last_frame_time
        if dt < 0:
            dt = 0.0
        self._last_frame_time = now

        if self._recording:
            self._record_pulse = (self._record_pulse + dt * 3.2) % (2 * math.pi)
            intensity = 0.5 + 0.5 * math.sin(self._record_pulse)
            record_color = []
            for i in range(3):
                base = self._record_color_recording[i]
                record_color.append(int(base + (255 - base) * intensity * 0.6))
            dpg.configure_item("record_indicator", color=(*record_color, 255))

            if self._record_duration > 0.0:
                progress = (now - self._record_start_time) / self._record_duration
            else:
                progress = 0.0
            progress = max(0.0, min(progress, 0.99))
            dpg.set_value("record_progress", progress)
            dpg.configure_item("record_progress", overlay=f"{progress * 100:.0f}%")
        else:
            if self._post_record_flash_until > now and self._post_record_flash_duration > 0:
                ratio = (self._post_record_flash_until - now) / self._post_record_flash_duration
                ratio = max(0.0, min(1.0, ratio))
                faded_color = []
                for i in range(3):
                    idle = self._record_color_idle[i]
                    flash = self._post_record_flash_color[i]
                    faded_color.append(int(idle + (flash - idle) * ratio))
                dpg.configure_item("record_indicator", color=(*faded_color, 255))
            elif self._post_record_flash_until:
                self._post_record_flash_until = 0.0
                self._post_record_flash_duration = 0.0
                self._post_record_flash_color = self._record_color_idle
                dpg.configure_item("record_indicator", color=self._record_color_idle)

        base_conn = self._conn_color_connected if self._connected else self._conn_color_disconnected
        mix_conn = (180, 235, 200) if self._connected else (255, 160, 160)
        speed = 1.25 if self._connected else 0.8
        self._conn_pulse = (self._conn_pulse + dt * speed) % (2 * math.pi)
        conn_wave = 0.5 + 0.5 * math.sin(self._conn_pulse)
        conn_color = []
        for i in range(3):
            base = base_conn[i]
            mix = mix_conn[i]
            conn_color.append(int(base + (mix - base) * conn_wave * 0.6))
        dpg.configure_item("conn_indicator", color=(*conn_color, 255))

    def _finish_recording(self, data: Optional[np.ndarray], sr: int, error: Optional[Exception]):
        self._record_thread = None
        self._recording = False
        self._set_recording_enabled(True)
        self._record_duration = 0.0

        if error is not None:
            self._set_record_visual_error()
            self._post_record_flash_duration = 2.0
            self._post_record_flash_until = time.perf_counter() + 2.0
            self.set_status(f"Record error: {error}")
            return

        if data is None:
            self._set_record_visual_error()
            self._post_record_flash_duration = 2.0
            self._post_record_flash_until = time.perf_counter() + 2.0
            self.set_status("No data received.")
            return

        self.current_samples = data
        self.current_sr = sr
        t = np.arange(len(data)) / float(sr)
        dpg.set_value("series", [t.tolist(), data.astype(float).tolist()])
        dpg.fit_axis_data("xaxis")
        dpg.fit_axis_data("yaxis")
        dpg.set_value("record_progress", 1.0)
        dpg.configure_item("record_progress", overlay="100%")
        self._set_record_visual_success()
        self._post_record_flash_duration = 1.5
        self._post_record_flash_until = time.perf_counter() + 1.5
        self.set_status(f"Received {len(data)} samples.")


if __name__ == "__main__":
    App().run()
