"""MCP server for Bambu Lab printers that stay in Cloud mode.

- Status/temps/AMS/camera: read-only over the LAN (allowed in Cloud mode).
- Slicing/scaling/copies: Bambu Studio CLI, fully local.
- Starting prints: hands the sliced file to Bambu Connect, Bambu's official, authorized sender.
This server never sends a control command to a printer, so it cannot pause, stop or alter a print.
"""
import functools
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import mesh
import plugin_send
import printer
import uia_connect
import profiles
import slicer

PREFLIGHT_TTL_S = 600
STARTED = Path(__file__).parent / "started_jobs.json"  # jobs the assistant started: scope of pause authority


def _started() -> dict:
    return json.loads(STARTED.read_text(encoding="utf-8")) if STARTED.exists() else {}


def _job_matches(job: str | None, stem: str) -> bool:
    """Printer job name vs our file stem ('cube_h2c' vs 'cube_h2c.gcode.3mf' / 'cube_h2c')."""
    return bool(job) and bool(stem) and job.lower().split(".")[0] == stem.lower()
_preflights: dict[tuple[str, str], float] = {}
_planned_slots: dict[tuple[str, str], list] = {}  # AMS slot labels preflight chose, e.g. ["A1"]  # (printer, file) -> time a preflight passed

BAMBU_CONNECT = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "bambu-connect" / "Bambu Connect.exe"

mcp = MCPServer(
    "bambu",
    instructions=("Local Bambu Lab printer integration. Follow explicit user confirmation before starting, pausing, resuming, or stopping any physical print. Check the configured printer, loaded filament, plate, and live printer status before a print action. Inspect camera output and slicer previews when available. Fail closed when status or hardware state is uncertain."),
)


def tool(fn):
    """Register fn as a tool whose failures reach the model with their message (MCP 2.x masks
    plain exceptions as a generic crash)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return mcp.tool()(wrapper)


def _rgb(h):
    h = (h or "").lstrip("#")[:6]
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) if len(h) == 6 else None


def _closest_tray(ftype: str, fcol: str, trays: list[dict]) -> tuple[dict | None, float | None]:
    """The loaded AMS tray of this type nearest in colour, and its RGB distance."""
    want = _rgb(fcol)
    cands = [t for t in trays if t["type"] == ftype and _rgb(t["color"])]
    if not want or not cands:
        return None, None
    best = min(cands, key=lambda t: sum((a - b) ** 2 for a, b in zip(want, _rgb(t["color"]))))
    return best, sum((a - b) ** 2 for a, b in zip(want, _rgb(best["color"]))) ** 0.5


def _slot_label(tray: dict) -> str:
    """AMS tray -> the label Bambu Connect/the printer show: AMS 0..3 = A..D, slot 0..3 = 1..4,
    AMS HT (id 128+) = HT-A.., external spool = Ext."""
    ams = int(tray["ams"])
    if ams >= 128:
        return f"HT-{chr(65 + ams - 128)}"
    return f"{chr(65 + ams)}{int(tray['slot']) + 1}"


def _check_filaments(printer_name: str, plates: list[dict]) -> None:
    """Refuse filament types a printer is hard-restricted from (`allowed_filament_types` in
    printers.json; unused right now, kept for temporary restrictions)."""
    allowed = printer.load_config()["printers"].get(printer_name, {}).get("allowed_filament_types")
    if not allowed:
        return
    bad = sorted({f["type"] for p in plates for f in p["filaments"] if f["type"] not in allowed})
    if bad:
        raise ValueError(f"{printer_name} is restricted to {allowed} right now; this file uses {bad}. "
                         "Use another printer, or ask the user whether the restriction is lifted.")


def _check_model(printer_name: str, plates: list[dict]) -> None:
    """Refuse G-code sliced for a different printer model (wrong kinematics/bed = crash risk)."""
    model = printer.load_config()["printers"][printer_name]["model"]
    want = profiles.resolve("machine", slicer.MODELS[model]["machine"]).get("model_id")
    got = sorted({p.get("printer_model_id") or "unknown" for p in plates})
    if got != [want]:
        raise ValueError(f"File was sliced for printer model {got}, but {printer_name} is {want}. "
                         f"Re-slice it for {printer_name}.")


@tool
def list_printers() -> dict:
    """Configured printers (name, model, serial, last known IP)."""
    return printer.load_config()["printers"]


@tool
def printer_status(printer_name: str, raw: bool = False) -> dict:
    """Live status: state, job, progress, layers, remaining time, nozzle/bed/chamber temps, fans,
    AMS trays, HMS errors. `busy` is true for anything other than IDLE/FINISH/FAILED.
    raw=True returns the printer's full report instead of the summary."""
    r = printer.raw_status(printer_name)
    if raw:
        return r
    s = printer.summarize(r)
    note = printer.load_config()["printers"][printer_name].get("note")
    if note:
        s["note"] = note
    return s


