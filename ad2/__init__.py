"""Instrument layer for the Analog Discovery 2 MCP server.

Each module wraps one instrument on top of the vendored WF_SDK package and
raises AD2Error for anything the caller can fix (bad arguments, device state).
"""
