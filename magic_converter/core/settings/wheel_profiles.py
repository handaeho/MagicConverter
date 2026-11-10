"""Resolve visible slots from the same recipes that will be submitted on drop."""

from dataclasses import replace

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.file_router import ALL_ACTIONS, Slot, normalize_paths, slots_for
from magic_converter.core.settings.preferences import Preferences


