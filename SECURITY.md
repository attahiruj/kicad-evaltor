# Security Policy

## Supported versions

This project is pre-release (0.x). Fixes land on the latest release only.

| Version | Supported |
|---------|-----------|
| 0.1.x   | yes       |
| < 0.1   | no        |

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Open a private advisory via GitHub's
[security advisory form](https://github.com/kicad-evaltor/kicad-evaltor/security/advisories/new)
on this repository. Include what you found, the version affected, and how to
reproduce it. You should get an acknowledgement within a week.

## Scope

Worth reporting: parsing of untrusted `.kicad_sch` / `.kicad_pcb` files, path
handling in `DesignContext`, and command construction for `kicad-cli`
subprocesses — in particular anything that lets a crafted design file run an
arbitrary command or read outside the project.

Not a vulnerability: a check reporting a false positive, or a crash on a
malformed file that does not escape the project directory.
