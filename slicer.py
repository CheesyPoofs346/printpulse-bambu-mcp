"""Headless slicing through the Bambu Studio CLI using its bundled system presets."""
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import profiles

STUDIO = Path(r"C:\Program Files\Bambu Studio\bambu-studio.exe")
OUT_DIR = Path(__file__).parent / "prints"

MODELS = {
    "x1c": {"machine": "Bambu Lab X1 Carbon", "tag": "X1C"},
    "h2c": {"machine": "Bambu Lab H2C", "tag": "H2C"},
}
# CLI noise that also appears on successful GUI-equivalent slices.
BENIGN = ("nozzle_volume_type not found", "parent path", "Invalid T command")


def _preset(kind: str, base: str, model: str, nozzle: float) -> str:
    if base in profiles.names(kind):
        return base
    tag = MODELS[model]["tag"]
    # Un-suffixed presets are the 0.4 mm ones; other nozzles only get their own variants
    # (a 0.4 process on a 0.6 nozzle is rejected by the slicer anyway, with a cryptic error).
    tries = [f"{base} @BBL {tag}"] if nozzle == 0.4 else [f"{base} @BBL {tag} {nozzle} nozzle"]
    if nozzle != 0.4 and kind == "filament":
        tries.append(f"{base} @BBL {tag}")  # some filaments share one preset across nozzles
    for t in tries:
        if t in profiles.names(kind):
            return t
    if nozzle != 0.4 and kind == "process":
        fits = [n for n in profiles.names(kind, f"@BBL {tag} {nozzle} nozzle")]
        raise ValueError(f"No {kind} preset {base!r} for a {nozzle} mm nozzle. Options: {fits}")
    near = profiles.names(kind, f"@BBL {tag}")
    near = [n for n in near if base.lower().split()[0] in n.lower()][:15] or near[:15]
    raise ValueError(f"No {kind} preset {tries[0]!r}. Close matches: {near}")


def _apply_overrides(files: list[str], overrides: dict) -> list[str]:
    """Write each override into every preset file that defines that key (process, each filament
    slot, machine). Returns keys no preset defines."""
    used = set()
    for f in files:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        for k, v in overrides.items():
            if k in data:
                cur = data[k]
                data[k] = [str(v)] * len(cur) if isinstance(cur, list) else str(v)
                used.add(k)
        Path(f).write_text(json.dumps(data, indent=1), encoding="utf-8")
    return [k for k in overrides if k not in used]


def _stamp_model_id(path: Path, model_id: str) -> None:
    """The CLI leaves printer_model_id blank; the GUI and printers expect it set."""
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Metadata/slice_info.config":
                data = data.replace(b'key="printer_model_id" value=""',
                                    f'key="printer_model_id" value="{model_id}"'.encode())
            zout.writestr(item, data)
    tmp.replace(path)


