import json
import tempfile
import unittest
from pathlib import Path

from assglass.config import (ConfigError, create_sidecar, load_project, load_sidecar, merge_overrides,
                             parse_marker, resolve_config, resolve_event_config)
from test_ass import document


class MarkerTests(unittest.TestCase):
    def test_actor_literal_prefix_semantics(self):
        for actor in ("bgblur", "bgblurred", "bgblurSpeaker", "bgblur 文字", "bgblurSuffix{feather=99}"):
            with self.subTest(actor=actor):
                self.assertEqual(parse_marker(actor).values, {})
        for actor in ("notbgblur", "BGBlur", " bgblur", "speaker;bgblur", ""):
            self.assertIsNone(parse_marker(actor))
        self.assertIsNotNone(parse_marker("背景角色", "背景"))

    def test_brace_legacy_aliases_and_explicit_zero(self):
        first = parse_marker("bgblur{ feather = 0 ; strength=0;radius=10;include=shadow+character}")
        second = parse_marker("bgblur;feather_sigma=0;strength=0;corner_radius=10;include_types=character+shadow")
        self.assertEqual(first.values, second.values)
        self.assertEqual(first.values["strength"], 0)
        self.assertEqual(parse_marker("bgblur{}").values, {})

    def test_invalid_parameters(self):
        examples = ("bgblur{feather=2", "bgblur{feather=2}suffix", "bgblur{feather=2;}", "bgblur;",
                    "bgblur{feather=2;;strength=1}", "bgblur{feather=1;feather_sigma=1}", "bgblur{=1}",
                    "bgblur{strength=}", "bgblur{unknown=1}", "bgblur{radius=-1}", "bgblur{strength=1.1}",
                    "bgblur{feather=NaN}", "bgblur{feather=Inf}", "bgblur{padding_x=-1.5}",
                    "bgblur{include=character+character}", "bgblur{include=bogus}", "bgblur{{feather=2}}",
                    "bgblur{sigma=18}", "bgblur{blur=18}", "bgblur{blur_sigma=18}", "bgblur{burn_subtitles=false}")
        for marker in examples:
            with self.subTest(marker=marker), self.assertRaises(ConfigError):
                parse_marker(marker)


