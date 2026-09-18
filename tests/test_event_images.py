"""Real libass event attribution and independent background-box regressions.

Vector fixtures keep geometry assertions independent of installed fonts. The
collision fixture additionally compares the untouched public libass output;
fixture colours identify the expected events only, never production selection.
"""
from collections import Counter
from contextlib import contextmanager
from fractions import Fraction
import gc
from types import SimpleNamespace

import pytest

from assglass.ass import SourceDocument
from assglass.config import resolve_config
from assglass.contracts import FrameRequest, MaskContext
from assglass.masks import create_builder, merge_masks
from assglass.native import NativeBudget, NativeSession, ffmpeg_time_ms
from assglass.selection import SelectionError, build_selection_plan, create_backend
from test_ass import HEADER


SIZE = (640, 360)


def test_event_images_native_allocation_retry_preserves_frame_sequence():
    from test_cache import assert_render_budget_recovery
    assert_render_budget_recovery('event-images')


def dialogue(text, actor="bgblur", start="0:00:00.00", end="0:00:01.00",
             layer=0, effect=""):
    return "Dialogue: {},{},{},Default,{},0,0,0,{},{}\n".format(
        layer, start, end, actor, effect, text)


def rectangle(x, y, width=80, height=30, tags=""):
    return (r"{\an7\pos(%d,%d)\bord0\shad0%s\p1}"
            r"m 0 0 l %d 0 %d %d 0 %d") % (
                x, y, tags, width, width, height, height)


def source_of(*rows):
    return SourceDocument.from_bytes((HEADER + "".join(rows)).encode("utf-8"))


def prepare(source, budget=None):
    config = resolve_config({
        "selection": {"backend": "event-images", "grouping": "per-event"},
        "defaults": {"padding_x": 0, "padding_y": 0, "corner_radius": 0,
                     "feather_sigma": 0, "strength": 1, "opacity_threshold": .5},
    })
    profile = SimpleNamespace(frame_size=SIZE, fonts_dir=None,
                              native_budget=budget or NativeBudget(8 * 1024 * 1024),
                              profile_id="event-images-fixture")
    backend = create_backend("event-images")
    prepared = backend.preflight(source, build_selection_plan(source, config), profile)
    return backend, prepared, profile.native_budget


@contextmanager
def opened(source):
    backend, prepared, budget = prepare(source)
    session = backend.open(prepared)
    try:
        yield session, budget
    finally:
        session.close()


def request(index, fps=50):
    return FrameRequest(index, index, Fraction(1, fps), SIZE)


@contextmanager
def mask_for(selection, budget):
    context = MaskContext(SIZE, budget)
    masks = []
    merged = None
    try:
        for group in selection.groups:
            masks.append(create_builder(group.effect_config.mode).build(
                group, group.effect_config, context))
        merged = merge_masks(masks, context)
        yield merged
    finally:
        if merged is not None:
            merged.release()
        for mask in masks:
            mask.release()


def at(mask, x, y):
    if mask.empty:
        return 0.0
    left, top, right, bottom = mask.roi
    if not (left <= x < right and top <= y < bottom):
        return 0.0
    return mask.weights[(y - top) * (right - left) + x - left]


def snapshot(images, colors=None):
    return Counter((plane.type, plane.dst_x, plane.dst_y, plane.w, plane.h,
                    plane.color, bytes(plane.coverage))
                   for plane in images
                   if colors is None or plane.color >> 8 in colors)


def test_separate_events_do_not_fill_the_empty_bridge_between_boxes():
    source = source_of(dialogue(rectangle(60, 70)), dialogue(rectangle(420, 250)))
    with opened(source) as (session, budget), session.render(request(0)) as selected:
        assert selected.capabilities.supports_event_groups
        assert len(selected.groups) == 2
        assert {group.target_event_keys for group in selected.groups} == {
            (source.events[0].key,), (source.events[1].key,)}
        assert len({group.group_id for group in selected.groups}) == 2
        with mask_for(selected, budget) as mask:
            assert at(mask, 90, 85) == 1
            assert at(mask, 450, 265) == 1
            assert at(mask, 300, 180) == 0
            assert at(mask, 90, 265) == 0


def test_explicit_group_unions_source_layers_before_constructing_one_box():
    source = source_of(
        dialogue(rectangle(60, 70), actor="bgblur(group=layered)"),
        dialogue(rectangle(100, 80), actor="bgblur(group=layered)", layer=1),
        dialogue(rectangle(420, 250)))
    with opened(source) as (session, budget), session.render(request(0)) as selected:
        assert len(selected.groups) == 2
        layered = next(group for group in selected.groups if len(group.target_event_keys) == 2)
        assert set(layered.target_event_keys) == {event.key for event in source.events[:2]}
        assert layered.images.source_stats(opacity_threshold=.5)[0] == (60, 70, 180, 110)
        with mask_for(selected, budget) as mask:
            # This corner is outside both vector rectangles, but inside their
            # explicitly requested common box. The unrelated box stays separate.
            assert at(mask, 165, 75) == 1
            assert at(mask, 300, 180) == 0


