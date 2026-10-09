"""Read-only access to Bambu printers on the LAN: discovery, status (local MQTT) and camera (RTSPS).

Safety invariant: the only thing this module ever publishes to a printer is the `pushall`
status request. Nothing here can start, pause, stop, or change a print.
"""
import concurrent.futures as cf
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

CONFIG = Path(__file__).parent / "printers.json"
STUDIO_CONF = Path(os.environ["APPDATA"]) / "BambuStudio" / "BambuStudio.conf"
PUSHALL = json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})

# gcode_state values where a print is (or may be) in progress. Anything not in IDLE_STATES
# counts as busy, so an unknown/new state fails closed.
IDLE_STATES = {"IDLE", "FINISH", "FAILED"}


def _tls() -> ssl.SSLContext:
    # Printers use a self-signed cert chain from Bambu's own CA.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_ip(name: str, ip: str) -> None:
    cfg = load_config()
    cfg["printers"][name]["ip"] = ip
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def printer(name: str) -> dict:
    ps = load_config()["printers"]
    if name not in ps:
        raise ValueError(f"Unknown printer {name!r}; known: {', '.join(ps)}")
    return {"name": name, **ps[name]}


def access_code(p: dict) -> str:
    """Prefer an explicit code in printers.json, else the one Bambu Studio already stores."""
    if p.get("access_code"):
        return p["access_code"]
    text = STUDIO_CONF.read_text(encoding="utf-8")
    conf, _ = json.JSONDecoder().raw_decode(text)
    for key in ("user_access_code", "access_code"):
        code = conf.get(key, {}).get(p["serial"])
        if code:
            return code
    raise RuntimeError(
        f"No access code for {p['name']} ({p['serial']}). Open the printer once in Bambu Studio's "
        "Device tab, or add \"access_code\" to printers.json.")


# ---------- discovery ----------

def _local_subnets() -> list[str]:
    out = subprocess.run(["ipconfig"], capture_output=True, text=True).stdout
    nets = []
    for line in out.splitlines():
        if "IPv4 Address" in line:
            ip = line.split(":")[-1].strip()
            if ip.startswith(("10.", "192.168.", "172.")):
                nets.append(ip.rsplit(".", 1)[0])
    # Include explicitly configured remote subnets when discovery crosses routed networks.
    nets += [n for n in load_config().get("extra_subnets", []) if n not in nets]
    return nets


def _cert_has_serial(ip: str, serial: str) -> bool:
    try:
        with socket.create_connection((ip, 8883), timeout=1.5) as s:
            with _tls().wrap_socket(s) as t:
                return serial.encode() in (t.getpeercert(binary_form=True) or b"")
    except OSError:
        return False


def _open_8883(ip: str) -> bool:
    try:  # Wi-Fi printers can take >1s to answer
        socket.create_connection((ip, 8883), timeout=2).close()
        return True
    except OSError:
        return False


def ssdp_discover(wait: float = 3.0) -> dict[str, str]:
    """serial -> IP for every Bambu printer that answers an SSDP search on the local subnets."""
    q = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\n"
         b"ST: urn:bambulab-com:device:3dprinter:1\r\n\r\n")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(0.5)
    found: dict[str, str] = {}
    try:
        for dest in ["239.255.255.250"] + [f"{n}.255" for n in _local_subnets()]:
            for port in (1990, 2021):
                try:
                    s.sendto(q, (dest, port))
                except OSError:
                    pass
        end = time.time() + wait
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            hdr = {k.strip().lower(): v.strip() for k, v in
                   (l.split(":", 1) for l in data.decode(errors="ignore").splitlines() if ":" in l)}
            # Trust the printer's own Location header when routed discovery changes the
            # apparent source address of the response.
            ip = hdr.get("location") or addr[0]
            if hdr.get("usn") and not ip.endswith((".255", ".0")):
                found[hdr["usn"]] = ip
    finally:
        s.close()
    return found


