import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel_config import resolve_channel_config


class ResolveChannelConfigTests(unittest.TestCase):
    def test_enabled_channel_requires_explicit_supported_mode(self):
        for config in (
                {},
                {"transcode_mode": None},
                {"transcode_mode": "invalid"},
                {"transcode_mode": "Transcode"},
                {"transcode_mode": 1}):
            with self.subTest(config=config), self.assertRaisesRegex(
                    ValueError, "transcode_mode"):
                resolve_channel_config(config)

    def test_transcode_mode_defaults_absent_encoder_keys_independently(self):
        self.assertEqual(
            resolve_channel_config({"transcode_mode": "transcode"}),
            {"enabled": True, "transcode_mode": "transcode",
             "video_encoder": "avc", "audio_encoder": "aac"})
        self.assertEqual(
            resolve_channel_config({
                "transcode_mode": "transcode",
                "video_encoder": "h264"}),
            {"enabled": True, "transcode_mode": "transcode",
             "video_encoder": "libx264", "audio_encoder": "aac"})
        self.assertEqual(
            resolve_channel_config({
                "transcode_mode": "transcode",
                "audio_encoder": "opus"}),
            {"enabled": True, "transcode_mode": "transcode",
             "video_encoder": "avc", "audio_encoder": "opus"})

    def test_each_supported_encoder_name_is_normalized_and_mapped(self):
        supported = {
            "video_encoder": {
                "avc": "libx264",
                "h264": "libx264",
                "h.264": "libx264",
                "libx264": "libx264",
                "libx265": "libx265",
            },
            "audio_encoder": {
                "aac": "aac",
                "libfdk_aac": "libfdk_aac",
                "opus": "opus",
            },
        }
        defaults = {"video_encoder": "avc", "audio_encoder": "aac"}
        for key, names in supported.items():
            for name, expected in names.items():
                with self.subTest(encoder=key, name=name):
                    config = {
                        "transcode_mode": "transcode",
                        key: f" \t{name.swapcase()} \n",
                    }
                    result = resolve_channel_config(config)
                    self.assertEqual(result[key], expected)
                    other_key = "audio_encoder" if key == "video_encoder" \
                        else "video_encoder"
                    self.assertEqual(result[other_key], defaults[other_key])

    def test_explicit_null_encoder_is_rejected_independently(self):
        for key in ("video_encoder", "audio_encoder"):
            with self.subTest(encoder=key), self.assertRaisesRegex(
                    ValueError, key):
                resolve_channel_config({
                    "transcode_mode": "transcode", key: None})

    def test_invalid_encoder_values_are_rejected_independently(self):
        invalid_values = (
            None, "", "   ", "NoNe", "COPY", "hevc", "not-an-encoder",
            0, 1, False, [], {},
        )
        for key in ("video_encoder", "audio_encoder"):
            for value in invalid_values:
                with self.subTest(encoder=key, value=value), \
                        self.assertRaisesRegex(ValueError, key):
                    resolve_channel_config({
                        "transcode_mode": "transcode", key: value})

    def test_disabled_channel_is_exempt_from_mode_and_encoder_validation(self):
        for config in (
                {"enabled": False},
                {"enabled": False, "transcode_mode": "invalid",
                 "video_encoder": None, "audio_encoder": "copy"}):
            with self.subTest(config=config):
                self.assertEqual(resolve_channel_config(config),
                                 {"enabled": False})

    def test_passthrough_ignores_and_omits_encoder_values(self):
        class UninspectableConfig(dict):
            def __getitem__(self, key):
                if key in ("video_encoder", "audio_encoder"):
                    raise AssertionError("passthrough inspected encoder field")
                return super().__getitem__(key)

        configs = (
            {"transcode_mode": "passthrough"},
            {"transcode_mode": "passthrough",
             "video_encoder": None, "audio_encoder": ""},
            {"transcode_mode": "passthrough",
             "video_encoder": "   ", "audio_encoder": "none"},
            {"transcode_mode": "passthrough",
             "video_encoder": "copy", "audio_encoder": "unsupported"},
            {"transcode_mode": "passthrough",
             "video_encoder": object(), "audio_encoder": []},
        )
        for config in configs:
            with self.subTest(config=config):
                self.assertEqual(
                    resolve_channel_config(UninspectableConfig(config)),
                    {"enabled": True, "transcode_mode": "passthrough"})


if __name__ == "__main__":
    unittest.main()