@tool
def camera_snapshot(printer_name: str, frames: int = 1, interval_s: float = 5) -> list[Image]:
    """Grab frame(s) from the printer camera. frames>1 watches over time (max 6)."""
    out = []
    for i in range(max(1, min(frames, 6))):
        if i:
            time.sleep(interval_s)
        out.append(Image(data=printer.snapshot(printer_name), format="jpeg"))
    return out


@tool
def list_presets(printer_model: str, kind: str, contains: str = "") -> list[str]:
    """System presets for a printer. kind: process | filament | machine. printer_model: x1c | h2c."""
    m = slicer.MODELS[printer_model]
    key = m["machine"] if kind == "machine" else f"@BBL {m['tag']}"
    return [n for n in profiles.names(kind, contains) if key in n]


@tool
def model_info(path: str) -> dict:
    """Size, volume and triangle count of an STL/3MF/OBJ/STEP."""
    return slicer.model_info(path)


@tool
def analyze_model(path: str, printer_model: str) -> dict:
    """Judge how a model should sit on the bed BEFORE slicing: size and bed fit, and a ranked list of
    lay-flat orientations with overhang area needing support (>60° from vertical), 45-60° risky
    overhang, bed contact area and tall/thin ratio. Each option gives rotate_x/rotate_y to pass to
    slice_models `rotations`. Then also think about strength (layer lines are weakest across Z),
    which surfaces must look good, holes/threads and text, and decide supports/brim."""
    return mesh.analyze(path, printer_model)


