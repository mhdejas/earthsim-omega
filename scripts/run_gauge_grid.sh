#!/usr/bin/env bash
# Runs the gauge_validation_v1 simulations: 2018 grid + prior, then the 2019 baselines (scored only in `test`).
set -u
cd "$(dirname "$0")/.."
jobs="
2018 0.9 0.08
2018 0.9 0.16
2018 1.25 0.08
2018 1.25 0.16
2018 1.75 0.08
2018 1.75 0.16
2018 2.5 0.08
2018 2.5 0.16
2018 0.7 0.08
2019 0.7 0.08
2019 0.9 0.16
"
echo "$jobs" | grep . | xargs -P "${PAR:-4}" -L 1 bash -c \
  'NUMBA_NUM_THREADS=${THREADS:-2} .venv/bin/python -m earthsim.gauges simulate --season $0 --f $1 --n $2 > logs/gauges/run_$0_$1_$2.log 2>&1; echo "done $0 $1 $2 exit $?"'