def find_ip(p: dict) -> str:
    """Cached IP if it still answers, else SSDP, else scan local /24s for the serial; caches the hit."""
    if p.get("ip") and _open_8883(p["ip"]):
        return p["ip"]
    ip = ssdp_discover().get(p["serial"])
    if ip:
        save_ip(p["name"], ip)
        return ip
    candidates = []
    with cf.ThreadPoolExecutor(128) as ex:
        ips = [f"{n}.{i}" for n in _local_subnets() for i in range(1, 255)]
        for ip, ok in zip(ips, ex.map(_open_8883, ips)):
            if ok:
                candidates.append(ip)
    for ip in candidates:
        if _cert_has_serial(ip, p["serial"]):
            save_ip(p["name"], ip)
            return ip
    # Cert didn't name the serial: fall back to whichever candidate reports on this serial's topic.
    for ip in candidates:
        try:
            _pushall(ip, p["serial"], access_code(p), timeout=4)
            save_ip(p["name"], ip)
            return ip
        except Exception:
            continue
    raise RuntimeError(
        f"{p['name']} not found on {', '.join(n + '.x' for n in _local_subnets()) or 'any LAN'}. "
        "Is it powered on and on the same network as this PC? You can also set its \"ip\" in printers.json.")


# ---------- status ----------

def _merge(a: dict, b: dict) -> None:
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v


def _pushall(ip: str, serial: str, code: str, timeout: float = 10) -> dict:
    report: dict = {}
    got_full = threading.Event()

    def on_connect(c, _u, _f, rc, _p):
        if rc == 0:
            c.subscribe(f"device/{serial}/report")
            c.publish(f"device/{serial}/request", PUSHALL)

    def on_message(_c, _u, msg):
        data = json.loads(msg.payload).get("print")
        if isinstance(data, dict):
            _merge(report, data)
            if "gcode_state" in report:
                got_full.set()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"claude-ro-{os.getpid()}-{time.time_ns()}")
    c.username_pw_set("bblp", code)
    c.tls_set_context(_tls())
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(ip, 8883, keepalive=30)
    c.loop_start()
    try:
        if not got_full.wait(timeout):
            raise TimeoutError(f"No status from {ip} in {timeout}s (wrong access code or printer busy/asleep?)")
        time.sleep(1.0)  # H2-series splits the report across several messages
    finally:
        c.disconnect()
        c.loop_stop()
    return report


def raw_status(name: str) -> dict:
    p = printer(name)
    return _pushall(find_ip(p), p["serial"], access_code(p))


def _temp_pair(v) -> tuple:
    """H2-series `device` temps are packed as (target << 16) | current."""
    return (v & 0xFFFF, v >> 16) if isinstance(v, int) else (v, None)


NOZZLE_MATERIAL = {"00": "stainless_steel", "01": "hardened_steel", "05": "tungsten_carbide"}


def nozzle_material(code: str | None) -> str:
    """Bambu nozzle type code, e.g. HS01: last two digits = material (decoding as in ha-bambulab)."""
    return NOZZLE_MATERIAL.get((code or "")[2:4], "unknown")


def _hotends(r: dict) -> list[dict]:
    info = ((r.get("device") or {}).get("nozzle") or {}).get("info") or []
    out = [{"id": n.get("id"), "diameter": float(n["diameter"]), "type": n.get("type"),
            "material": nozzle_material(n.get("type"))} for n in info if n.get("diameter")]
    if not out and r.get("nozzle_diameter"):
        out = [{"id": 0, "diameter": float(r["nozzle_diameter"]), "type": r.get("nozzle_type"),
                "material": nozzle_material(r.get("nozzle_type"))}]
    return out


def hms_level(code: str) -> int:
    """Severity from the 3rd group of an HMS code: 1 fatal, 2 serious, 3 common, 4 info
    (community-documented convention, e.g. ha-bambulab; the 0003 codes seen here are maintenance
    reminders, which fits)."""
    try:
        return int(code.split("_")[2], 16)
    except (IndexError, ValueError):
        return 0


def _hms(e: dict) -> str:
    """HMS code in Bambu's wiki format, e.g. 0501_0400_0003_0004."""
    a, c = e.get("attr", 0), e.get("code", 0)
    return f"{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}"


