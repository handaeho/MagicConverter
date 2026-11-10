"""Observe the real source GUI's input/target lifetime; never synthesize input.

This runner patches only diagnostic wrappers, not event results or timing rules.
Its reports are source evidence and must not be reported as frozen EXE evidence.
"""
from __future__ import annotations

import argparse
import datetime
import functools
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trace', type=Path, required=True)
    args, application_args = parser.parse_known_args()
    trace = args.trace.resolve()
    assert trace.is_relative_to(ROOT / '.test-artifacts')
    stream = trace.open('x', encoding='utf-8', buffering=1)
    started = time.monotonic()

    def record(event, **values):
        stream.write(json.dumps({'event': event, 'elapsed': round(time.monotonic()-started, 6),
            'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), **values},
            ensure_ascii=False) + '\n')

    from magic_converter.core.platform.hook_manager import HookManager
    from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
    source_files = ['magic_converter/main.py', 'magic_converter/core/platform/hook_manager.py',
                    'magic_converter/ui/shell/radial_wheel.py',
                    'magic_converter/core/automation/native_drop.py']
    record('source_identity', frozen=False, synthesized_input=False,
           sha256={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_files})

    original_init = HookManager.__init__

    @functools.wraps(original_init)
    def hook_init(self, *positional, **keywords):
        original_init(self, *positional, **keywords)
        original_reader = self._reader
        last = None

        def reader():
            nonlocal last
            state = original_reader()
            keys = (state.left, state.shift, state.control, state.escape)
            if keys != last and (self._wheel_pending or self._mode is not None
                                 or state.left and (state.shift or state.control)):
                record('physical_keys', left=state.left, shift=state.shift, control=state.control,
                       escape=state.escape, mode=self._mode, pending=self._wheel_pending)
                last = keys
            return state

        self._reader = reader
        self.wheel_requested.connect(lambda mode, point: record('wheel_requested', mode=mode))
        self.dismiss_requested.connect(lambda reason: record('dismiss_requested', reason=reason))

    HookManager.__init__ = hook_init

    def wheel_state(self):
        return dict(visible=self.isVisible(), drag_inside=self._drag_inside,
            released_inside=self._released_inside, option=self._drag_preferences.release_modifier_inside,
            native_modifiers=self._native_modifiers.value, ready=self._ready_for_drop(),
            path_count=len(self.paths), hovered=self.hovered, timer=self._release_timer.isActive())

    def wrap(name):
        original = getattr(RadialWheelWindow, name)

        @functools.wraps(original)
        def observed(self, *positional, **keywords):
            details = {}
            if name == 'request_dismiss':
                details['reason'] = positional[0]
            if name in {'dragEnterEvent', 'dropEvent'}:
                event = positional[0]
                details.update(modifiers=event.modifiers().value, proposed=event.proposedAction().value,
                               possible=event.possibleActions().value)
            record(name + ':before', **wheel_state(self), **details)
            try:
                result = original(self, *positional, **keywords)
            except Exception as error:
                record(name + ':error', error=type(error).__name__ + ': ' + str(error))
                raise
            if name in {'dragEnterEvent', 'dropEvent'}:
                details.update(accepted=positional[0].isAccepted(), effect=positional[0].dropAction().value)
            record(name + ':after', **wheel_state(self), **details)
            return result

        setattr(RadialWheelWindow, name, observed)

    for name in ['show_for_drag', 'request_dismiss', 'dismiss', 'dragEnterEvent',
                 'dragLeaveEvent', 'dropEvent', 'showEvent', 'hideEvent']:
        wrap(name)
    from magic_converter.main import main as application_main
    try:
        return application_main(application_args)
    finally:
        record('source_exit')
        stream.close()


if __name__ == '__main__':
    raise SystemExit(main())
