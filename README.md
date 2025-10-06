# XIAO MG24 Sense USB Audio Recorder

This repository contains the firmware and a Python-based GUI for recording audio from a XIAO MG24 Sense board over a USB serial connection.

## Project Structure

-   `firmware/`: Contains the Arduino source code (`.ino`) for the XIAO MG24 Sense device.
-   `gui/`: Contains the Python application for recording, plotting, playing, and saving the audio data from the device.

## Firmware Setup

1.  Open the `firmware/firmware.ino` file in the Arduino IDE.
2.  Install the Seeed Studio XIAO MG24 board support package via the Arduino Board Manager (Seeed provides [step-by-step instructions here](https://wiki.seeedstudio.com/xiao_mg24_getting_started/)).
3.  Select the **Seeed Studio XIAO MG24 Sense** board and the correct port.
4.  Upload the sketch to your XIAO MG24 Sense.

## GUI Setup and Usage

1.  Navigate to the `gui` directory:
    ```bash
    cd gui
    ```
2.  Install the required Python dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Run the application:
    ```bash
    python main.py
    ```
4.  In the GUI:
    -   Select the serial port corresponding to your XIAO board and click "Connect".
    -   Set your desired sample rate and recording duration.
    -   Click "Record" to capture audio.
    -   The waveform will be displayed. You can then play the audio or save it as a `.wav` file.