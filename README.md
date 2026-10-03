# even-g2-protocol

A third-party Python library and protocol mapping for Even Realities G2 smart glasses. This package provides direct access to the dual-lens BLE characteristics, native dashboard layout controls, raw LC3 microphone streams, and IMU telemetry.

## Installation

Install the library directly from GitHub using `pip`:

```bash
# Install the latest version from the main branch
pip install git+https://github.com/ithinkthisiscool/even-g2-protocol

# Or clone and install in editable mode for local development
git clone https://github.com/ithinkthisiscool/even-g2-protocol
cd even-g2-protocol
pip install -e .
```

### System & Python Dependencies

The following packages are installed automatically alongside the library:

* **`bleak`**: Cross-platform Bluetooth Low Energy client used to manage links to both lenses.
* **`dbus-fast`**: Enables the high-performance BlueZ connection fast-path on Linux.
* **`aiohttp`**: Manages asynchronous server polling and external payload fetch routines.
* **`lc3py`**: Raw audio compression codec processing the 205-byte microphone data bursts.
* **`anthropic`**: Backend client bindings routing custom voice queries to Claude models.

## Features

* **Session Management**: Automated connection handling for both lenses via BlueZ mac address fast-path or background scanning, including the required 4-second `SID 0xE0` heartbeat loop.
* **Dashboard Modification**: Updates text fields and switches page configurations inside the native on-glasses UI.
* **Audio Capture**: Asynchronous queues for extracting raw 205-byte LC3 microphone frames from the left and right stems.
* **Subsystem Routing**: Built-in mapping that decodes and routes inbound data packets based on their one-byte Service ID (SID).

---

## Service ID (SID) Map

Every BLE frame to or from the glasses contains a one-byte Service ID (SID) at byte index 6 of the header. This byte routes the data packet to a specific subsystem on the glasses.

### Core Applications
* `0x01` (Dashboard): Direct modifications to the native HUD home dashboard.
* `0x03` (Menu): Interaction with the app selection carousel.
* `0x04` (Notification): Plaintext phone notifications and HUD popups.
* `0x05` / `0x06` (Translate / Teleprompter): Dedicated text-streaming modes.
* `0x07` (Even AI): Default assistant app connection routes.
* `0x08` (Navigation): Map turn indicators and location vectors.
* `0x0C` (Quicklist): Task checklists and tick tracking.

### System & Peripherals
* `0x0D` (App Sync): Reports which application is currently open on the glasses.
* `0x80` (Device Config): BLE authentication, initial bonding, and device setup.
* `0x90` / `0x91` (Ring / Ring 2): Captures inputs and gestures from the R1 Ring companion controller.
* `0xC4` / `0xC5` (File Command / Data): Low-level Embedded File System (EFS) read and write operations.
* `0xE0` (EvenHub UI): Session keep-alives and custom structural container canvas pages.

---

## Code Examples

### 1. Connection & HUD Text Injection
Connects to the hardware using `G2_RIGHT_MAC` and `G2_LEFT_MAC` environment variables (or auto-discovery), runs the authentication handshake, and updates a native dashboard text container.

```python
import asyncio
from g2_protocol import open_session

async def update_hud():
    # skip_page=True leaves the native dashboard visible without drawing an app over it
    async with open_session(launch_app=True, skip_page=True) as session:
        print(f"Connected: {session.is_connected}")
        
        # Query battery status and firmware metadata
        settings = await session.query_settings()
        print(f"Device Info: {settings}")

        # Push updated text to the main dashboard container
        await session.update_text("System Ready.")
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(update_hud())
```

### 2. Streaming Raw Microphone Data
Subscribes to notifications on characteristic `0x6402` across both lenses to capture streaming microphone data.

```python
import asyncio
from g2_protocol import open_session

async def run_mic_stream():
    async with open_session(launch_app=True, skip_page=True) as session:
        await session.start_audio_stream()
        print("Microphone stream active. Press Ctrl+C to stop.")
        
        try:
            while True:
                # Extracts (lens_side 'R'/'L', and the 205-byte frame buffer)
                side, packet = await session.audio_packets.get()
                
                # Packet contains 200 bytes LC3 audio data + 5 trailer status bytes
                print(f"[{side}] Received {len(packet)} byte audio chunk.")
        except asyncio.CancelledError:
            pass
        finally:
            await session.stop_audio_stream()

if __name__ == "__main__":
    asyncio.run(run_mic_stream())
```

### 3. Passive Frame Interception & Packet Sniffing
Uses the `on_raw_frame` callback hook to monitor incoming data streams in real time without blocking core message execution tasks.

```python
import asyncio
from g2_protocol import open_session, sids

def log_frame(svc_hi, svc_lo, payload):
    # Resolves the raw service byte to its name using the sids mapping module
    name = sids.get_name(svc_hi)
    print(f"[{name}] Flag: 0x{svc_lo:02x} | Payload: {len(payload)} bytes")

async def monitor_bus():
    async with open_session(launch_app=True, skip_page=True) as session:
        session.on_raw_frame = log_frame
        
        # Turn on the IMU stream at a 500ms interval
        await session.enable_imu(enable=True, report_freq=500)
        await asyncio.sleep(30.0)

if __name__ == "__main__":
    asyncio.run(monitor_bus())
```

---

## Technical Disclaimer

This is a community reverse-engineering project developed for interoperability testing and educational research. This software is completely independent and has no official affiliation with Even Realities. Distributed under the MIT License.