class ConfigurationTests(unittest.TestCase):
    def test_precedence_no_row_inheritance(self):
        cfg = resolve_config({"defaults": {"blur": 19, "feather": 7, "strength": 0.6}},
                             cli_defaults=["feather=8", "strength=0.8"], blur_sigma=20)
        special = resolve_event_config(cfg, parse_marker("bgblur{feather=10;strength=0.5}"))
        normal = resolve_event_config(cfg, parse_marker("bgblur"))
        self.assertEqual(cfg.video_blur.blur_sigma, 20)
        self.assertEqual((special.feather_sigma, special.strength), (10, 0.5))
        self.assertEqual((normal.feather_sigma, normal.strength), (8, 0.8))
        self.assertEqual(normal.padding_x, 28)
        self.assertEqual(special.sources["feather_sigma"], "actor")
        self.assertNotIn("blur_sigma", special.__dict__)

    def test_opacity_threshold_precedence_alias_and_no_pixel_scaling(self):
        baseline = resolve_event_config(resolve_config(), parse_marker("bgblur"))
        self.assertEqual(baseline.opacity_threshold, 0.5)
        cfg = resolve_config({"defaults": {"threshold": 0.25}, "render": {"target_height": 540}},
                             cli_defaults=["opacity_threshold=0.75"])
        normal = resolve_event_config(cfg, parse_marker("bgblur"))
        legacy = resolve_event_config(cfg, parse_marker("bgblur{threshold=0}"))
        empty = resolve_event_config(cfg, parse_marker("bgblur{opacity_threshold=1}"))
        self.assertEqual((normal.opacity_threshold, legacy.opacity_threshold, empty.opacity_threshold), (0.75, 0, 1))
        self.assertEqual((normal.sources["opacity_threshold"], legacy.sources["opacity_threshold"]), ("cli", "actor"))
        for value in (-0.1, 1.1, float("nan"), float("inf"), True, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                resolve_config({"defaults": {"opacity_threshold": value}})
        with self.assertRaises(ConfigError):
            parse_marker("bgblur{threshold=0.5;opacity_threshold=0.5}")

    def test_burn_boolean_precedence(self):
        self.assertTrue(resolve_config().output.burn_subtitles)
        self.assertFalse(resolve_config({"output": {"burn_subtitles": False}}).output.burn_subtitles)
        self.assertTrue(resolve_config({"output": {"burn_subtitles": False}}, burn_subtitles=True).output.burn_subtitles)
        self.assertFalse(resolve_config(burn_subtitles=False).output.burn_subtitles)
        for value in (None, "false", 0, 1):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                resolve_config({"output": {"burn_subtitles": value}})

    def test_auto_backend_is_resolved_only_after_runtime_detection(self):
        self.assertEqual(resolve_config().selection["backend"], "auto")
        self.assertEqual(resolve_config().selection["grouping"], "per-event")
        self.assertEqual(resolve_config({"selection": {"allow_merged_box": True}}).selection["grouping"], "merged")
        with self.assertRaises(ConfigError):
            resolve_config({"selection": {"allow_merged_box": True, "grouping": "per-event"}})

    def test_group_is_selection_metadata_in_actor_and_sidecar(self):
        marker = parse_marker("bgblur{group=为啥;strength=0.75}")
        self.assertEqual(marker.values["group"], "为啥")
        effect = resolve_event_config(resolve_config(), marker)
        self.assertEqual(effect.strength, .75)
        self.assertNotIn("group", effect.sources)
        self.assertFalse(hasattr(effect, "group"))
        source = document("text")
        sidecar = create_sidecar(source, [0])
        sidecar["events"][0]["overrides"] = {"group": "why"}
        self.assertEqual(load_sidecar(sidecar, source)[0].values, {"group": "why"})
        with self.assertRaisesRegex(ConfigError, "conflict"):
            merge_overrides(marker, load_sidecar(sidecar, source)[0])
        for value in ("", "a b", "x,y", "a;b", "a=b", "a" * 129, True, 1, None):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                sidecar["events"][0]["overrides"] = {"group": value}
                load_sidecar(sidecar, source)
        with self.assertRaises(ConfigError):
            resolve_config({"defaults": {"group": "everyone"}})

    def test_defaults_validation_and_duplicate_aliases(self):
        configs = ({"defaults": {"feather": 1, "feather_sigma": 1}}, {"defaults": {"strength": None}},
                   {"defaults": {"include": "character+outline"}}, {"defaults": {"mode": "unknown-shape"}},
                   {"defaults": {"alpha_policy": "follow-visual-alpha"}}, {"selection": {"alpha_unsafe": "warn"}},
                   {"selection": {"alpha_unsafe": "allow"}}, {"marker_prefix": ""}, {"unknown": {}},
                   {"transport": {"max_in_flight_bytes": 0}}, {"runtime": {"native_workers": 2}})
        for cfg in configs:
            with self.subTest(cfg=cfg), self.assertRaises(ConfigError):
                resolve_config(cfg)
        with self.assertRaises(ConfigError):
            resolve_config(cli_defaults=["sigma=18"], blur_sigma=18)

    def test_mask_cache_is_bounded_and_can_be_disabled(self):
        self.assertEqual(resolve_config().transport["mask_cache_bytes"], 16 * 1024 * 1024)
        self.assertEqual(resolve_config({"transport": {"mask_cache_bytes": 0}}).transport["mask_cache_bytes"], 0)
        self.assertEqual(resolve_config({"transport": {"max_in_flight_bytes": 1024}}).transport["mask_cache_bytes"], 1024)
        for invalid in (-1, True, 2.5, "1024", None):
            with self.subTest(value=invalid), self.assertRaises(ConfigError):
                resolve_config({"transport": {"mask_cache_bytes": invalid}})

    def test_weight_cache_and_checksum_configuration(self):
        self.assertEqual(resolve_config().transport["weight_cache_bytes"], 16 * 1024 * 1024)
        self.assertEqual(resolve_config().transport["mask_hash"], "crc32")
        self.assertEqual(resolve_config({"transport": {"weight_cache_bytes": 0, "mask_hash": "sha256"}}).transport["weight_cache_bytes"], 0)
        self.assertEqual(resolve_config({"transport": {"max_in_flight_bytes": 1024}}).transport["weight_cache_bytes"], 1024)
        for invalid in (-1, True, 2.5, "1024", None):
            with self.subTest(value=invalid), self.assertRaises(ConfigError):
                resolve_config({"transport": {"weight_cache_bytes": invalid}})
        for invalid in ("md5", "CRC32", True, None):
            with self.subTest(value=invalid), self.assertRaises(ConfigError):
                resolve_config({"transport": {"mask_hash": invalid}})

    def test_global_organic_presets_dont_affect_box_equality(self):
        baseline = resolve_event_config(resolve_config(), parse_marker("bgblur"))
        cfg = resolve_config({"defaults": {"close": 99, "expand_x": 200}})
        self.assertEqual(resolve_event_config(cfg, parse_marker("bgblur")), baseline)
        with self.assertRaises(ConfigError):
            resolve_event_config(cfg, parse_marker("bgblur{close=5}"))

    def test_half_up_geometric_scaling_and_encoder_alias_scope(self):
        cfg = resolve_config({"defaults": {"padding_x": 1.5, "radius": 2.49}, "encoder": {"options": {"vb": "8M"}}})
        result = resolve_event_config(cfg, parse_marker("bgblur"))
        self.assertEqual((result.padding_x, result.corner_radius), (2, 2))
        self.assertEqual(cfg.encoder, {"options": {"vb": "8M"}})
        cfg = resolve_config({"render": {"target_height": 540}})
        result = resolve_event_config(cfg, parse_marker("bgblur{padding_x=3;feather=5}"))
        self.assertEqual((result.padding_x, result.feather_sigma), (2, 2.5))

    def test_organic_defaults_overrides_scaling_and_mode_switch(self):
        cfg = resolve_config(cli_defaults=["mode=organic"])
        default = resolve_event_config(cfg, parse_marker("bgblur"))
        self.assertEqual((default.mode, default.expand_x, default.expand_y, default.close),
                         ("organic", 48, 36, 48))
        self.assertEqual((default.feather_sigma, default.strength, default.opacity_threshold), (16, 1, .5))
        self.assertEqual(default.algorithm_version, "organic-ellipse-expand-rect-close-v2")
        cfg = resolve_config({"defaults": {"mode": "organic", "expand_x": 10, "feather": 3},
                              "render": {"target_height": 540}},
                             cli_defaults=["expand_x=5", "close=0"])
        row = resolve_event_config(cfg, parse_marker("bgblur{expand_y=3;strength=0.7}"))
        self.assertEqual((row.expand_x, row.expand_y, row.close, row.feather_sigma), (3, 2, 0, 1.5))
        self.assertEqual((row.strength, row.opacity_threshold), (.7, .5))
        self.assertEqual((row.sources["expand_x"], row.sources["expand_y"], row.sources["feather_sigma"]),
                         ("cli", "actor", "project"))
        box = resolve_event_config(cfg, parse_marker("bgblur{mode=box;padding_x=4}"))
        self.assertEqual((box.mode, box.padding_x), ("box", 2))
        self.assertEqual(box.algorithm_version, "roundrect-ss4-opacity-threshold-v2")
        self.assertEqual(resolve_event_config(cfg, parse_marker("bgblur")).mode, "organic")

    def test_organic_smoothing_presets_follow_row_mode_and_explicit_overrides(self):
        for default_mode in ("box", "organic"):
            cfg = resolve_config({"defaults": {"mode": default_mode}})
            organic = resolve_event_config(cfg, parse_marker("bgblur{mode=organic}"))
            box = resolve_event_config(cfg, parse_marker("bgblur{mode=box}"))
            self.assertEqual((organic.expand_x, organic.expand_y, organic.close, organic.feather_sigma),
                             (48, 36, 48, 16))
            self.assertEqual((box.padding_x, box.padding_y, box.feather_sigma), (28, 16, 12))
            self.assertEqual((organic.sources["close"], organic.sources["feather_sigma"]),
                             ("builtin", "builtin"))
        scaled = resolve_event_config(resolve_config({"render": {"target_height": 540}}),
                                      parse_marker("bgblur{mode=organic}"))
        self.assertEqual((scaled.expand_x, scaled.expand_y, scaled.close, scaled.feather_sigma),
                         (24, 18, 24, 8))
        # Explicit values, including zero and the old default, never disappear
        # when the final row mode chooses a different preset.
        for feather in (0, 12):
            for origin in ("project", "cli", "actor", "sidecar"):
                with self.subTest(feather=feather, origin=origin):
                    values = {"feather": feather, "close": 0}
                    project = {"defaults": values} if origin == "project" else {}
                    cli = ["feather=" + str(feather), "close=0"] if origin == "cli" else []
                    marker = parse_marker("bgblur{mode=organic}")
                    if origin == "actor":
                        marker = parse_marker("bgblur{mode=organic;feather=%s;close=0}" % feather)
                    if origin == "sidecar":
                        source = document("text")
                        sidecar = create_sidecar(source, [0])
                        sidecar["events"][0]["overrides"] = values
                        marker = merge_overrides(marker, load_sidecar(sidecar, source)[0])
                    result = resolve_event_config(resolve_config(project, cli_defaults=cli), marker)
                    self.assertEqual((result.close, result.feather_sigma), (0, feather))
                    self.assertEqual(result.sources["feather_sigma"], origin)

    def test_box_defaults_do_not_change_organic_identity_and_wrong_row_keys_fail(self):
        base = resolve_config({"defaults": {"mode": "organic"}})
        combined = resolve_config({"defaults": {"mode": "organic", "padding_x": 999, "radius": 555}})
        first = resolve_event_config(base, parse_marker("bgblur"))
        second = resolve_event_config(combined, parse_marker("bgblur"))
        self.assertEqual(first, second)
        self.assertEqual(second.sources["padding_x"], "builtin")
        for key in ("padding_x", "padding_y", "radius"):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "Organic row.*Box"):
                resolve_event_config(base, parse_marker("bgblur{%s=2}" % key))
        with self.assertRaisesRegex(ConfigError, "Box row.*Organic"):
            resolve_event_config(base, parse_marker("bgblur{mode=box;close=2}"))

    def test_organic_sidecar_shape_parameters(self):
        source = document("text")
        sidecar = create_sidecar(source, [0])
        sidecar["events"][0]["overrides"] = {"mode": "organic", "expand_x": 0, "close": 4}
        result = resolve_event_config(resolve_config(), load_sidecar(sidecar, source)[0])
        self.assertEqual((result.mode, result.expand_x, result.close), ("organic", 0, 4))
        self.assertEqual(result.sources["close"], "sidecar")

    def test_json_duplicate_keys_and_safe_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            path.write_text('{"defaults":{"strength":0.5},"output":{"burn_subtitles":false}}')
            self.assertFalse(resolve_config(path).output.burn_subtitles)
            path.write_text('{"defaults":{"strength":0.5,"strength":0.8}}')
            with self.assertRaises(ConfigError):
                load_project(path)
            try:
                import yaml
            except ImportError:
                return
            path = Path(directory) / "project.yaml"
            path.write_text("defaults:\n  feather: 3\noutput:\n  burn_subtitles: false\n")
            self.assertFalse(resolve_config(path).output.burn_subtitles)
            path.write_text("defaults:\n  feather: 3\n  feather: 4\n")
            with self.assertRaises(ConfigError):
                load_project(path)
            path.write_text("defaults: [invalid\n")
            with self.assertRaises(ConfigError):
                load_project(path)

    def test_config_relative_font_and_manifest_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"render": {"fonts_dir": "fonts", "manifest_output": "render.json"}}))
            cfg = resolve_config(path)
            self.assertEqual(cfg.render["fonts_dir"], str((path.parent / "fonts").resolve()))
            self.assertEqual(cfg.render["manifest_output"], str((path.parent / "render.json").resolve()))