def test_distinct_simultaneous_groups_keep_independent_effect_parameters():
    source = source_of(
        dialogue(rectangle(60, 70), actor="bgblur(strength=0.25)"),
        dialogue(rectangle(420, 250), actor="bgblur(strength=0.75)"))
    with opened(source) as (session, budget), session.render(request(0)) as selected:
        assert sorted(group.effect_config.strength for group in selected.groups) == [.25, .75]
        with mask_for(selected, budget) as mask:
            assert at(mask, 90, 85) == .25
            assert at(mask, 450, 265) == .75
            assert at(mask, 300, 180) == 0


@pytest.mark.parametrize("backend,grouping,named,bridge", [
    ("event-images", "per-event", False, 0),
    ("event-images", "per-event", True, 1),
    ("alpha", "merged", False, 1),
])
def test_organic_closing_is_per_group_not_across_unrelated_events(backend, grouping, named, bridge):
    actor = "bgblur(group=joined)" if named else "bgblur"
    source = source_of(dialogue(rectangle(60, 70, 20, 30), actor=actor),
                       dialogue(rectangle(84, 70, 20, 30), actor=actor))
    cfg = resolve_config({
        "selection": {"backend": backend, "grouping": grouping},
        "defaults": {"mode": "organic", "expand_x": 0, "expand_y": 0,
                     "close": 4, "feather": 0},
    })
    budget = NativeBudget(8 * 1024 * 1024)
    profile = SimpleNamespace(frame_size=SIZE, fonts_dir=None, native_budget=budget,
                              profile_id="organic-group-fixture")
    provider = create_backend(backend)
    prepared = provider.preflight(source, build_selection_plan(source, cfg), profile)
    with provider.open(prepared) as session, session.render(request(0)) as selected:
        with mask_for(selected, budget) as mask:
            assert at(mask, 70, 85) == 1
            assert at(mask, 94, 85) == 1
            assert at(mask, 82, 85) == bridge
            assert at(mask, 82, 50) == 0


def test_organic_and_box_coexist_with_distinct_shapes_and_parameters():
    # Two disconnected pieces of one ASS drawing have the same bbox in both
    # events. Organic retains the gap; Box intentionally fills its rectangle.
    drawing = r"{\an7\pos(%d,70)\bord0\shad0\p1}m 0 0 l 20 0 20 20 0 20 m 60 0 l 80 0 80 20 60 20"
    source = source_of(
        dialogue(drawing % 60, actor="bgblur(mode=organic;expand_x=0;expand_y=0;close=0;strength=0.75)"),
        dialogue(drawing % 420, actor="bgblur(strength=0.25)"))
    with opened(source) as (session, budget), session.render(request(0)) as selected:
        assert {group.effect_config.mode for group in selected.groups} == {"organic", "box"}
        with mask_for(selected, budget) as mask:
            assert at(mask, 70, 80) == .75
            assert at(mask, 100, 80) == 0
            assert at(mask, 460, 80) == .25


def test_conflicting_parameters_are_rejected_only_within_active_shared_group():
    source = source_of(
        dialogue(rectangle(60, 70), actor="bgblur(group=same;strength=0.25)"),
        dialogue(rectangle(100, 80), actor="bgblur(group=same;strength=0.75)"))
    with pytest.raises(SelectionError, match="strength"):
        prepare(source)

    successive = source_of(
        dialogue(rectangle(60, 70), actor="bgblur(group=same;strength=0.25)",
                 end="0:00:00.04"),
        dialogue(rectangle(100, 80), actor="bgblur(group=same;strength=0.75)",
                 start="0:00:00.04", end="0:00:00.08"))
    with opened(successive) as (session, _):
        strengths, identities = [], []
        for index in range(4):
            with session.render(request(index)) as selected:
                assert len(selected.groups) == 1
                strengths.append(selected.groups[0].effect_config.strength)
                identities.append(selected.groups[0].group_id)
        assert strengths == [.25, .25, .75, .75]
        assert len(set(identities)) == 1


def test_original_complex_unmarked_events_remain_intact_and_unselected():
    # These rules are deliberately outside the Alpha rewrite whitelist.
    source = source_of(
        dialogue(rectangle(60, 70, tags=r"\1c&H332211&")),
        dialogue(r"{\an7\pos(300,160)\1c&H00FF00&\k10}karaoke"
                 r"{\r\1c&H00FF00&\k10} context", actor="ordinary", effect="fx"),
        dialogue(rectangle(430, 260, tags=r"\1c&HFF0000&"), actor="ordinary"))
    original_bytes = source.raw
    backend, prepared, budget = prepare(source)
    assert prepared.analysis_data == original_bytes
    with backend.open(prepared) as session, NativeSession(source.raw, *SIZE) as original:
        for index in range(12):
            with session.render(request(index)) as selected, original.render(index * 20) as public:
                assert len(selected.groups) == 1
                assert selected.groups[0].target_event_keys == (source.events[0].key,)
                assert snapshot(selected.groups[0].images) == snapshot(public, {0x112233})
                assert snapshot(public, {0x00FF00, 0x0000FF})
                with mask_for(selected, budget) as mask:
                    assert at(mask, 90, 85) == 1
                    assert at(mask, 450, 275) == 0
    assert source.raw == original_bytes


