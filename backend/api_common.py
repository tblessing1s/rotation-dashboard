"""Tiny helpers shared by every blueprint module."""
from __future__ import annotations

from flask import jsonify


def _err(e: Exception, code: int = 500):
    return jsonify({"error": str(e)}), code
