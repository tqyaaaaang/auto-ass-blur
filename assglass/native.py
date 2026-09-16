"""Owned native operations with optional libass EventImages ABI detection.

ctypes crosses the ABI once per render/build/encode operation. Linked-list walks,
bitmap copies, Gaussian filtering and chroma sampling stay entirely in C++.
"""
import ctypes as C
from dataclasses import dataclass
import importlib.util
import importlib.machinery
import hashlib
import os
import tempfile
from pathlib import Path
import sys
import threading
from typing import Optional

_LIB = None
_LOAD_LOCK = threading.Lock()
DEFAULT_BUDGET = 64 * 1024 * 1024
IMAGE_TYPES = {'character': 0, 'outline': 1, 'shadow': 2}


def library():
    global _LIB
    if _LIB is not None:
        return _LIB
    with _LOAD_LOCK:
        if _LIB is not None:
            return _LIB
        package = Path(__file__).resolve().parent
        root = package.parent / 'native'
        if (root / 'assglass.cpp').is_file() and (root / 'build.py').is_file():
            suffix = '.dylib' if sys.platform == 'darwin' else '.so'
            path = root / ('libassglass' + suffix)
            inputs = [root / 'assglass.cpp', root / 'build.py']
            private_pc = package.parent / '.tools/libass-event-images/lib/pkgconfig/libass.pc'
            if private_pc.exists():
                inputs.append(private_pc)
            newest_input = max(item.stat().st_mtime for item in inputs)
            if not path.exists() or path.stat().st_mtime < newest_input:
                spec = importlib.util.spec_from_file_location('_assglass_build', str(root / 'build.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not os.access(str(root), os.W_OK):
                    digest = hashlib.sha256((root / 'assglass.cpp').read_bytes()).hexdigest()[:20]
                    cache = Path(tempfile.gettempdir()) / ('assglass-native-' + str(os.getuid()))
                    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
                    path = cache / ('libassglass-' + digest + suffix)
                if not path.exists() or path.stat().st_mtime < newest_input:
                    fd, temporary = tempfile.mkstemp(prefix='assglass-build-', suffix=suffix, dir=str(path.parent))
                    os.close(fd)
                    try:
                        module.build(temporary)
                        os.replace(temporary, str(path))
                    finally:
                        if os.path.exists(temporary):
                            os.unlink(temporary)
        else:
            candidates = [package / ('_assglass_native' + suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES]
            path = next((candidate for candidate in candidates if candidate.is_file()), None)
            if path is None:
                raise RuntimeError('The installed native bridge is missing; reinstall assglass with a C++ compiler, pkg-config and libass development files')
        lib = C.CDLL(str(path))
        P, I, S, F, D, U, Q = C.c_void_p, C.c_int, C.c_size_t, C.c_float, C.c_double, C.c_uint32, C.c_int64
        bytep, floatp, intp = C.POINTER(C.c_uint8), C.POINTER(F), C.POINTER(I)
        signatures = {
            'ag_error': (C.c_char_p, []),
            'ag_budget_new': (P, [S]), 'ag_budget_free': (None, [P]),
            'ag_budget_used': (S, [P]), 'ag_budget_peak': (S, [P]),
            'ag_libass_version': (I, []), 'ag_libass_path': (C.c_char_p, []),
            'ag_event_export_abi': (C.c_uint, []),
            'ag_time_ms': (Q, [Q, Q, Q]),
            'ag_session_new': (P, [C.c_char_p, S, I, I, C.c_char_p, P]),
            'ag_session_free': (None, [P]), 'ag_session_logs': (C.c_char_p, [P]),
            'ag_session_render': (P, [P, Q]),
            'ag_session_event_count': (I, [P]),
            'ag_session_event_metadata': (I, [P, I, C.POINTER(Q), intp, C.POINTER(C.c_char_p)]),
            'ag_session_enable_events': (I, [P, intp, S]),
            'ag_session_render_events': (P, [P, Q]),
            'ag_event_frame_free': (None, [P]),
            'ag_event_frame_changed': (I, [P]),
            'ag_event_frame_take': (P, [P, I, P]),
            'ag_images_new': (P, [P]),
            'ag_images_combine': (P, [C.POINTER(P), S, P]),
            'ag_images_append': (I, [P, I, I, I, I, I, U, I, bytep, S]),
            'ag_images_count': (S, [P]), 'ag_images_changed': (I, [P]),
            'ag_images_digest': (C.c_uint64, [P]),
            'ag_images_get': (I, [P, S, intp, C.POINTER(U), C.POINTER(bytep)]),
            'ag_images_stats': (I, [P, I, D, intp, C.POINTER(U)]),
            'ag_mask_new': (P, [I, I, I, I, floatp, P]),
            'ag_box': (P, [P, I, I, I, I, I, I, F, F, D, I, P]),
            'ag_mask_union': (P, [C.POINTER(P), S, I, I, P]),
            'ag_mask_get': (I, [P, intp, C.POINTER(floatp)]),
            'ag_weights': (P, [P, I, I, P]),
            'ag_weights_get': (S, [P, C.POINTER(bytep)]),
        }
        for kind in ('images', 'mask', 'weights'):
            signatures['ag_' + kind + '_retain'] = (P, [P])
            signatures['ag_' + kind + '_free'] = (None, [P])
        for name, (result, args) in signatures.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = result, args
        _LIB = lib
        return lib


def _check(value):
    if not value:
        raise RuntimeError(library().ag_error().decode('utf-8', 'replace'))
    return value


def _status(value):
    if value < 0:
        raise RuntimeError(library().ag_error().decode('utf-8', 'replace'))
    return value


def libass_info():
    lib = library()
    version = lib.ag_libass_version()
    return {'version_hex': hex(version), 'version': '%x.%x.%x' % ((version >> 28) & 15, (version >> 20) & 255, (version >> 12) & 255),
            'path': lib.ag_libass_path().decode('utf-8', 'replace'),
            'event_export_abi': lib.ag_event_export_abi()}


def event_export_available():
    return library().ag_event_export_abi() == 1


def ffmpeg_time_ms(pts, time_base, denominator=None):
    if denominator is None:
        numerator, denominator = time_base.numerator, time_base.denominator
    else:
        numerator = time_base
    if denominator <= 0:
        raise ValueError('time_base denominator must be positive')
    values = (int(pts), int(numerator), int(denominator))
    if any(value < -(1 << 63) or value >= (1 << 63) for value in values):
        raise ValueError('timestamp does not fit signed 64 bits')
    if abs(float(pts) * float(numerator) / float(denominator) * 1000) >= (1 << 63):
        raise ValueError('timestamp in milliseconds does not fit signed 64 bits')
    return library().ag_time_ms(*values)


class NativeBudget:
    def __init__(self, limit=DEFAULT_BUDGET):
        if type(limit) is not int or not 0 < limit <= sys.maxsize:
            raise ValueError('max_in_flight_bytes must be a positive platform-sized integer')
        self.limit = int(limit)
        self._lib = library()
        self._handle = _check(self._lib.ag_budget_new(self.limit))

    @property
    def used(self):
        return self._lib.ag_budget_used(self._handle)

    @property
    def peak(self):
        return self._lib.ag_budget_peak(self._handle)

    def __del__(self):
        if getattr(self, '_handle', None):
            self._lib.ag_budget_free(self._handle)
            self._handle = None


class _Owner:
    kind = ''

    def __init__(self, handle, budget):
        self._lib = library()
        self._handle = _check(handle)
        self.budget = budget

    @property
    def handle(self):
        if not self._handle:
            raise RuntimeError('native %s owner is released' % self.kind)
        return self._handle

    def retain(self):
        return type(self)(getattr(self._lib, 'ag_' + self.kind + '_retain')(self.handle), self.budget)

    def release(self):
        if getattr(self, '_handle', None):
            getattr(self._lib, 'ag_' + self.kind + '_free')(self._handle)
            self._handle = None

    def __del__(self):
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()

    def _view(self, pointer, count, scalar, format):
        array = (scalar * count).from_address(C.addressof(pointer.contents)) if count else (scalar * 0)()
        # The buffer independently retains native storage: even explicit parent
        # release or a later render cannot invalidate a previously returned view.
        array._native_owner = self.retain()
        return memoryview(array).cast('B').cast(format).toreadonly()


@dataclass(frozen=True)
class ImagePlane:
    dst_x: int
    dst_y: int
    w: int
    h: int
    type: str
    color: int
    coverage: memoryview

    @property
    def stride(self):
        return self.w

    @property
    def bitmap(self):
        return self.coverage


class NativeImages(_Owner):
    kind = 'images'

    @classmethod
    def empty(cls, budget=None):
        budget = budget or NativeBudget()
        return cls(library().ag_images_new(budget._handle), budget)

    @classmethod
    def combine(cls, images, budget=None):
        """Copy source planes into one owned group, keeping type/color/coverage."""
        images = tuple(images)
        budget = budget or (images[0].budget if images else NativeBudget())
        handles = (C.c_void_p * len(images))(*(image.handle for image in images))
        return cls(library().ag_images_combine(handles, len(images), budget._handle), budget)

    def append(self, x, y, w, h, coverage, color=0xFFFFFF00, image_type='character', stride=None):
        """Test/debug import; production uses one C++ copy of the libass chain."""
        payload = bytes(coverage)
        buf = (C.c_uint8 * len(payload)).from_buffer_copy(payload)
        typ = IMAGE_TYPES[image_type] if isinstance(image_type, str) else image_type
        _status(self._lib.ag_images_append(self.handle, x, y, w, h, w if stride is None else stride, color, typ, buf, len(payload)))
        return self

    @property
    def image_count(self):
        return self._lib.ag_images_count(self.handle)

    @property
    def changed(self):
        return self._lib.ag_images_changed(self.handle)

    @property
    def digest(self):
        return '%016x' % self._lib.ag_images_digest(self.handle)

    def __len__(self):
        return self.image_count

    def __iter__(self):
        for i in range(self.image_count):
            yield self[i]

    def __getitem__(self, index):
        if not 0 <= index < self.image_count:
            raise IndexError(index)
        desc, color, ptr = (C.c_int * 5)(), C.c_uint32(), C.POINTER(C.c_uint8)()
        _status(self._lib.ag_images_get(self.handle, index, desc, C.byref(color), C.byref(ptr)))
        return ImagePlane(desc[0], desc[1], desc[2], desc[3], ('character', 'outline', 'shadow')[desc[4]], color.value,
                          self._view(ptr, desc[2] * desc[3], C.c_uint8, 'B'))

    def source_stats(self, include_types=('character', 'outline'), opacity_threshold=0.0):
        """Return thresholded ink bbox and the unthresholded peak effective opacity."""
        bbox, peak = (C.c_int * 4)(), C.c_uint32()
        valid = _status(self._lib.ag_images_stats(self.handle, type_mask(include_types), opacity_threshold,
                                                bbox, C.byref(peak)))
        return (tuple(bbox) if valid else None), peak.value / 65025.0


class NativeSession:
    def __init__(self, ass_data, width, height, fonts_dir=None, budget=None):
        self.budget = budget or NativeBudget()
        self._lib, self._handle, self._logs = library(), None, ''
        self._lock = threading.Lock()
        self._handle = _check(self._lib.ag_session_new(ass_data, len(ass_data), width, height,
                            str(fonts_dir).encode() if fonts_dir else None, self.budget._handle))

    def render(self, time_ms):
        with self._lock:
            if not self._handle:
                raise RuntimeError('render session is closed')
            return NativeImages(self._lib.ag_session_render(self._handle, time_ms), self.budget)

    @property
    def event_metadata(self):
        """Frozen libass event order for validating the Python Dialogue mapping."""
        with self._lock:
            if not self._handle:
                raise RuntimeError('render session is closed')
            result = []
            for index in range(self._lib.ag_session_event_count(self._handle)):
                timing, fields, strings = (C.c_int64 * 2)(), (C.c_int * 4)(), (C.c_char_p * 4)()
                _status(self._lib.ag_session_event_metadata(self._handle, index, timing, fields, strings))
                text = [(value or b'').decode('utf-8', 'replace') for value in strings]
                result.append(dict(start_ms=timing[0], duration_ms=timing[1], layer=fields[0],
                                   margin_l=fields[1], margin_r=fields[2], margin_v=fields[3],
                                   style=text[0], name=text[1], text=text[2], effect=text[3]))
            return tuple(result)

    @property
    def logs(self):
        with self._lock:
            return self._lib.ag_session_logs(self._handle).decode('utf-8', 'replace') if self._handle else self._logs

    def close(self):
        with self._lock:
            if self._handle:
                self._logs = self._lib.ag_session_logs(self._handle).decode('utf-8', 'replace')
                self._lib.ag_session_free(self._handle)
                self._handle = None

    def __del__(self):
        if getattr(self, '_handle', None):
            self.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class NativeEventFrame:
    """Owns callback copies until transferred; no libass pointers escape render."""
    def __init__(self, handle, budget):
        self._lib = library()
        self._handle = _check(handle)
        self.budget = budget
        self.changed = self._lib.ag_event_frame_changed(self._handle)
        self._taken = set()

    def take(self, index):
        if not self._handle:
            raise RuntimeError('EventImages frame is released')
        if type(index) is not int or not 0 <= index < 2**31:
            raise ValueError('event index must be a nonnegative 32-bit integer')
        if index in self._taken:
            raise RuntimeError('event image ownership was already transferred')
        result = NativeImages(self._lib.ag_event_frame_take(self._handle, index, self.budget._handle), self.budget)
        self._taken.add(index)
        return result

    def release(self):
        if getattr(self, '_handle', None):
            self._lib.ag_event_frame_free(self._handle)
            self._handle = None

    def __del__(self):
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class NativeEventSession(NativeSession):
    def __init__(self, ass_data, width, height, fonts_dir=None, budget=None, selected_indices=()):
        super().__init__(ass_data, width, height, fonts_dir, budget)
        try:
            selected_indices = tuple(selected_indices)
            if any(type(index) is not int or not 0 <= index < 2**31 for index in selected_indices):
                raise ValueError('selected event indices must be nonnegative 32-bit integers')
            indices = (C.c_int * len(selected_indices))(*selected_indices)
            _status(self._lib.ag_session_enable_events(self._handle, indices, len(indices)))
        except BaseException:
            self.close()
            raise

    def render(self, time_ms):
        with self._lock:
            if not self._handle:
                raise RuntimeError('render session is closed')
            return NativeEventFrame(self._lib.ag_session_render_events(self._handle, time_ms), self.budget)


class NativeMask(_Owner):
    kind = 'mask'

    @classmethod
    def from_values(cls, roi=None, values=(), budget=None):
        budget = budget or NativeBudget()
        x0, y0, x1, y1 = roi or (0, 0, 0, 0)
        data = (C.c_float * len(values))(*values)
        if len(data) != (x1 - x0) * (y1 - y0):
            raise ValueError('mask ROI and sample count disagree')
        return cls(library().ag_mask_new(x0, y0, x1 - x0, y1 - y0, data, budget._handle), budget)

    def _info(self):
        roi, ptr = (C.c_int * 4)(), C.POINTER(C.c_float)()
        _status(self._lib.ag_mask_get(self.handle, roi, C.byref(ptr)))
        return tuple(roi), ptr

    @property
    def roi(self):
        roi = self._info()[0]
        return roi if roi[0] < roi[2] and roi[1] < roi[3] else None

    @property
    def weights(self):
        roi, ptr = self._info()
        return self._view(ptr, (roi[2] - roi[0]) * (roi[3] - roi[1]), C.c_float, 'f')


class NativeWeights(_Owner):
    kind = 'weights'

    @property
    def size(self):
        ptr = C.POINTER(C.c_uint8)()
        return self._lib.ag_weights_get(self.handle, C.byref(ptr))

    @property
    def buffer(self):
        ptr = C.POINTER(C.c_uint8)()
        size = self._lib.ag_weights_get(self.handle, C.byref(ptr))
        return self._view(ptr, size, C.c_uint8, 'B')


def type_mask(include_types):
    result = 0
    for typ in include_types:
        if typ not in IMAGE_TYPES:
            raise ValueError('unsupported ASS image type: %s' % typ)
        result |= 1 << IMAGE_TYPES[typ]
    return result


def box_mask(images, cfg, frame_size, budget=None, allow_visual=False):
    budget = budget or images.budget
    visual = cfg.alpha_policy == 'follow-visual-alpha'
    if visual and not allow_visual:
        raise ValueError('follow-visual-alpha is a future capability; use geometry-only')
    handle = library().ag_box(images.handle, frame_size[0], frame_size[1], type_mask(cfg.include_types),
                             cfg.padding_x, cfg.padding_y, cfg.corner_radius, cfg.feather_sigma,
                             cfg.strength, cfg.opacity_threshold, int(visual), budget._handle)
    return NativeMask(handle, budget)


def union_native(masks, frame_size, budget=None):
    budget = budget or (masks[0].budget if masks else NativeBudget())
    handles = (C.c_void_p * len(masks))(*(mask.handle for mask in masks))
    return NativeMask(library().ag_mask_union(handles, len(masks), *frame_size, budget._handle), budget)


def encode_native(mask, frame_size, budget=None):
    budget = budget or mask.budget
    return NativeWeights(library().ag_weights(mask.handle, *frame_size, budget._handle), budget)