@tool
def slice_models(
    printer_model: str,
    inputs: list[str],
    copies: list[int] | None = None,
    scale: float | None = None,
    rotate_z: float | None = None,
    auto_orient: bool = False,
    process: str = "0.20mm Standard",
    filament: str = "Bambu PLA Basic",
    nozzle: float = 0.4,
    overrides: dict | None = None,
    use_project_settings: bool = False,
    name: str | None = None,
    plate_type: str = "Textured PEI Plate",
    rotations: list[list[float]] | None = None,
    supports: str | None = None,
    supports_build_plate_only: bool = False,
    brim: str = "auto",
    filaments: list[dict] | None = None,
    input_filaments: list[int] | None = None,
    assemble: bool = False,
    color_changes: list[dict] | None = None,
) -> list:
    """Slice models into a printable .gcode.3mf (auto-arranged; extra plates are made if needed).

    printer_model: x1c | h2c. inputs: STL/3MF/OBJ/STEP paths, several allowed.
    copies: per-input count, e.g. [4, 1] = four of the first model, one of the second.
    scale: factor (1.1 = 110%). rotate_z: degrees. process/filament: preset base names
    (see list_presets). overrides: raw Bambu setting keys, e.g. {"layer_height": 0.16,
    "sparse_infill_density": "25%", "wall_loops": 3, "enable_support": 1,
    "nozzle_temperature": 225, "hot_plate_temp": 60}.
    plate_type: Textured PEI Plate | Cool Plate | Engineering Plate | High Temp Plate (sets bed temp).
    use_project_settings: slice a .3mf with its own embedded settings (e.g. a MakerWorld project).
    rotations: per-input [rotate_x, rotate_y] degrees from analyze_model, e.g. [[90, 180], [0, 0]]
    (rotating a 3MF/STEP converts it to a plain mesh: colours/modifiers are lost).
    supports: off | auto (Bambu tree) | tree | normal. supports_build_plate_only: no supports
    resting on the model itself. brim: auto | on | off.
    MULTICOLOUR: filaments = colour slots, e.g. [{"preset": "Bambu PLA Basic", "color": "#FFFFFF"},
    {"preset": "Bambu PLA Basic", "color": "#000000"}] (use the exact AMS tray colours from
    printer_status so Bambu Connect maps them). input_filaments = slot per input, e.g. [1, 2].
    assemble=True merges all inputs into ONE model keeping their relative positions (a model
    exported as one STL per colour); False keeps them as separate objects, each in its own colour.
    color_changes = colour by height, e.g. [{"at_mm": 3, "filament": 2}] = everything above 3 mm in
    slot 2 (needs `filaments` with that slot). Good for signs/keychains/lettering on a base.
    A multicolour 3MF made for THIS printer: use_project_settings=True keeps its painting.
    Returns the result plus the slicer's render of plate 1: LOOK at it (orientation, placement,
    supports) before calling it good.
    """
    sup = {"off": ("0", None), "auto": ("1", "tree(auto)"), "tree": ("1", "tree(auto)"),
           "normal": ("1", "normal(auto)")}
    brims = {"auto": "auto_brim", "on": "outer_only", "off": "no_brim"}
    if (supports is not None and supports not in sup) or brim not in brims:
        raise ValueError(f"supports must be one of {list(sup)} (or omitted = decide from the model), brim one of {list(brims)}")
    decisions = []
    for f in filaments or []:
        if f.get("color") and not re.fullmatch(r"#[0-9A-Fa-f]{6}", f["color"]):
            raise ValueError(f"colour {f['color']!r} must be #RRGGBB")
    auto_colour_note = None
    if not filaments and not use_project_settings:
        # No colour given: take it from the loaded spool, or Connect maps the preset's default green
        # to whatever it guesses (it has mapped black PLA to the wrong slot before).
        preset = slicer._preset("filament", filament, printer_model, nozzle)
        ftype = (profiles.resolve("filament", preset).get("filament_type") or ["?"])[0]
        try:
            trays = [t for t in printer.summarize(printer.raw_status(printer_model))["ams_trays"] if t["type"] == ftype]
        except Exception:
            trays = None
        if trays is None:
            auto_colour_note = "printer unreachable: file colour is the preset default; re-slice with `filaments` before printing"
        elif not trays:
            raise ValueError(f"No {ftype} spool is loaded on {printer_model}. Ask the user which filament to use.")
        elif len({t["color"][:6] for t in trays}) > 1:
            opts = ", ".join(f"{_slot_label(t)} #{t['color'][:6]}" for t in trays)
            raise ValueError(f"Several {ftype} spools are loaded on {printer_model} ({opts}). Ask the user which "
                             f"colour, then pass filaments=[{{\"preset\": \"{filament}\", \"color\": \"#RRGGBB\"}}].")
        else:
            filaments = [{"preset": filament, "color": "#" + trays[0]["color"][:6]}]
            auto_colour_note = f"colour taken from the only loaded {ftype} spool ({_slot_label(trays[0])} {filaments[0]['color']})"
    work_inputs = list(inputs)
    if rotations:
        if len(rotations) != len(inputs):
            raise ValueError("rotations needs one [rotate_x, rotate_y] per input")
        if assemble and len({tuple(r) for r in rotations}) > 1:
            raise ValueError("assembled parts must all get the same rotation or they come apart")
    mesh_path = not use_project_settings
    need_support = 0.0
    if mesh_path and not rotations and not assemble:
        # No orientation given: use the analyzer's pick (stability first, then least support), like
        # laying a part flat in Studio. The caller can always pass explicit rotations instead.
        rotations = []
        for i in inputs:
            a = mesh.analyze(i, printer_model)
            b = a["recommended"]
            rotations.append([b["rotate_x"], b["rotate_y"]])
            need_support = max(need_support, b["overhang_needs_support_mm2"])
            if b["option"] != 0:
                decisions.append(f"{Path(i).name}: rotated to '{b['label']}' (stable, {b['bed_contact_mm2']} mm2 on the plate)")
    elif mesh_path:
        for i, r in zip(inputs, rotations or [[0, 0]] * len(inputs)):
            m = next(o for o in mesh.analyze(mesh.transformed_copy(i, r[0], r[1], 1, slicer.OUT_DIR), printer_model)["options"]
                     if o["option"] == 0)
            need_support = max(need_support, m["overhang_needs_support_mm2"])
        for f in slicer.OUT_DIR.glob("*_rx*_ry*_s1.stl"):
            f.unlink(missing_ok=True)
    if supports is None:
        # Nothing may print onto thin air: supports on whenever the part has real overhangs.
        supports = "tree" if (need_support > 5 or not mesh_path) and not use_project_settings else "off"
        decisions.append(f"supports {supports} (overhang needing support: {need_support:.0f} mm2)")
    overrides = dict(overrides or {})
    overrides.setdefault("enable_support", sup[supports][0])
    if sup[supports][1]:
        overrides.setdefault("support_type", sup[supports][1])
    overrides.setdefault("support_on_build_plate_only", "1" if supports_build_plate_only else "0")
    rots = rotations or [[0, 0]] * len(inputs)
    # Rotate/scale meshes ourselves (and always drop them flat on the plate): the CLI's --scale ignores
    # --ensure-on-bed for assembled models, and it never lowers a floating model. Project 3MFs keep the CLI path.
    if mesh_path:
        tdir = slicer.OUT_DIR / "transformed"
        tdir.mkdir(parents=True, exist_ok=True)
        shared = (min(mesh.min_z_after(i, rx, ry, scale or 1) for i, (rx, ry) in zip(inputs, rots))
                  if assemble else None)
        work_inputs = [mesh.transformed_copy(i, rx, ry, scale or 1, tdir, shared) for i, (rx, ry) in zip(inputs, rots)]
        scale = None
    expect_h = None
    if mesh_path:
        hs = [mesh.z_extent(work_inputs)] if assemble else [mesh.z_extent([w]) for w in work_inputs]
        expect_h = max(hs)
    res = slicer.slice_models(printer_model, work_inputs, copies, scale, rotate_z, auto_orient, process,
                              filament, nozzle, overrides, use_project_settings,
                              name or Path(inputs[0]).name.split(".")[0], plate_type,
                              None if use_project_settings else {"brim_type": brims[brim]},
                              filaments, input_filaments, assemble)
    for w in work_inputs:  # rotated/scaled temp copies aren't needed once sliced
        if Path(w).parent.name == "transformed":
            Path(w).unlink(missing_ok=True)
    if color_changes:
        if use_project_settings or not filaments:
            raise ValueError("color_changes needs `filaments` slots (and not use_project_settings)")
        first = Path(res["file"])
        res = slicer.add_layer_colour_changes(first, printer_model, color_changes,
                                              [f.get("color") or "#FFFFFF" for f in filaments],
                                              (name or Path(inputs[0]).name.split(".")[0]))
        if first != Path(res["file"]):
            first.unlink(missing_ok=True)
    try:
        _check_filaments(printer_model, res["plates"])
    except ValueError:
        Path(res["file"]).unlink(missing_ok=True)
        raise
    if expect_h is not None:
        # Truncation guard: the printed height must match the model (catches parts sunk into the bed).
        got_h = slicer.gcode_max_z(Path(res["file"]))
        if got_h is not None and abs(got_h - expect_h) > 0.3:
            Path(res["file"]).unlink(missing_ok=True)
            raise RuntimeError(f"Sliced height {got_h} mm doesn't match the model's {expect_h:.2f} mm: part of "
                               "the model would be cut off or float. Not saved.")
    if mesh_path and need_support > 150 and sup[supports][0] == "1" and not any(p["support_blocks"] for p in res["plates"]):
        # Bambu's slicer judges overhangs properly (short ledges off a wall print fine unsupported, so
        # zero support there is correct). Only a LARGE flagged area with no support is suspicious.
        decisions.append(f"WARNING: {need_support:.0f} mm2 of steep overhang but Bambu generated no support; "
                         "look at the plate render / consider supports='normal' or another orientation")
    if decisions:
        res["decisions"] = decisions
    if auto_colour_note:
        res["colour_note"] = auto_colour_note
    if mesh_path and not rotations:
        # Orientation check: warn when the file is sliced as modelled but a better way to lay it exists.
        try:
            for i in inputs:
                a = mesh.analyze(i, printer_model)
                asis = next(o for o in a["options"] if o["option"] == 0)
                if a["recommended"]["option"] != 0 and not asis["stable"]:
                    res.setdefault("orientation_warnings", []).append(
                        f"{Path(i).name} is sliced as modelled, which is unstable (contact {asis['bed_contact_mm2']} mm2, "
                        f"tall/thin {asis['tall_thin_ratio']}); analyze_model recommends {a['recommended']['label']} "
                        f"(rotations=[[{a['recommended']['rotate_x']}, {a['recommended']['rotate_y']}]]).")
        except Exception:
            pass
    png = slicer.plate_preview(Path(res["file"]))
    return [json.dumps(res, indent=1)] + ([Image(data=png, format="png")] if png else [])


