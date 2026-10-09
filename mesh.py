"""Model geometry: load meshes, rotate them, and judge print orientation (overhangs, bed contact, fit)."""
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np

import profiles
import slicer

SUPPORT_NEEDED = np.sin(np.radians(60))  # >60° from vertical: needs support (Bambu default 30° from horizontal)
SUPPORT_RISKY = np.sin(np.radians(45))   # 45-60°: usually prints, may sag


def load(path: str) -> np.ndarray:
    """Triangles as an (N, 3, 3) array. Non-STL files are converted by the Bambu Studio CLI."""
    p = Path(path)
    if p.suffix.lower() == ".3mf":
        return _load_3mf(p)
    if p.suffix.lower() != ".stl":
        with tempfile.TemporaryDirectory() as d:
            # --export-stls writes one STL per object (--export-stl names its file just "stl").
            subprocess.run([str(slicer.STUDIO), "--export-stls", d, str(p.resolve())],
                           capture_output=True, text=True, timeout=300)
            stls = sorted(Path(d).glob("*.stl"))
            if not stls:
                raise RuntimeError(f"Could not convert {p.name} to a mesh")
            return np.concatenate([load(str(s)) for s in stls])
    data = p.read_bytes()
    n = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) == 84 + 50 * n:
        rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                            count=n, offset=84)
        return rec["v"].astype(np.float64)
    verts = [list(map(float, l.split()[1:4])) for l in data.decode(errors="ignore").splitlines()
             if l.strip().startswith("vertex")]
    return np.array(verts, dtype=np.float64).reshape(-1, 3, 3)


def _load_3mf(p: Path) -> np.ndarray:
    """Read a 3MF's placed geometry directly (build items -> components -> meshes, with transforms).
    Avoids the Studio CLI, whose --export-stls occasionally hangs on sliced 3MFs."""
    import xml.etree.ElementTree as ET
    import zipfile
    ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
    pns = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}"
    z = zipfile.ZipFile(p)
    docs: dict = {}

    def doc(name: str):
        name = name.lstrip("/")
        if name not in docs:
            docs[name] = ET.fromstring(z.read(name))
        return docs[name]

    def mat(s: str | None) -> np.ndarray:
        m = np.eye(4)
        if s:
            v = [float(x) for x in s.split()]
            m[:3, :3] = np.array(v[:9]).reshape(3, 3)  # 3MF stores the matrix row-vector style
            m[3, :3] = v[9:12]
        return m

    def obj_tris(model_file: str, oid: str, m: np.ndarray) -> list:
        root = doc(model_file)
        o = next(o for o in root.iter(ns + "object") if o.get("id") == oid)
        out = []
        mesh_el = o.find(ns + "mesh")
        if mesh_el is not None:
            vs = np.array([[float(v.get(a)) for a in "xyz"] for v in mesh_el.iter(ns + "vertex")])
            ts = np.array([[int(t.get(a)) for a in ("v1", "v2", "v3")] for t in mesh_el.iter(ns + "triangle")])
            if len(ts):
                h = np.c_[vs, np.ones(len(vs))] @ m
                out.append(h[:, :3][ts])
        for c in o.iter(ns + "component"):
            out += obj_tris(c.get(pns + "path") or model_file, c.get("objectid"), mat(c.get("transform")) @ m)
        return out

    tris = []
    try:
        for item in doc("3D/3dmodel.model").iter(ns + "item"):
            tris += obj_tris("3D/3dmodel.model", item.get("objectid"), mat(item.get("transform")))
    finally:
        z.close()  # an open handle stops Windows deleting/overwriting the file later
    if not tris:
        raise RuntimeError(f"No geometry found in {p.name}")
    return np.concatenate(tris)


def save(tris: np.ndarray, path: Path) -> None:
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    rec = np.zeros(len(tris), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"], rec["v"] = normals, tris
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(tris)) + rec.tobytes())


