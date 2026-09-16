import dataclasses
import unittest
from fractions import Fraction
from types import SimpleNamespace

from assglass.ass import SourceDocument
from assglass.config import create_sidecar, resolve_config
from assglass.contracts import BackendCapabilities, FrameRequest, FrameSelection, ImageGroup, MaskContext, ResolvedMaskConfig
from assglass.selection import AlphaTrackBackend, SelectionError, build_selection_plan, create_backend, register_backend
from test_ass import HEADER, document


PROFILE = SimpleNamespace(frame_size=(640, 360), fonts_dir=None, profile_id="test-fixture")


def alpha_config(project=None):
    """Keep the original Alpha compatibility suite explicit about legacy grouping."""
    values = dict(project or {})
    values['selection'] = dict({'backend': 'alpha', 'grouping': 'merged'}, **values.get('selection', {}))
    return resolve_config(values)


class SelectionPlanningTests(unittest.TestCase):
    def preflight(self, source, **kwargs):
        cfg = alpha_config(kwargs)
        plan = build_selection_plan(source, cfg)
        return AlphaTrackBackend().preflight(source, plan, PROFILE)

    def test_actor_not_effect_selection_and_sidecar(self):
        source = document("ordinary", effect="bgblur", second="target")
        plan = build_selection_plan(source, alpha_config())
        self.assertEqual(plan.target_indices, (1,))
        # A real unselected Effect needs EventImages; Actor does not consume it.
        with self.assertRaises(ValueError):
            AlphaTrackBackend().preflight(source, plan, PROFILE)
        sidecar = create_sidecar(source, [0])
        plan = build_selection_plan(source, alpha_config(), sidecar)
        self.assertEqual(plan.target_indices, (0, 1))

    def test_same_config_box_requires_explicit_merge(self):
        source = document("one", actor="bgblur", second="two")
        with self.assertRaisesRegex(SelectionError, "allow_merged_box"):
            self.preflight(source)
        prepared = self.preflight(source, selection={"allow_merged_box": True})
        self.assertEqual(prepared.plan.target_indices, (0, 1))
        self.assertEqual(prepared.analysis_data, source.raw)

    def test_effective_difference_rejected_even_when_zero_strength(self):
        source = document("one", actor="bgblur{feather=4;strength=0}", second="two")
        with self.assertRaisesRegex(SelectionError, "feather_sigma"):
            self.preflight(source, selection={"allow_merged_box": True})
        source = document("one", actor="bgblur{strength=0}", second="two")
        with self.assertRaisesRegex(SelectionError, "strength"):
            self.preflight(source, selection={"allow_merged_box": True})

    def test_nonoverlap_and_end_exclusive(self):
        raw = (HEADER + "Dialogue: 0,0:00:00.00,0:00:01.00,Default,bgblur{feather=2},0,0,0,,one\n"
               "Dialogue: 0,0:00:01.00,0:00:02.00,Default,bgblur{feather=3},0,0,0,,two\n").encode()
        prepared = self.preflight(SourceDocument.from_bytes(raw))
        self.assertEqual(len(prepared.plan.targets), 2)

    def test_overlapping_targets_require_same_opacity_threshold(self):
        source = document("one", actor="bgblur{threshold=0}", second="two")
        with self.assertRaisesRegex(SelectionError, "opacity_threshold"):
            self.preflight(source, selection={"allow_merged_box": True})

    def test_aliases_and_sources_do_not_conflict(self):
        source = document("one", actor="bgblur{feather=12;strength=1.0}", second="two")
        self.preflight(source, selection={"allow_merged_box": True})

    def test_unsupported_backends_grouping_and_profile_binding(self):
        self.assertEqual(create_backend("event-images").name, "event-images")
        with self.assertRaises(SelectionError):
            create_backend("missing")
        source = document("one", actor="bgblur")
        with self.assertRaisesRegex(SelectionError, "per-event"):
            self.preflight(source, selection={"grouping": "per-event"})
        prepared = self.preflight(source)
        prepared.profile.frame_size = (320, 180)
        try:
            with self.assertRaisesRegex(SelectionError, "profile mismatch"):
                AlphaTrackBackend().open(prepared)
        finally:
            prepared.profile.frame_size = (640, 360)

    def test_registry_replacement_boundary(self):
        class SyntheticBackend:
            def preflight(self, source, plan, profile):
                return source.sha256, plan.target_indices, profile.frame_size
        register_backend("test-synthetic", SyntheticBackend)
        backend = create_backend("test-synthetic")
        source = document("one", actor="bgblur")
        plan = build_selection_plan(source, alpha_config())
        self.assertEqual(backend.preflight(source, plan, PROFILE)[1], (0,))

    def test_event_group_backend_swaps_without_box_or_encoder_changes(self):
        from assglass.masks import create_builder, merge_masks
        from assglass.native import NativeBudget, NativeImages
        from assglass.weights import YUV420PLeftWeightEncoder
        budget = NativeBudget(1024 * 1024)
        first = NativeImages.empty(budget).append(0, 0, 2, 2, [255] * 4)
        second = NativeImages.empty(budget).append(4, 4, 2, 2, [255] * 4)
        cfg = ResolvedMaskConfig(padding_x=0, padding_y=0, corner_radius=0, feather_sigma=0, strength=1)
        class GroupSession:
            def render(self, frame):
                return FrameSelection(frame, 0, (
                    ImageGroup("event-1", "dialogue:1", ("dialogue:1",), cfg, first),
                    ImageGroup("event-2", "dialogue:2", ("dialogue:2",), dataclasses.replace(cfg, strength=0.5), second)),
                    backend="test-event-groups", capabilities=BackendCapabilities(True, "visible-only"))
            def close(self):
                pass
        class GroupBackend:
            def preflight(self, source, plan, profile):
                return None
            def open(self, prepared):
                return GroupSession()
        register_backend("test-event-groups", GroupBackend)
        backend = create_backend("test-event-groups")
        session = backend.open(backend.preflight(None, None, None))
        frame = FrameRequest(0, 0, Fraction(1, 60), (8, 8))
        selected = session.render(frame)
        context = MaskContext((8, 8), budget)
        masks = [create_builder(group.effect_config.mode).build(group, group.effect_config, context) for group in selected.groups]
        merged = merge_masks(masks, context)
        output = YUV420PLeftWeightEncoder(budget).encode(merged, frame, SimpleNamespace(frame_size=(8, 8), pix_fmt="yuv420p", sampler_id="left-tent2-v1"))
        view = output.buffer
        try:
            payload = bytes(view)
            self.assertEqual(payload[0], 255)
            self.assertEqual(payload[4 * 8 + 4], 128)
            self.assertEqual(payload[3 * 8 + 3], 0)
        finally:
            view.release()
            output.release()
            merged.release()
            for mask in masks:
                mask.release()
            selected.release()
            session.close()
        self.assertEqual(budget.used, 0)


