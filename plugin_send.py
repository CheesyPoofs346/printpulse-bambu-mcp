"""Headless cloud send: runs Bambu Studio's own networking plugin (libbambu_networking.so 02.08.04.60) in WSL
through open-bamboo-networking's plugin_runner (cloud-only patch). The stock plugin does all auth and signing
itself; nothing here holds keys or forges requests. Verified on the X1C 2026-09-23 (task 1276692215).

Files in cloudprint/: plugin_runner (built with ABI 0x020804; rebuild if the plugin version changes), plugin/,
ca_bundle.pem (Studio's slicer_base64.cer + system CAs; SSL_CERT_FILE/CURL_CA_BUNDLE must point at it or
HTTPS fails with unknown_ca -> -3010), studio_login.secret.json (Studio-shaped login envelope: SECRET, never
print it). Every attempt leaves a record in cloudprint/jobs/."""
import hashlib
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DIR = Path(__file__).parent / "cloudprint"
LOGIN = DIR / "studio_login.secret.json"
JOBS = DIR / "jobs"
REFRESH_URL = "https://api.bambulab.com/v1/user-service/user/refreshtoken"
CONNECT_DATA = Path.home() / "AppData" / "Roaming" / "Bambu Connect"
# start_print codes that fail before a cloud task exists.
PRE_TASK_CODES = {-3140: "ENC_FLAG_NOT_READY", -3010: "REQUEST_PROJECT_ID_FAILED"}
BED = {"Textured PEI Plate": "textured_plate", "Cool Plate": "cool_plate", "Engineering Plate": "eng_plate",
       "High Temp Plate": "hot_plate", "Smooth PEI Plate": "hot_plate", "Cool Plate (SuperTack)": "supertack_plate",
       "Supertack Plate": "supertack_plate"}


class CloudRefused(RuntimeError):
    """Failed before any cloud print task was created; no UI fallback is permitted."""


def available() -> bool:
    return all(p.exists() for p in (DIR / "plugin_runner", DIR / "plugin" / "libbambu_networking.so",
                                    DIR / "ca_bundle.pem", LOGIN))


def _save_login(envelope: dict) -> None:
    """Replace the private login file only after a complete new credential is available."""
    DIR.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".studio_login.", suffix=".json", dir=DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(envelope, out)
        os.replace(path, LOGIN)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _sync_connect_login() -> bool:
    """Reuse a newer official Connect session from encrypted local storage, without opening its UI."""
    try:
        import win32crypt
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        state = json.loads((CONNECT_DATA / "Local State").read_text(encoding="utf-8"))
        wrapped = base64.b64decode(state["os_crypt"]["encrypted_key"])
        blob = base64.b64decode(json.loads((CONNECT_DATA / "secure-config.json").read_text(
            encoding="utf-8"))["token"])
        if not wrapped.startswith(b"DPAPI") or not blob.startswith(b"v10"):
            return False
        key = win32crypt.CryptUnprotectData(wrapped[5:], None, None, None, 0)[1]
        token = AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8").strip()
    except Exception:  # stale/unreadable official session is an optional credential source
        return False
    envelope = json.loads(LOGIN.read_text(encoding="utf-8"))
    if not token or token == envelope["data"].get("token"):
        return False
    envelope["data"].update(token=token, refresh_token="")
    envelope["data"].pop("issued_at", None)  # Connect does not expose this token's issue time.
    _save_login(envelope)
    return True


