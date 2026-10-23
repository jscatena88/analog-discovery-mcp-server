"""Write full-resolution captures to disk so tool results stay small."""

import csv
import os
from datetime import datetime


def capture_dir() -> str:
    return os.environ.get("AD2_CAPTURE_DIR") or os.path.join(os.getcwd(), "captures")


def write_capture_csv(path: str | None, sampling_frequency: float, data: dict[int, list[float]]) -> str:
    """Columns: time_s, ch<N>_v for each captured channel. Returns the absolute path written."""
    if not path:
        stamp = datetime.now().strftime("capture_%Y%m%d_%H%M%S_%f")
        path = os.path.join(capture_dir(), stamp + ".csv")
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    channels = sorted(data)
    length = len(data[channels[0]]) if channels else 0
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s"] + [f"ch{c}_v" for c in channels])
        for i in range(length):
            writer.writerow([f"{i / sampling_frequency:.9g}"] + [f"{data[c][i]:.6g}" for c in channels])
    return path
