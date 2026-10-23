"""Byte parsing/formatting shared by the protocol tools."""

import re

from .errors import AD2Error

_HEX_TOKEN = re.compile(r"^(0x)?([0-9a-fA-F]{1,2})$")


def parse_bytes(data, name: str = "data") -> bytes:
    """Accept a list of ints 0-255, or a hex string like "01 ab ff", "01,0xAB", "01abff"."""
    if data is None:
        return b""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return b""
        tokens = re.split(r"[\s,]+", text)
        if len(tokens) == 1 and not tokens[0].lower().startswith("0x"):
            compact = tokens[0]
            if len(compact) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", compact):
                raise AD2Error(f"{name} must be hex bytes such as \"01 ab ff\" or \"01abff\", got {data!r}")
            return bytes.fromhex(compact)
        out = bytearray()
        for token in tokens:
            match = _HEX_TOKEN.match(token)
            if not match:
                raise AD2Error(f"{name} must be hex bytes such as \"01 ab ff\", got token {token!r}")
            out.append(int(match.group(2), 16))
        return bytes(out)
    out = bytearray()
    for item in data:
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255:
            raise AD2Error(f"{name} list must contain integers between 0 and 255, got {item!r}")
        out.append(item)
    return bytes(out)


def format_bytes(data) -> dict:
    """Both representations a caller might want: a hex string and a list of ints."""
    data = bytes(data)
    return {"hex": " ".join(f"{b:02x}" for b in data), "bytes": list(data), "length": len(data)}


def parse_text_or_hex(data: str, data_format: str, name: str = "data") -> bytes:
    if data_format == "text":
        return str(data).encode("utf-8")
    if data_format == "hex":
        return parse_bytes(data, name)
    raise AD2Error("data_format must be 'text' or 'hex'")
