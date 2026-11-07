#!/usr/bin/env bash
set -euo pipefail

# Smoke test for prusaslicer CLI using Docker Compose

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
chmod 777 "$WORK"

# Create a minimal valid STL file for testing
cat > "$WORK/test_part.stl" <<'STL'
solid test
  facet normal 0 0 1
    vertex 0 0 0
    vertex 1 0 0
    vertex 0 1 0
  endfacet
endsolid test
STL

cp "$WORK/test_part.stl" .
cp ./spec.json "$WORK/"

run_case() {
  local case_name=$1
  local state=$2
  
  mkdir -p "$WORK/$case_name"
  cp ./test_part.stl "$WORK/$case_name/"
  cp ./spec.json "$WORK/$case_name/"
  
  echo "Running $case_name case (printer=$state)..."
  MOONRAKER_STATE=$state docker compose -f docker-compose.test.yml \
    --project-directory . \
    --project-name gpu-batch-smoke \
    up --abort-on-container-exit --remove-orphans
  
  if [ $? -ne 0 ]; then
    echo "$case_name: FAILED"
    return 1
  fi
  
  echo "$case_name: PASSED"
}

run_case idle standby
run_case busy printing

echo "SMOKE-COMPLETE"
