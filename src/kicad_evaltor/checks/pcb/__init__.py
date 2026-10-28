from kicad_evaltor.checks.pcb.copper import (
    CurrentCapacityCheck,
    PlaneCoverageCheck,
    ThermalViasCheck,
    ViaRulesCheck,
)
from kicad_evaltor.checks.pcb.diff_pair import DiffPairCheck
from kicad_evaltor.checks.pcb.drc_check import DRCRunCheck
from kicad_evaltor.checks.pcb.drc_rules import (
    FootprintMatchesLibraryCheck,
    RoutingCompleteCheck,
    SchematicParityCheck,
    SilkOverCopperCheck,
    SilkOverlapCheck,
    TuningRulesCheck,
)
from kicad_evaltor.checks.pcb.fabrication import FabExportsCheck, PositionFileCheck
from kicad_evaltor.checks.pcb.footprint_exists import FootprintExistsCheck
from kicad_evaltor.checks.pcb.footprint_overlap import FootprintOverlapCheck
from kicad_evaltor.checks.pcb.matches_schematic import FootprintMatchesSchematicCheck
from kicad_evaltor.checks.pcb.mechanical import (
    BoardOutlineCheck,
    FootprintOnBoardCheck,
    FootprintPlacementCheck,
    MountingHolesCheck,
)
from kicad_evaltor.checks.pcb.net_rules import LengthMatchCheck, NetLayersCheck, NetViasCheck
from kicad_evaltor.checks.pcb.power_pour import PowerPourCheck
from kicad_evaltor.checks.pcb.proximity import DecouplingCoverageCheck, FootprintNearCheck
from kicad_evaltor.checks.pcb.silkscreen import ReferenceVisibleCheck, TextOffBoardCheck
from kicad_evaltor.checks.pcb.trace_length import TraceLengthCheck
from kicad_evaltor.checks.pcb.trace_width import TraceWidthCheck

__all__ = [
    "BoardOutlineCheck",
    "CurrentCapacityCheck",
    "DRCRunCheck",
    "DecouplingCoverageCheck",
    "DiffPairCheck",
    "FabExportsCheck",
    "FootprintExistsCheck",
    "FootprintMatchesLibraryCheck",
    "FootprintMatchesSchematicCheck",
    "FootprintNearCheck",
    "FootprintOnBoardCheck",
    "FootprintOverlapCheck",
    "FootprintPlacementCheck",
    "LengthMatchCheck",
    "MountingHolesCheck",
    "NetLayersCheck",
    "NetViasCheck",
    "PlaneCoverageCheck",
    "PositionFileCheck",
    "PowerPourCheck",
    "ReferenceVisibleCheck",
    "RoutingCompleteCheck",
    "SchematicParityCheck",
    "SilkOverCopperCheck",
    "SilkOverlapCheck",
    "TextOffBoardCheck",
    "ThermalViasCheck",
    "TraceLengthCheck",
    "TraceWidthCheck",
    "TuningRulesCheck",
    "ViaRulesCheck",
]
