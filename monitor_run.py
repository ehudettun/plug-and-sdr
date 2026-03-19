#!/usr/bin/env python3
"""
SDR 10-minute-per-mode monitoring run.
Cycles through: pagers → sensors → acars → aircraft → scanner(NOAA) → scanner(ATC)
Logs findings to /tmp/sdr_monitor_YYYYMMDD_HHMM.log
"""
import requests, time, json, os, re, subprocess
from datetime import datetime

BASE = 'http://localhost:8888'
LOG  = f'/tmp/sdr_monitor_{datetime.now().strftime("%Y%m%d_%H%M")}.log'
DURATION = 600   # 10 minutes each

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line)
    with open(LOG, 'a') as f:
        f.write(line + '\n')

def switch(mode, freq=None):
    try:
        requests.post(f'{BASE}/api/mode/{mode}', timeout=5)
        if freq:
            requests.post(f'{BASE}/api/pagers/freq',
                          json={'freq': freq}, timeout=5)
    except Exception as e:
        log(f'  ⚠ mode switch error: {e}')
    time.sleep(4)

def tail_log(path, since_pos):
    """Read new lines from a log file since a given position."""
    try:
        with open(path, 'r', errors='replace') as f:
            f.seek(since_pos)
            return f.read(), f.tell()
    except:
        return '', since_pos

def aircraft_snapshot():
    try:
        r = requests.get(f'{BASE}/api/planes', timeout=5)
        return r.json() if r.ok else []
    except:
        return []

# ─────────────────────────────────────────────────────────────────────
log('=' * 60)
log(f'SDR MONITOR RUN — {datetime.now().strftime("%Y-%m-%d %H:%M")}')
log('=' * 60)

# ── 1. PAGERS ────────────────────────────────────────────────────────
log('\n📟 MODE: PAGERS (152.5984 MHz hospital FLEX) — 10 min')
switch('pagers')
with open('/tmp/pagers.log', 'r', errors='replace') as f:
    f.seek(0, 2)
    start_pos = f.tell()

pager_msgs = []
t0 = time.time()
while time.time() - t0 < DURATION:
    time.sleep(15)
    new, start_pos = tail_log('/tmp/pagers.log', start_pos)
    for line in new.splitlines():
        if line.startswith('FLEX') or line.startswith('POCSAG'):
            pager_msgs.append(line)
    elapsed = int(time.time() - t0)
    log(f'  [{elapsed:3d}s] pager lines so far: {len(pager_msgs)}')

log(f'\n  PAGER SUMMARY — {len(pager_msgs)} messages in 10 min')
readable = [l for l in pager_msgs if re.search(r'[A-Za-z ]{6,}', l.split('|')[-1] if '|' in l else l)]
caps = {}
for l in pager_msgs:
    parts = l.split('|')
    if len(parts) >= 5:
        cap = parts[4].lstrip('0') or '0'
        caps[cap] = caps.get(cap, 0) + 1
top_caps = sorted(caps.items(), key=lambda x: -x[1])[:5]
log(f'  Readable messages: {len(readable)}')
log(f'  Unique CAP codes: {len(caps)}')
log(f'  Top CAP codes: {top_caps}')
for l in readable[:5]:
    parts = l.split('|')
    text = parts[-1][:120] if '|' in l else l[:120]
    log(f'  → {text}')

# ── 2. SENSORS (rtl_433) ─────────────────────────────────────────────
log('\n🌡️  MODE: SENSORS (rtl_433 — 315/345/433 MHz) — 10 min')
switch('sensors')
open('/tmp/sensors.log', 'a').close()
with open('/tmp/sensors.log', 'r', errors='replace') as f:
    f.seek(0, 2)
    start_pos = f.tell()

sensor_events = []
t0 = time.time()
while time.time() - t0 < DURATION:
    time.sleep(15)
    new, start_pos = tail_log('/tmp/sensors.log', start_pos)
    for line in new.splitlines():
        if line.strip().startswith('{'):
            try:
                obj = json.loads(line)
                sensor_events.append(obj)
            except:
                pass
    log(f'  [{int(time.time()-t0):3d}s] sensor events so far: {len(sensor_events)}')

