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
import threading
import struct
import wave
from typing import Optional

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
    ser.write(cmd)
    ser.flush()

    # Expect: DATA,<n>\n
    header = ser.readline().decode(errors="ignore").strip()
    if not header.startswith("DATA,"):
        # Some cores echo ACK first
        if header == "ACK":
            header = ser.readline().decode(errors="ignore").strip()
        if not header.startswith("DATA,"):
            raise RuntimeError(f"Unexpected header from device: {header!r}")

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

        dpg.create_context()
        with dpg.font_registry():
            pass  # use default fonts

        with dpg.window(tag="main", label="XIAO MG24 Sense — USB CDC Audio", width=950, height=640):
            with dpg.group(horizontal=True):
                dpg.add_text("Port:")
                dpg.add_combo(self.ports_labels, width=320, tag="port_combo")
                dpg.add_button(label="Refresh", callback=self.on_refresh_ports)
                dpg.add_button(label="Connect", callback=self.on_connect)
                dpg.add_button(label="Disconnect", callback=self.on_disconnect)
                dpg.add_spacer(width=20)
                dpg.add_text("Sample rate:")
                dpg.add_input_int(tag="sr", default_value=DEVICE_MAX_SR, min_value=4000, max_value=DEVICE_MAX_SR, width=100)
                dpg.add_text("Duration (s):")
                dpg.add_input_float(tag="dur", default_value=2.0, min_value=0.1, max_value=30.0, width=100, format="%.2f")
                dpg.add_button(label="Record", callback=self.on_record, tag="record_btn")

            dpg.add_separator()
            dpg.add_text("Status:")
            dpg.add_input_text(tag="status", default_value="Idle", readonly=True, width=900)

            dpg.add_separator()
            with dpg.plot(label="Waveform", height=360, width=-1):
                dpg.add_plot_axis(dpg.mvXAxis, label="Time (s)", tag="xaxis")
                yaxis = dpg.add_plot_axis(dpg.mvYAxis, label="Amplitude", tag="yaxis")
                dpg.add_line_series([], [], parent=yaxis, tag="series")

            with dpg.group(horizontal=True):
                dpg.add_button(label="Play", callback=self.on_play)
                dpg.add_button(label="Save WAV", callback=self.on_save)
                dpg.add_button(label="Clear", callback=self.on_clear)

        dpg.create_viewport(title="XIAO MG24 Sense — Audio GUI", width=980, height=720)
        dpg.setup_dearpygui()
        dpg.show_viewport()

    # ---------- UI callbacks ----------

    def set_status(self, txt: str):
        dpg.configure_item("status", default_value=txt)

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
        if isinstance(idx, int):
            # Dear PyGui returns index for combos
            if idx < 0 or idx >= len(self.ports_devices):
                self.set_status("Select a serial port first.")
                return
            port = self.ports_devices[idx]
        else:
            # Older DPG may return label; map it
            label = idx
            try:
                port = self.ports_devices[self.ports_labels.index(label)]
            except Exception:
                self.set_status("Select a serial port first.")
                return

        try:
            self.ser = open_serial(port)
            self.set_status(f"Connected to {port}.")
        except Exception as e:
            self.set_status(f"Failed to open {port}: {e}")
            self.ser = None

    def on_disconnect(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
            self.set_status("Disconnected.")

    def on_record(self):
        if self._recording:
            self.set_status("Recording already in progress.")
            return
        if not self.ser:
            self.set_status("Not connected.")
            return

        requested_sr = int(dpg.get_value("sr"))
        sr = self._clamp_sample_rate(requested_sr)
        dur = float(dpg.get_value("dur"))
        self.current_sr = sr
        self._recording = True
        self._set_recording_enabled(False)
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

    def run(self):
        while dpg.is_dearpygui_running():
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

    # ---------- Background helpers ----------

    def _record_worker(self, sr: int, dur: float):
        ser = self.ser
        if ser is None:
            dpg.invoke(lambda: self._finish_recording(None, sr, RuntimeError("Serial port disconnected.")))
            return
        try:
            data = rec_once(ser, sr, dur)
        except Exception as exc:
            dpg.invoke(lambda: self._finish_recording(None, sr, exc))
            return
        dpg.invoke(lambda: self._finish_recording(data, sr, None))

    def _finish_recording(self, data: Optional[np.ndarray], sr: int, error: Optional[Exception]):
        self._record_thread = None
        self._recording = False
        self._set_recording_enabled(True)

        if error is not None:
            self.set_status(f"Record error: {error}")
            return

        if data is None:
            self.set_status("No data received.")
            return

        self.current_samples = data
        self.current_sr = sr
        t = np.arange(len(data)) / float(sr)
        dpg.set_value("series", [t.tolist(), data.astype(float).tolist()])
        dpg.fit_axis_data("xaxis")
        dpg.fit_axis_data("yaxis")
        self.set_status(f"Received {len(data)} samples.")


if __name__ == "__main__":
    App().run()
