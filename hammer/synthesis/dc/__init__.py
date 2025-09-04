#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer-vlsi plugin for Synopsys DC.
#
#  See LICENSE for licence details.

from typing import List, Optional, Dict

import os
import re

from hammer.vlsi import HammerSynthesisTool, HammerToolStep
from hammer.vlsi import SynopsysTool
from hammer.logging import HammerVLSILogging
import hammer.tech as hammer_tech
from hammer.tech import HammerTechnologyUtils

import os
import re
from pathlib import Path
import importlib.resources

class DC(HammerSynthesisTool, SynopsysTool):
    def fill_outputs(self) -> bool:
        # Check that the mapped.v exists if the synthesis run was successful
        # TODO: move this check upwards?
        mapped_v = os.path.join(self.result_dir, self.top_module + ".mapped.v")
        if not os.path.isfile(mapped_v):
            raise ValueError("Output mapped verilog %s not found" % (mapped_v))  # better error?
        self.output_files = [mapped_v]
        return True

    def tool_config_prefix(self) -> str:
        return "synthesis.dc"

    @property
    def post_synth_sdc(self) -> Optional[str]:
        return os.path.join(self.result_dir, self.top_module + ".mapped.sdc")

    @property
    def steps(self) -> List[HammerToolStep]:
        return self.make_steps_from_methods([
            self.init_environment,
            self.elaborate_design,
            self.apply_constraints,
            self.optimize_design,
            self.generate_reports,
            self.write_outputs,
        ])

    def tool_config_prefix(self) -> str:
        return "synthesis.dc"

    # TODO(edwardw): move this to synopsys common
    def generate_tcl_preferred_routing_direction(self):
        """
        Generate a TCL fragment for setting preferred routing directions.
        """
        output = []

        # Suppress PSYN-882 ("Warning: Consecutive metal layers have the same preferred routing direction") while the layer routing is being built.
        output.append("set suppress_errors  [concat $suppress_errors  [list PSYN-882]]")

        # TODO This is broken with the API change to stackups ucb-bar/hammer#308
        #for library in self.technology.config.libraries:
        #    if library.metal_layers is not None:
        #        for layer in library.metal_layers:
        #            output.append("set_preferred_routing_direction -layers {{ {0} }} -direction {1}".format(layer.name, layer.preferred_routing_direction))


        output.append("set suppress_errors  [lminus $suppress_errors  [list PSYN-882]]")
        output.append("")  # Add newline at the end
        return "\n".join(output)

    def disable_congestion_map(self) -> None:
        """Disables the congestion map generation in rm_dc_scripts/dc.tcl since it requires a GUI and licences.
        """
        dc_tcl_path = os.path.join(self.run_dir, "rm_dc_scripts/dc.tcl")

        with open(dc_tcl_path) as f:
            dc_tcl = f.read()

        congestion_map_fragment = """
  # Use the following to generate and write out a congestion map from batch mode
  # This requires a GUI session to be temporarily opened and closed so a valid DISPLAY
  # must be set in your UNIX environment.

  if {[info exists env(DISPLAY)]} {
    gui_start
"""
        congestion_map_search = re.escape(congestion_map_fragment)
        # We want to capture & replace that condition.
        # Unfortunately, we can't replace within a group, so we'll have to replace around it.
        # e.g. foobarbaz -> foo123baz requires (foo)bar(baz) -> \1 123 \2 -> foo123baz
        cond = re.escape("[info exists env(DISPLAY)]")
        congestion_map_search_and_capture = "(" + congestion_map_search.replace(cond, ")(" + cond + ")(") + ")"

        output = re.sub(congestion_map_search_and_capture, "\g<1>false\g<3>", dc_tcl)

        self.write_contents_to_path(output, dc_tcl_path)

    def main_step(self) -> bool:
        # TODO(edwardw): move most of this to Synopsys common since it's not DC-specific.
        # Locate reference methodology tarball.
        synopsys_rm_tarball = self.get_synopsys_rm_tarball("DC")

        # Library setup
        for db in self.timing_dbs:
            if not os.path.exists(db):
                self.logger.error("Cannot find %s" % db)
                return False
        self.append("set_app_var target_library \"%s\"" % ' '.join(self.timing_dbs))
        self.append("set_app_var synthetic_library dw_foundation.sldb")
        self.append("set_app_var link_library \"* $target_library $synthetic_library\"")

        # For designs that don't have tight QoR constraints and don't have register retiming,
        # you can use the following variable to enable the highest productivity single pass flow.
        # This flow modifies the optimizations to make verification easier.
        # This variable setting should be applied prior to reading in the RTL for the design.
        self.append("set_app_var simplified_verification_mode false")
        self.append("set_svf results/%s.mapped.svf" % self.top_module)

        return True

    def elaborate_design(self) -> bool:
        # Add any verilog_synth wrappers
        # (which are needed in some technologies e.g. for SRAMs)
        # which need to be synthesized.
        verilog = self.verilog + self.technology.read_libs([
            hammer_tech.filters.verilog_synth_filter
        ], HammerTechnologyUtils.to_plain_item)

        # Generate preferred_routing_directions.
        preferred_routing_directions_fragment = os.path.join(self.run_dir, "preferred_routing_directions.tcl")
        self.write_contents_to_path(self.generate_tcl_preferred_routing_direction(), preferred_routing_directions_fragment)

        # Generate clock constraints.
        clock_constraints_fragment = os.path.join(self.run_dir, "clock_constraints_fragment.tcl")
        self.write_contents_to_path(self.sdc_clock_constraints, clock_constraints_fragment)

        # Get libraries.
        lib_args = self.technology.read_libs([
            hammer_tech.filters.timing_db_filter.copy(update={'tag': 'lib'}),
            hammer_tech.filters.milkyway_lib_dir_filter.copy(update={'tag': "milkyway"}),
            hammer_tech.filters.tlu_max_cap_filter.copy(update={'tag': "tlu_max"}),
            hammer_tech.filters.tlu_min_cap_filter.copy(update={'tag': "tlu_min"}),
            hammer_tech.filters.tlu_map_file_filter.copy(update={'tag': "tlu_map"}),
            hammer_tech.filters.milkyway_techfile_filter.copy(update={'tag': "tf"})
        ], HammerTechnologyUtils.to_command_line_args)

        # Pre-extract the tarball (so that we can make TCL modifications in Python)
        self.run_executable([
            "tar", "-xf", synopsys_rm_tarball, "-C", self.run_dir, "--strip-components=1"
        ])

        # Disable the DC congestion map if needed.
        if not self.get_setting("synthesis.dc.enable_congestion_map"):
            self.disable_congestion_map()

        compile_args = self.get_setting("synthesis.dc.compile_args")
        if not compile_args:
          compile_args = []

        # Build args.
        syn_script_path = Path(self.technology.cache_dir) / "run-synthesis"
        syn_script_txt = importlib.resources.files("hammer.synthesis.dc.tools").joinpath("run-synthesis").read_text()
        syn_script_path.write_text(syn_script_txt)

        tcl_path = Path(self.technology.cache_dir) / "find_regs.tcl"
        tcl_txt = importlib.resources.files("hammer.synthesis.dc.tools").joinpath("find_regs.tcl").read_text()
        tcl_path.write_text(tcl_txt)

        args = [
            syn_script_path,
            "--dc", dc_bin,
            "--clock_constraints_fragment", clock_constraints_fragment,
            "--preferred_routing_directions_fragment", preferred_routing_directions_fragment,
            "--find_regs_tcl", tcl_path,
            "--run_dir", self.run_dir,
            "--top", self.top_module
        ]
        args.extend(input_files)  # We asserted these are Verilog above
        args.extend(lib_args)
        for compile_arg in compile_args:
          args.extend(["--compile_arg", compile_arg])

        # Temporarily disable colours/tag to make DC run output more readable.
        # TODO: think of a more elegant way to do this?
        HammerVLSILogging.enable_colour = False
        HammerVLSILogging.enable_tag = False
        dc_bin = os.path.basename(self.get_setting("synthesis.dc.dc_bin"))
        dc_tcl = os.path.join(self.script_dir, "dc.tcl")
        with open(dc_tcl, 'w') as _f:
            _f.write('\n'.join(self.output))
            _f.write('\nexit')
        args = [dc_bin, "-64bit", "-f", dc_tcl]
        # TODO: check outputs from lines?
        lines = self.run_executable(args, self.run_dir)
        HammerVLSILogging.enable_colour = True
        HammerVLSILogging.enable_tag = True
        return True

tool = DC
