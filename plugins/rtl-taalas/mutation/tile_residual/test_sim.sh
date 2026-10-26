#!/bin/bash
set -u
unset PYTHONHOME PYTHONPATH
bash $SCRIPTS/create_mutated.sh
rm -rf obj_dir
verilator --binary --timing -Wno-fatal --top-module tile_residual_tb $PRJDIR/tile_residual_tb.v mutated.v > compile.log 2>&1
if [ $? -ne 0 ]; then echo "1 ERROR" > output.txt; exit 0; fi
./obj_dir/Vtile_residual_tb > sim.out 2>&1
if grep -q PASS sim.out; then echo "1 PASS" > output.txt; else echo "1 FAIL" > output.txt; fi
exit 0