#!/usr/bin/env python3
"""Example usage of kicad-evaltor."""

from kicad_evaltor import (
    CheckRegistry,
)


def main():
    # List all available checks
    print("Available checks:")
    for check_id, cls in CheckRegistry.list():
        print(f"  {check_id}: {cls.name}")

    # Example: Run checks on a KiCad project
    # with DesignContext(project_path="myproject.kicad_pro", headless=True) as ctx:
    #     runner = TestRunner([
    #         ComponentExistsCheck(reference="R1"),
    #         ComponentValueCheck(reference="R1", expected="10k"),
    #         ComponentsConnectedCheck(reference_a="R1", reference_b="C1"),
    #         ERCRunCheck(severity="error"),
    #         FootprintExistsCheck(reference="R1"),
    #         TraceWidthCheck(min_width_mm=0.2, net_name="VCC"),
    #         DRCRunCheck(severity="error"),
    #     ])
    #     report = runner.run(ctx)
    #
    #     print(report.summary())
    #     assert report.all_passed

    print("\nExample usage (commented out - requires KiCad project):")
    print("""
with DesignContext(project_path="myproject.kicad_pro", headless=True) as ctx:
    runner = TestRunner([
        ComponentExistsCheck(reference="R1"),
        ComponentValueCheck(reference="R1", expected="10k"),
        ComponentsConnectedCheck(reference_a="R1", reference_b="C1"),
        ERCRunCheck(severity="error"),
    ])
    report = runner.run(ctx)
    print(report.summary())
    assert report.all_passed
""")


if __name__ == "__main__":
    main()
