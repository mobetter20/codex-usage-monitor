#!/usr/bin/env python3
"""Compatibility wrapper for hk.py audit."""

from __future__ import annotations

from hk import run_legacy_audit


if __name__ == "__main__":
    raise SystemExit(run_legacy_audit())
