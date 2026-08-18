"""Unit tests for the Magpie capture engine's pure logic.

Run: python3 -m unittest discover -s tests -v  (from scripts/capture/)
No network, no ffmpeg — pure functions only.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture import estimate_frame_tokens, estimate_transcript_tokens, slugify  # noqa: E402
from media import allocate_candidates, frame_budget, frame_width, hamming  # noqa: E402
from transcribe import cues_to_markdown, fmt_ts, parse_ts, parse_vtt  # noqa: E402


class TestTimestamps(unittest.TestCase):
    def test_parse_plain_seconds(self):
        self.assertEqual(parse_ts("45"), 45.0)

    def test_parse_mm_ss(self):
        self.assertEqual(parse_ts("12:34"), 754.0)

    def test_parse_hh_mm_ss(self):
        self.assertEqual(parse_ts("1:02:03"), 3723.0)

    def test_parse_vtt_style_millis(self):
        self.assertEqual(parse_ts("00:01:02.500"), 62.5)

    def test_fmt_roundtrip(self):
        self.assertEqual(fmt_ts(754), "12:34")
        self.assertEqual(fmt_ts(3723), "1:02:03")
        self.assertEqual(fmt_ts(5), "0:05")


class TestFrameBudget(unittest.TestCase):
    def test_short_video_floors(self):
        # 2-minute video never drops below the floor for its mode
        self.assertEqual(frame_budget(120, "glance"), 6)
        self.assertEqual(frame_budget(120, "standard"), 10)
        self.assertEqual(frame_budget(120, "fine"), 16)

    def test_long_video_caps(self):
        # 3-hour video hits the ceiling, never unbounded
        self.assertEqual(frame_budget(3 * 3600, "glance"), 16)
        self.assertEqual(frame_budget(3 * 3600, "standard"), 32)
        self.assertEqual(frame_budget(3 * 3600, "fine"), 64)

    def test_mid_video_scales_between(self):
        b = frame_budget(30 * 60, "standard")  # 30 min
        self.assertGreater(b, 10)
        self.assertLessEqual(b, 32)

    def test_modes_are_ordered(self):
        for dur in (60, 600, 3600, 7200):
            self.assertLessEqual(frame_budget(dur, "glance"), frame_budget(dur, "standard"))
            self.assertLessEqual(frame_budget(dur, "standard"), frame_budget(dur, "fine"))

    def test_widths(self):
        self.assertEqual(frame_width("glance"), 640)
        self.assertEqual(frame_width("standard"), 960)
        self.assertEqual(frame_width("fine"), 1280)


class TestCandidateAllocation(unittest.TestCase):
    def test_uniform_no_chapters(self):
        cands = allocate_candidates(600, budget=10, chapters=[])
        self.assertGreaterEqual(len(cands), 20)  # oversampled >= 2x budget
        times = [c.t for c in cands]
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(times[0], 0)
        self.assertLess(times[-1], 600)
        self.assertFalse(any(c.protected for c in cands))

    def test_chapter_starts_are_protected(self):
        chapters = [
            {"title": "Intro", "start_time": 0, "end_time": 100},
            {"title": "Build", "start_time": 100, "end_time": 500},
            {"title": "Wrap", "start_time": 500, "end_time": 600},
        ]
        cands = allocate_candidates(600, budget=12, chapters=chapters)
        protected = [c for c in cands if c.protected]
        self.assertEqual(len(protected), 3)
        # each protected candidate sits just after its chapter start
        starts = sorted(c.t for c in protected)
        for got, want in zip(starts, [0, 100, 500]):
            self.assertLessEqual(abs(got - (want + 1.0)), 1.5)
        # chapter titles ride along for the manifest
        self.assertEqual({c.chapter for c in cands if c.chapter} ,
                         {"Intro", "Build", "Wrap"})

    def test_window_respected(self):
        cands = allocate_candidates(600, budget=8, chapters=[], start=120, end=300)
        for c in cands:
            self.assertGreaterEqual(c.t, 120)
            self.assertLessEqual(c.t, 300)


class TestHamming(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(hamming(0xFFFF, 0xFFFF), 0)

    def test_known_distance(self):
        self.assertEqual(hamming(0b1010, 0b0101), 4)
        self.assertEqual(hamming(0, (1 << 64) - 1), 64)


class TestVtt(unittest.TestCase):
    FIXTURE = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
so today we're going to

00:00:03.000 --> 00:00:05.000 align:start position:0%
so today we're going to
<c>build something new</c>

00:00:05.000 --> 00:00:07.000
build something new

00:00:07.000 --> 00:00:09.000
it has three parts
"""

    def test_rolling_duplicates_collapse(self):
        cues = parse_vtt(self.FIXTURE)
        text = " ".join(c["text"] for c in cues)
        self.assertEqual(text.count("so today we're going to"), 1)
        self.assertEqual(text.count("build something new"), 1)
        self.assertIn("it has three parts", text)

    def test_tags_stripped(self):
        cues = parse_vtt(self.FIXTURE)
        for c in cues:
            self.assertNotIn("<c>", c["text"])
            self.assertNotIn("align:", c["text"])

    def test_cues_carry_start_seconds(self):
        cues = parse_vtt(self.FIXTURE)
        self.assertEqual(cues[0]["start"], 1.0)

    def test_markdown_groups_by_window(self):
        cues = [
            {"start": 0, "text": "alpha"},
            {"start": 10, "text": "beta"},
            {"start": 40, "text": "gamma"},
        ]
        md = cues_to_markdown(cues, group_s=30)
        self.assertIn("**[0:00]**", md)
        self.assertIn("**[0:40]**", md)
        # alpha and beta share the first 30s block
        block = [ln for ln in md.splitlines() if "0:00" in ln][0]
        self.assertIn("alpha", block)
        self.assertIn("beta", block)