def rotation(rx: float, ry: float) -> np.ndarray:
    """Rotate about X by rx, then about Y by ry (degrees). The convention used everywhere here."""
    a, b = np.radians(rx), np.radians(ry)
    Rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    Ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    return Ry @ Rx


def face_down(n: np.ndarray) -> tuple[float, float]:
    """(rx, ry) that turns unit normal n to point straight down (-Z), i.e. puts that face on the bed."""
    a = np.arctan2(n[1], n[2])
    w = n[1] * np.sin(a) + n[2] * np.cos(a)
    b = np.arctan2(n[0], -w)
    return round(float(np.degrees(a)), 2), round(float(np.degrees(b)), 2)


def _metrics(tris: np.ndarray) -> dict:
    v = tris.reshape(-1, 3)
    lo, hi = v.min(0), v.max(0)
    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    area = np.linalg.norm(cross, axis=1) / 2
    nz = np.divide(cross[:, 2], 2 * area, out=np.zeros_like(area), where=area > 0)
    cz = tris[:, :, 2].mean(1)
    on_bed = (cz < lo[2] + 0.3) & (nz < -0.99)
    above = cz > lo[2] + 0.5
    need = area[above & (-nz > SUPPORT_NEEDED)].sum()
    risky = area[above & (-nz > SUPPORT_RISKY) & (-nz <= SUPPORT_NEEDED)].sum()
    contact = area[on_bed].sum()
    size = hi - lo
    return {
        "size_mm": [round(float(x), 1) for x in size],
        "bed_contact_mm2": round(float(contact), 1),
        "overhang_needs_support_mm2": round(float(need), 1),
        "overhang_45_60deg_mm2": round(float(risky), 1),
        "tall_thin_ratio": round(float(size[2] / max(np.sqrt(contact), 1e-6)), 1) if contact else None,
    }


