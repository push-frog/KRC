# -*- coding: utf-8 -*-
import asyncio
import sys

if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass

from .client import KeeneticAdvancedClient
from .gui import KeeneticAdvancedGUI
from .util import (
    BATCH_CHUNK_SIZE,
    DEFAULT_INTERFACE,
    cidr_to_mask,
    decode_output,
    mask_to_cidr,
    routes_to_bat_lines,
    save_routes_to_file,
    strip_ansi,
)

__all__ = [
    "BATCH_CHUNK_SIZE",
    "DEFAULT_INTERFACE",
    "KeeneticAdvancedClient",
    "KeeneticAdvancedGUI",
    "cidr_to_mask",
    "decode_output",
    "mask_to_cidr",
    "routes_to_bat_lines",
    "save_routes_to_file",
    "strip_ansi",
]