class TestReceiptMath(unittest.TestCase):
    def test_transcript_tokens_chars_over_4(self):
        self.assertEqual(estimate_transcript_tokens("x" * 4000), 1000)

    def test_frame_tokens_area_over_750(self):
        # 960x540 ≈ 691 tokens
        self.assertEqual(estimate_frame_tokens([(960, 540)]), 691)
        self.assertEqual(estimate_frame_tokens([(960, 540)] * 10), 6912)

    def test_empty(self):
        self.assertEqual(estimate_frame_tokens([]), 0)
        self.assertEqual(estimate_transcript_tokens(""), 0)


class TestSlug(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("How I Built This: Part 2!"), "how-i-built-this-part-2")

    def test_truncates_long(self):
        self.assertLessEqual(len(slugify("word " * 40)), 60)

    def test_empty_fallback(self):
        self.assertEqual(slugify("???"), "capture")




class TestCaptionTrackSelection(unittest.TestCase):
    def test_prefers_manual_over_auto(self):
        from media import pick_caption_track
        subs = {"en": [{"url": "http://manual", "ext": "vtt"}]}
        auto = {"en": [{"url": "http://auto", "ext": "vtt"}]}
        self.assertEqual(pick_caption_track(subs, auto), "http://manual")

    def test_falls_back_to_auto(self):
        from media import pick_caption_track
        auto = {"en": [{"url": "http://auto", "ext": "vtt"}]}
        self.assertEqual(pick_caption_track({}, auto), "http://auto")

    def test_prefers_vtt_ext_and_en_variants(self):
        from media import pick_caption_track
        subs = {"en-US": [{"url": "http://srv3", "ext": "srv3"},
                          {"url": "http://vtt", "ext": "vtt"}]}
        self.assertEqual(pick_caption_track(subs, {}), "http://vtt")

    def test_none_when_no_english(self):
        from media import pick_caption_track
        self.assertIsNone(pick_caption_track({"fr": [{"url": "x", "ext": "vtt"}]}, {}))
        self.assertIsNone(pick_caption_track({}, {}))


if __name__ == "__main__":
    unittest.main()