class SelectionNativeTests(unittest.TestCase):
    def test_sequential_render_empty_frames_and_owned_images(self):
        from assglass.native import NativeBudget
        source = SourceDocument.from_bytes((HEADER +
            "Dialogue: 0,0:00:00.02,0:00:00.05,Default,bgblur,0,0,0,,Target\n"
            "Dialogue: 0,0:00:00.00,0:00:00.10,Default,,0,0,0,,ordinary\n").encode())
        profile = SimpleNamespace(frame_size=(640, 360), fonts_dir=None, native_budget=NativeBudget(4 * 1024 * 1024))
        cfg = alpha_config()
        backend = AlphaTrackBackend()
        prepared = backend.preflight(source, build_selection_plan(source, cfg), profile)
        session = backend.open(prepared)
        frames = []
        try:
            for index in range(7):
                frames.append(session.render(FrameRequest(index, index, Fraction(1, 100), (640, 360))))
            self.assertEqual([bool(frame.groups[0].target_event_keys) for frame in frames], [False, False, True, True, True, False, False])
            self.assertTrue(frames[2].groups[0].images.image_count)
            self.assertFalse(frames[0].groups[0].images.image_count)
            self.assertFalse(frames[5].groups[0].images.image_count)
            digest = frames[2].groups[0].images.digest
            session.close()
            self.assertEqual(frames[2].groups[0].images.digest, digest)
            session.close()
            with self.assertRaises(SelectionError):
                session.render(FrameRequest(7, 7, Fraction(1, 100), (640, 360)))
        finally:
            session.close()
            for frame in frames:
                frame.release()

    def test_native_hiding_all_accepted_rules(self):
        from assglass.ass import build_analysis
        from assglass.native import NativeSession
        samples = ("word", r"{\alpha&H80}word", r"word{\rAlt}word",
                   r"{\alpha&H80}word{\rAlt\alpha&H80}word",
                   r"{\t(\alpha&H80)}word", r"{\t(2,\1a&H80\2a&HFF)}word",
                   r"{\rAlt\t(0,1000,\alpha&H80)}word", r"{\t(0,3000,0.5,\alpha&H80)}word")
        for text in samples:
            with self.subTest(text=text):
                plan = build_analysis(document(text), [])
                session = NativeSession(plan.analysis_data, 640, 360)
                try:
                    # Full actual 60 fps sequence, not isolated transform samples.
                    for index in range(121):
                        images = session.render(int(index * 1000 / 60))
                        self.assertEqual(images.image_count, 0)
                        images.release()
                finally:
                    session.close()

    def test_original_and_analysis_target_layout_collision_and_history(self):
        from assglass.ass import build_analysis
        from assglass.native import NativeSession, ffmpeg_time_ms
        # Distinct colours identify reference target images only inside this
        # fixture; production never guesses event identities from colours.
        samples = ("Long ordinary text " * 5, r"{\alpha&H80}Original alpha text",
                   r"first{\alpha&H00} second", r"first{\rAlt} second",
                   r"{\alpha&H80}first{\rAlt\alpha&H80} second",
                   r"{\t(\alpha&H80)}whole animation", r"{\t(2,\1a&H80\2a&HFF)}animation",
                   r"{\rAlt\t(0,1000,\alpha&H80)}animation", r"{\t(0,3000,0.5,\alpha&H80)}animation",
                   r"{\b700\i1\u1\s1\fnArial\fs26\fscx100\fscy90\fsp1}Japanese 日本語 Arabic عربي",
                   r"{\bord2\xbord3\ybord1\shad2\xshad-1\yshad1\be1\blur1\c&HFF00&}appearance",
                   r"{\an2\q0\pos(320,300)\org(320,300)\frx2\fry3\frz4\fax0.1\fay0.1\clip(0,0,640,360)}geometry")
        target = r"{\1c&H332211&\3c&H665544&\4c&H998877&}Target"
        colors = {0x112233, 0x445566, 0x778899}
        def snapshot(images, filter_colors=False):
            return tuple((image.type, image.dst_x, image.dst_y, image.w, image.h, image.color, bytes(image.coverage))
                         for image in images if not filter_colors or image.color >> 8 in colors)
        for sample in samples:
            with self.subTest(sample=sample):
                source = document(sample, second=target)
                # A third, unmarked event enters midway, advancing collision history.
                source = SourceDocument.from_bytes(source.raw + b"Dialogue: 0,0:00:00.50,0:00:01.00,Default,,0,0,0,,late context\n")
                plan = build_analysis(source, [1])
                original = NativeSession(source.raw, 640, 360)
                analysis = NativeSession(plan.analysis_data, 640, 360)
                try:
                    for index in range(121):
                        time_ms = ffmpeg_time_ms(index, Fraction(1, 60))
                        left, right = original.render(time_ms), analysis.render(time_ms)
                        try:
                            self.assertEqual(snapshot(left, True), snapshot(right), "frame {}".format(index))
                        finally:
                            left.release()
                            right.release()
                finally:
                    original.close()
                    analysis.close()


if __name__ == "__main__":
    unittest.main()
