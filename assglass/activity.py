"""Conservative, bounded-memory activity scan for the video blur fast path.

The scan renders every subtitle frame in order so libass history is preserved.
It examines native source geometry only: it never runs a mask builder. A true
result means that blur *may* be needed; false is safe to bypass.
"""
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import struct
import time


def group_may_need_blur(group, frame_size):
    """Return false only when the group's supported mask is provably empty."""
    cfg, images = group.effect_config, group.images
    if cfg.strength == 0 or images is None:
        return False
    if cfg.mode not in ('box', 'organic'):
        return True
    source_stats = getattr(images, 'source_stats', None)
    if source_stats is None:
        # Third-party image owners need not implement the native statistics
        # API. Their builders remain authoritative.
        return True
    bbox, _ = source_stats(cfg.include_types, cfg.opacity_threshold)
    if bbox is None:
        return False
    if cfg.mode == 'box':
        # Match the existing native Box ABI's float32 sigma conversion.
        sigma = struct.unpack('f', struct.pack('f', cfg.feather_sigma))[0]
        radius = math.ceil(3 * sigma)
        halo_x, halo_y = cfg.padding_x + radius, cfg.padding_y + radius
    else:
        radius = math.ceil(3 * cfg.feather_sigma)
        halo_x = cfg.expand_x + 2 * cfg.close + radius
        halo_y = cfg.expand_y + 2 * cfg.close + radius
    width, height = frame_size
    left, top, right, bottom = bbox
    return (left - halo_x < width and top - halo_y < height
            and right + halo_x > 0 and bottom + halo_y > 0)


def _timestamp(timestamp):
    # Transitions are between neighboring frame PTS, far from either sample.
    # Six decimal places safely resolve the supported 60 and 60000/1001 fps
    # profiles without ever converting the rational timestamp to float.
    with localcontext() as context:
        context.prec = max(50, len(str(abs(timestamp.numerator))) + 24)
        seconds = Decimal(timestamp.numerator) / Decimal(timestamp.denominator)
        return format(seconds, '.6f')


def _command(start, end, active):
    state = int(active)
    return ('%s-%s [enter] gblur@assglass_blur enable %d, '
            '[enter] maskedmerge@assglass_merge enable %d;\n'
            % (_timestamp(start), _timestamp(end), state, state)).encode('ascii')


def scan_activity(backend, prepared, ledger, command_path, flags_path, progress=None):
    """Write frame flags and FFmpeg sendcmd transitions without storing frames.

    ``flags_path`` receives exactly one byte (0 or 1) per ledger frame. Every
    command changes both expensive filters at once. The initial command is at
    zero; later commands occur at the midpoint between neighboring frame PTS.
    Intervals have finite adjoining ends, so old sendcmd intervals do not stay
    active for the rest of the video. Only the current interval is retained.
    ``progress``, when supplied, accepts the number of scanned frames.
    """
    command_path, flags_path = Path(command_path), Path(flags_path)
    if command_path.resolve() == flags_path.resolve():
        raise ValueError('activity command and flags paths must be different')
    for path in (command_path, flags_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    started = last_progress = time.monotonic()
    commands_digest, flags_digest = hashlib.sha256(), hashlib.sha256()
    count = active_count = intervals = transitions = 0
    previous_active = previous_stamp = None
    interval_start, last_step = Fraction(0), Fraction(1, 60)

    with command_path.open('wb', buffering=65536) as commands, \
            flags_path.open('wb', buffering=65536) as flags, \
            backend.open(prepared) as session:
        for frame in ledger.frames():
            stamp = frame.pts * frame.time_base
            if previous_stamp is not None and stamp <= previous_stamp:
                raise ValueError('activity scan requires strictly increasing frame PTS')
            last_step = (frame.time_base if previous_stamp is None
                         else stamp - previous_stamp)
            with session.render(frame) as selection:
                active = any(group_may_need_blur(group, frame.frame_size)
                             for group in selection.groups)
            flag = b'\x01' if active else b'\x00'
            flags.write(flag)
            flags_digest.update(flag)
            if previous_active is not None and active != previous_active:
                timestamp = (previous_stamp + stamp) / 2
                command = _command(interval_start, timestamp, previous_active)
                commands.write(command)
                commands_digest.update(command)
                interval_start = timestamp
                transitions += 1
            if active and not previous_active:
                intervals += 1
            previous_active, previous_stamp = active, stamp
            count += 1
            active_count += int(active)
            now = time.monotonic()
            if progress is not None and now - last_progress >= 2:
                progress(count)
                last_progress = now
        end = (previous_stamp if count else Fraction(0)) + max(last_step / 2, Fraction(1, 1000000))
        command = _command(interval_start, end, bool(previous_active))
        commands.write(command)
        commands_digest.update(command)
    if count != ledger.count:
        raise ValueError('activity scan frame count does not match verified ledger')
    if progress is not None:
        progress(count)
    return {'frames': count, 'active_frames': active_count,
            'inactive_frames': count - active_count, 'active_intervals': intervals,
            'transitions': transitions, 'command_sha256': commands_digest.hexdigest(),
            'flags_sha256': flags_digest.hexdigest(),
            'command_path': str(command_path), 'flags_path': str(flags_path),
            'seconds': time.monotonic() - started}