@tool
def preflight_check(printer_name: str, file: str) -> list:
    """MANDATORY before print_file. Re-reads status, checks the file against the printer, and returns
    a fresh camera frame. YOU must then inspect the image and confirm ALL of:
    - build plate is present, seated flat, the right way round / correct side up, and matches the
      file's plate type (e.g. Textured PEI vs Cool/smooth);
    - bed is completely clear: no finished parts, purge blobs, spaghetti, tools or debris;
    - nothing hanging from the nozzle/toolhead, no filament tangles visible;
    - nothing else looks off (door/lid, loose parts, odd positions).
    If anything is unclear or not visible in the frame, ask the user; never assume. Show the user the
    frame and the checklist when asking for their yes. A pass is valid for 10 minutes.
    """
    path = Path(file).resolve()
    plates = slicer.read_plates(path)
    if not plates or not all(p["has_gcode"] for p in plates):
        raise ValueError(f"{path.name} has no G-code; slice it first.")
    _check_model(printer_name, plates)
    _check_filaments(printer_name, plates)
    st = printer.summarize(printer.raw_status(printer_name))
    blockers, warnings = [], []
    if st["busy"]:
        blockers.append(f"printer is {st['state']} ({st['job']}, {st['percent']}%)")
    if st["print_error"]:
        blockers.append(f"print_error {st['print_error']} is active; resolve it on the printer first")
    severe = [c for c in st["hms_errors"] if printer.hms_level(c) in (1, 2)]
    if severe:
        blockers.append(f"fatal/serious HMS errors {severe}: resolve on the printer first")
    if set(st["hms_errors"]) - set(severe):
        warnings.append(f"HMS messages {sorted(set(st['hms_errors']) - set(severe))}: look them up and tell "
                        "the user before printing")
    file_nozzles = sorted({float(x) for x in (slicer.read_settings(path).get("nozzle_diameter") or [])})
    if not file_nozzles:
        blockers.append("file doesn't say which nozzle size it was sliced for; re-slice it")
    elif any(d not in st["nozzle_diameters_available"] for d in file_nozzles):
        blockers.append(f"file is sliced for {file_nozzles} mm nozzle(s); printer has "
                        f"{st['nozzle_diameters_available']} mm")
    elif st["nozzle_diameter"] and float(st["nozzle_diameter"]) not in file_nozzles:
        warnings.append(f"mounted nozzle reports {st['nozzle_diameter']} mm, file wants {file_nozzles}; confirm "
                        "the right hotend will be used")
    if st["bed"]["now"] and st["bed"]["now"] > 45:
        warnings.append(f"bed is still {st['bed']['now']}°C; a just-finished part may still be on it")
    need = sorted({f["type"] for p in plates for f in p["filaments"]})
    loaded = sorted({t["type"] for t in st["ams_trays"]} | ({st["external_spool"]["type"]} if st.get("external_spool") else set()))
    missing = [n for n in need if n not in loaded]
    if missing:
        blockers.append(f"file needs {missing} but loaded filament is {loaded}")
    # Bambu Connect prints one plate per send, so each filament must cover the hungriest plate.
    worst: dict = {}
    for p in plates:
        for f in p["filaments"]:
            k = (f["type"], f["color"])
            if float(f.get("used_m") or 0) >= float(worst.get(k, {}).get("used_m") or 0):
                worst[k] = f
    for need_f in worst.values():
        want_m = float(need_f.get("used_m") or 0) * 1.15  # 15% margin for purge/waste
        trays = [t for t in st["ams_trays"] if t["type"] == need_f["type"]]
        known = [t for t in trays if t["remain_m"] is not None]
        if not trays:
            continue
        if any(t["remain_m"] >= want_m for t in known):
            continue
        if known and len(known) == len(trays):
            blockers.append(f"not enough {need_f['type']}: need ~{want_m:.1f} m, most left is "
                            f"{max(t['remain_m'] for t in known)} m")
        else:
            warnings.append(f"can't read remaining {need_f['type']} on some spools (no RFID); job needs "
                            f"~{want_m:.1f} m ({need_f.get('used_g')} g). Ask the user if there's enough.")
    # The printers' own AI (spaghetti detection + auto-halt) is the only watchdog when no chat is open.
    ai = st.get("ai_safety") or {}
    off = [k for k in ("spaghetti_detector", "print_halt", "printing_monitor") if ai.get(k) is False]
    if off:
        warnings.append(f"printer AI safety is OFF ({off}): nothing will stop a failed print while no one "
                        "is watching. Ask the user to turn it back on (printer Settings > AI monitoring).")
    # Abrasive filament (carbon/glass fibre etc.) chews through stainless nozzles.
    abrasive = sorted({f["type"] for p in plates for f in p["filaments"]
                       if any(x in (f["type"] or "").upper() for x in ("CF", "GF", "PAHT", "PPA"))})
    if abrasive:
        hard = [h for h in st["hotends"] if h["material"] in ("hardened_steel", "tungsten_carbide")
                and h["diameter"] in file_nozzles]
        if not hard:
            blockers.append(f"abrasive filament {abrasive} needs a hardened nozzle; hotends are "
                            f"{[(h['diameter'], h['material']) for h in st['hotends']]}")
        warnings.append(f"abrasive filament {abrasive}: glow-in-the-dark PLA is abrasive too; confirm with "
                        "the user that the hardened hotend will be used")
    # Smooth Cool Plate at ~35C gives PLA little grip: small prints detach (happened 2026-09-22).
    if (slicer.read_plate_type(path) or "").startswith("Cool Plate") and "PLA" in need:
        warnings.append("Cool Plate + PLA: the plate must be washed (dish soap, no fingerprints) and glue-sticked, "
                        "and small parts need brim 'on'. Ask the user, or suggest a Textured PEI plate (55C).")
    # Colour: each file filament should be loaded (same type, close colour) or Connect maps it to
    # whatever is there and the model comes out in the wrong colours.
    colour_map = []
    for (ftype, fcol), f in sorted(worst.items()):
        best, dist = _closest_tray(ftype, fcol, st["ams_trays"])
        colour_map.append({"file_filament": f"{ftype} {fcol}", "closest_loaded": f"AMS {best['ams']} slot {best['slot']} {best['color'][:6]}" if best else None,
                           "colour_distance": round(dist) if dist is not None else None,
                           "planned_slot": _slot_label(best) if best else None})
        if best is None or dist > 60:
            blockers.append(f"colour {fcol} ({ftype}) isn't loaded (closest: {colour_map[-1]['closest_loaded']}): "
                            "Connect would map it to a guessed spool. Re-slice with the exact AMS colour "
                            "(filaments=[{\"color\": ...}]) or ask the user.")
    report = {
        "printer": printer_name, "file": path.name, "state": st["state"], "colour_map": colour_map,
        "file_plate_type": slicer.read_plate_type(path),
        "will_use": [f"{c['planned_slot']}: {c['closest_loaded']}" for c in colour_map],
        "file_filaments": need, "loaded_filaments": st["ams_trays"],
        "plates": [{k: p[k] for k in ("plate", "print_time_min", "filament_g", "support_blocks")} for p in plates],
        "file_nozzle_mm": file_nozzles, "printer_nozzles_mm": st["nozzle_diameters_available"],
        "hotends": st["hotends"], "ai_safety": st.get("ai_safety"),
        "blockers": blockers, "warnings": warnings,
    }
    cfg = printer.load_config()["printers"][printer_name]
    if cfg.get("note"):
        report["note"] = cfg["note"]
    usual = cfg.get("usual_filament_types")
    unusual = sorted({f["type"] for p in plates for f in p["filaments"]} - set(usual or [])) if usual else []
    if unusual:
        warnings.append(f"{printer_name} usually runs {usual}; this file uses {unusual}. Confirm with the user "
                        "that it's set up for that material.")
    frame = None
    try:
        frame = Image(data=printer.snapshot(printer_name), format="jpeg")
    except Exception as e:
        blockers.append(f"no camera frame ({e}); cannot verify the bed visually")
    if blockers:
        report["result"] = "FAIL: do not print. Fix the blockers, then run preflight_check again."
    else:
        _preflights[(printer_name, str(path))] = time.time()
        _planned_slots[(printer_name, str(path))] = [c["planned_slot"] for c in colour_map]
        report["result"] = ("AUTOMATED CHECKS PASSED. Now inspect the camera frame against the checklist "
                            "in this tool's description, show the user, and get their yes.")
    return [json.dumps(report, indent=1)] + ([frame] if frame else [])