def _refresh_login() -> None:
    """Rotate a Studio login envelope using its real refresh token, never an access-token guess."""
    envelope = json.loads(LOGIN.read_text(encoding="utf-8"))
    data = envelope["data"]
    refresh = data.get("refresh_token")
    if not refresh:
        raise CloudRefused("cloud login expired and the saved login has no refresh token; "
                           "a new authenticated login is required")
    request = urllib.request.Request(
        REFRESH_URL,
        data=json.dumps({"refreshToken": refresh}).encode("utf-8"),
        headers={"Authorization": "Bearer " + data["token"], "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            rotated = json.load(response)
    except urllib.error.HTTPError as exc:
        raise CloudRefused(f"cloud login refresh rejected (HTTP {exc.code}); "
                           "a new authenticated login may be required") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CloudRefused(f"cloud login refresh unavailable ({type(exc).__name__}); "
                           "nothing was sent") from None
    except (ValueError, TypeError) as exc:
        raise CloudRefused("cloud login refresh returned malformed JSON; nothing was sent") from None
    if not isinstance(rotated, dict):
        raise CloudRefused("cloud login refresh returned an unexpected response; nothing was sent")
    access, new_refresh = rotated.get("accessToken"), rotated.get("refreshToken")
    if not isinstance(access, str) or not access or not isinstance(new_refresh, str) or not new_refresh:
        raise CloudRefused("cloud login refresh returned no usable token pair; nothing was sent")
    data.update(token=access, refresh_token=new_refresh,
                expires_in=str(rotated.get("expiresIn", data.get("expires_in", ""))),
                refresh_expires_in=str(rotated.get("refreshExpiresIn", data.get("refresh_expires_in", ""))),
                issued_at=time.time())
    _save_login(envelope)


def _renew_login() -> None:
    if not _sync_connect_login():
        _refresh_login()


def _refresh_if_due() -> None:
    data = json.loads(LOGIN.read_text(encoding="utf-8"))["data"]
    # expires_in is a duration, not an expiry date. Older envelopes lack issued_at.
    if data.get("issued_at") and data.get("expires_in"):
        try:
            due = float(data["issued_at"]) + float(data["expires_in"]) - 86400
        except (TypeError, ValueError):
            return
        if time.time() >= due:
            _renew_login()


def _wsl(p: Path) -> str:
    p = p.resolve()
    return f"/mnt/{p.drive[0].lower()}{p.as_posix()[2:]}"


def _config_3mf(src: Path, dst: Path):
    """Config-only copy of the print file (everything but G-code/auxiliaries), as Studio/Connect upload first."""
    with zipfile.ZipFile(src) as s, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as d:
        for item in s.infolist():
            if not (item.filename.endswith(".gcode") or item.filename.startswith("Auxiliaries")):
                d.writestr(item, s.read(item.filename))


def _rgba(c: str | None) -> str:
    c = (c or "").lstrip("#").upper()
    return "#" + (c + "FF")[:8] if len(c) in (6, 8) else ""


def params(file: Path, config: Path, serial: str, plate: int, bed_type: str, filaments: list[dict],
           trays: dict, nozzle_mm: float) -> dict:
    """filaments: the plate's slice_info filaments (id, type, color). trays: filament id -> AMS tray dict
    (ams, slot, type, color, info_idx). Nested fields are JSON strings: that's the plugin ABI's PrintParams."""
    if bed_type not in BED:
        raise CloudRefused(f"unknown plate type {bed_type!r} for the cloud route")
    # An X1C slice made with Studio's generic inherited start macro can look
    # valid yet begin layer one without loading its first AMS filament.
    with zipfile.ZipFile(file) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        if settings.get("printer_model") == "Bambu Lab X1 Carbon":
            gcode = archive.read(f"Metadata/plate_{plate}.gcode").decode(errors="replace")
            layer = gcode.find("; layer num/total_layer_count: 1/")
            if layer < 0:
                raise CloudRefused("X1C G-code has no first-layer marker")
            start = gcode[:layer]
            selected = set(re.findall(r"(?m)^M620 S(\d+)A\b", start))
            loaded = set(re.findall(r"(?m)^M621 S(\d+)A\b", start))
            if not selected.intersection(loaded):
                raise CloudRefused("X1C G-code does not select and load its initial AMS filament before layer one; re-slice with the full X1C machine preset")
    n = max(int(f["id"]) for f in filaments)
    mapping, mapping2, info = [-1] * n, [{"ams_id": 255, "slot_id": 255}] * n, []
    for f in filaments:
        i, t = int(f["id"]), trays[f["id"]]
        mapping[i - 1] = int(t["ams"]) * 4 + int(t["slot"])
        mapping2[i - 1] = {"ams_id": int(t["ams"]), "slot_id": int(t["slot"])}
        info.append({"ams": mapping[i - 1], "amsId": int(t["ams"]), "slotId": int(t["slot"]),
                     "filamentId": t.get("info_idx") or "", "filamentType": t["type"],
                     "nozzleId": 0, "sourceColor": _rgba(t["color"]), "targetColor": _rgba(f["color"])})
    name = file.name
    return {
        "dev_id": serial, "filename": _wsl(file), "config_filename": _wsl(config),
        "project_name": name, "task_name": name, "preset_name": name.split(".")[0], "plate_index": plate,
        "connection_type": "cloud", "task_bed_type": BED[bed_type], "task_bed_leveling": True,
        "task_flow_cali": False, "task_vibration_cali": False, "task_layer_inspect": True,
        "task_record_timelapse": False, "task_use_ams": True, "auto_bed_leveling": 1, "auto_flow_cali": 0,
        "auto_offset_cali": 0, "extruder_cali_manual_mode": 0,
        "ams_mapping": json.dumps(mapping), "ams_mapping2": json.dumps(mapping2),
        "ams_mapping_info": json.dumps(info),
        "nozzles_info": json.dumps([{"id": 0, "type": "", "flowSize": "standard_flow", "diameter": nozzle_mm}]),
        "print_type": "from_normal", "comments": "no_ips",
    }


def _events(log: Path) -> list[dict]:
    out = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def send(file: Path, serial: str, job_params: dict, action: str = "cloud_print", timeout_s: int = 180,
         _auth_retry: bool = True) -> dict:
    """Submit once. CloudRefused means no task was created; an unclear result must not be retried."""
    JOBS.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()[:12]
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}-{digest}-{action}"
    config, pfile, log, guard = (JOBS / f"{stem}.config.3mf", JOBS / f"{stem}.params.json",
                                 JOBS / f"{stem}.log", JOBS / f"{stem}.guard.json")
    _config_3mf(file, config)
    job_params = {**job_params, "config_filename": _wsl(config)}
    pfile.write_text(json.dumps(job_params, indent=1), encoding="utf-8")
    record = {"file": str(file), "sha256_12": digest, "serial": serial, "action": action,
              "plate": job_params.get("plate_index"), "started": time.time()}
    with open(guard, "x", encoding="utf-8") as g:  # written before any network activity
        json.dump(record, g)
    try:
        _refresh_if_due()
    except CloudRefused as exc:
        guard.write_text(json.dumps({**record, "ended": time.time(), "error": str(exc)}), encoding="utf-8")
        raise
    ca = _wsl(DIR / "ca_bundle.pem")
    cmd = ["wsl.exe", "--", "env", f"SSL_CERT_FILE={ca}", f"CURL_CA_BUNDLE={ca}", _wsl(DIR / "plugin_runner"),
           "--plugin-path", _wsl(DIR / "plugin" / "libbambu_networking.so"), "--action", action,
           "--country", "US", "--dev-id", serial, "--user-info", "@" + _wsl(LOGIN),
           "--client-name", "BambuStudio", "--cert-file", ca, "--timeout", str(timeout_s), "--fast-exit"]
    if action == "cloud_print":
        cmd += ["--params-json", _wsl(pfile)]
    with open(log, "w", encoding="utf-8") as out:
        try:
            rc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, timeout=timeout_s + 60).returncode
        except subprocess.TimeoutExpired:
            rc = None
    ev = _events(log)
    kinds = {e.get("_kind"): e for e in ev}
    stages = [e.get("stage") for e in ev if e.get("_kind") in ("on_update", "finished") and e.get("stage")]
    dispatched, fin = kinds.get("action_dispatched"), kinds.get("finished")
    summary = {"exit_code": rc, "stages": stages, "dispatch_rc": dispatched and dispatched.get("rc"),
               "finished_code": fin and fin.get("code"), "log": str(log)}
    record.update(summary, ended=time.time())
    guard.write_text(json.dumps(record), encoding="utf-8")
    if fin and fin.get("stage") == "Finished" and fin.get("code") == 0 and (not dispatched or dispatched.get("rc") == 0):
        return summary
    login = kinds.get("change_user")
    if not dispatched and not any(s in stages for s in ("Upload", "Sending")):
        if login and not login.get("is_logged_in") and _auth_retry:
            _renew_login()
            return send(file, serial, job_params, action, timeout_s, _auth_retry=False)
        why = ("cloud login rejected after refresh attempt" if login and not login.get("is_logged_in")
               else f"setup failed before sending (runner exit {rc}; see {log.name})")
        raise CloudRefused(why)
    if dispatched and dispatched.get("rc") in PRE_TASK_CODES and not any(s in stages for s in ("Upload", "Sending")):
        raise CloudRefused(f"plugin start_print {dispatched['rc']} ({PRE_TASK_CODES[dispatched['rc']]})")
    raise RuntimeError(f"cloud send outcome unclear: {summary}")
