import hashlib
import unittest

from assglass.ass import ASSError, AlphaRewriteError, SourceDocument, build_analysis, rewrite_event


HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,28,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1
Style: Alt,Arial,34,&H00FF0000,&H0000FF00,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,20,20,20,1
Style: Transparent,Arial,28,&H80FFFFFF,&H80FFFFFF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def document(text, actor="", effect="", second=None, bom=False, newline="\n", style="Default"):
    value = HEADER + "Dialogue: 0,0:00:00.00,0:00:02.00,{},{},0,0,0,{},{}\n".format(style, actor, effect, text)
    if second is not None:
        value += "Dialogue: 0,0:00:00.00,0:00:02.00,Default,bgblur,0,0,0,,{}\n".format(second)
    raw = value.replace("\n", newline).encode("utf-8")
    return SourceDocument.from_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)


class ASSParserTests(unittest.TestCase):
    def test_raw_unicode_commas_bom_and_newlines(self):
        for bom in (False, True):
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(bom=bom, newline=newline):
                    source = document("字,日本語,אבג\u2028\u0085\ufeff", bom=bom, newline=newline, second="目标,{\\fad(20,20)}")
                    original = source.raw
                    self.assertEqual(source.events[0].text, "字,日本語,אבג\u2028\u0085\ufeff")
                    self.assertEqual(original[source.events[0].text_start:source.events[0].text_end].decode(), source.events[0].text)
                    plan = build_analysis(source, [1])
                    self.assertEqual(plan.analysis_data, original[:source.events[0].text_start] + b"{\\alpha&HFF&}" + original[source.events[0].text_start:])
                    self.assertEqual(source.raw, original)
                    self.assertEqual(source.sha256, hashlib.sha256(original).hexdigest())

    def test_format_actor_alias_and_comments(self):
        raw = (HEADER.replace("Style, Name,", "Style, Actor,") +
               "Comment: 0,0:00:00.00,0:00:02.00,Default,bgblur,0,0,0,,ignored\n" +
               "Dialogue: 0,0:00:00.00,0:00:02.00,Default,bgblurSpeaker,0,0,0,,text,commas").encode()
        source = SourceDocument.from_bytes(raw)
        self.assertEqual(len(source.events), 1)
        self.assertEqual(source.events[0].index, 0)
        self.assertEqual(source.events[0].actor, "bgblurSpeaker")
        self.assertEqual(source.events[0].text, "text,commas")

    def test_blank_lines_keep_physical_line_numbers(self):
        raw = document("text").raw.replace(b"[Events]", b"\n\n[Events]")
        source = SourceDocument.from_bytes(raw)
        prefix = raw[:source.events[0].text_start]
        self.assertEqual(source.events[0].line_number, prefix.count(b"\n") + 1)

    def test_bad_encodings_and_nul(self):
        for raw in (b"\xff", HEADER.encode("utf-16"), b"hello\0there"):
            with self.subTest(raw=raw[:8]), self.assertRaises(ASSError):
                SourceDocument.from_bytes(raw)

    def test_malformed_formats_events_and_styles(self):
        for value in (HEADER.replace("Effect, Text", "Text, Effect"),
                      HEADER.replace("Style, Name,", "Style, Name, Actor,"),
                      HEADER + "Dialogue: 0,broken\n",
                      HEADER.replace("Style: Alt,", "Style: Default,")):
            with self.subTest(value=value[-60:]), self.assertRaises(ASSError):
                SourceDocument.from_bytes(value.encode())


