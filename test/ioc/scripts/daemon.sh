#!/bin/sh

# Figure out script dir and cd there
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
if [ -n "$DIR" ] ; then
  cd $DIR
else
  echo "error determining script dir"
  exit 1
fi

if [ -f 'pid.txt' ] ; then
  echo "pid.txt lock file found.  Not starting softIOC"
  exit 1
fi

# Run the IOC
procServ -n "DAQ Test SoftIOC" -L /dev/null -i ^D^C 21000 ./start.sh
#procServ -n "DAQ Test SoftIOC" -L /dev/null -i ^D^C 20000 ./start.sh 2> /dev/null
#procServ -n "DAQ Test SoftIOC" -L /dev/null -i  20000 ./start.sh
#procServ -n "DAQ Test SoftIOC" -L /dev/null -i 20000 ./start.sh 2> /dev/null

export PYTHONPATH="$DIR/../../../src/"

# Run the app that provides the IOC initialization and logic
/usr/csite/pubtools/bin/python3.7 ./run_ioc.py
