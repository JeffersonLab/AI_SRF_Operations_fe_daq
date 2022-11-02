#!/bin/env bash

# Get the directory containing this script
DIR="$( cd "$( dirname "$(readlink -f "${BASH_SOURCE[0]}")" )" >/dev/null 2>&1 && pwd )"

# Make sure our package search path is right
export PATH="/usr/csite/pubtools/python/3.7/bin:$PATH"
export PYTHONPATH="${DIR}/../src/:${PYTHONPATH}"

# Run the app passing along all of the args
python3.7 ${DIR}/../src/fe_daq/main.py "$@"
