# g2-glasses-sdk

An advanced, community-built Python SDK that fully reverse-engineers the dual-lens communication protocols, peripheral pipelines, and native dashboard interface of the **Even Realities G2 Smart Glasses**.

This library abstracts the underlying BLE transport layers into a clean, event-driven `GlassesSession` architecture, giving you complete control over the heads-up display (HUD), audio recording streams, IMU motion telemetry, and peripheral device pairing routines.

---

## ✨ Key Capabilities

* **Dual-Lens Session Management**: Synchronizes connection orchestration (fast-path address via BlueZ or callback scanning) and automated background heartbeat (`SID 0xE0`) loops.
* **Native Dashboard Customization**: Real-time canvas text adjustments, card layout upgrades, and multi-container viewport mapping through protobuf abstractions.
* **High-Fidelity Audio Capture**: Asynchronous streaming queues extracting raw 205-byte LC3 microphone notifications (`0x6402`) direct from the right/left stems.
* **Service Architecture Routing**: Built-in protocol router mapping incoming and outgoing data frames directly to their respective on-glasses subsystems.

---

## 🛠️ Protocol Architecture (Service IDs)

Every BLE frame sent to or from the glasses carries a one-byte **Service ID (SID)** in its header (byte 6). This acts exactly like a network port number, routing your payloads to specific systems or apps running on the lenses. 

This SDK natively maps and handles these core subsystems:

### Core Applications & HUD Displays
* **`0x01` (Dashboard)**: Interacts with the native HUD home dashboard display.
* **`0x03` (Menu)**: Interacts with the main application item carousel.
* **`0x04` (Notification)**: System notification forwarding and HUD popups.
* **`0x05` / `0x06` (Translate / Teleprompter)**: Specialized real-time text streaming applications.
* **`0x07` (Even AI)**: Voice assistant pipelines and active processing routes.
* **`0x08` (Navigation)**: Turn-by-turn map displays and routing telemetry vectors.
* **`0x0C` (Quicklist)**: Task checklists and item tick tracking.

### System & Peripheral Controls
* **`0x0D` (App Sync)**: Tracks and reports which app is currently open in the foreground.
* **`0x80` (Device Config)**: Low-level BLE authentication, bonding, and initialization.
* **`0x90` / `0x91` (Ring / Ring 2)**: Captured inputs from the R1 Ring smart controller channels.
* **`0xC4` / `0xC5` (File Command / Data)**: Embedded File System (EFS) transfer pipelines.
* **`0xE0` (EvenHub UI)**: Custom programmatic text/list UI layers and connection heartbeats.

---

## 🚀 Quick Start & Code Examples

### 1. Connection Lifecycle & HUD Text Pushes
This snippet demonstrates establishing a session using environment variables (`G2_RIGHT_MAC` / `G2_LEFT_MAC`) or automatic hardware pair discovery, querying hardware details, and pushing text elements into the native HUD.

```python
import asyncio
from g2_sdk import open_session

async def update_hud_view():
    # Launches connection sequence (auth, protocol prelude, and 4s background heartbeats)
    # Keeping skip_page=True ensures the native operating dashboard remains active
    async with open_session(launch_app=True, skip_page=True) as session:
        print(f"Verifying connections... Status: {session.is_connected}")
        
        # Pull system status queries (battery levels, charging flags, firmware variants)
        device_telemetry = await session.query_settings()
        print(f"Hardware Status: {device_telemetry}")

        # Update text configurations in the viewport's primary dashboard module
        print("Pushing updated context packet onto the heads-up display stack...")
        await session.update_text("System Active: Jarvis Interface Engaged.")
        
        # Keep connection open briefly to verify receipt
        await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(update_hud_view())
```

### 2. Live Audio Streaming (LC3 Mic Packets)
Leverage the asynchronous queues to pull raw microphone packets directly out of the dual-microphone architecture.

```python
import asyncio
from g2_sdk import open_session

async def stream_mic_feeds():
    async with open_session(launch_app=True, skip_page=True) as session:
        # Spin up micro-characteristic observers across both glasses stems
        print("Initializing microphone stream listener hooks...")
        await session.start_audio_stream()
        
        try:
            print("Listening for inbound voice payloads. Press Ctrl+C to terminate...")
            while True:
                # Extracts (arm identifier 'R'/'L', and 205-byte payload data buffer)
                arm_side, raw_packet = await session.audio_packets.get()
                
                # Payload format: 200 bytes LC3 audio data + signal markers
                print(f"[{arm_side} Lens] Extracted {len(raw_packet)} byte LC3 buffer chunk")
                
        except asyncio.CancelledError:
            print("Stopping audio pipeline capture...")
        finally:
            await session.stop_audio_stream()

if __name__ == "__main__":
    asyncio.run(stream_mic_feeds())
```

### 3. Passive Frame Sniffing & Logging
Register a custom callback onto the low-level Bluetooth notification worker to inspect incoming traffic across any service channel in real time without stealing frame execution tokens from your primary commands.

```python
import asyncio
from g2_sdk import open_session, sids

def handle_incoming_frame(svc_hi, svc_lo, protobuf_bytes):
    # Resolve the raw byte into a human-readable service name using our sids registry
    service_name = sids.get_name(svc_hi)
    print(f"[{service_name}] Flag: 0x{svc_lo:02x} | Payload Size: {len(protobuf_bytes)} bytes")

async def capture_telemetry_events():
    async with open_session(launch_app=True, skip_page=True) as session:
        # Bind custom handler directly onto the Bluetooth notification thread
        session.on_raw_frame = handle_incoming_frame
        
        print("Activating Inertial Measurement Unit (IMU) telemetry matrix...")
        await session.enable_imu(enable=True, report_freq=500)
        
        # Maintain live session to process real-time events
        await asyncio.sleep(30.0)

if __name__ == "__main__":
    asyncio.run(capture_telemetry_events())
```

---

## 🤝 Contributing

Found undocumented `SID` codes, alternative frame flags, or new protobuf payload structures? Open an issue or drop a Pull Request detailing your hardware sniffing outputs! 

## 📜 License & Disclaimer

This project is licensed under the MIT License. It is a completely independent, community-driven reverse engineering project meant for educational and prototyping purposes. It is entirely unaffiliated with Even Realities.