def test_event_exports_preserve_normal_collision_and_full_track_history():
    # Default-position text exercises libass collision placement, which would
    # change if selected events were rendered in isolated subset tracks.
    target_one = r"{\1c&H332211&\3c&H665544&\4c&H998877&}First target"
    target_two = r"{\1c&HCCBBAA&\3c&HFFEEDD&\4c&HCC9966&}Second target"
    expected_colors = ({0x112233, 0x445566, 0x778899},
                       {0xAABBCC, 0xDDEEFF, 0x6699CC})
    source = source_of(
        dialogue("ordinary line before both targets", actor="", end="0:00:00.70"),
        dialogue(target_one, end="0:00:00.90"),
        dialogue(target_two, start="0:00:00.10", end="0:00:00.80"),
        dialogue("late collision context", actor="", start="0:00:00.30",
                 end="0:00:00.60"))
    with opened(source) as (session, _), NativeSession(source.raw, *SIZE) as original:
        for index in range(61):
            frame = request(index, fps=60)
            with session.render(frame) as selected, original.render(
                    ffmpeg_time_ms(frame.pts, frame.time_base)) as public:
                actual = Counter()
                for group in selected.groups:
                    actual.update(snapshot(group.images))
                expected = snapshot(public, expected_colors[0] | expected_colors[1])
                assert actual == expected, "collision/history mismatch at frame {}".format(index)


def test_identical_bitmaps_switch_event_identity_without_stale_owned_buffers():
    source = source_of(
        dialogue(rectangle(60, 70), end="0:00:00.04"),
        dialogue(rectangle(60, 70), start="0:00:00.04", end="0:00:00.08"))
    backend, prepared, budget = prepare(source)
    session = backend.open(prepared)
    frames = []
    try:
        for index in range(5):
            frames.append(session.render(request(index)))
        first, same, replacement, _, empty = frames
        # libass reports unchanged public pixels even when the originating
        # Dialogue changes; selection identity must still be refreshed.
        assert same.changed == 0
        assert replacement.changed == 0
        assert first.groups[0].images.digest == same.groups[0].images.digest
        assert first.groups[0].images.digest == replacement.groups[0].images.digest
        assert first.groups[0].target_event_keys == (source.events[0].key,)
        assert replacement.groups[0].target_event_keys == (source.events[1].key,)
        assert first.groups[0].group_id != replacement.groups[0].group_id
        assert first.selection_digest != replacement.selection_digest
        assert not any(group.images.image_count for group in empty.groups)
        retained = snapshot(first.groups[0].images)
        session.close()
        assert snapshot(first.groups[0].images) == retained
        assert snapshot(replacement.groups[0].images) == retained
    finally:
        session.close()
        for frame in frames:
            frame.release()
    gc.collect()
    assert budget.used == 0


def test_rotated_multilayer_exclamation_does_not_expand_the_lower_sentence_box():
    lower = dialogue(rectangle(80, 290, width=480, height=25))
    layers = [dialogue(rectangle(460, 210, width=100, height=30,
                                tags=r"\frz20\bord%d" % border),
                       actor="bgblur(group=why)", layer=layer)
              for layer, border in enumerate((5, 3, 0))]
    source = source_of(lower, *layers)
    with opened(source) as (session, budget), session.render(request(0)) as selected:
        assert sorted(len(group.target_event_keys) for group in selected.groups) == [1, 3]
        why = next(group for group in selected.groups if len(group.target_event_keys) == 3)
        left, top, right, bottom = why.images.source_stats(opacity_threshold=.5)[0]
        assert left > 400 and top < 210 and bottom < 290
        with mask_for(selected, budget) as mask:
            assert at(mask, 200, 300) == 1
            assert at(mask, (left + right) // 2, (top + bottom) // 2) == 1
            # This empty corner belonged to the previous one-big-box result.
            assert at(mask, 100, 230) == 0


def test_event_session_enforces_sequential_history_and_profile_size():
    source = source_of(dialogue(rectangle(60, 70)))
    with opened(source) as (session, _):
        with pytest.raises(SelectionError, match="consecutive"):
            session.render(request(1))
        with pytest.raises(SelectionError, match="size"):
            session.render(FrameRequest(0, 0, Fraction(1, 50), (320, 180)))
        with session.render(request(0)) as selected:
            assert selected.groups
        with pytest.raises(SelectionError, match="consecutive"):
            session.render(request(0))