log(f'\n  SENSOR SUMMARY — {len(sensor_events)} events in 10 min')
by_model = {}
for e in sensor_events:
    m = e.get('model', 'unknown')
    by_model[m] = by_model.get(m, 0) + 1
for model, count in sorted(by_model.items(), key=lambda x: -x[1]):
    sample = next((e for e in sensor_events if e.get('model') == model), {})
    detail = []
    for k in ['id','channel','temperature_C','humidity','pressure_hPa','battery_ok']:
        if k in sample:
            detail.append(f'{k}={sample[k]}')
    log(f'  {count:3d}x {model}  ({", ".join(detail[:4])})')

# ── 3. ACARS ─────────────────────────────────────────────────────────
log('\n✈️  MODE: ACARS (acarsdec — 129.125/130.025/130.450 MHz) — 10 min')
switch('acars')
open('/tmp/acars.log', 'a').close()
with open('/tmp/acars.log', 'r', errors='replace') as f:
    f.seek(0, 2)
    start_pos = f.tell()

acars_msgs = []
t0 = time.time()
while time.time() - t0 < DURATION:
    time.sleep(15)
    new, start_pos = tail_log('/tmp/acars.log', start_pos)
    for line in new.splitlines():
        if re.search(r'\[#\d+', line):
            acars_msgs.append(line)
    log(f'  [{int(time.time()-t0):3d}s] ACARS blocks so far: {len(acars_msgs)}')

log(f'\n  ACARS SUMMARY — {len(acars_msgs)} message headers in 10 min')
flights = set()
tails   = set()
for l in acars_msgs:
    m = re.search(r'Flight:\s*(\S+)', l)
    if m: flights.add(m.group(1))
    m = re.search(r'Reg:\s*(\S+)', l)
    if m: tails.add(m.group(1))
log(f'  Unique flights: {sorted(flights)}')
log(f'  Tail numbers:   {sorted(tails)}')

# ── 4. AIRCRAFT (ADS-B) ──────────────────────────────────────────────
log('\n🗺️  MODE: PLANES (dump1090 ADS-B — 1090 MHz) — 10 min')
switch('planes')
seen_icao = {}
t0 = time.time()
while time.time() - t0 < DURATION:
    time.sleep(30)
    planes = aircraft_snapshot()
    for p in planes:
        icao = p.get('hex','?')
        if icao not in seen_icao:
            seen_icao[icao] = p
    log(f'  [{int(time.time()-t0):3d}s] unique aircraft seen: {len(seen_icao)}')

log(f'\n  AIRCRAFT SUMMARY — {len(seen_icao)} unique aircraft in 10 min')
with_flight = [(icao, p) for icao, p in seen_icao.items() if p.get('flight','').strip()]
log(f'  With callsign: {len(with_flight)}')
highest = sorted(seen_icao.values(), key=lambda p: p.get('altitude', 0) or 0, reverse=True)[:5]
for p in highest:
    fl = p.get('flight','?').strip()
    alt = p.get('altitude','?')
    spd = p.get('speed','?')
    lat = p.get('lat','')
    lon = p.get('lon','')
    log(f'  {fl or p.get("hex","?")} — {alt}ft  {spd}kts  ({lat},{lon})')

# ── 5. SCANNER: NOAA ─────────────────────────────────────────────────
log('\n📡 MODE: SCANNER — NOAA Weather 162.400 MHz — 2 min audio check')
switch('scanner')
time.sleep(5)
log('  Checking audio stream for 30 seconds...')
try:
    result = subprocess.run(
        ['curl', '-s', '--max-time', '30',
         'http://localhost:8888/api/audio/stream?freq=162.400&mode=fm'],
        capture_output=True, timeout=35
    )
    size = len(result.stdout)
    log(f'  NOAA audio stream: {size} bytes received in 30s ({size//1000} KB/s approx)')
    log(f'  Status: {"✅ WORKING" if size > 50000 else "⚠ LOW — possible reception issue"}')
except Exception as e:
    log(f'  ⚠ audio stream error: {e}')

# ── DONE ─────────────────────────────────────────────────────────────
log('\n' + '=' * 60)
log('RUN COMPLETE')
log(f'Full log saved to: {LOG}')
log('=' * 60)

# Switch back to planes
switch('planes')
print(f'\nLog file: {LOG}')
