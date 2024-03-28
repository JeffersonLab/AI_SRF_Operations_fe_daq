#!/bin/env bash

# Get the directory containing this script
DIR="$( cd "$( dirname "$(readlink -f "${BASH_SOURCE[0]}")" )" >/dev/null 2>&1 && pwd )"

# Define the CSUE python
PYTHON_BASE="$HBASE/apps/f/fe_daq/dvl/support/python3"
PYTHON="$PYTHON_BASE/bin/python3.7"

# Make sure our package search path is right
export PATH="$PYTHON_BASE/bin:$PATH"
export PYTHONPATH="${DIR}/../src/fe_daq/src:${PYTHONPATH}"


# Run the app passing along all of the args
#python3.7 ${DIR}/../src/fe_daq/src/fe_daq/main.py "$@"
$PYTHON ${DIR}/../src/fe_daq/src/fe_daq/main.py "$@"
