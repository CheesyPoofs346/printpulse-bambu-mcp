"""Send a print through Bambu's own cloud API with the user's account token (the same kind of request
the Handy app makes). No keys are extracted or signatures forged: if Bambu's cloud refuses, it refuses.
The token lives in cloud_token.json, filled in by the user; it's never printed or logged here."""
import json
from pathlib import Path

TOKEN_FILE = Path(__file__).parent / "cloud_token.json"
PLACEHOLDER = "PASTE_YOUR_BAMBU_ACCESS_TOKEN_HERE"


class CloudRefused(RuntimeError):
    """The cloud clearly said no before any print could start: safe to fall back to Connect."""


def token() -> str | None:
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(json.dumps({"access_token": PLACEHOLDER}, indent=2) + "\n", encoding="utf-8")
        return None
    t = json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("access_token", "").strip()
    return t if t and t != PLACEHOLDER else None


def send(file: Path, serial: str) -> dict:
    from bambulab import BambuClient
    from bambulab.client import BambuAPIError
    client = BambuClient(token=token())
    try:
        up = client.upload_file(str(file))
    except BambuAPIError as e:
        raise CloudRefused(f"cloud upload refused: {e}") from e
    try:
        job = client.start_print_job(device_id=serial, file_id=up.get("file_id"),
                                     file_name=file.name, file_url=up.get("file_url"))
    except BambuAPIError as e:
        # An HTTP error status means the request was rejected: nothing was started.
        if any(code in str(e) for code in ("400", "401", "403", "404", "405", "422")):
            raise CloudRefused(f"cloud print refused: {e}") from e
        raise  # anything else is ambiguous: don't fall back (could print twice)
    return {"upload": {k: up.get(k) for k in ("file_id", "file_size")}, "job": job}
