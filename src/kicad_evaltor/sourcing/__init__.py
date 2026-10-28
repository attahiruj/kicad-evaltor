"""Online sourcing: look part numbers up in distributor catalogues.

Install with the ``sourcing`` extra (it adds no dependencies; lookups use the
standard library). Importing this package registers :class:`SourcingCheck`
under ``sch.sourcing``.
"""

from kicad_evaltor.sourcing.check import SourcingCheck, SourcingParams
from kicad_evaltor.sourcing.match import (
    PartRule,
    PartView,
    catalogue_mismatch,
    part_colours,
    part_identity,
    rule_for,
    same_part,
)
from kicad_evaltor.sourcing.providers import (
    DigiKeyProvider,
    HttpResponse,
    JlcpcbProvider,
    MouserProvider,
    SourcingLookupError,
    SourcingProvider,
    Transport,
    default_provider,
    urllib_transport,
)
from kicad_evaltor.sourcing.record import (
    CHIP_SIZES,
    Inferred,
    PartRecord,
    colours_in,
    make_record,
)

LcscProvider = JlcpcbProvider
"""LCSC numbers are looked up through the JLCPCB catalogue; same class."""

__all__ = [
    "CHIP_SIZES",
    "DigiKeyProvider",
    "HttpResponse",
    "Inferred",
    "JlcpcbProvider",
    "LcscProvider",
    "MouserProvider",
    "PartRecord",
    "PartRule",
    "PartView",
    "SourcingCheck",
    "SourcingLookupError",
    "SourcingParams",
    "SourcingProvider",
    "Transport",
    "catalogue_mismatch",
    "colours_in",
    "default_provider",
    "make_record",
    "part_colours",
    "part_identity",
    "rule_for",
    "same_part",
    "urllib_transport",
]
