# g2 — Even Realities G2 Python Library

An open-source Python library for **Even Realities G2 smart glasses** over BLE. Reverse-engineered from protocol traces for custom dashboard execution, raw LC3 audio streaming, and IMU tracking.

---

## Installation & Dependencies

```bash
pip install git+https://github.com/ithinkthisiscool/even-g2-protocol
```

The following packages are installed automatically alongside the library:

* **`bleak`**: Cross-platform Bluetooth Low Energy client used to manage links to both lenses.
* **`dbus-fast`**: Enables the high-performance BlueZ connection fast-path on Linux.
* **`aiohttp`**: Manages asynchronous server polling and external payload fetch routines.
* **`lc3py`**: Raw audio compression codec processing the 205-byte microphone data bursts.
* **`anthropic`**: Backend client bindings routing custom voice queries to Claude models.

---

## Code Architecture

The package exposes clean, top-level unified modules out of `/g2/` while keeping individual layers separated.

```
.
─────── g2/
      ├── __init__.py   # Global namespace exports
      ├── sids.py       # Service ID registries
      ├── transport/    # Connection loops, auth, and BLE hooks
      ├── apps/         # App modules (translation, teleprompter, assistant)
      ├── widgets/      # Framing components and layout canvas engines
      ├── device/       # System telemetry and configurations
      ├── tests/        # Offline golden-byte validation suites
```

To execute the offline test suite without connecting physical hardware:
```bash
python -m pytest tests
```

---

## Service ID (SID) Map

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

## Usage Examples

### 1. Update HUD Dashboard Text
```python
import asyncio
from g2 import GlassesSession

async def main():
    async with GlassesSession(launch_app=True, skip_page=True) as session:
        await session.update_text("System Ready.")
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
```

### 2. Stream Raw Audio & Packet Sniffing
For audio streaming and IMU packet monitoring via callbacks, initialize `GlassesSession` and call `start_audio_stream()` or configure `on_raw_frame` handlers as documented in the source repository.

---

## Disclaimer
Independent reverse-engineered research toolkit. Not affiliated with Even Realities. MIT License.