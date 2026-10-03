"""Answer "Hey Even" with your own text (Even AI, SID 0x07), optionally recording the question.

Say "Hey Even" (or open Even AI from the glasses menu). The flow:
    glasses → CTRL WAKE_UP
    us      → CTRL ENTER          within a few seconds, or the glasses give up
    us      → HEARTBEAT every 2 s keeps the Even AI screen open (10 s timeout in the firmware)
    (mic)     LC3 audio on 6402 while the user talks; --wav records --listen seconds of it
    us      → ASK "question"      shows the question
    us      → ANALYSE             "thinking" screen
    us      → REPLY chunks        ≤512 UTF-8 bytes each, ~150 ms apart, last one final
    glasses → EVENT STREAM_COMPLETE
    us      → CTRL EXIT           after --show seconds (or the user closes it first)

Recording needs the lc3 module (pip package lc3py; it is in the project venv). The glasses never
send a voice-activity END, so this script simply records a fixed --listen time. Speech-to-text
and an LLM are out of scope here; see hub/even_ai_assistant.py for the full voice loop.
"""
import asyncio
import sys
import wave

import _common as c
from g2 import even_ai as ai

STATUS = c.CONFIRMED_LIVE + ' (wake, ENTER, heartbeat, ASK/ANALYSE/REPLY, EXIT and mic audio)'
SAMPLE_RATE, FRAME_BYTES, FRAMES_PER_PACKET = 16000, 40, 5


def build_parser():
    p = c.parser(__doc__, STATUS)
    p.add_argument('--question', default='What can you do?', help='text shown as the question (ASK)')
    p.add_argument('--reply', default='I am an answer sent from Python with the g2 library.',
                   help='answer text (split into 512-byte chunks)')
    p.add_argument('--turns', type=int, default=1, help='wake-ups to answer before exiting (default 1)')
    p.add_argument('--wait', type=float, default=120.0, help='seconds to wait for "Hey Even" (default 120)')
    p.add_argument('--show', type=float, default=10.0, help='seconds the answer stays before EXIT')
    p.add_argument('--wav', metavar='FILE', help='record the mic after ENTER to this WAV file (needs lc3)')
    p.add_argument('--listen', type=float, default=5.0, help='seconds to record with --wav (default 5)')
    return p


def decode_lc3(packets):
    """[(arm, packet)] → 16-bit mono PCM at 16 kHz. Uses the left lens if it streamed (its mic is
    cleaner), else the right. Each packet holds 5 LC3 frames of 40 bytes (+ 5 trailer bytes)."""
    import lc3
    arm = 'L' if any(a == 'L' for a, _ in packets) else 'R'
    dec = lc3.Decoder(frame_duration_us=10000, sample_rate_hz=SAMPLE_RATE, num_channels=1)
    pcm = bytearray()
    for a, pkt in packets:
        if a != arm or len(pkt) < FRAME_BYTES * FRAMES_PER_PACKET:
            continue
        for i in range(FRAMES_PER_PACKET):
            try:
                pcm += dec.decode(pkt[i * FRAME_BYTES:(i + 1) * FRAME_BYTES], bit_depth=16)
            except Exception:           # a damaged frame: skip it, keep the rest
                pass
    return bytes(pcm), arm


def write_wav(path, pcm):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


class EvenAI:
    def __init__(self, session):
        self.s = session
        self.wakes = asyncio.Queue()
        self.exited = asyncio.Event()
        self.complete = asyncio.Event()

    def on_frame(self, sid, flag, pb):
        """Listener: runs in the Bluetooth callback, so it only records what arrived."""
        if sid != ai.SID:
            return
        msg = ai.decode(pb)
        if msg['cmd'] != ai.CMD_HEARTBEAT:
            print(f'  glasses: {msg["label"]}', flush=True)
        if msg['cmd'] == ai.CMD_CTRL and msg['value'] == ai.CTRL_WAKE_UP:
            self.wakes.put_nowait(msg)
        elif msg['cmd'] == ai.CMD_CTRL and msg['value'] == ai.CTRL_EXIT:
            self.exited.set()
        elif msg['cmd'] == ai.CMD_EVENT and msg['value'] == ai.EVENT_STREAM_COMPLETE:
            self.complete.set()

    async def send(self, pb):
        await self.s.send(ai.SID, pb)

    async def heartbeat(self):
        n = 0
        while True:
            await asyncio.sleep(ai.HEARTBEAT_INTERVAL_S)
            n += 1
            await self.send(ai.build_heartbeat(self.s.next_magic(), n))

    async def record(self, seconds):
        await self.s.start_audio_stream()
        packets = []
        try:
            loop = asyncio.get_running_loop()
            end = loop.time() + seconds
            while loop.time() < end and not self.exited.is_set():
                try:
                    packets.append(await asyncio.wait_for(self.s.audio_packets.get(), 0.2))
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.s.stop_audio_stream()
        return packets

    async def answer(self, question, reply, show_s, wav=None, listen_s=0.0):
        self.exited.clear()
        self.complete.clear()
        await self.send(ai.build_ctrl(self.s.next_magic(), ai.CTRL_ENTER))
        hb = asyncio.ensure_future(self.heartbeat())
        try:
            if wav:
                packets = await self.record(listen_s)
                pcm, arm = await asyncio.to_thread(decode_lc3, packets)
                write_wav(wav, pcm)
                print(f'Recorded {len(packets)} packets ({len(pcm) / 2 / SAMPLE_RATE:.1f} s, lens {arm}) → {wav}')
            if self.exited.is_set():
                return False
            await self.send(ai.build_ask(self.s.next_magic(), question))
            await asyncio.sleep(0.3)
            await self.send(ai.build_analyse(self.s.next_magic()))
            chunks = ai.reply_chunks(reply)
            for i, chunk in enumerate(chunks):
                await self.send(ai.build_reply(self.s.next_magic(), chunk, final=i == len(chunks) - 1))
                if i < len(chunks) - 1:
                    await asyncio.sleep(ai.REPLY_PACE_S)
            if show_s > 0:
                with_exit = asyncio.ensure_future(self.exited.wait())
                await asyncio.wait({with_exit}, timeout=show_s)
                with_exit.cancel()
            return True
        finally:
            hb.cancel()
            if not self.exited.is_set():
                await self.send(ai.build_ctrl(self.s.next_magic(), ai.CTRL_EXIT))


async def main(argv=None):
    args = build_parser().parse_args(argv)
    c.setup_logging(args.verbose)
    if args.wav:
        try:
            import lc3  # noqa: F401
        except ImportError:
            print('Error: --wav needs the lc3 module (run with the project venv: .venv/bin/python)', file=sys.stderr)
            return 2
    c.banner('Even AI', STATUS)
    answered = 0
    async with c.glasses(args) as s:
        bot = EvenAI(s)
        c.listen(s, bot.on_frame)
        while answered < args.turns:
            print(f'Say "Hey Even" (or open Even AI on the glasses)... waiting up to {args.wait:.0f} s', flush=True)
            try:
                await asyncio.wait_for(bot.wakes.get(), args.wait)
            except asyncio.TimeoutError:
                print('No wake-up received.')
                break
            if await bot.answer(args.question, args.reply, args.show, args.wav, args.listen):
                answered += 1
                print(f'Answered ({answered}/{args.turns}).', flush=True)
            else:
                print('The user closed Even AI before the answer.')
    return 0 if answered else 1


if __name__ == '__main__':
    sys.exit(c.run(main))
