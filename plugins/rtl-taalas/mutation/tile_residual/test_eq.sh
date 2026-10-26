#!/bin/bash
set -u
unset PYTHONHOME PYTHONPATH
bash $SCRIPTS/create_mutated.sh -o mutated.il
cp $PRJDIR/tile_residual.v .
yosys -q -s $PRJDIR/test_eq.ys > eq.log 2>&1
if [ $? -eq 0 ]; then echo "1 PASS" > output.txt; else echo "1 FAIL" > output.txt; fi
exit 0