def read_settings(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        if "Metadata/project_settings.config" not in z.namelist():
            return {}
        return json.loads(z.read("Metadata/project_settings.config"))


def read_plate_type(path: Path) -> str | None:
    with zipfile.ZipFile(path) as z:
        if "Metadata/project_settings.config" not in z.namelist():
            return None
        return json.loads(z.read("Metadata/project_settings.config")).get("curr_bed_type")


def gcode_max_z(path: Path) -> float | None:
    """Tallest plate's max Z from the G-code headers."""
    zs = []
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.startswith("Metadata/plate_") and n.endswith(".gcode"):
                head = z.read(n)[:200000].decode(errors="ignore")
                for line in head.splitlines():
                    if line.startswith("; max_z_height"):
                        zs.append(float(line.split(":")[1]))
                        break
    return max(zs) if zs else None


def plate_preview(path: Path, plate: int = 1) -> bytes | None:
    """The slicer's rendered picture of a plate (models as placed on the bed)."""
    with zipfile.ZipFile(path) as z:
        name = f"Metadata/plate_{plate}.png"
        return z.read(name) if name in z.namelist() else None


def _count_support(path: Path, gname: str) -> int:
    with zipfile.ZipFile(path) as z:
        return z.read(gname).count(b"; FEATURE: Support")


def read_plates(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        info = z.read("Metadata/slice_info.config").decode() if "Metadata/slice_info.config" in names else ""
    plates = []
    for block in re.findall(r"<plate>(.*?)</plate>", info, re.S):
        meta = dict(re.findall(r'key="(\w+)" value="([^"]*)"', block))
        fil = [dict(re.findall(r'(\w+)="([^"]*)"', f)) for f in re.findall(r"<filament ([^>]*)/>", block)]
        idx = meta.get("index")
        gname = f"Metadata/plate_{idx}.gcode"
        plates.append({
            "plate": int(idx) if idx else None,
            "has_gcode": gname in names,
            # The plate render shows models only; this is how much of the toolpath is support.
            "support_blocks": _count_support(path, gname) if gname in names else None,
            "print_time_min": round(int(meta.get("prediction", 0) or 0) / 60, 1),
            "filament_g": float(meta.get("weight", 0) or 0),
            "printer_model_id": meta.get("printer_model_id"),
            "filaments": [{k: f.get(k) for k in ("id", "type", "color", "used_g", "used_m")} for f in fil],
        })
    return plates


def slice_models(model: str, inputs: list[str], copies: list[int] | None, scale: float | None,
                 rotate_z: float | None, orient: bool, process: str, filament: str,
                 nozzle: float, overrides: dict | None, use_project_settings: bool,
                 name: str | None, plate_type: str = "Textured PEI Plate",
                 project: dict | None = None, filament_slots: list[dict] | None = None,
                 input_filaments: list[int] | None = None, assemble: bool = False) -> dict:
    """`project`: settings no preset defines (e.g. brim_type); injected, then verified in the output.
    Multicolour: `filament_slots` = [{"preset", "color"}, ...] (slot 1, 2, ...); `input_filaments`
    = slot per input; `assemble` merges all inputs into ONE object keeping their positions (parts of
    one model exported separately, e.g. body + lettering)."""
    project = {"curr_bed_type": plate_type, **(project or {})}
    if model not in MODELS:
        raise ValueError(f"model must be one of {list(MODELS)}")
    files = [str(Path(i).resolve()) for i in inputs]
    for f in files:
        if not Path(f).is_file():
            raise FileNotFoundError(f)
    OUT_DIR.mkdir(exist_ok=True)
    stem = name or Path(files[0]).name.split(".")[0]
    out = OUT_DIR / f"{stem}_{model}.gcode.3mf"

    with tempfile.TemporaryDirectory() as work:
        args = [str(STUDIO), "--debug", "1", "--slice", "0", "--outputdir", work]
        unapplied: list[str] = []
        if not use_project_settings:
            machine = _preset("machine", f"{MODELS[model]['machine']} {nozzle} nozzle", model, nozzle)
            proc = _preset("process", process, model, nozzle)
            slots = filament_slots or [{"preset": filament, "color": None}]
            fils = [_preset("filament", s.get("preset") or filament, model, nozzle) for s in slots]
            flat = []
            for kind, n in (("process", proc), ("machine", machine)):
                dst = Path(work) / f"{kind}.json"
                shutil.copy(profiles.flat_file(kind, n), dst)
                flat.append(str(dst))
            fil_files = []
            for i, (s, n) in enumerate(zip(slots, fils), 1):
                data = json.loads(Path(profiles.flat_file("filament", n)).read_text(encoding="utf-8"))
                if s.get("color"):
                    data["filament_colour"] = data["default_filament_colour"] = [s["color"]]
                # One file per slot: passing the same file twice makes the CLI collapse the slots.
                dst = Path(work) / f"filament_{i}.json"
                dst.write_text(json.dumps(data, indent=1), encoding="utf-8")
                fil_files.append(str(dst))
            if overrides:
                unapplied = _apply_overrides(flat + fil_files, overrides)
            # Project-level settings (bed type, brim type) aren't in any preset; the CLI honours them
            # from the process file.
            proc_data = json.loads(Path(flat[0]).read_text(encoding="utf-8"))
            proc_data.update({k: str(v) for k, v in project.items()})
            Path(flat[0]).write_text(json.dumps(proc_data, indent=1), encoding="utf-8")
            args += ["--load-settings", f"{flat[1]};{flat[0]}", "--load-filaments", ";".join(fil_files)]
            fil = fils[0] if len(fils) == 1 else fils
        # --ensure-on-bed: the CLI scales/rotates about the model centre, which otherwise sinks
        # enlarged models into the bed (silently truncated) and floats shrunk ones (slice error).
        args += ["--arrange", "1", "--orient", "1" if orient else "0", "--ensure-on-bed"]
        if scale:
            args += ["--scale", str(scale)]
        if rotate_z:
            args += ["--rotate", str(rotate_z)]
        multi = assemble or input_filaments or (filament_slots and len(filament_slots) > 1)
        if multi and not use_project_settings:
            # Per-object/per-part filaments only work through an assemble list (plain CLI
            # --load-filament-ids segfaults Studio 2.8 with several objects).
            n = len(files)
            slot_of = input_filaments or [1] * n
            if len(slot_of) != n or any(not 1 <= s <= len(slots) for s in slot_of):
                raise ValueError(f"input_filaments needs one slot (1..{len(slots)}) per input")
            cnt = copies or [1] * n
            if assemble and len(set(cnt)) != 1:
                raise ValueError("assembled parts must all have the same copies count")
            objs = [{"path": f.replace("\\", "/"), "count": c, "filaments": [s],
                     "assemble_index": list(range(1, c + 1)) if assemble else [0] * c}
                    for f, s, c in zip(files, slot_of, cnt)]
            lst = Path(work) / "assemble.json"
            lst.write_text(json.dumps({"plates": [{"plate_name": stem, "need_arrange": True,
                                                   "objects": objs}]}), encoding="utf-8")
            args += ["--load-assemble-list", str(lst), "--export-3mf", out.name]
        else:
            if copies and any(c > 1 for c in copies):
                args += ["--clone-objects", ",".join(str(c) for c in copies)]
            args += ["--export-3mf", out.name, *files]

        r = subprocess.run(args, capture_output=True, text=True, timeout=300)
        log = (r.stdout + r.stderr).splitlines()
        problems = [l.split("]")[-1].strip() for l in log
                    if re.search(r"\[(error|fatal)\]", l) and not any(b in l for b in BENIGN)]
        produced = Path(work) / out.name
        # Studio 2.8+ prints nothing to the console; its verdict is in result.json.
        rj = Path(work) / "result.json"
        result = json.loads(rj.read_text(encoding="utf-8")) if rj.exists() else {}
        if r.returncode != 0 or not produced.exists() or result.get("return_code", 0) != 0:
            why = result.get("error_string") or problems[-8:] or log[-8:] or "no result.json (bad input or settings?)"
            raise RuntimeError(f"Slicing failed (exit {r.returncode}): {why}")
        shutil.move(produced, out)

    with zipfile.ZipFile(out) as z:
        settings = json.loads(z.read("Metadata/project_settings.config"))
    # The G-code is only valid for the machine it was sliced with. A project .3mf keeps its own
    # machine (e.g. a MakerWorld file made for a P1S), so check before labelling it for `model`.
    baked = settings.get("printer_model")
    if baked != MODELS[model]["machine"]:
        out.unlink()
        raise ValueError(f"This file slices for {baked!r}, not {MODELS[model]['machine']!r}. Re-slice "
                         "without use_project_settings, or open it in Bambu Studio and switch printer.")
    model_id = profiles.resolve("machine", MODELS[model]["machine"]).get("model_id", "")
    _stamp_model_id(out, model_id)
    plates = read_plates(out)
    bed = settings.get("curr_bed_type")
    if not use_project_settings:
        rejected = {k: settings.get(k) for k, v in project.items() if settings.get(k) != str(v)}
        if rejected:
            out.unlink()
            raise ValueError(f"Settings not accepted by the slicer (file has {rejected}). Plate types: Cool "
                             "Plate, Engineering Plate, High Temp Plate, Textured PEI Plate. Brim: auto_brim, "
                             "outer_only, no_brim.")
    if not plates or not all(p["has_gcode"] for p in plates):
        raise RuntimeError(f"Sliced file has plates without G-code: {plates}")
    for p, rp in zip(plates, result.get("sliced_plates", [])):
        # total - main = filament purged on colour/material changes (prime tower + flushing)
        p["filament_changes"] = rp.get("filament_change_times")
        p["purge_waste_g"] = round(sum(f.get("total_used_g", 0) - f.get("main_used_g", 0)
                                       for f in rp.get("filaments", [])), 2)
    res = {"file": str(out), "printer": model, "plate_type": bed, "plates": plates, "warnings": problems[:10],
           "nozzle_diameter": settings.get("nozzle_diameter"),
           "supports": {k: settings.get(k) for k in ("enable_support", "support_type", "support_on_build_plate_only")},
           "brim_type": settings.get("brim_type")}
    if not use_project_settings:
        res["presets"] = {"process": proc, "filament": fil, "machine": machine}
    if unapplied:
        res["unknown_override_keys"] = unapplied
    return res


def add_layer_colour_changes(sliced: Path, model: str, changes: list[dict], colours: list[str],
                             name: str) -> dict:
    """Colour by height: from `at_mm` upward print with filament slot `filament`.
    Re-slices `sliced` as a project with Bambu's per-layer tool-change list injected. The change is
    placed on the first layer whose top is ABOVE at_mm, so the layer ending at at_mm (e.g. the top
    surface of a base) keeps the previous colour."""
    s = read_settings(sliced)
    first, lh = float(s["initial_layer_print_height"]), float(s["layer_height"])
    items = []
    for c in sorted(changes, key=lambda c: c["at_mm"]):
        k = max(0, int((c["at_mm"] - first) / lh + 1e-6) + 1)
        top = round(first + k * lh, 3)
        slot = int(c["filament"])
        if not 1 <= slot <= len(colours):
            raise ValueError(f"colour change filament {slot} isn't one of the {len(colours)} filament slots")
        items.append(f'<layer top_z="{top}" type="2" extruder="{slot}" color="{colours[slot - 1]}" extra="" '
                     'gcode="tool_change"/>')
    xml = "\n".join(['<?xml version="1.0" encoding="utf-8"?>', "<custom_gcodes_per_layer>", "<plate>",
                     '<plate_info id="1"/>', *items, '<mode value="MultiAsSingle"/>', "</plate>",
                     "</custom_gcodes_per_layer>", ""])
    proj = sliced.with_name(sliced.name.replace(".gcode.3mf", "_layers.3mf"))
    with zipfile.ZipFile(sliced) as zin, zipfile.ZipFile(proj, "w", zipfile.ZIP_DEFLATED) as zout:
        for it in zin.infolist():
            if it.filename.endswith((".gcode", ".md5")) or it.filename == "Metadata/custom_gcode_per_layer.xml":
                continue
            zout.writestr(it, zin.read(it.filename))
        zout.writestr("Metadata/custom_gcode_per_layer.xml", xml)
    try:
        res = slice_models(model, [str(proj)], None, None, None, False, "", "", 0.4, None, True, name)
    finally:
        proj.unlink(missing_ok=True)
    res["colour_changes_at_layer_top_mm"] = [float(i.split('"')[1]) for i in items]
    return res


def model_info(path: str) -> dict:
    import mesh  # local: mesh imports this module
    tris = mesh.load(path)
    v = tris.reshape(-1, 3)
    cross = __import__("numpy").cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    volume = float((tris[:, 0] * cross).sum() / 6)
    return {"file": Path(path).name, "triangles": int(len(tris)),
            "size_mm": [round(float(x), 2) for x in v.max(0) - v.min(0)],
            "volume_cm3": round(abs(volume) / 1000, 2)}
