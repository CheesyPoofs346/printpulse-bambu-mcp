"""Resolve Bambu Studio system presets (which use `inherits` chains) into flat JSON the CLI accepts."""
import json
import os
from functools import cache
from pathlib import Path

def _newest_system_dir() -> Path:
    """Bundled presets (Program Files) vs ones Studio downloaded into AppData: use the newer set."""
    def version(root: Path) -> tuple:
        try:
            v = json.loads((root / "BBL.json").read_text(encoding="utf-8"))["version"]
            return tuple(int(x) for x in v.split("."))
        except (OSError, KeyError, ValueError):
            return ()
    roots = [Path(os.environ.get("ProgramFiles", r"C:/Program Files")) / "Bambu Studio" / "resources" / "profiles",
             Path(os.environ["APPDATA"]) / "BambuStudio" / "system"]
    return max(roots, key=version) / "BBL"


SYSTEM_DIR = _newest_system_dir()
FLAT_DIR = Path(__file__).parent / ".flat_profiles"


@cache
def _index(kind: str) -> dict[str, Path]:
    """name -> file for one preset kind (machine/process/filament)."""
    out = {}
    for p in (SYSTEM_DIR / kind).glob("*.json"):
        try:
            out[json.loads(p.read_text(encoding="utf-8"))["name"]] = p
        except (KeyError, ValueError):
            pass
    return out


def resolve(kind: str, name: str) -> dict:
    idx = _index(kind)
    if name not in idx:
        raise ValueError(f"No {kind} preset named {name!r}")
    data = json.loads(idx[name].read_text(encoding="utf-8"))
    parent = data.pop("inherits", "")
    merged = resolve(kind, parent) if parent else {}
    merged.update(data)
    # Studio 2.8 keeps each printer's G-code macros in separate "<preset> template <key>" files
    # (instantiation false). Without them a preset inherits fdm_machine_common's 577-char generic start,
    # which never loads the first AMS filament: prints start with no filament fed (2026-09-23).
    prefix = f"{name} template "
    for tname, tpath in idx.items():
        if tname.startswith(prefix):
            key = tname[len(prefix):]
            tdata = json.loads(tpath.read_text(encoding="utf-8"))
            if key in tdata:
                merged[key] = tdata[key]
    return merged


def flat_file(kind: str, name: str) -> str:
    """Write the resolved preset to disk (cached by name) and return its path."""
    FLAT_DIR.mkdir(exist_ok=True)
    data = resolve(kind, name)
    data["from"] = "system"
    out = FLAT_DIR / f"{kind}__{name}.json".replace("/", "_").replace("@", "at_")
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return str(out)


def names(kind: str, contains: str = "") -> list[str]:
    c = contains.lower()
    return sorted(n for n in _index(kind) if c in n.lower())