def _remaining(t: dict) -> dict:
    """Remaining filament for one AMS tray. The printer only knows this for RFID-tagged Bambu
    spools; untagged spools report 0 or -1, which means unknown, not empty."""
    pct = t.get("remain")
    rfid = t.get("tag_uid", "0" * 16).strip("0") != ""
    if not isinstance(pct, int) or pct < 0 or (pct == 0 and not rfid):
        return {"remain_pct": None, "remain_m": None}
    return {"remain_pct": pct, "remain_m": round(pct / 100 * int(t.get("total_len", 0)) / 1000, 1)}


def summarize(r: dict) -> dict:
    s = {
        "state": r.get("gcode_state"),
        "busy": r.get("gcode_state") not in IDLE_STATES,
        "job": r.get("subtask_name"),
        "percent": r.get("mc_percent"),
        "remaining_min": r.get("mc_remaining_time"),
        "layer": f"{r.get('layer_num')}/{r.get('total_layer_num')}",
        "stage": r.get("stg_cur"),
        "speed_level": r.get("spd_lvl"),
        "bed": {"now": r.get("bed_temper"), "target": r.get("bed_target_temper")},
        "chamber": r.get("chamber_temper"),
        "nozzle": {"now": r.get("nozzle_temper"), "target": r.get("nozzle_target_temper")},
        "fans": {k: r.get(k) for k in ("cooling_fan_speed", "big_fan1_speed", "big_fan2_speed", "heatbreak_fan_speed") if k in r},
        "print_error": f"{r['print_error'] >> 16:04X}_{r['print_error'] & 0xFFFF:04X}" if r.get("print_error") else None,
        "hms_errors": [_hms(e) for e in r.get("hms", [])],
        "wifi": r.get("wifi_signal"),
        "task_id": r.get("task_id"),
        "hotends": _hotends(r),
        "ai_safety": {k: (r.get("xcam") or {}).get(k) for k in
                      ("spaghetti_detector", "print_halt", "printing_monitor", "first_layer_inspector",
                       "buildplate_marker_detector", "halt_print_sensitivity")},
        "nozzle_diameter": r.get("nozzle_diameter"),
        # H2-series lists every hotend in the rack; older models only the mounted one.
        "nozzle_diameters_available": sorted({float(n["diameter"]) for n in
                                              ((r.get("device") or {}).get("nozzle") or {}).get("info", [])
                                              if n.get("diameter")}) or [float(r.get("nozzle_diameter") or 0)],
        "camera_liveview": (r.get("ipcam") or {}).get("rtsp_url", "unknown") != "disable",
    }
    dev = r.get("device") or {}
    if s["chamber"] is None and "temp" in dev.get("ctc", {}).get("info", {}):
        now, target = _temp_pair(dev["ctc"]["info"]["temp"])
        s["chamber"] = {"now": now, "target": target}
    ext = dev.get("extruder", {}).get("info")
    if isinstance(ext, list):  # H2 series: one entry per nozzle
        s["nozzles"] = [dict(zip(("now", "target"), _temp_pair(e.get("temp"))), id=e.get("id")) for e in ext]
    trays = []
    for unit in (r.get("ams") or {}).get("ams", []):
        for t in unit.get("tray", []):
            if t.get("tray_type"):
                trays.append({"ams": unit.get("id"), "slot": t.get("id"), "type": t.get("tray_type"),
                              "name": t.get("tray_sub_brands"), "color": t.get("tray_color"), "info_idx": t.get("tray_info_idx"),
                              **_remaining(t)})
    s["ams_trays"] = trays
    if r.get("vt_tray", {}).get("tray_type"):
        s["external_spool"] = {"type": r["vt_tray"]["tray_type"], "color": r["vt_tray"].get("tray_color")}
    return s


# ---------- camera ----------

def snapshot(name: str) -> bytes:
    p = printer(name)
    ip = find_ip(p)
    url = f"rtsps://bblp:{access_code(p)}@{ip}:322/streaming/live/1"
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "frame.jpg"
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-y",
             "-i", url, "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True, text=True, timeout=25)
        if not out.exists():
            err = r.stderr.replace(access_code(p), "***").strip()[-400:]
            raise RuntimeError(
                f"Camera on {name} gave no frame: {err or 'no output'}. On the printer touchscreen turn on "
                "Settings > LAN Only page > 'LAN Only Liveview' (leave 'LAN Only' itself off, so cloud "
                "mode stays on).")
        return out.read_bytes()

