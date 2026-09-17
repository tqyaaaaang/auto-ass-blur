"""Strict, byte-preserving ASS parsing and the versioned alpha rewrite.

Event identities are zero-based Dialogue indices in the original byte stream.
Comment lines never acquire an event identity. Parsing never normalizes Unicode,
line endings, names, or subtitle text.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

PARSER_VERSION = "assglass-ass-v1"
ALPHA_RULES_VERSION = "alpha-v1.1"
HIDDEN = r"{\alpha&HFF&}"
_ALPHA_NAMES = {"alpha", "1a", "2a", "3a", "4a"}
_TIME = re.compile(r"(\d+):([0-5]\d):([0-5]\d)\.(\d{2})\Z")
_DECIMAL = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_INTEGER = re.compile(r"-?\d+\Z")


class ASSError(ValueError):
    pass


class AlphaRewriteError(ASSError):
    def __init__(self, rule: str, event: "Event", detail: str):
        self.rule = rule
        self.event = event
        super().__init__("{}: Dialogue {} (line {}, {}..{} ms): {}".format(
            rule, event.index, event.line_number, event.start_ms, event.end_ms, detail))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_time(value: str) -> int:
    match = _TIME.fullmatch(value.strip())
    if not match:
        raise ASSError("Invalid ASS timestamp {!r}; expected H:MM:SS.cc".format(value))
    h, m, s, cs = map(int, match.groups())
    return ((h * 60 + m) * 60 + s) * 1000 + cs * 10


@dataclass(frozen=True)
class Event:
    index: int
    line_number: int
    start_ms: int
    end_ms: int
    style: str
    actor: str
    effect: str
    text: str
    text_start: int
    text_end: int
    fields: Mapping[str, str]
    event_sha256: str

    @property
    def key(self) -> str:
        return "dialogue:{}".format(self.index)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class Style:
    name: str
    fields: Mapping[str, str]
    line_number: int

    def alpha(self) -> Tuple[int, int, int, int]:
        try:
            return tuple(_style_color(self.fields[key]) >> 24 for key in (
                "primarycolour", "secondarycolour", "outlinecolour", "backcolour"))
        except (KeyError, ValueError) as error:
            raise ASSError("Style {!r}, line {}: invalid or missing colour: {}".format(
                self.name, self.line_number, error))


def _style_color(value: str) -> int:
    value = value.strip()
    if re.fullmatch(r"&H[0-9A-Fa-f]{1,8}&?", value):
        return int(value[2:].rstrip("&"), 16)
    if re.fullmatch(r"-?\d+", value):
        integer = int(value)
        if -(1 << 31) <= integer < (1 << 32):
            return integer & 0xFFFFFFFF
    raise ValueError("invalid ASS colour {!r}".format(value))


@dataclass(frozen=True)
class SourceDocument:
    raw: bytes
    sha256: str
    bom: bool
    events: Tuple[Event, ...]
    styles: Mapping[str, Style]
    path: Optional[str] = None
    parser_version: str = PARSER_VERSION

    @classmethod
    def read(cls, path) -> "SourceDocument":
        return cls.from_bytes(Path(path).read_bytes(), str(Path(path).resolve()))

    @classmethod
    def from_bytes(cls, raw: bytes, path: Optional[str] = None) -> "SourceDocument":
        if not isinstance(raw, bytes):
            raise TypeError("ASS input must be bytes")
        if b"\x00" in raw:
            raise ASSError("ASS contains a NUL byte at byte {}; only strict UTF-8 is supported".format(raw.index(b"\x00")))
        bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ASSError("ASS is not strict UTF-8 at byte {}: {}".format(error.start, error.reason))
        events = []
        styles = {}
        section = ""
        event_format = None
        style_format = None
        offset = 0
        # str.splitlines also treats U+0085/U+2028/U+2029 as record separators;
        # those are ordinary subtitle characters and must stay in Text.
        lines = (match.group() for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z", text))
        for number, original_line in enumerate(lines, 1):
            line = original_line.rstrip("\r\n")
            line_prefix_bytes = 0
            if number == 1 and bom:
                line = line[1:]
                line_prefix_bytes = 3
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.lower()
            elif ":" in line and section in ("[events]", "[v4+ styles]"):
                label, payload = line.split(":", 1)
                kind = label.strip().lower()
                if kind == "format":
                    fields = tuple(part.strip().lower() for part in payload.split(","))
                    if not all(fields) or len(set(fields)) != len(fields):
                        raise ASSError("Invalid duplicate/empty Format field at line {}".format(number))
                    if section == "[events]":
                        if not {"start", "end", "style", "effect", "text"}.issubset(fields):
                            raise ASSError("Events Format missing required fields at line {}".format(number))
                        if not ("name" in fields or "actor" in fields) or {"name", "actor"}.issubset(fields):
                            raise ASSError("Events Format must have exactly one Name or Actor field at line {}".format(number))
                        if fields[-1] != "text":
                            raise ASSError("Text must be the last Events Format field for unambiguous comma preservation (line {})".format(number))
                        event_format = fields
                    else:
                        if "name" not in fields:
                            raise ASSError("Styles Format missing Name at line {}".format(number))
                        style_format = fields
                elif section == "[events]" and kind == "dialogue":
                    if event_format is None:
                        raise ASSError("Dialogue before Events Format at line {}".format(number))
                    # Only whitespace before the first field is syntactic. Actor/Text are exact values.
                    parts = payload.split(",", len(event_format) - 1)
                    if len(parts) != len(event_format):
                        raise ASSError("Malformed Dialogue field count at line {}".format(number))
                    fields = dict(zip(event_format, parts))
                    start, end = parse_time(fields["start"]), parse_time(fields["end"])
                    if end < start:
                        raise ASSError("Dialogue End is before Start at line {}".format(number))
                    actor = fields.get("actor", fields.get("name", ""))
                    before_text = line[:len(label) + 1] + ",".join(parts[:-1]) + ","
                    text_start = offset + line_prefix_bytes + len(before_text.encode("utf-8"))
                    body = fields["text"]
                    diagnostic = json.dumps({"start_ms": start, "end_ms": end,
                        "style": fields["style"].strip(), "text": body,
                        "actor": actor, "effect": fields["effect"]}, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    events.append(Event(len(events), number, start, end, fields["style"].strip(),
                        actor, fields["effect"], body, text_start, text_start + len(body.encode("utf-8")),
                        fields, _hash(diagnostic)))
                elif section == "[v4+ styles]" and kind == "style":
                    if style_format is None:
                        raise ASSError("Style before Styles Format at line {}".format(number))
                    parts = payload.split(",")
                    if len(parts) != len(style_format):
                        raise ASSError("Malformed Style field count at line {}".format(number))
                    fields = dict(zip(style_format, (part.strip() for part in parts)))
                    name = fields["name"]
                    if not name or name in styles:
                        raise ASSError("Empty or ambiguous duplicate Style {!r} at line {}".format(name, number))
                    styles[name] = Style(name, fields, number)
            offset += len(original_line.encode("utf-8"))
        if event_format is None:
            raise ASSError("ASS is missing an [Events] Format")
        return cls(raw, _hash(raw), bom, tuple(events), styles, path)

    def replace_texts(self, replacements: Mapping[int, str]) -> bytes:
        output = bytearray()
        cursor = 0
        for event in self.events:
            if event.index not in replacements:
                continue
            output.extend(self.raw[cursor:event.text_start])
            output.extend(replacements[event.index].encode("utf-8"))
            cursor = event.text_end
        output.extend(self.raw[cursor:])
        return bytes(output)

    def manifest(self) -> dict:
        return {"ass_sha256": self.sha256, "ass_encoding": "utf-8", "ass_bom": self.bom,
                "parser_version": self.parser_version, "dialogue_count": len(self.events)}


@dataclass(frozen=True)
class Token:
    name: str
    value: str
    start: int
    end: int
    children: Tuple["Token", ...] = ()


@dataclass(frozen=True)
class TextPart:
    text: str
    start: int


def _fail(event: Event, kind: str, detail: str):
    raise AlphaRewriteError("AV1-REJECT-" + kind, event, detail)


_KNOWN_TAGS = tuple(sorted((
    "alpha", "1a", "2a", "3a", "4a", "fscx", "fscy", "xbord", "ybord",
    "xshad", "yshad", "iclip", "blur", "bord", "shad", "clip", "pos", "org",
    "frx", "fry", "frz", "fax", "fay", "fsp", "fade", "move", "fad",
    "1c", "2c", "3c", "4c", "an", "fn", "fs", "fr", "be", "kf", "ko", "kt",
    "b", "i", "u", "s", "c", "q", "r", "t", "a", "p", "k", "K", "fe", "pbo"), key=len, reverse=True))


def _tokens(content: str, base: int, event: Event) -> Tuple[Token, ...]:
    """Parse complete override tokens, respecting parentheses and source offsets."""
    output = []
    cursor = 0
    # A block with no backslash is a comment. Mixed comments are ambiguous.
    if "\\" not in content:
        return ()
    if not content.startswith("\\"):
        _fail(event, "SYNTAX", "mixed comment/tag block at text offset {}".format(base))
    while cursor < len(content):
        start = cursor
        if content[cursor] != "\\":
            _fail(event, "SYNTAX", "expected tag at text offset {}".format(base + cursor))
        cursor += 1
        name = next((name for name in _KNOWN_TAGS if content.startswith(name, cursor)), None)
        if name is None:
            unknown = content[cursor:].split("\\", 1)[0]
            _fail(event, "COMBINATION", "unsupported tag \\{}".format(unknown))
        cursor += len(name)
        value_start = cursor
        depth = 0
        while cursor < len(content):
            char = content[cursor]
            if char == "\\" and depth == 0:
                break
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    _fail(event, "SYNTAX", "unmatched ')' at text offset {}".format(base + cursor))
            cursor += 1
        if depth:
            _fail(event, "SYNTAX", "unclosed parentheses in \\{} at text offset {}".format(name, base + start))
        value = content[value_start:cursor]
        children = ()
        if name == "t":
            if not value.startswith("(") or not value.endswith(")"):
                _fail(event, "SYNTAX", "transform requires complete parentheses")
            inner = value[1:-1]
            slash = inner.find("\\")
            if slash < 0:
                _fail(event, "SYNTAX", "transform is missing alpha tags")
            children = _tokens(inner[slash:], base + value_start + 1 + slash, event)
        output.append(Token(name, value, base + start, base + cursor, children))
    return tuple(output)


def parse_override_text(text: str, event: Event) -> Tuple[object, ...]:
    output = []
    cursor = 0
    while cursor < len(text):
        if text[cursor] == "}":
            _fail(event, "SYNTAX", "unmatched '}}' at text offset {}".format(cursor))
        if text[cursor] == "{":
            end = text.find("}", cursor + 1)
            if end < 0 or "{" in text[cursor + 1:end]:
                _fail(event, "SYNTAX", "malformed override block at text offset {}".format(cursor))
            output.extend(_tokens(text[cursor + 1:end], cursor + 1, event))
            cursor = end + 1
        else:
            end = cursor + 1
            while end < len(text) and text[end] not in "{}":
                end += 1
            output.append(TextPart(text[cursor:end], cursor))
            cursor = end
    return tuple(output)


def _numeric(value: str, event: Event, name: str, integer=False) -> float:
    import math
    if not (_INTEGER if integer else _DECIMAL).fullmatch(value):
        _fail(event, "SYNTAX", "invalid {} parameter {!r}".format(name, value))
    result = float(value)
    if not math.isfinite(result):
        _fail(event, "SYNTAX", "nonfinite {} parameter".format(name))
    return result


def _alpha_value(value: str, event: Event) -> int:
    if not re.fullmatch(r"&H[0-9A-Fa-f]{1,2}&?", value):
        _fail(event, "SYNTAX", "invalid alpha literal {!r}".format(value))
    return int(value[2:].rstrip("&"), 16)


def _style(document: SourceDocument, name: str, event: Event) -> Tuple[int, ...]:
    style = document.styles.get(name)
    if style is None:
        _fail(event, "SYNTAX", "unknown Style {!r}".format(name))
    try:
        border = int(style.fields["borderstyle"])
        result = style.alpha()
    except (ValueError, KeyError) as error:
        _fail(event, "SYNTAX", str(error))
    if border != 1:
        _fail(event, "COMBINATION", "Style {!r} has unsupported BorderStyle={}".format(name, border))
    return result


def _validate_token(token: Token, event: Event, geometry_counts: Dict[str, int]):
    name, value = token.name, token.value
    if name in _ALPHA_NAMES:
        _alpha_value(value, event)
    elif name == "r":
        pass  # Style references must keep their internal spaces and are resolved below.
    elif name == "fn":
        if not value or not value.strip():
            _fail(event, "SYNTAX", "font name must be nonempty")
    elif name in {"c", "1c", "2c", "3c", "4c"}:
        # An omitted RGB value restores the corresponding current Style colour
        # in libass. It preserves alpha, so retain the reset token unchanged.
        if value and not re.fullmatch(r"&H[0-9A-Fa-f]{1,6}&?", value):
            _fail(event, "SYNTAX", "invalid RGB override {!r}".format(value))
    elif name in {"pos", "org", "clip", "iclip"}:
        if not value.startswith("(") or not value.endswith(")"):
            _fail(event, "SYNTAX", "{} requires parentheses".format(name))
        parts = value[1:-1].split(",")
        count = 2 if name in {"pos", "org"} else 4
        if len(parts) != count:
            _fail(event, "COMBINATION" if name in {"clip", "iclip"} else "SYNTAX", "unsupported {} arguments".format(name))
        for part in parts:
            _numeric(part.strip(), event, name)
        counter = "clip" if name == "iclip" else name
        geometry_counts[counter] = geometry_counts.get(counter, 0) + 1
        if geometry_counts[counter] > 1:
            _fail(event, "COMBINATION", "multiple {} tags in one line".format(counter))
    elif name in {"b", "i", "u", "s", "an", "q", "be"}:
        number = _numeric(value, event, name, integer=True)
        valid = (name == "b" and (number in (0, 1, -1) or 100 <= number <= 1000)
                 or name in {"i", "u", "s"} and number in (0, 1)
                 or name == "an" and 1 <= number <= 9
                 or name == "q" and 0 <= number <= 3
                 or name == "be" and number >= 0)
        if not valid:
            _fail(event, "SYNTAX", "{} value out of range".format(name))
    elif name in {"fs", "fscx", "fscy", "fsp", "bord", "xbord", "ybord", "shad", "xshad", "yshad", "blur", "fr", "frx", "fry", "frz", "fax", "fay"}:
        if name == "fs" and value[:1] in ("+", "-"):
            _fail(event, "COMBINATION", "relative font-size syntax is unsupported")
        number = _numeric(value, event, name)
        if name in {"fs", "fscx", "fscy", "bord", "xbord", "ybord", "blur", "shad"} and number < 0:
            _fail(event, "SYNTAX", "{} must be nonnegative".format(name))
    elif name == "t":
        _validate_transform(token, event)
    else:
        _fail(event, "COMBINATION", "unsupported tag \\{}".format(name))


def _validate_transform(token: Token, event: Event):
    inner = token.value[1:-1]
    slash = inner.find("\\")
    head = inner[:slash]
    if head:
        if not head.endswith(","):
            _fail(event, "SYNTAX", "transform timing must end with comma")
        arguments = head[:-1].split(",")
    else:
        arguments = []
    if len(arguments) > 3:
        _fail(event, "SYNTAX", "too many transform timing arguments")
    if len(arguments) in (2, 3):
        start = _numeric(arguments[0].strip(), event, "t1", integer=True)
        end = _numeric(arguments[1].strip(), event, "t2", integer=True)
        if start < 0 or end < 0 or start >= end:
            _fail(event, "COMBINATION", "transform requires 0 <= t1 < t2")
    elif event.duration_ms <= 0:
        _fail(event, "COMBINATION", "transform without explicit times requires positive event duration")
    if len(arguments) in (1, 3):
        accel = _numeric(arguments[-1].strip(), event, "accel")
        if accel <= 0:
            _fail(event, "SYNTAX", "transform acceleration must be positive")
    if not token.children:
        _fail(event, "SYNTAX", "empty transform")
    for child in token.children:
        if child.name not in _ALPHA_NAMES:
            _fail(event, "COMBINATION", "only one nonnested pure-alpha transform is supported")
        _alpha_value(child.value, event)


@dataclass(frozen=True)
class RewriteRecord:
    event_index: int
    rule_id: str
    action: str
    original_sha256: str
    rewritten_sha256: str
    text: str
    edits: Tuple[Tuple[int, int, str], ...]
    hidden_verified: bool = True
    structure_verified: bool = True


def rewrite_event(document: SourceDocument, event: Event) -> RewriteRecord:
    """Classify original alpha states before performing any byte-local rewrite."""
    state = _style(document, event.style, event)
    # libass trims ASCII spaces/tabs from fields and recognises only these
    # case-sensitive transition prefixes. Unknown effects (including Aegisub's
    # "fx" marker) are inert; preserve them instead of rejecting all metadata.
    if event.effect.strip(" \t").startswith(("Banner;", "Scroll up;", "Scroll down;")):
        _fail(event, "COMBINATION", "unselected event uses a scrolling Effect unsupported by alpha analysis: {!r}".format(event.effect))
    items = parse_override_text(event.text, event)
    geometry_counts = {}
    for item in items:
        if isinstance(item, Token):
            _validate_token(item, event, geometry_counts)
            if item.name == "r":
                _style(document, item.value or event.style, event)
    transforms = [item for item in items if isinstance(item, Token) and item.name == "t"]
    if len(transforms) > 1:
        _fail(event, "COMBINATION", "multiple transforms are unsupported")
    seen_text = False
    seen_transform = False
    seen_alpha = False
    seen_reset = False
    states = []
    edits = []
    for item in items:
        if isinstance(item, TextPart):
            seen_text = True
            states.append(state)
            continue
        if item.name in _ALPHA_NAMES:
            if seen_transform:
                _fail(event, "COMBINATION", "alpha after transform is unsupported")
            seen_alpha = True
            value = _alpha_value(item.value, event)
            state = ((value,) * 4 if item.name == "alpha" else
                     tuple(value if channel == int(item.name[0]) - 1 else previous for channel, previous in enumerate(state)))
            edits.append((item.start + len(item.name) + 1, item.end, "&HFF&"))
        elif item.name == "r":
            if seen_transform:
                _fail(event, "COMBINATION", "reset after transform is unsupported")
            seen_reset = True
            state = _style(document, item.value or event.style, event)
            edits.append((item.end, item.end, r"\alpha&HFF&"))
        elif item.name == "t":
            if seen_text:
                _fail(event, "COMBINATION", "transform must precede the first body character")
            seen_transform = True
            for child in item.children:
                edits.append((child.start + len(child.name) + 1, child.end, "&HFF&"))
            # Some stock libass builds interpolate FF -> FF using floating
            # arithmetic and truncate the result to FE at intermediate times.
            # Keep the original transform (and its collision semantics), then
            # restore exact static FF before any body glyph is processed.
            edits.append((item.end, item.end, r"\alpha&HFF&"))
    if not transforms and states and any(current != states[0] for current in states[1:]):
        _fail(event, "SEGMENT", "body fragments use different original four-channel alpha states; hiding would change layout segmentation")
    rule = "AV1-T-WHOLE" if transforms else "AV1-RESET" if seen_reset else "AV1-STATIC" if seen_alpha else "AV1-PREFIX"
    changed = event.text
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        changed = changed[:start] + replacement + changed[end:]
    changed = HIDDEN + changed
    _verify_hidden(changed, event, document)
    # Removing only alpha tokens from both token streams verifies all body, RGB,
    # geometry, reset names, and transform timing/structure remain byte-identical.
    if _without_alpha(event.text, event) != _without_alpha(changed, event):
        _fail(event, "SYNTAX", "internal rewrite structure verification failed")
    return RewriteRecord(event.index, rule, "prefix" if rule == "AV1-PREFIX" else "normalize",
        _hash(event.text.encode("utf-8")), _hash(changed.encode("utf-8")), changed, tuple(edits))


def _without_alpha(text: str, event: Event) -> tuple:
    output = []
    for item in parse_override_text(text, event):
        if isinstance(item, TextPart):
            # Adjacent text segments separated only by alpha blocks are equivalent.
            if output and output[-1][0] == "text":
                output[-1] = ("text", output[-1][1] + item.text)
            else:
                output.append(("text", item.text))
        elif item.name not in _ALPHA_NAMES:
            if item.name == "t":
                slash = item.value.find("\\")
                output.append(("t", item.value[:slash], tuple(child.name for child in item.children)))
            else:
                output.append((item.name, item.value))
    return tuple(output)


def _verify_hidden(text: str, event: Event, document: SourceDocument):
    state = _style(document, event.style, event)
    for item in parse_override_text(text, event):
        if isinstance(item, TextPart):
            if state != (255, 255, 255, 255):
                _fail(event, "SYNTAX", "internal rewrite hidden-state verification failed")
        elif item.name == "r":
            state = _style(document, item.value or event.style, event)
        elif item.name in _ALPHA_NAMES:
            value = _alpha_value(item.value, event)
            state = ((value,) * 4 if item.name == "alpha" else
                     tuple(value if channel == int(item.name[0]) - 1 else previous for channel, previous in enumerate(state)))
        elif item.name == "t":
            if state != (255, 255, 255, 255) or any(_alpha_value(child.value, event) != 255 for child in item.children):
                _fail(event, "SYNTAX", "internal transform hidden-state verification failed")


@dataclass(frozen=True)
class AlphaRewritePlan:
    source_sha256: str
    analysis_sha256: str
    analysis_data: bytes
    records: Tuple[RewriteRecord, ...]
    parser_version: str = PARSER_VERSION
    alpha_rules_version: str = ALPHA_RULES_VERSION

    def manifest(self) -> dict:
        return {"source_sha256": self.source_sha256, "analysis_sha256": self.analysis_sha256,
            "parser_version": self.parser_version, "alpha_rules_version": self.alpha_rules_version,
            "transform_rounding_guard": "static-ff-after-transform-v1",
            "prefix_count": sum(item.action == "prefix" for item in self.records),
            "normalize_count": sum(item.action == "normalize" for item in self.records),
            "rules": [{"event_index": item.event_index, "rule_id": item.rule_id,
                       "hidden_verified": item.hidden_verified, "structure_verified": item.structure_verified}
                      for item in self.records]}


def build_analysis(document: SourceDocument, target_indices: Sequence[int]) -> AlphaRewritePlan:
    selected = frozenset(target_indices)
    if any(type(index) is not int or index < 0 or index >= len(document.events) for index in selected):
        raise ASSError("Selection contains invalid Dialogue index")
    records = tuple(rewrite_event(document, event) for event in document.events if event.index not in selected)
    data = document.replace_texts({record.event_index: record.text for record in records})
    reparsed = SourceDocument.from_bytes(data)
    if len(reparsed.events) != len(document.events):
        raise ASSError("Analysis rewrite changed event count")
    for before, after in zip(document.events, reparsed.events):
        if {key: value for key, value in before.fields.items() if key != "text"} != {key: value for key, value in after.fields.items() if key != "text"}:
            raise ASSError("Analysis rewrite changed event metadata")
        if before.index in selected and before.text != after.text:
            raise ASSError("Analysis rewrite changed selected Text")
    return AlphaRewritePlan(document.sha256, _hash(data), data, records)
