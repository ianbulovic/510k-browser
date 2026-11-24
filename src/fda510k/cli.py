import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from fda510k import __version__
from fda510k.config import Paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fda510k",
        description="Pipeline for 510k-browser: FDA 510(k) clearances and their predicate graph.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--root", type=Path, default=Path(), help="repository root (default: current directory)"
    )
    sub = parser.add_subparsers(dest="command", metavar="<stage>")

    sub.add_parser("ingest", help="download openFDA 510(k) and classification metadata")

    seed = sub.add_parser("import-seed", help="import seed edges from an upstream clone")
    seed.add_argument(
        "--from",
        dest="upstream",
        type=Path,
        required=True,
        help="path to a clone of wcedmisten/fda-510k-analysis",
    )

    sub.add_parser("graph", help="merge edge sources and overrides into the predicate graph")

    site = sub.add_parser("build-site-data", help="emit static JSON for the frontend")
    site.add_argument("--out", type=Path, help="output directory (default: build/site-data)")

    sub.add_parser("all", help="run ingest, graph and build-site-data in order")

    probe = sub.add_parser(
        "probe",
        help="check whether this machine can fetch from accessdata.fda.gov (7 polite requests)",
    )
    probe.add_argument(
        "--wait-hours",
        type=float,
        default=0,
        help="if outside FDA's visiting window, wait up to this long for it to open (default: 0)",
    )
    probe.add_argument("--out", type=Path, help="output directory (default: build/probe)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = Paths(args.root.resolve())

    match args.command:
        case None:
            parser.print_help()
        case "ingest":
            _report(_ingest(paths))
        case "import-seed":
            from fda510k import seed

            _report(asdict(seed.run(paths, args.upstream)))
        case "graph":
            _report(_graph(paths))
        case "build-site-data":
            _report(_site_data(paths, args.out))
        case "all":
            _report({"ingest": _ingest(paths), "graph": _graph(paths)})
            _report(_site_data(paths, None))
        case "probe":
            from fda510k import probe

            result = probe.run(args.out or paths.probe, wait_hours=args.wait_hours)
            print(probe.render_summary(result), end="")
            return probe.EXIT_CODES[result.verdict]
    return 0


def _ingest(paths: Paths) -> dict[str, object]:
    from fda510k import ingest

    return asdict(ingest.run(paths))


def _graph(paths: Paths) -> dict[str, object]:
    from fda510k import graph

    return asdict(graph.run(paths))


def _site_data(paths: Paths, out: Path | None) -> dict[str, object]:
    from fda510k import site_data

    meta = site_data.run(paths, out)
    return {"counts": meta["counts"], "file_bytes": meta["file_bytes"]}


def _report(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))
