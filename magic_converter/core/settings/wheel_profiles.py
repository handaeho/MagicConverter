"""Resolve visible slots from the same recipes that will be submitted on drop."""

from dataclasses import replace

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.file_router import ALL_ACTIONS, Slot, normalize_paths, slots_for
from magic_converter.core.settings.preferences import Preferences


def profile_slots(paths, mode, preferences: Preferences, ffmpeg=True, presentation=True, rhwp=None):
    if rhwp is None:
        from magic_converter.core.engines.rhwp_engine import locate
        rhwp = locate() is not None
    selected = getattr(preferences, mode + "_profile")
    if not selected:
        return slots_for(paths, mode, ffmpeg, presentation, rhwp)
    profile = next(profile for profile in preferences.profiles if profile.id == selected)
    recipes = {preset.id: preset for preset in preferences.presets}
    files = normalize_paths(paths)
    catalog = create_catalog(ffmpeg_available=ffmpeg, presentation_available=presentation,
                             rhwp_available=rhwp)
    slots = []
    for preset_id in profile.preset_ids:
        preset = recipes[preset_id]
        reason = "파일을 드래그하세요" if not files else ""
        for source in files:
            available = catalog.assess(source, preset.action_key)
            if available.route is None:
                reason = f"{source.name}: {available.reason}"
                break
        rule = preset.output or preferences.output
        description = ALL_ACTIONS[preset.action_key].description
        if preset.action_key.startswith("tool:"):
            from magic_converter.core.actions.tool_definitions import tool_summary
            description += " · " + tool_summary(preset.action_key, preset.tools)
        if preset.action_key.startswith("image:"):
            description += f" · 품질 {preset.options.image_quality}"
            if preset.options.image_max_edge:
                description += f" · 긴 변 {preset.options.image_max_edge}px"
        if preset.options.video_crf is not None:
            description += f" · CRF {preset.options.video_crf}"
        if preset.options.audio_bitrate is not None:
            description += f" · {preset.options.audio_bitrate}kbps"
        description += " · " + ("원본 폴더" if rule.location == "source" else rule.directory)
        action = replace(ALL_ACTIONS[preset.action_key], label=preset.name, description=description)
        slots.append(Slot(action, not reason, reason, preset))
    return tuple(slots)
