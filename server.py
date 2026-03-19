#!/usr/bin/env python3
import subprocess, os, signal, time, json, threading, socket, re, types
import yaml
from flask import Flask, Response, jsonify, send_from_directory, request
from flask_cors import CORS

app = Flask(__name__, static_folder='static')
CORS(app)


def _load_config():
    defaults = {
        'site_lat': 0.0, 'site_lon': 0.0, 'site_name': 'My SDR Station',
        'aircraft_json': '/usr/share/dump1090-mutability/html/data/aircraft.json',
        'pager_freq': '152.5984', 'rtl_gain': 49, 'dashboard_port': 8888,
        'pager_orgs': [],
    }
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
    try:
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        defaults.update(data)
    except FileNotFoundError:
        pass
    return types.SimpleNamespace(**defaults)

cfg = _load_config()

PAGER_LOG   = '/tmp/pagers.log'
ACARS_LOG   = '/tmp/acars.log'
SENSORS_LOG = '/tmp/sensors.log'
APRS_LOG    = '/tmp/aprs.log'
AIRCRAFT_JSON = cfg.aircraft_json
HTML_DIR      = '/usr/share/dump1090-mutability/html'

current_mode = None
sdr_procs    = []
mode_lock    = threading.Lock()

pager_running   = False
ais_running     = False
acars_running   = False
sensors_running = False
aprs_running    = False

ais_ships      = {}
ais_ships_lock = threading.Lock()

aprs_stations      = {}   # callsign -> station dict
aprs_stations_lock = threading.Lock()
ais_fragments  = {}   # key: (count, seq, channel) -> {num: payload}


# ──────────────────────────────────────────────────────────────────────
# SDR process management
# ──────────────────────────────────────────────────────────────────────

def kill_sdr():
    global sdr_procs
    for p in sdr_procs:
        try: p.kill()
        except: pass
        try: p.wait(timeout=2)
        except: pass
    sdr_procs = []
    os.system('sudo pkill -9 -f dump1090-mutability 2>/dev/null')
    os.system('sudo pkill -9 rtl_fm       2>/dev/null')
    os.system('sudo pkill -9 multimon-ng  2>/dev/null')
    os.system('sudo pkill -9 rtl_ais      2>/dev/null')
    os.system('sudo pkill -9 acarsdec     2>/dev/null')
    os.system('sudo pkill -9 rtl_433      2>/dev/null')
    os.system('sudo pkill -9 direwolf     2>/dev/null')
    for _ in range(10):
        result = os.popen('sudo lsof /dev/bus/usb/001/006 2>/dev/null').read()
        if not result.strip():
            break
        time.sleep(0.5)
    time.sleep(0.5)


# ──────────────────────────────────────────────────────────────────────
# Mode starters
# ──────────────────────────────────────────────────────────────────────

def start_planes():
    global pager_running, ais_running, acars_running, sensors_running
    pager_running = ais_running = acars_running = sensors_running = aprs_running = vlf_running = False
    kill_sdr()
    p = subprocess.Popen(
        ['sudo', 'dump1090-mutability', '--net',
         '--write-json', '/usr/share/dump1090-mutability/html/data', '--quiet'],
        preexec_fn=os.setsid
    )
    sdr_procs.append(p)


def start_scanner():
    global pager_running, ais_running, acars_running, sensors_running
    pager_running = ais_running = acars_running = sensors_running = aprs_running = vlf_running = False
    kill_sdr()


PAGER_FREQS = {
    '152.5984': {'label': '152.5984 MHz — FLEX Paging (VHF)',   'mode': 'fm', 'gain': '40'},
    '929.5875': {'label': '929.5875 MHz — Spok Paging (UHF)',   'mode': 'fm', 'gain': '48'},
    '931.8625': {'label': '931.8625 MHz — Metrocall/Spok (UHF)','mode': 'fm', 'gain': '48'},
}
pager_freq = cfg.pager_freq   # default from config

