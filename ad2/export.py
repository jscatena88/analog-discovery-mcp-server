"""Write full-resolution captures to disk so tool results stay small."""

import csv
import os
from datetime import datetime


def capture_dir() -> str:
    return os.environ.get("AD2_CAPTURE_DIR") or os.path.join(os.getcwd(), "captures")


def write_rows(path: str | None, prefix: str, header: list[str], rows) -> str:
    """Write a CSV; a missing path becomes <capture_dir>/<prefix>_<timestamp>.csv. Returns the absolute path."""
    if not path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(capture_dir(), f"{prefix}_{stamp}.csv")
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def write_capture_csv(path: str | None, sampling_frequency: float, data: dict[int, list[float]]) -> str:
    """Columns: time_s, ch<N>_v for each captured channel. Returns the absolute path written."""
    channels = sorted(data)
    length = len(data[channels[0]]) if channels else 0
    rows = ([f"{i / sampling_frequency:.9g}"] + [f"{data[c][i]:.6g}" for c in channels] for i in range(length))
    return write_rows(path, "capture", ["time_s"] + [f"ch{c}_v" for c in channels], rows)