def analyze(path: str, printer_model: str, candidates: int = 6) -> dict:
    tris = load(path)
    if not len(tris):
        raise ValueError(f"{Path(path).name} has no triangles")
    machine = profiles.resolve("machine", f"{slicer.MODELS[printer_model]['machine']} 0.4 nozzle")
    pts = [tuple(map(float, p.split("x"))) for p in machine["printable_area"]]
    bed = (max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts),
           float(machine["printable_height"]))

    cross = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    area = np.linalg.norm(cross, axis=1) / 2
    unit = np.divide(cross, (2 * area)[:, None], out=np.zeros_like(cross), where=area[:, None] > 0)
    # Group faces by normal direction; the biggest groups are the natural "lay flat" candidates.
    keys = np.round(unit, 2)
    uniq, inv = np.unique(keys, axis=0, return_inverse=True)
    sums = np.bincount(inv.ravel(), weights=area)
    order = np.argsort(sums)[::-1]
    dirs = [np.array([0.0, 0.0, -1.0])]  # as modelled
    for i in order:
        n = uniq[i] / (np.linalg.norm(uniq[i]) or 1)
        if all(np.dot(n, d) < 0.98 for d in dirs):
            dirs.append(n)
        if len(dirs) > candidates:
            break

    options = []
    for i, n in enumerate(dirs):
        rx, ry = (0.0, 0.0) if i == 0 else face_down(n)
        m = _metrics(tris @ rotation(rx, ry).T)
        s = m["size_mm"]
        m["fits_bed"] = (min(s[0], s[1]) <= min(bed[:2]) and max(s[0], s[1]) <= max(bed[:2]) and s[2] <= bed[2])
        m.update(option=i, rotate_x=rx, rotate_y=ry, label="as modelled" if i == 0 else f"face {i} down")
        options.append(m)
    # Rank: must fit; must be STABLE (a decent footprint, not tall and thin: a 35 mm stick on a
    # 25 mm2 base won't stay on the plate however little support it needs); then least support
    # (differences under 25 mm2 don't matter); then more bed contact; then lower height.
    max_contact = max(o["bed_contact_mm2"] for o in options) or 1
    for o in options:
        o["stable"] = (o["bed_contact_mm2"] >= min(100, 0.3 * max_contact)
                       and (o["tall_thin_ratio"] is not None and o["tall_thin_ratio"] <= 3))
    ranked = sorted(options, key=lambda o: (not o["fits_bed"], not o["stable"],
                                            round(o["overhang_needs_support_mm2"] / 25),
                                            -o["bed_contact_mm2"], o["size_mm"][2]))
    best = ranked[0]
    advice = []
    # Mesh health: every edge of a closed solid is shared by exactly two triangles.
    q = np.round(tris.reshape(-1, 3), 4)
    _, vid = np.unique(q, axis=0, return_inverse=True)
    vid = vid.reshape(-1, 3)
    edges = np.sort(np.stack([vid[:, [0, 1]], vid[:, [1, 2]], vid[:, [2, 0]]], 1).reshape(-1, 2), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    bad_edges = int((counts != 2).sum())
    if bad_edges:
        advice.append(f"Mesh isn't watertight ({bad_edges} open/non-manifold edges): the slicer may fill or "
                      "drop parts. Check the plate render and layer count; repair the model if it looks wrong.")
    dims = sorted(float(x) for x in np.ptp(tris.reshape(-1, 3), axis=0))
    if dims[-1] < 3:
        advice.append(f"Largest dimension is only {dims[-1]:.2f} mm: probably exported in inches or metres "
                      "(scale 25.4 or 1000?). Ask the user what size it should be.")
    elif dims[-1] > 2000:
        advice.append(f"Largest dimension is {dims[-1]:.0f} mm: probably exported in the wrong units. Ask.")
    if not best["fits_bed"]:
        advice.append(f"Doesn't fit the {printer_model} ({bed[0]:.0f}x{bed[1]:.0f}x{bed[2]:.0f} mm) in any "
                      "candidate orientation: scale it down or split it.")
    if best["overhang_needs_support_mm2"] > 25:
        advice.append("Needs supports even in the best orientation (tree supports are usually best; use "
                      "build-plate-only if supports would land on delicate surfaces).")
    if best["bed_contact_mm2"] < 100 or (best["tall_thin_ratio"] or 0) > 4:
        advice.append("Small footprint or tall and thin: add a brim to stop it detaching or wobbling.")
    return {
        "file": Path(path).name, "triangles": int(len(tris)), "printer": printer_model,
        "watertight": bad_edges == 0,
        "bed_mm": [round(b) for b in bed], "recommended": best, "options": ranked,
        "advice": advice,
        "caveats": "Geometry only. Also weigh strength (layer lines are weakest in Z: orient so loads "
                   "run along layers), which face should look best (top and sides beat supported "
                   "surfaces), and hole/text orientation. Check the plate render after slicing.",
    }


def transformed_copy(path: str, rx: float, ry: float, scale: float, out_dir: Path,
                     z_drop: float | None = None) -> str:
    """Write a rotated (rx then ry) and uniformly scaled STL copy. Scaling is about the model's own
    origin, so separately exported parts of one model keep their relative positions."""
    tris = (load(path) @ rotation(rx, ry).T) * scale
    # Drop it onto the plate like Bambu Studio does on load. Parts of one assembled model share one
    # drop (z_drop) so they stay together.
    tris[:, :, 2] -= tris[:, :, 2].min() if z_drop is None else z_drop
    out = out_dir / f"{Path(path).name.split('.')[0]}_rx{rx:g}_ry{ry:g}_s{scale:g}.stl"
    save(tris, out)
    return str(out)


def min_z_after(path: str, rx: float, ry: float, scale: float) -> float:
    return float(((load(path) @ rotation(rx, ry).T) * scale)[:, :, 2].min())


def z_extent(paths: list[str]) -> float:
    """Height of the union of these meshes as positioned (assembled parts share coordinates)."""
    zs = np.concatenate([load(p)[:, :, 2].ravel() for p in paths])
    return float(zs.max() - zs.min())
