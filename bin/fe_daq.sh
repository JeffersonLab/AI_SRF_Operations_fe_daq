#!/cs/dvlhome/apps/f/fe_daq/dvl/support/csueLib/bin/csueLaunch /bin/bash -f
# DO NOT MODIFY ABOVE LINE - it is managed automatically
csueAppName="fe_daq"
csueAppVer="dvl"
csueAppLetter="f"
# USE THESE VARIABLES TO BUILD PATHS TO SUPPORT OR FILEIO
# (Leave them here for automatic management)
#
# The following code creates a variable which holds the path to
# the current version of the application ($csueAppPath).
# It can be used to build  paths to the support directory or
# fileio directories.
#
# This code may be stripped out if it is not needed.
#
csueAppPath="$HBASE/apps/$csueAppLetter/$csueAppName/$csueAppVer"
#
# Make the req() function available, as in csh scripts
source $csueAppPath/support/csueLib/lib/Csue.sh
#
# End of Template Code


# Get the directory containing this script
DIR="$( cd "$( dirname "$(readlink -f "${BASH_SOURCE[0]}")" )" >/dev/null 2>&1 && pwd )"

# Define the CSUE python
PYTHON_BASE="$csueAppPath/support/python3"
#PYTHON_BASE="$HBASE/apps/f/fe_daq/dvl/support/python3"
PYTHON="$PYTHON_BASE/bin/python3.7"

# Make sure our package search path is right
export PATH="$PYTHON_BASE/bin:$PATH"
export PYTHONPATH="${csueAppPath}/src/fe_daq/src:${PYTHONPATH}"


# Run the app passing along all of the args
#python3.7 ${DIR}/../src/fe_daq/src/fe_daq/main.py "$@"
$PYTHON ${csueAppPath}/src/fe_daq/src/fe_daq/main.py "$@"
