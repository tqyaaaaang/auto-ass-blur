#!/usr/bin/env python3
"""Create a SHA-256-bound sidecar using zero-based Dialogue indices."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assglass.ass import SourceDocument
from assglass.config import create_sidecar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subtitle", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--index", type=int, action="append", default=[])
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    source = SourceDocument.read(args.subtitle)
    if args.list:
        for event in source.events:
            print(json.dumps({"index": event.index, "line": event.line_number,
                              "start_ms": event.start_ms, "end_ms": event.end_ms,
                              "actor": event.actor, "text": event.text}, ensure_ascii=False))
        return
    if args.output is None:
        parser.error("需要 -o，或使用 --list 列出事件")
    if len(set(args.index)) != len(args.index) or any(i < 0 or i >= len(source.events) for i in args.index):
        parser.error("--index 必须是不重复、从 0 开始的有效 Dialogue 序号")
    data = create_sidecar(source, args.index)
    with args.output.open("x", encoding="utf-8") as sink:
        json.dump(data, sink, ensure_ascii=False, indent=2)
        sink.write("\n")


if __name__ == "__main__":
    main()