@tool
def print_file(file: str, printer_name: str, user_confirmed: bool = False,
               visual_check_passed: bool = False, h2c_requested: bool = False) -> dict:
    """Send a sliced .gcode.3mf. X1C: headless Bambu Cloud plugin. H2C: only when the user explicitly asked
    for the H2C in this conversation (h2c_requested=true); it goes through Bambu Connect (UI Automation, no
    clicks), which maps the H2C's nozzles/hotends itself, because headless H2C nozzle mapping is unverified.

    user_confirmed: set true ONLY after the user explicitly said yes to printing this file on this
    printer in the current conversation.
    visual_check_passed: set true ONLY if you inspected the latest preflight_check camera frame and the
    plate is present, correct side and type, bed clear, toolhead clean. Requires a passing
    preflight_check for this printer and file within the last 10 minutes.

    Refuses if the printer is busy or its status cannot be read (never interrupts a print).
    X1C: Bambu Studio's own network plugin (WSL, cloudprint/), AMS slots mapped to the
    closest loaded colour. H2C mapping is not validated and fails closed. No UI fallback.
    """
    if not user_confirmed:
        raise ValueError("Ask the user first: state printer, file, plate type, print time and filament, "
                         "get an explicit yes, then call again with user_confirmed=true.")
    if not visual_check_passed:
        raise ValueError("Run preflight_check, inspect the camera frame (plate present, correct side and "
                         "type, bed clear, toolhead clean), then call with visual_check_passed=true.")
    path = Path(file).resolve()
    passed_at = _preflights.get((printer_name, str(path)))
    if not passed_at or time.time() - passed_at > PREFLIGHT_TTL_S:
        raise ValueError(f"No passing preflight_check for {printer_name} + {path.name} in the last "
                         f"{PREFLIGHT_TTL_S // 60} minutes. Run it again and re-inspect the camera frame.")
    if not path.name.endswith(".gcode.3mf"):
        raise ValueError("Slice first: print_file needs a .gcode.3mf from slice_models or Bambu Studio.")
    plates = slicer.read_plates(path)
    if not plates or not all(p["has_gcode"] for p in plates):
        raise ValueError(f"{path.name} has no G-code; slice it first.")
    _check_model(printer_name, plates)
    _check_filaments(printer_name, plates)
    try:
        st = printer.summarize(printer.raw_status(printer_name))
    except Exception as e:
        raise RuntimeError(f"Refusing to send: cannot read {printer_name}'s status, so I can't prove "
                           f"it's idle ({e}).") from e
    if st["busy"]:
        raise RuntimeError(f"Refusing to send: {printer_name} is {st['state']} "
                           f"({st['job']}, {st['percent']}%). Nothing was sent.")
    if any(p.get("printer_model_id") != "BL-P001" for p in plates):
        if not h2c_requested:
            raise RuntimeError("H2C sends go through Bambu Connect and need the user to have explicitly asked "
                               "for the H2C: ask, then call with h2c_requested=true. Nothing was sent.")
        if not BAMBU_CONNECT.exists():
            raise RuntimeError(f"Bambu Connect not found at {BAMBU_CONNECT}; nothing was sent.")
        label = printer.load_config()["printers"][printer_name].get("connect_name")
        _preflights.pop((printer_name, str(path)), None)
        slots = _planned_slots.pop((printer_name, str(path)), None)
        sent = uia_connect.send(path, label, expected_slots=slots)
        return {"sent": sent["sent"], "via": "Bambu Connect (UI Automation), H2C requested by user",
                "printer_state_before": st["state"], "connect_dialog": sent, "plates": plates,
                "next": "Call confirm_print_started now, then monitor_print through the first layers."}
    if not plugin_send.available():
        raise RuntimeError("Headless cloud sender is unavailable; nothing was sent.")
    _preflights.pop((printer_name, str(path)), None)  # one preflight per send, even if it fails
    _planned_slots.pop((printer_name, str(path)), None)
    serial = printer.load_config()["printers"][printer_name]["serial"]
    via, cloud_note, sent = "Bambu cloud, headless (Bambu Studio network plugin)", None, None
    if plugin_send.available():
        # Headless: Bambu Studio's own network plugin does the cloud auth and signing.
        try:
            plate = plates[0]
            trays = {}
            for f in plate["filaments"]:
                best, dist = _closest_tray(f["type"], f["color"], st["ams_trays"])
                if best is None or dist > 60:
                    raise plugin_send.CloudRefused(f"no loaded tray matches {f['type']} {f['color']}")
                trays[f["id"]] = best
            jp = plugin_send.params(path, path, serial, plate["plate"], slicer.read_plate_type(path) or "",
                                    plate["filaments"], trays, float(st["nozzle_diameter"] or 0.4))
            sent = {"sent": True, "cloud_plugin": plugin_send.send(path, serial, jp),
                    "ams": {f["id"]: _slot_label(t) for f, t in zip(plate["filaments"], trays.values())}}
            via = "Bambu cloud, headless (Bambu Studio network plugin)"
        except plugin_send.CloudRefused as e:
            raise RuntimeError(f"Headless cloud send stopped before creating a task: {e}. "
                               "Nothing was sent through Bambu Connect.") from e
        except Exception as e:
            raise RuntimeError(f"Cloud send result is unclear ({e}). NOT falling back, to avoid a double print: "
                               "call confirm_print_started to see whether it started.") from e
    if sent is None:
        raise RuntimeError("Headless cloud send was unavailable; nothing was sent.")
    return {
        "sent": sent["sent"],
        "via": via,
        "cloud_note": cloud_note,
        "printer_state_before": st["state"],
        "connect_dialog": sent,
        "plates": plates,
        "next": "Call confirm_print_started now, then monitor_print through the first layers.",
    }


