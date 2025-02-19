"""
Fixture collector class used to collect, sort and combine the different types of generated
fixtures.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Literal, Optional, Tuple

from ethereum_test_base_types import to_json

from .base import BaseFixture
from .consume import FixtureConsumer
from .file import Fixtures


def strip_test_prefix(name: str) -> str:
    """Remove test prefix from a test case name."""
    test_prefix = "test_"
    if name.startswith(test_prefix):
        return name[len(test_prefix) :]
    return name


def get_module_relative_output_dir(test_module: Path, filler_path: Path) -> Path:
    """
    Return a directory name for the provided test_module (relative to the
    base ./tests directory) that can be used for output (within the
    configured fixtures output path or the base_dump_dir directory).

    Example:
    tests/shanghai/eip3855_push0/test_push0.py -> shanghai/eip3855_push0/test_push0

    """
    basename = test_module.with_suffix("").absolute()
    basename_relative = basename.relative_to(
        os.path.commonpath([filler_path.absolute(), basename])
    )
    module_path = basename_relative.parent / basename_relative.stem
    return module_path


@dataclass(kw_only=True)
class TestInfo:
    """Contains test information from the current node."""

    name: str  # pytest: Item.name
    id: str  # pytest: Item.nodeid
    original_name: str  # pytest: Item.originalname
    path: Path  # pytest: Item.path

    def get_name_and_parameters(self) -> Tuple[str, str]:
        """
        Convert test name to a tuple containing the test name and test parameters.

        Example:
        test_push0_key_sstore[fork_Shanghai] -> test_push0_key_sstore, fork_Shanghai

        """
        test_name, parameters = self.name.split("[")
        return test_name, re.sub(r"[\[\-]", "_", parameters).replace("]", "")

    def get_single_test_name(self) -> str:
        """Convert test name to a single test name."""
        test_name, test_parameters = self.get_name_and_parameters()
        return f"{test_name}__{test_parameters}"

    def get_dump_dir_path(
        self,
        base_dump_dir: Optional[Path],
        filler_path: Path,
        level: Literal["test_module", "test_function", "test_parameter"] = "test_parameter",
    ) -> Optional[Path]:
        """Path to dump the debug output as defined by the level to dump at."""
        if not base_dump_dir:
            return None
        test_module_relative_dir = get_module_relative_output_dir(self.path, filler_path)
        if level == "test_module":
            return Path(base_dump_dir) / Path(str(test_module_relative_dir).replace(os.sep, "__"))
        test_name, test_parameter_string = self.get_name_and_parameters()
        flat_path = f"{str(test_module_relative_dir).replace(os.sep, '__')}__{test_name}"
        if level == "test_function":
            return Path(base_dump_dir) / flat_path
        elif level == "test_parameter":
            return Path(base_dump_dir) / flat_path / test_parameter_string
        raise Exception("Unexpected level.")


@dataclass(kw_only=True)
class FixtureCollector:
    """Collects all fixtures generated by the test cases."""

    output_dir: Path
    flat_output: bool
    single_fixture_per_file: bool
    filler_path: Path
    base_dump_dir: Optional[Path] = None

    # Internal state
    all_fixtures: Dict[Path, Fixtures] = field(default_factory=dict)
    json_path_to_test_item: Dict[Path, TestInfo] = field(default_factory=dict)

    def get_fixture_basename(self, info: TestInfo) -> Path:
        """Return basename of the fixture file for a given test case."""
        if self.flat_output:
            if self.single_fixture_per_file:
                return Path(strip_test_prefix(info.get_single_test_name()))
            return Path(strip_test_prefix(info.original_name))
        else:
            relative_fixture_output_dir = Path(info.path).parent / strip_test_prefix(
                Path(info.path).stem
            )
            module_relative_output_dir = get_module_relative_output_dir(
                relative_fixture_output_dir, self.filler_path
            )

            if self.single_fixture_per_file:
                return module_relative_output_dir / strip_test_prefix(info.get_single_test_name())
            return module_relative_output_dir / strip_test_prefix(info.original_name)

    def add_fixture(self, info: TestInfo, fixture: BaseFixture) -> Path:
        """Add fixture to the list of fixtures of a given test case."""
        fixture_basename = self.get_fixture_basename(info)

        fixture_path = (
            self.output_dir
            / fixture.output_base_dir_name()
            / fixture_basename.with_suffix(fixture.output_file_extension)
        )
        if fixture_path not in self.all_fixtures.keys():  # relevant when we group by test function
            self.all_fixtures[fixture_path] = Fixtures(root={})
            self.json_path_to_test_item[fixture_path] = info

        self.all_fixtures[fixture_path][info.id] = fixture

        return fixture_path

    def dump_fixtures(self) -> None:
        """Dump all collected fixtures to their respective files."""
        if self.output_dir.name == "stdout":
            combined_fixtures = {
                k: to_json(v) for fixture in self.all_fixtures.values() for k, v in fixture.items()
            }
            json.dump(combined_fixtures, sys.stdout, indent=4)
            return
        os.makedirs(self.output_dir, exist_ok=True)
        for fixture_path, fixtures in self.all_fixtures.items():
            os.makedirs(fixture_path.parent, exist_ok=True)
            if len({fixture.__class__ for fixture in fixtures.values()}) != 1:
                raise TypeError("All fixtures in a single file must have the same format.")
            fixtures.collect_into_file(fixture_path)

    def verify_fixture_files(self, evm_fixture_verification: FixtureConsumer) -> None:
        """Run `evm [state|block]test` on each fixture."""
        for fixture_path, name_fixture_dict in self.all_fixtures.items():
            for _fixture_name, fixture in name_fixture_dict.items():
                if evm_fixture_verification.can_consume(fixture.__class__):
                    info = self.json_path_to_test_item[fixture_path]
                    consume_direct_dump_dir = self._get_consume_direct_dump_dir(info)
                    evm_fixture_verification.consume_fixture(
                        fixture.__class__,
                        fixture_path,
                        fixture_name=None,
                        debug_output_path=consume_direct_dump_dir,
                    )

    def _get_consume_direct_dump_dir(
        self,
        info: TestInfo,
    ):
        """
        Directory to dump the current test function's fixture.json and fixture
        verification debug output.
        """
        if not self.base_dump_dir:
            return None
        if self.single_fixture_per_file:
            return info.get_dump_dir_path(
                self.base_dump_dir, self.filler_path, level="test_parameter"
            )
        else:
            return info.get_dump_dir_path(
                self.base_dump_dir, self.filler_path, level="test_function"
            )
