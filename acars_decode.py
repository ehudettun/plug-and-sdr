#!/usr/bin/env python3
"""
Minimal ACARS decoder reading S16LE audio from stdin (piped from rtl_fm).
ACARS uses AM 131.x MHz, MSK-1200 baud.
"""
import sys, struct, time, math, collections

RATE = 22050
BAUD = 2400
SPB  = RATE / BAUD  # samples per bit ~9.2

# ACARS bit sync and framing constants
SYNC   = 0x2B2B2B2B  # preamble pattern
SOH    = 0x01
STX    = 0x02
ETX    = 0x03
DEL    = 0x7F
BLOCK  = 127  # max block size

def demod_am(samples):
    """Simple AM envelope detector."""
    out = []
    prev = 0
    for s in samples:
        mag = abs(s)
        # Low-pass filter
        prev = 0.05 * mag + 0.95 * prev
        out.append(prev)
    return out

def decode_bits(envelope, spb):
    """MSK clock recovery and bit extraction."""
    bits = []
    i = spb / 2
    while i < len(envelope) - 1:
        bit = 1 if envelope[int(i)] > 127 else 0
        bits.append(bit)
        i += spb
    return bits

class AcarsDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.last_msg = time.time()

    def feed(self, raw_bytes):
        samples = struct.unpack(f'<{len(raw_bytes)//2}h', raw_bytes)
        # Normalize to 0-255
        norm = [int((s + 32768) / 256) for s in samples]
        env = demod_am(norm)
        bits = decode_bits(env, SPB)

        # Look for ACARS preamble (alternating 1/0 bits + SOH)
        byte_stream = []
        for i in range(0, len(bits) - 8, 8):
            byte = 0
            for b in range(8):
                byte = (byte >> 1) | (bits[i + b] << 7)
            byte_stream.append(byte & 0x7F)

        # Scan for message
        for i in range(len(byte_stream) - 10):
            if byte_stream[i] == SOH:
                # Try to extract message
                msg = bytes(byte_stream[i:i+100])
                text = msg.decode('ascii', errors='replace').replace('\x00', '')
                if STX in byte_stream[i:i+20]:
                    print(f"ACARS|{time.strftime('%Y-%m-%d %H:%M:%S')}|{text[:80]}", flush=True)
                    return

if __name__ == '__main__':
    dec = AcarsDecoder()
    chunk = 22050 * 2  # 1 second of audio
    while True:
        data = sys.stdin.buffer.read(chunk)
        if not data:
            break
        dec.feed(data)