@tool
def confirm_print_started(printer_name: str, file: str, timeout_s: int = 240) -> dict:
    """After print_file + pressing Send: poll until the printer is actually running THIS file.
    Records it as a Claude-started job (which monitor_print may pause if it goes wrong)."""
    stem = Path(file).name.split(".")[0]
    deadline = time.time() + min(timeout_s, 600)
    st = {}
    while time.time() < deadline:
        st = printer.summarize(printer.raw_status(printer_name))
        if st["print_error"]:
            return {"started": False, "reason": f"print_error {st['print_error']}", "status": st}
        if st["state"] in ("PREPARE", "RUNNING", "SLICING") and _job_matches(st["job"], stem):
            jobs = _started()
            jobs[printer_name] = {"file": str(Path(file).resolve()), "job": st["job"], "task_id": st["task_id"],
                                  "started_at": time.time()}
            STARTED.write_text(json.dumps(jobs, indent=1), encoding="utf-8")
            return {"started": True, "status": st,
                    "next": "Run monitor_print through the first layers (bed-level/calibration can take "
                            "several minutes before extrusion starts)."}
        if st["state"] in ("PREPARE", "RUNNING") and not _job_matches(st["job"], stem):
            return {"started": False, "reason": f"printer is running a different job: {st['job']}", "status": st}
        time.sleep(10)
    # Bambu Connect lets you pick any printer: make sure it didn't go to the other one.
    for other in printer.load_config()["printers"]:
        if other == printer_name:
            continue
        try:
            o = printer.summarize(printer.raw_status(other))
        except Exception:
            continue
        if o["state"] in ("PREPARE", "RUNNING") and _job_matches(o["job"], stem):
            return {"started": False, "reason": f"WRONG PRINTER: this file is running on {other}, not "
                    f"{printer_name}. It was never preflighted there: check {other} now (pause it if the "
                    "bed/plate isn't right) and tell the user.", "status": o}
    return {"started": False, "reason": f"still {st.get('state')} after {timeout_s}s. Was Send pressed in "
            "Bambu Connect? Check the Bambu Connect window.", "status": st}