def start_pagers(freq=None):
    global pager_running, ais_running, acars_running, sensors_running, pager_freq
    if freq and freq in PAGER_FREQS:
        pager_freq = freq
    ais_running = acars_running = sensors_running = aprs_running = vlf_running = False
    kill_sdr()
    open(PAGER_LOG, 'w').close()
    pager_running = True

    def run_loop():
        global pager_running
        while pager_running:
            f    = pager_freq
            info = PAGER_FREQS.get(f, PAGER_FREQS['152.5984'])
            cmd = (f'sudo rtl_fm -f {f}M -M {info["mode"]} -s 22050 -g {info["gain"]} 2>/dev/null | '
                   f'multimon-ng -t raw -a POCSAG512 -a POCSAG1200 -a POCSAG2400 -a FLEX -f alpha - '
                   f'>> {PAGER_LOG} 2>&1')
            p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
            sdr_procs.append(p)
            p.wait()
            if p in sdr_procs:
                sdr_procs.remove(p)
            if pager_running:
                time.sleep(2)

    threading.Thread(target=run_loop, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────
# AIS helpers
# ──────────────────────────────────────────────────────────────────────

def _ais_bits_list(payload):
    bits = []
    for c in payload:
        v = ord(c) - 48
        if v > 40:
            v -= 8
        for i in range(5, -1, -1):
            bits.append((v >> i) & 1)
    return bits


def _get_bits(bits, start, length, signed=False):
    if start + length > len(bits):
        return 0
    val = 0
    for i in range(length):
        val = (val << 1) | bits[start + i]
    if signed and length > 0 and bits[start]:
        val -= (1 << length)
    return val


_AIS_CHARS = '@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !"#$%&\'()*+,-./0123456789:;<=>?'


def _ais_text(bits, start, char_count):
    result = []
    for i in range(char_count):
        v = _get_bits(bits, start + i * 6, 6)
        if v < len(_AIS_CHARS):
            result.append(_AIS_CHARS[v])
    return ''.join(result).strip().rstrip('@').strip()


def parse_ais_payload(payload):
    try:
        bits = _ais_bits_list(payload)
        if len(bits) < 38:
            return None
        mtype = _get_bits(bits, 0, 6)
        mmsi  = _get_bits(bits, 8, 30)
        if mmsi == 0:
            return None
        if mtype in (1, 2, 3):
            if len(bits) < 128:
                return None
            speed  = _get_bits(bits, 50, 10) / 10.0
            lon    = _get_bits(bits, 61, 28, signed=True) / 600000.0
            lat    = _get_bits(bits, 89, 27, signed=True) / 600000.0
            course = _get_bits(bits, 116, 12) / 10.0
            return {'mmsi': mmsi, 'type': mtype,
                    'speed': speed, 'lon': lon, 'lat': lat, 'course': course}
        elif mtype == 5:
            if len(bits) < 426:
                return None
            name = _ais_text(bits, 112, 20)
            return {'mmsi': mmsi, 'type': 5, 'name': name}
        elif mtype == 18:
            if len(bits) < 124:
                return None
            speed  = _get_bits(bits, 46, 10) / 10.0
            lon    = _get_bits(bits, 57, 28, signed=True) / 600000.0
            lat    = _get_bits(bits, 85, 27, signed=True) / 600000.0
            course = _get_bits(bits, 112, 12) / 10.0
            return {'mmsi': mmsi, 'type': 18,
                    'speed': speed, 'lon': lon, 'lat': lat, 'course': course}
    except Exception:
        pass
    return None


def _update_ship(parsed):
    mmsi = str(parsed['mmsi'])
    with ais_ships_lock:
        if mmsi not in ais_ships:
            ais_ships[mmsi] = {'mmsi': mmsi, 'name': '', 'lat': 0.0, 'lon': 0.0,
                               'speed': 0.0, 'course': 0.0, 'last_seen': time.time()}
        ship = ais_ships[mmsi]
        if parsed['type'] == 5:
            ship['name'] = parsed.get('name', '')
        else:
            ship['lat']    = parsed.get('lat', ship['lat'])
            ship['lon']    = parsed.get('lon', ship['lon'])
            ship['speed']  = parsed.get('speed', ship['speed'])
            ship['course'] = parsed.get('course', ship['course'])
        ship['last_seen'] = time.time()


def _process_nmea(sentence):
    global ais_fragments
    if not sentence.startswith('!AIVDM'):
        return
    if '*' in sentence:
        sentence = sentence[:sentence.rindex('*')]
    parts = sentence.split(',')
    if len(parts) < 6:
        return
    try:
        count = int(parts[1])
        num   = int(parts[2])
    except (ValueError, IndexError):
        return
    seq     = parts[3]
    channel = parts[4]
    payload = parts[5]
    if not payload:
        return
    if count == 1:
        parsed = parse_ais_payload(payload)
        if parsed:
            _update_ship(parsed)
    else:
        key = (count, seq, channel)
        if key not in ais_fragments:
            ais_fragments[key] = {}
        ais_fragments[key][num] = payload
        if len(ais_fragments[key]) == count:
            full = ''.join(ais_fragments[key].get(i, '') for i in range(1, count + 1))
            del ais_fragments[key]
            parsed = parse_ais_payload(full)
            if parsed:
                _update_ship(parsed)


def _ais_udp_listener():
    global ais_running
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(1.0)
    try:
        sock.bind(('0.0.0.0', 10110))
    except Exception as e:
        print(f'AIS UDP bind error: {e}')
        return
    while ais_running:
        try:
            data, _ = sock.recvfrom(4096)
            text = data.decode('ascii', errors='ignore')
            for line in text.split('\n'):
                line = line.strip()
                if line:
                    _process_nmea(line)
        except socket.timeout:
            continue
        except Exception:
            pass
    sock.close()


def start_ais():
    global ais_running, pager_running, acars_running, sensors_running
    pager_running = acars_running = sensors_running = False
    kill_sdr()
    ais_running = True

    def run_loop():
        global ais_running
        while ais_running:
            p = subprocess.Popen(
                ['sudo', 'rtl_ais', '-n', '-h', '127.0.0.1', '-P', '10110'],
                stderr=subprocess.DEVNULL, preexec_fn=os.setsid
            )
            sdr_procs.append(p)
            p.wait()
            if p in sdr_procs:
                sdr_procs.remove(p)
            if ais_running:
                time.sleep(2)

    threading.Thread(target=run_loop, daemon=True).start()
    threading.Thread(target=_ais_udp_listener, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────
# ACARS
# ──────────────────────────────────────────────────────────────────────

def start_acars():
    global acars_running, pager_running, ais_running, sensors_running
    pager_running = ais_running = sensors_running = False
    kill_sdr()
    open(ACARS_LOG, 'w').close()
    acars_running = True

    def run_loop():
        global acars_running
        consecutive_fails = 0
        while acars_running:
            cmd = (f'sudo acarsdec -o 2 -g 48 -e -r 0 131.125 131.550 131.725 131.825 '
                   f'>> {ACARS_LOG} 2>&1')
            p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
            sdr_procs.append(p)
            p.wait()
            if p in sdr_procs:
                sdr_procs.remove(p)
            if acars_running:
                # Back off longer if crashing rapidly (avoid fd leak loop)
                if p.returncode not in (0, -15):
                    consecutive_fails += 1
                else:
                    consecutive_fails = 0
                delay = min(2 * consecutive_fails, 15) if consecutive_fails > 1 else 2
                time.sleep(delay)

    threading.Thread(target=run_loop, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────
# Sensors (rtl_433)
# ──────────────────────────────────────────────────────────────────────

def start_sensors():
    global sensors_running, pager_running, ais_running, acars_running
    pager_running = ais_running = acars_running = False
    kill_sdr()
    open(SENSORS_LOG, 'w').close()
    sensors_running = True

    def run_loop():
        global sensors_running
        while sensors_running:
            cmd = f'sudo rtl_433 -F json -f 433.92M -f 315M -f 345M -g 48 >> {SENSORS_LOG} 2>&1'
            p = subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)
            sdr_procs.append(p)
            p.wait()
            if p in sdr_procs:
                sdr_procs.remove(p)
            if sensors_running:
                time.sleep(2)

    threading.Thread(target=run_loop, daemon=True).start()


def parse_aprs_packet(line):
    """Parse a decoded direwolf APRS line into a structured dict."""
    import re
    # direwolf line format: [signal] CALLSIGN>DEST,PATH*:DATA
    m = re.match(r'\[[\d.]+\]\s+([A-Z0-9-]+)>([^,: ]+)([^:]*):(.+)', line)
    if not m:
        return None
    callsign = m.group(1)
    dest     = m.group(2)
    path     = m.group(3).strip(',')
    data     = m.group(4)

    station = {
        'callsign': callsign,
        'dest': dest,
        'path': path,
        'raw': data,
        'ts': time.strftime('%H:%M:%S'),
        'lat': None, 'lon': None,
        'comment': '',
        'symbol': '?',
        'type': 'unknown',
    }

    # Detect type
    if dest.startswith('AP'):
        station['type'] = 'station'
    if 'DIGI' in data.upper() or dest == 'BEACON':
        station['type'] = 'digi'
    if callsign.endswith('-9') or callsign.endswith('-7'):
        station['type'] = 'mobile'

    # Weather data: _DDHHMMz or contains t=temp
    wx = re.search(r't(-?\d+)', data)
    if wx or '_' in data[:3]:
        station['type'] = 'weather'
        temp_m = re.search(r't(-?\d+)', data)
        if temp_m:
            station['temp_f'] = int(temp_m.group(1))
        wind_m = re.search(r'(\d{3})/(\d{3})', data)
        if wind_m:
            station['wind_dir'] = int(wind_m.group(1))
            station['wind_spd'] = int(wind_m.group(2))

    # Parse position — uncompressed: DDMM.mmN/DDDMM.mmW or E
    pos = re.search(r'(\d{4}\.\d{2})([NS])[/\\I](\d{5}\.\d{2})([EW])', data)
    if pos:
        def dmm(val, hemi):
            d = int(val[:2]) if hemi in 'NS' else int(val[:3])
            rest_start = 2 if hemi in 'NS' else 3
            m2 = float(val[rest_start:]) / 60
            deg = d + m2
            return -deg if hemi in 'SW' else deg
        lat_str, lat_h, lon_str, lon_h = pos.groups()
        station['lat'] = round(dmm(lat_str, lat_h), 5)
        station['lon'] = round(dmm(lon_str, lon_h), 6)

    # Extract altitude
    alt_m = re.search(r'A=(\d+)', data)
    if alt_m:
        station['alt_ft'] = int(alt_m.group(1))

    # Comment is everything after the position symbol or end of known fields
    comment_m = re.search(r'[NS][/\\I]\d{5}\.\d{2}[EW].(.{0,60})', data)
    if comment_m:
        station['comment'] = re.sub(r'[^\x20-\x7E]', '', comment_m.group(1)).strip()

    return station


def start_aprs():
    global aprs_running, pager_running, ais_running, acars_running, sensors_running
    pager_running = ais_running = acars_running = sensors_running = aprs_running = vlf_running = False
    kill_sdr()
    open(APRS_LOG, 'w').close()
    aprs_running = True

    def run_loop():
        global aprs_running
        while aprs_running:
            rtl = subprocess.Popen(
                ['sudo', 'rtl_fm', '-f', '144.39M', '-M', 'fm', '-s', '22050', '-g', '48'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            dw = subprocess.Popen(
                ['direwolf', '-r', '22050', '-'],
                stdin=rtl.stdout, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            sdr_procs.extend([rtl, dw])
            try:
                for raw in dw.stdout:
                    if not aprs_running:
                        break
                    line = raw.decode('utf-8', errors='replace').rstrip()
                    # Write raw to log
                    with open(APRS_LOG, 'a') as f:
                        f.write(line + '\n')
                    # Parse and store
                    if re.match(r'\[[\d.]+\]', line):
                        pkt = parse_aprs_packet(line)
                        if pkt:
                            with aprs_stations_lock:
                                existing = aprs_stations.get(pkt['callsign'], {})
                                # Keep lat/lon if new packet doesn't have it
                                if pkt['lat'] is None and existing.get('lat'):
                                    pkt['lat'] = existing['lat']
                                    pkt['lon'] = existing['lon']
                                aprs_stations[pkt['callsign']] = pkt
            except Exception:
                pass
            finally:
                for p in [rtl, dw]:
                    try: p.terminate()
                    except: pass
                for p in [rtl, dw]:
                    if p in sdr_procs: sdr_procs.remove(p)
            if aprs_running:
                time.sleep(3)

    threading.Thread(target=run_loop, daemon=True).start()


VLF_STATIONS = {
    10.0:  {'name': 'NAA',   'label': 'NAA — US Navy',          'loc': 'Cutler, ME',        'color': '#4fc3f7'},
    19.8:  {'name': 'NWC',   'label': 'NWC — Royal Australian Navy', 'loc': 'Perth, Australia', 'color': '#ff7043'},
    21.4:  {'name': 'NPM',   'label': 'NPM — US Navy',          'loc': 'Hawaii',            'color': '#4fc3f7'},
    24.0:  {'name': 'NAA',   'label': 'NAA — US Navy',          'loc': 'Cutler, ME',        'color': '#4fc3f7'},
    25.2:  {'name': 'NWC2',  'label': 'NWC alt',                'loc': 'Australia',         'color': '#ff7043'},
    37.5:  {'name': 'TBB',   'label': 'TBB — Turkish Navy',     'loc': 'Bafa, Turkey',      'color': '#ab47bc'},
    40.0:  {'name': 'NAA40', 'label': '40 kHz unknown',         'loc': '?',                 'color': '#78909c'},
    45.9:  {'name': 'NSS',   'label': 'NSS — US Navy (hist)',   'loc': 'Annapolis, MD',     'color': '#4fc3f7'},
    60.0:  {'name': 'WWVB',  'label': 'WWVB — NIST Atomic Clock','loc': 'Fort Collins, CO', 'color': '#66bb6a'},
    77.5:  {'name': 'DCF77', 'label': 'DCF77 — German Atomic',  'loc': 'Mainflingen, DE',   'color': '#ffd54f'},
    85.7:  {'name': 'DHO38', 'label': 'DHO38 — German Navy',    'loc': 'Rhauderfehn, DE',   'color': '#ab47bc'},
}

vlf_latest   = {}    # latest scan result
vlf_lock     = threading.Lock()
vlf_running  = False

def run_vlf_scan():
    """Run one rtl_power scan of the VLF band, return list of {freq_khz, power}."""
    csv_file = '/tmp/vlf_scan_live.csv'
    p = subprocess.Popen(
        ['sudo', '/usr/local/bin/rtl_power', '-f', '10k:100k:500',
         '-g', str(cfg.rtl_gain), '-i', '8', '-1', csv_file],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    sdr_procs.append(p)
    p.wait()
    if p in sdr_procs: sdr_procs.remove(p)
    try:
        with open(csv_file) as f:
            line = f.readline().strip()
        parts = line.split(', ')
        if len(parts) < 7: return []
        start = float(parts[2]); step = float(parts[4])
        vals  = [float(x) for x in parts[6:] if x.strip()]
        result = [{'freq': round((start + i*step)/1000, 2), 'power': round(v, 2)}
                  for i, v in enumerate(vals)]
        return result
    except Exception:
        return []

def start_vlf():
    global vlf_running, pager_running, ais_running, acars_running, sensors_running, aprs_running
    pager_running = ais_running = acars_running = sensors_running = aprs_running = False
    kill_sdr()
    vlf_running = True

    def scan_loop():
        global vlf_running
        while vlf_running:
            data = run_vlf_scan()
            if data:
                noise = sorted(data, key=lambda x: x['power'])[len(data)//2]['power']
                with vlf_lock:
                    vlf_latest['data']       = data
                    vlf_latest['noise']      = round(noise, 2)
                    vlf_latest['ts']         = time.strftime('%H:%M:%S')
                    vlf_latest['stations']   = VLF_STATIONS
            if vlf_running:
                time.sleep(5)   # pause between scans

    threading.Thread(target=scan_loop, daemon=True).start()


# ──────────────────────────────────────────────────────────────────────
# Flask routes
# ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    resp = send_from_directory('static', 'index.html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/planes/<path:filename>')
def planes_files(filename):
    return send_from_directory(HTML_DIR, filename)


@app.route('/api/mode/<mode>', methods=['POST'])
def set_mode(mode):
    global current_mode

    def do_switch():
        with mode_lock:
            if   mode == 'planes':  start_planes()
            elif mode == 'pagers':  start_pagers()
            elif mode == 'ais':     start_ais()
            elif mode == 'acars':   start_acars()
            elif mode == 'sensors': start_sensors()
            elif mode == 'aprs':    start_aprs()
            elif mode == 'vlf':     start_vlf()
            elif mode == 'scanner': start_scanner()

    current_mode = mode
    save_mode(mode)
    threading.Thread(target=do_switch, daemon=True).start()
    return jsonify({'mode': mode, 'status': 'started'})


@app.route('/api/device/status')
def device_status():
    import subprocess as sp
    try:
        out = sp.check_output('sudo lsof /dev/bus/usb/001/* 2>/dev/null | grep -v COMMAND | awk \'{print $1,$2}\'',
                              shell=True, timeout=3).decode().strip()
        lines = [l for l in out.splitlines() if l]
        if lines:
            proc = lines[0].split()[0]
            pid  = lines[0].split()[1]
            return json.dumps({'ok': True, 'process': proc, 'pid': pid})
    except Exception:
        pass
    # Fallback: check by active mode flags
    if pager_running or ais_running or acars_running or sensors_running or planes_running:
        return json.dumps({'ok': True, 'process': 'starting', 'pid': ''})
    return json.dumps({'ok': False, 'process': None, 'pid': ''})

@app.route('/api/mode')
def get_mode():
    return jsonify({'mode': current_mode})


def parse_pager_line(line):
    """Parse a FLEX or POCSAG line into structured dict."""
    import re
    msg = {'raw': line, 'ts': time.time()}

    # FLEX|2026-03-17 16:23:42|1600/4/K/A|05.127|000187351|ALN|message text
    m = re.match(r'^FLEX\|(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\|(\d+)/(\d)/(\w)/(\w)\|(\d+\.\d+)\|(\d+)\|(\w+)\|(.*)$', line)
    if m:
        msg['protocol'] = 'FLEX'
        msg['timestamp'] = m.group(1)
        msg['baud']    = m.group(2)
        msg['phase']   = m.group(4)
        msg['frame']   = m.group(6)
        msg['cap']     = m.group(7).lstrip('0') or '0'
        msg['cap_raw'] = m.group(7)
        msg['msgtype'] = m.group(8)   # ALN, NUM, etc.
        msg['text']    = m.group(9)
    else:
        # FLEX: 2026-03-17 16:23:42 1600/4/B 05.127 [000183299] NUM 0626
        m2 = re.match(r'^FLEX:\s+(\S+ \S+)\s+(\d+)/\d/\S+\s+[\d.]+\s+\[(\d+)\]\s+(\w+)\s+(.*)$', line)
        if m2:
            msg['protocol'] = 'FLEX'
            msg['timestamp'] = m2.group(1)
            msg['baud']    = m2.group(2)
            msg['cap']     = m2.group(3).lstrip('0') or '0'
            msg['cap_raw'] = m2.group(3)
            msg['msgtype'] = m2.group(4)
            msg['text']    = m2.group(5)
        else:
            # POCSAG512: Address: 1234567 Function: 0 Alpha: message
            m3 = re.match(r'^(POCSAG\d+):\s+Address:\s*(\d+)\s+Function:\s*(\d+)(?:\s+Alpha:\s*(.+))?$', line)
            if m3:
                msg['protocol'] = m3.group(1)
                msg['cap']      = m3.group(2)
                msg['cap_raw']  = m3.group(2).zfill(7)
                msg['msgtype']  = 'ALN' if m3.group(4) else 'NUM'
                msg['text']     = m3.group(4) or ''
            else:
                return None  # unparseable

    # Detect if text is human-readable (real words, not encoded garbage)
    text = msg.get('text', '')
    if len(text) < 3:
        msg['readable'] = False
    else:
        # Count "normal" chars vs special/control chars
        normal = sum(1 for c in text if c.isalnum() or c in ' .,!?;:-/()')
        special = sum(1 for c in text if c in r'[]{}\\^`<>=@#~|_')
        ratio = normal / len(text)
        # Real words: at least 2 words of 3+ letters with spaces between
        word_count = len(re.findall(r'\b[A-Za-z]{3,}\b', text))
        msg['readable'] = ratio > 0.6 and word_count >= 2

    # Categorize by keywords
    tl = text.lower()
    cap = msg.get('cap', '0')
    if re.search(r'\b(pt|patient|md|dr|nurse|rx|dose|mg|lab|icu|er|stat|code|room|floor|ward|hospital|clinic|pharmacy|surgery|admit|discharge|bp|ekg|ecg|mrn|rn |pacu|desat|fluoro|abx)\b', tl):
        msg['category'] = 'medical'
    elif re.search(r'\b(fire|ems|rescue|ambulance|police|dispatch|unit|engine|ladder|mutual|aid)\b', tl):
        msg['category'] = 'emergency'
    elif re.search(r'\b(alarm|fault|hvac|temperature|sensor|building|system|alert|maintenance|ritm|servicenow)\b', tl):
        msg['category'] = 'facility'
    elif msg.get('msgtype') == 'NUM':
        msg['category'] = 'numeric'
    else:
        msg['category'] = 'other'

    # Identify source organization from text clues (patterns loaded from config.yaml)
    org = None
    for po in cfg.pager_orgs:
        try:
            if re.search(po['pattern'], tl):
                org = po['name']
                break
        except (re.error, KeyError):
            pass
    if org:
        msg['org'] = org
        # Update CAP code directory with this org
        _update_cap_directory(msg.get('cap_raw', ''), org)

    return msg

# In-memory CAP code directory: cap_raw → {'org': ..., 'count': ..., 'last_text': ...}
_cap_directory = {}

def _update_cap_directory(cap_raw, org):
    if not cap_raw: return
    if cap_raw not in _cap_directory:
        _cap_directory[cap_raw] = {'org': org, 'count': 0}
    entry = _cap_directory[cap_raw]
    entry['count'] = entry.get('count', 0) + 1
    if org: entry['org'] = org  # update if we have new info

@app.route('/api/config')
def api_config():
    return jsonify({'lat': cfg.site_lat, 'lon': cfg.site_lon, 'name': cfg.site_name})


@app.route('/api/pagers/freqs')
def pager_freqs():
    return jsonify({k: v['label'] for k, v in PAGER_FREQS.items()})

@app.route('/api/pagers/freq', methods=['GET','POST'])
def pager_freq_ep():
    global pager_freq
    if request.method == 'POST':
        freq = request.json.get('freq', '152.5984')
        if freq in PAGER_FREQS and pager_running:
            pager_freq = freq
            kill_sdr()   # watchdog will restart on new freq
        return jsonify({'freq': pager_freq, 'label': PAGER_FREQS.get(pager_freq,{}).get('label','')})
    return jsonify({'freq': pager_freq, 'label': PAGER_FREQS.get(pager_freq,{}).get('label','')})

@app.route('/api/pagers/capdirectory')
def cap_directory():
    return jsonify(_cap_directory)

@app.route('/api/pagers/stream')
def pager_stream():
    def generate():
        with open(PAGER_LOG, 'a'):
            pass
        with open(PAGER_LOG, 'r') as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if line and (line.startswith('FLEX') or line.startswith('POCSAG')):
                        parsed = parse_pager_line(line)
                        if parsed:
                            # Enrich with any previously learned org for this CAP
                            cap_raw = parsed.get('cap_raw', '')
                            if cap_raw and not parsed.get('org') and cap_raw in _cap_directory:
                                parsed['org'] = _cap_directory[cap_raw].get('org')
                            yield f'data: {json.dumps(parsed)}\n\n'
                else:
                    time.sleep(0.2)
                    yield ': ping\n\n'
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/ais/ships')
def get_ships():
    with ais_ships_lock:
        ships = list(ais_ships.values())
    ships = [s for s in ships
             if s['lat'] != 0.0 and s['lon'] != 0.0
             and abs(s['lat']) < 90 and abs(s['lon']) < 180]
    return jsonify(ships)


def parse_acars_block(lines):
    """Parse a complete acarsdec message block into a structured dict."""
    import re
    msg = {}
    # Header: [#2 (F:131.550 L:-37.7 E:0) 17/03/2026 11:30:27.237
    header = lines[0] if lines else ''
    m = re.search(r'F:([\d.]+)', header)
    if m: msg['freq'] = m.group(1)
    m = re.search(r'L:([-\d.]+)', header)
    if m: msg['level'] = m.group(1)
    m = re.search(r'E:(\d+)', header)
    if m: msg['errors'] = int(m.group(1))
    m = re.search(r'(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})', header)
    if m: msg['timestamp'] = m.group(1)

    body_text = []
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith('---'): continue

        # "Mode : 2 Label : Q7 Id : 9 Nak" — compound line
        m2 = re.match(r'Mode\s*:\s*(\S+)\s+Label\s*:\s*(\S+)', line)
        if m2:
            msg['mode']  = m2.group(1)
            msg['label'] = m2.group(2)
            continue

        # "Aircraft reg: N923AE Flight id: PT5941" — may have flight on same line
        m2 = re.match(r'Aircraft reg\s*:\s*(\S+)(?:\s+Flight id\s*:\s*(\S+))?', line)
        if m2:
            msg['tail']   = m2.group(1)
            if m2.group(2): msg['flight'] = m2.group(2)
            continue

        # "Flight id: ..." standalone
        m2 = re.match(r'Flight id\s*:\s*(\S+)', line)
        if m2:
            msg['flight'] = m2.group(1)
            continue

        # "No: M19A" — sequence
        m2 = re.match(r'No\s*:\s*(\S+)', line)
        if m2:
            msg['seq'] = m2.group(1)
            continue

        body_text.append(line)

    msg['text'] = ' '.join(body_text).strip()

    # Decode label to human meaning
    label_map = {
        'Q0': 'Ping / Keep-alive',     'Q7': 'Departure/Arrival event',
        'H1': 'Weather / ATIS',        '35': 'Position report',
        'SQ': 'Auto position squitter','5Z': 'Maintenance message',
        '80': 'Weather observation',   '4T': 'Flight plan',
        '_d': 'Empty / Test',          '10': 'Date/time sync',
        ':;': 'Free text',             'AA': 'Arrival',
        'SA': 'SELCAL',               '1L': 'Oceanic clearance',
        'RA': 'ATC clearance',        'B6': 'Out/Off/On/In times',
        'PR': 'Position report',      '15': 'ETA report',
    }
    lbl = msg.get('label', '')
    msg['label_desc'] = label_map.get(lbl, f'Message type {lbl}' if lbl else 'Unknown')

    # Try to extract coordinates from text
    m = re.search(r'([NS]\d{2,4}[EW]\d{3,5}|(\d{4,5}[NS]\d{5,6}[EW]))', msg.get('text',''))
    if m: msg['coords_raw'] = m.group(0)

    msg['ts'] = time.time()
    return msg

@app.route('/api/acars/stream')
def acars_stream():
    def generate():
        with open(ACARS_LOG, 'a'):
            pass
        with open(ACARS_LOG, 'r') as f:
            f.seek(0, 2)
            block = []
            skip_words = ('Failed to open', 'Unable to init', 'usb_claim', 'rtlsdr device',
                          'Setting sample', 'Exact sample', 'Allocating', 'Found Rafael',
                          'RTL-SDR Blog', 'acarsdec', 'Acarsdec')
            while True:
                line = f.readline()
                if line:
                    stripped = line.strip()
                    if stripped.startswith('[#') and block:
                        parsed = parse_acars_block(block)
                        txt = parsed.get('text', '')
                        if not any(w in txt for w in skip_words):
                            if parsed.get('tail') or parsed.get('flight') or parsed.get('text'):
                                yield f'data: {json.dumps(parsed)}\n\n'
                        block = [stripped]
                    elif stripped.startswith('[#'):
                        block = [stripped]
                    else:
                        block.append(stripped)
                else:
                    time.sleep(0.2)
                    yield ': ping\n\n'
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/sensors/stream')
def sensors_stream():
    def generate():
        with open(SENSORS_LOG, 'a'):
            pass
        with open(SENSORS_LOG, 'r') as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            obj = json.loads(line)
                            yield f'data: {json.dumps(obj)}\n\n'
                        except Exception:
                            pass
                else:
                    time.sleep(0.2)
                    yield ': ping\n\n'
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/aircraft')
def aircraft():
    try:
        with open(AIRCRAFT_JSON) as f:
            return Response(f.read(), mimetype='application/json')
    except Exception:
        return jsonify({'aircraft': [], 'messages': 0})


@app.route('/api/vlf/scan')
def vlf_scan():
    with vlf_lock:
        if not vlf_latest:
            return jsonify({'status': 'scanning', 'data': [], 'noise': -16, 'ts': '—', 'stations': VLF_STATIONS})
        return jsonify(vlf_latest)

@app.route('/api/aprs/stations')
def aprs_stations_ep():
    with aprs_stations_lock:
        return jsonify(list(aprs_stations.values()))

@app.route('/api/aprs/stream')
def aprs_stream():
    def generate():
        with open(APRS_LOG, 'a'): pass
        with open(APRS_LOG, 'r') as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if re.match(r'\[[\d.]+\]', line):
                        pkt = parse_aprs_packet(line)
                        if pkt and (pkt['lat'] or pkt['comment'] or pkt.get('temp_f')):
                            yield f'data: {json.dumps(pkt)}\n\n'
                else:
                    time.sleep(0.3)
                    yield ': ping\n\n'
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/audio/stream')
def audio_stream():
    freq = request.args.get('freq', '128.800')
    mode = request.args.get('mode', 'fm')   # 'fm' for VHF/ham, 'am' for aircraft
    def generate():
        with mode_lock:
            global pager_running, ais_running, acars_running, sensors_running
            pager_running = ais_running = acars_running = sensors_running = aprs_running = vlf_running = False
            kill_sdr()
            time.sleep(0.5)
        rtl = subprocess.Popen(
            ['sudo', 'rtl_fm', '-f', f'{freq}M', '-M', mode,
             '-s', '240000', '-r', '48000', '-g', '48'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        ff = subprocess.Popen(
            ['ffmpeg', '-f', 's16le', '-ar', '48000', '-ac', '1',
             '-i', 'pipe:0', '-f', 'mp3', '-b:a', '96k',
             '-write_xing', '0', 'pipe:1'],
            stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        sdr_procs.extend([rtl, ff])
        try:
            while True:
                chunk = ff.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        finally:
            for p in [rtl, ff]:
                try: p.terminate()
                except: pass
            for p in [rtl, ff]:
                if p in sdr_procs: sdr_procs.remove(p)
    return Response(generate(), mimetype='audio/mpeg',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'X-Content-Type-Options': 'nosniff'})


# ──────────────────────────────────────────────────────────────────────
# Mode persistence
# ──────────────────────────────────────────────────────────────────────

MODE_FILE = '/tmp/sdr-mode.txt'

def save_mode(mode):
    try:
        open(MODE_FILE, 'w').write(mode)
    except Exception:
        pass

def load_mode():
    try:
        return open(MODE_FILE).read().strip()
    except Exception:
        return 'planes'


if __name__ == '__main__':
    last_mode = load_mode()
    current_mode = last_mode
    if   last_mode == 'pagers':  threading.Thread(target=start_pagers,  daemon=True).start()
    elif last_mode == 'ais':     threading.Thread(target=start_ais,     daemon=True).start()
    elif last_mode == 'acars':   threading.Thread(target=start_acars,   daemon=True).start()
    elif last_mode == 'sensors': threading.Thread(target=start_sensors, daemon=True).start()
    elif last_mode == 'scanner': pass  # audio is on-demand
    else:                        threading.Thread(target=start_planes,  daemon=True).start()
    app.run(host='0.0.0.0', port=cfg.dashboard_port, threaded=True)
