"""Explicit release-test toolchain gate; not invoked by library users."""

import argparse
import json
from dataclasses import asdict

from kicad_evaltor.toolchain import probe_toolchain
from kicad_evaltor.utils.kicad_cli import find_kicad_cli


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", required=True)
    args = parser.parse_args()
    cli = find_kicad_cli()
    info = probe_toolchain(str(cli) if cli else None, include_pcbnew=True, timeout=60)
    print(json.dumps(asdict(info), indent=2))
    if info.errors or info.cli_version != args.expected or info.versions_match is not True:
        raise SystemExit("The release-test toolchain does not match the declared versions")


if __name__ == "__main__":
    main()