class SidecarTests(unittest.TestCase):
    def test_roundtrip_hash_and_index_binding(self):
        source = document("字", second="字")
        data = create_sidecar(source, [1])
        data["events"][0]["overrides"] = {"strength": 0, "feather": 2}
        selected = load_sidecar(data, source)
        self.assertEqual(set(selected), {1})
        self.assertEqual(selected[1].values, {"strength": 0, "feather_sigma": 2})
        for mutation in (dict(data, ass_sha256="0" * 64), dict(data, schema_version=2),
                         dict(data, parser_version="unknown"), dict(data, events=[dict(data["events"][0], index=2)]),
                         dict(data, events=[dict(data["events"][0], event_sha256="wrong")]),
                         dict(data, events=data["events"] * 2)):
            with self.subTest(mutation=mutation), self.assertRaises(ConfigError):
                load_sidecar(mutation, source)
        with self.assertRaises(ConfigError):
            load_sidecar(data, document("changed", second="字"))

    def test_merging_explicit_keys_and_conflicts(self):
        source = document("text")
        data = create_sidecar(source, [0])
        data["events"][0]["overrides"] = {"strength": 0.5, "feather_sigma": 10}
        sidecar = load_sidecar(data, source)[0]
        merged = merge_overrides(parse_marker("bgblur{strength=0.5;padding_x=20}"), sidecar)
        self.assertEqual(merged.values, {"strength": 0.5, "padding_x": 20, "feather_sigma": 10})
        with self.assertRaises(ConfigError):
            merge_overrides(parse_marker("bgblur{strength=0.4}"), sidecar)
        for overrides in ({"sigma": 18}, {"strength": None}, {"burn_subtitles": True}):
            data["events"][0]["overrides"] = overrides
            with self.subTest(overrides=overrides), self.assertRaises(ConfigError):
                load_sidecar(data, source)


if __name__ == "__main__":
    unittest.main()
