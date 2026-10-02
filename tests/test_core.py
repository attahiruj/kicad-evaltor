from dataclasses import dataclass
import pytest
from kicad_evaltor.checks.base import Check, CheckCategory, CheckParams, CheckResult, TestStatus
from kicad_evaltor.checks.registry import CheckRegistry, register
from kicad_evaltor.utils.units import to_mm, to_nm, to_mils, from_mils


class TestTestStatus:
    def test_enum_values(self) -> None:
        assert TestStatus.PASS.value == "pass"
        assert TestStatus.FAIL.value == "fail"
        assert TestStatus.ERROR.value == "error"
        assert TestStatus.SKIP.value == "skip"


class TestCheckResult:
    def test_pass_factory(self) -> None:
        result = CheckResult.pass_("test.check", "All good", detail="value")
        assert result.status == TestStatus.PASS
        assert result.check_id == "test.check"
        assert result.message == "All good"
        assert result.details == {"detail": "value"}
        assert result.is_pass
        assert not result.is_fail

    def test_fail_factory(self) -> None:
        result = CheckResult.fail("test.check", "Failed", reason="bad")
        assert result.status == TestStatus.FAIL
        assert result.is_fail
        assert not result.is_pass

    def test_error_factory(self) -> None:
        result = CheckResult.error("test.check", "Error occurred")
        assert result.status == TestStatus.ERROR
        assert result.is_fail

    def test_skip_factory(self) -> None:
        result = CheckResult.skip("test.check", "Skipped")
        assert result.status == TestStatus.SKIP
        assert not result.is_pass
        assert not result.is_fail


class TestCheckParams:
    def test_validate_default(self) -> None:
        params = CheckParams()
        params.validate()

    def test_custom_validation(self) -> None:
        @dataclass
        class CustomParams(CheckParams):
            value: int

            def validate(self):
                if self.value < 0:
                    raise ValueError("value must be >= 0")

        params = CustomParams(value=5)
        params.validate()

        with pytest.raises(ValueError):
            CustomParams(value=-1).validate()


class TestCheckRegistry:
    def setup_method(self) -> None:
        CheckRegistry.clear()

    def test_register_decorator(self) -> None:
        @register
        class TestCheck(Check):
            id = "test.check"
            name = "Test Check"
            description = "A test check"
            category = CheckCategory.SCHEMATIC

            @dataclass
            class Params(CheckParams):
                pass

            def run(self, ctx):
                return CheckResult.pass_(self.id)

        assert CheckRegistry.get("test.check") is TestCheck
        assert ("test.check", TestCheck) in CheckRegistry.list()

    def test_register_with_custom_id(self) -> None:
        @register(id="custom.id")
        class TestCheck(Check):
            id = "original.id"
            name = "Test Check"
            description = "A test check"
            category = CheckCategory.SCHEMATIC

            @dataclass
            class Params(CheckParams):
                pass

            def run(self, ctx):
                return CheckResult.pass_(self.id)

        assert CheckRegistry.get("custom.id") is TestCheck
        assert "original.id" not in dict(CheckRegistry.list())

    def test_create(self) -> None:
        @register
        class TestCheck(Check):
            id = "test.check"
            name = "Test Check"
            description = "A test check"
            category = CheckCategory.SCHEMATIC

            @dataclass
            class Params(CheckParams):
                value: str = "default"

            def run(self, ctx):
                return CheckResult.pass_(self.id)

        check = CheckRegistry.create("test.check", value="custom")
        assert check.params.value == "custom"

    def test_get_missing(self) -> None:
        with pytest.raises(KeyError):
            CheckRegistry.get("nonexistent")

    def test_duplicate_registration(self) -> None:
        @register
        class TestCheck1(Check):
            id = "test.check.1"
            name = "Test Check 1"
            description = "A test check"
            category = CheckCategory.SCHEMATIC
            Params = CheckParams

            def run(self, ctx):
                return CheckResult.pass_(self.id)

        # Second registration with same ID should raise ValueError
        with pytest.raises(ValueError):

            @register(id="test.check.1")
            class TestCheck2(Check):
                id = "test.check.2"
                name = "Test Check 2"
                description = "A test check"
                category = CheckCategory.SCHEMATIC
                Params = CheckParams

                def run(self, ctx):
                    return CheckResult.pass_(self.id)


class TestUnits:
    def test_to_mm(self) -> None:
        assert to_mm(1_000_000) == 1.0
        assert to_mm(500_000) == 0.5
        assert to_mm(0) == 0.0

    def test_to_nm(self) -> None:
        assert to_nm(1.0) == 1_000_000
        assert to_nm(0.5) == 500_000

    def test_to_mils(self) -> None:
        assert abs(to_mils(1.0) - 39.3701) < 0.001

    def test_from_mils(self) -> None:
        assert abs(from_mils(39.3701) - 1.0) < 0.001

    def test_roundtrip(self) -> None:
        mm = 2.54
        nm = to_nm(mm)
        back = to_mm(nm)
        assert abs(back - mm) < 0.001


class MockContext:
    def __init__(self, has_schematic=True, has_board=True):
        self._has_schematic = has_schematic
        self._has_board = has_board

    def has_schematic(self):
        return self._has_schematic

    def has_board(self):
        return self._has_board


class TestComponentExistsCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.component_exists import ComponentExistsParams

        with pytest.raises(ValueError):
            ComponentExistsParams()

        params = ComponentExistsParams(reference="R1")
        params.validate()

        params = ComponentExistsParams(lib_id="Device:R")
        params.validate()

        params = ComponentExistsParams(lib_id_pattern="Device:*")
        params.validate()

        params = ComponentExistsParams(value="10k")
        params.validate()


class TestComponentValueCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.component_value import ComponentValueParams

        with pytest.raises(ValueError):
            ComponentValueParams(reference="R1", expected=None)

        with pytest.raises(ValueError):
            ComponentValueParams(reference="", expected="10k")

        params = ComponentValueParams(reference="R1", expected="10k")
        params.validate()

        params = ComponentValueParams(reference="R1", expected="10k", case_sensitive=True)
        params.validate()


class TestComponentsConnectedCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.components_connected import ComponentsConnectedParams

        with pytest.raises(ValueError):
            ComponentsConnectedParams(reference_a="R1", reference_b="", min_connections=1)

        with pytest.raises(ValueError):
            ComponentsConnectedParams(reference_a="R1", reference_b="C1", min_connections=0)

        params = ComponentsConnectedParams(reference_a="R1", reference_b="C1")
        params.validate()

        params = ComponentsConnectedParams(reference_a="R1", reference_b="C1", min_connections=2)
        params.validate()


class TestFootprintExistsCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.pcb.footprint_exists import FootprintExistsParams

        with pytest.raises(ValueError):
            FootprintExistsParams()

        params = FootprintExistsParams(reference="R1")
        params.validate()

        params = FootprintExistsParams(lib_id="Resistor_SMD:R_0805")
        params.validate()


class TestTraceWidthCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.pcb.trace_width import TraceWidthParams

        with pytest.raises(ValueError):
            TraceWidthParams(min_width_mm=0)

        with pytest.raises(ValueError):
            TraceWidthParams(min_width_mm=0.2, max_width_mm=0.1)

        with pytest.raises(ValueError):
            TraceWidthParams(min_width_mm=0.2)

        params = TraceWidthParams(min_width_mm=0.2, net_name="NET1")
        params.validate()

        params = TraceWidthParams(min_width_mm=0.2, reference="R1")
        params.validate()


class TestERCRunCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.erc_check import ERCRunParams

        params = ERCRunParams()
        params.validate()

        params = ERCRunParams(severity="warning")
        params.validate()

        params = ERCRunParams(severity="all")
        params.validate()

        with pytest.raises(ValueError):
            ERCRunParams(severity="invalid")


class TestDRCRunCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.pcb.drc_check import DRCRunParams

        params = DRCRunParams()
        params.validate()

        params = DRCRunParams(severity="warning", schematic_parity=False)
        params.validate()

        with pytest.raises(ValueError):
            DRCRunParams(severity="invalid")


class TestTraceLengthCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.pcb.trace_length import TraceLengthParams

        with pytest.raises(ValueError):
            TraceLengthParams(reference="R1", max_length_mm=0)

        with pytest.raises(ValueError):
            TraceLengthParams(reference="R1", max_length_mm=10, min_length_mm=-1)

        params = TraceLengthParams(reference="R1", max_length_mm=50)
        params.validate()

        params = TraceLengthParams(reference="R1", max_length_mm=50, min_length_mm=10)
        params.validate()


class TestFootprintOverlapCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.pcb.footprint_overlap import FootprintOverlapParams

        with pytest.raises(ValueError):
            FootprintOverlapParams(min_clearance_mm=-0.1)

        params = FootprintOverlapParams()
        params.validate()

        params = FootprintOverlapParams(min_clearance_mm=0.2, exclude_refs=["R1", "C1"])
        params.validate()


class TestPowerPourCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.pcb.power_pour import PowerPourParams

        with pytest.raises(ValueError):
            PowerPourParams(net_name="")

        with pytest.raises(ValueError):
            PowerPourParams(net_name="VCC", min_area_mm2=0)

        params = PowerPourParams(net_name="VCC")
        params.validate()

        params = PowerPourParams(net_name="VCC", min_area_mm2=100)
        params.validate()


class TestConsistencyCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.consistency import ConsistencyParams

        params = ConsistencyParams()
        params.validate()

        params = ConsistencyParams(require_both=False)
        params.validate()


class TestComponentPropertyCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.component_property import ComponentPropertyParams

        with pytest.raises(ValueError):
            ComponentPropertyParams(reference="", field="value", expected="10k")

        with pytest.raises(ValueError):
            ComponentPropertyParams(reference="R1", field="", expected="10k")

        params = ComponentPropertyParams(reference="R1", field="Datasheet", expected="url")
        params.validate()


class TestFootprintAssignedCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.footprint_assigned import FootprintAssignedParams

        params = FootprintAssignedParams()
        params.validate()

        params = FootprintAssignedParams(reference="R1", allow_none=True)
        params.validate()


class TestSymbolInLibraryCheck:
    def test_params_validation(self) -> None:
        from kicad_evaltor.checks.schematic.symbol_in_library import SymbolInLibraryParams

        with pytest.raises(ValueError):
            SymbolInLibraryParams(lib_id="")

        params = SymbolInLibraryParams(lib_id="Device:R")
        params.validate()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