class AlphaRulesTests(unittest.TestCase):
    def rule(self, text, expected, **kwargs):
        source = document(text, **kwargs)
        result = rewrite_event(source, source.events[0])
        self.assertEqual(result.rule_id, expected)
        self.assertTrue(result.hidden_verified)
        self.assertTrue(result.structure_verified)
        return result

    def reject(self, text, reason, **kwargs):
        source = document(text, **kwargs)
        with self.assertRaises(AlphaRewriteError) as caught:
            rewrite_event(source, source.events[0])
        self.assertEqual(caught.exception.rule, "AV1-REJECT-" + reason)
        self.assertIn("Dialogue 0", str(caught.exception))

    def test_prefix_plain_body_and_empty(self):
        for text in ("普通中/英/日文 spaces\\N第二行\\n软换行\\h硬空格", "", "{a comment}文字", "literal \\alpha&H80& text"):
            with self.subTest(text=text):
                self.rule(text, "AV1-PREFIX")

    def test_static_original_fragment_states(self):
        for text in (r"{\alpha&H80&}整行", r"{\1a&H7\2a&HFF\3a&Hf&\4a&H00}all",
                     r"前{\alpha&H00&}后", r"{\alpha&H00&}前{\alpha&H80\alpha&H00}后",
                     r"{\alpha&H80&}{comment}{\alpha&H80}同一状态", r"{\2a&HFF}"):
            with self.subTest(text=text):
                self.rule(text, "AV1-STATIC")
        for text in (r"前{\alpha&H80&}后", r"{\1a&H80}前{\1a&H00}后", r" {\alpha&H80}文字", "\u200b{\\alpha&H80}后"):
            with self.subTest(text=text):
                self.reject(text, "SEGMENT")

    def test_reset_equivalent_transient_and_different(self):
        for text in (r"前{\rAlt}后", r"{\alpha&H80}前{\rAlt\alpha&H80}后", r"{\r}字", r"{\rAlt}"):
            with self.subTest(text=text):
                result = self.rule(text, "AV1-RESET")
                self.assertIn(r"\alpha&HFF&}", result.text)
        self.reject(r"前{\rTransparent}后", "SEGMENT")
        self.reject(r"{\rMissing}后", "SYNTAX")

    def test_whole_transform_all_four_forms(self):
        for timing in ("", "2,", "0,1000,", "0,3000,0.5,"):
            text = r"{\rAlt\alpha&H10\t(" + timing + r"\alpha&H80\2a&HFF)}字{\fs32}后"
            with self.subTest(timing=timing):
                result = self.rule(text, "AV1-T-WHOLE")
                self.assertIn(r"\t(" + timing + r"\alpha&HFF&\2a&HFF&)", result.text)

    def test_transform_rejections(self):
        combos = (r"{\t(\alpha&H80)\r}字", r"{\t(\alpha&H80)\alpha&H80}字",
                  r"{\t(\alpha&H80)\t(\alpha&H00)}字", r"{\t(\t(\alpha&H80))}字",
                  r"{\t(\alpha&H80\fs30)}字", r"字{\t(\alpha&H80)}后",
                  r"{\t(50,50,\alpha&H80)}字", r"{\t(-1,100,\alpha&H80)}字",
                  r"{\t(100,0,\alpha&H80)}字")
        for text in combos:
            with self.subTest(text=text):
                self.reject(text, "COMBINATION")
        for text in (r"{\t(0,1.5,\alpha&H80)}字", r"{\t(0,100,0,\alpha&H80)}字",
                     r"{\t(0,100,NaN,\alpha&H80)}字", r"{\t()}字", r"{\t(\alpha&H80}字"):
            with self.subTest(text=text):
                self.reject(text, "SYNTAX")

    def test_non_alpha_whitelist(self):
        tags = (r"\b700\i1\u1\s0\fnArial Unicode MS\fs32\fscx120\fscy90\fsp-1.5",
                r"\bord3\xbord1\ybord2\shad2\xshad-2\yshad-1\be2\blur0.5",
                r"\c&Hff\1c&H123456&\2c&HAB\3c&H123\4c&H0",
                r"\an2\q0\pos(20.5,40)\org(-1,2)\fr10\frx2\fry3\frz4\fax-0.2\fay0.1\clip(0,1,200,201)",
                r"\iclip(0,0,20,20)")
        for text in tags:
            with self.subTest(text=text):
                result = self.rule("{" + text + "}word", "AV1-PREFIX")
                self.assertEqual(result.text, r"{\alpha&HFF&}{" + text + "}word")

    def test_syntax_and_unsupported(self):
        for tag in (r"\alpha&H000", r"\alpha&HGG", r"\1a", r"\alpha&H1&oops", r"\c&H12345678",
                    r"\fsNaN", r"\fn", r"\an10", r"\q4", r"\be-1", r"\pos(1)", r"\b1.5"):
            with self.subTest(tag=tag):
                self.reject("{" + tag + "}word", "SYNTAX")
        for tag in (r"\fad(10,20)", r"\fade(0,255,0,0,1,2,3)", r"\k20", r"\move(0,0,1,1)",
                    r"\a2", r"\fe1", r"\fs+2", r"\p1", r"\clip(m 0 0 l 2 2)", r"\zunknown42",
                    r"\pos(0,0)\pos(1,1)", r"\clip(0,0,1,1)\iclip(0,0,1,1)"):
            with self.subTest(tag=tag):
                self.reject("{" + tag + "}word", "COMBINATION")
        for text in ("{unclosed", "text}", "{{nested}}", r"{comment\alpha&H80}word"):
            with self.subTest(text=text):
                self.reject(text, "SYNTAX")
        self.reject("word", "COMBINATION", effect="Banner;10;0;0")

    def test_style_validation_and_selected_text_is_never_rewritten(self):
        source = document(r"{\fad(1,2)\t(\fs40)}目标", actor="bgblur")
        plan = build_analysis(source, [0])
        self.assertEqual(plan.analysis_data, source.raw)
        self.assertEqual(plan.records, ())
        source = SourceDocument.from_bytes(document("word").raw.replace(b",1,2,1,2,20", b",3,2,1,2,20"))
        with self.assertRaises(AlphaRewriteError) as caught:
            rewrite_event(source, source.events[0])
        self.assertEqual(caught.exception.rule, "AV1-REJECT-COMBINATION")


if __name__ == "__main__":
    unittest.main()