@tool
def monitor_print(printer_name: str, frames: int = 3, interval_s: float = 45) -> list:
    """Watch a print: status + camera frame, `frames` times (max 6), `interval_s` apart.
    Inspect every frame: is filament going down and sticking, first layer even, no spaghetti,
    blobs, curling, detached parts, or nozzle dragging? `claude_started` says whether this job is one
    Claude started (confirm_print_started). ONLY for such jobs, if something is clearly going wrong,
    pause it right away via the GUI (Bambu Connect printer page or Bambu Studio Device tab > Pause)
    and tell the user. Never pause/stop other jobs unless the user asks."""
    out: list = []
    for i in range(max(1, min(frames, 6))):
        if i:
            time.sleep(interval_s)
        st = printer.summarize(printer.raw_status(printer_name))
        mine = _started().get(printer_name, {})
        info = {k: st[k] for k in ("state", "job", "percent", "layer", "stage", "remaining_min",
                                   "print_error", "hms_errors", "bed", "nozzle")}
        # Same task id as the job confirm_print_started recorded: names can repeat, task ids don't.
        info["claude_started"] = bool(mine.get("task_id")) and st["task_id"] == mine.get("task_id")
        out.append(json.dumps(info))
        try:
            out.append(Image(data=printer.snapshot(printer_name), format="jpeg"))
        except Exception as e:
            out.append(f"camera unavailable: {e}")
        if st["state"] not in ("PREPARE", "RUNNING", "PAUSE", "SLICING"):
            break
    return out


@tool
def connect_status() -> dict:
    """Diagnose Bambu Connect automation (read-only): running, accessibility switch, window state,
    and whether its page can be read (it restores a minimized/hidden window without focusing it,
    then puts it back). Call this when print_file reports a Connect problem."""
    return uia_connect.status()


@tool
def open_in_studio(file: str) -> str:
    """Open a model or project in the Bambu Studio GUI for manual edits (painting, supports, etc.)."""
    os.startfile(str(slicer.STUDIO), arguments=f'"{Path(file).resolve()}"')
    return "Opened in Bambu Studio."


if __name__ == "__main__":
    mcp.run()


