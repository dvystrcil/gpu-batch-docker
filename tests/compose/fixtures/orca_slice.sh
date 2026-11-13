#!/bin/sh
# orca_slice.sh JOB STL: slice STL with the user's Neptune presets, the way vt350's slice-check.py
# --slicer orca does (gpu-batch-docker#13). The presets (fixtures/orca/, copies of vt350's
# parts/slice/orca/) hold only overrides: Orca resolves their `inherits` against <datadir>/system,
# which orca-datadir fills from the image's own profiles, and only for preset files that sit in
# <datadir>/user/default/<type>/.
set -eu
job=$1; stl=$2; dd=$job/tmp/datadir; u=$dd/user/default
orca-datadir "$dd" ${ORCA_VENDORS:-Elegoo OrcaFilamentLibrary}
for t in machine process filament; do cp /work/_fixtures/orca/$t/*.json "$u/$t/"; done
mkdir -p "$job/out"
m=$(ls "$u"/machine/*.json); p=$(ls "$u"/process/*.json); f=$(ls "$u"/filament/*.json)
exec orca-slicer --datadir "$dd" --load-settings "$m;$p" --load-filaments "$f" --orient 0 --arrange 1 \
  --slice 0 --export-statistics --outputdir "$job/out" "$stl"
