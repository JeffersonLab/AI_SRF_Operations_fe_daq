#!/bin/sh

# Figure out script dir and cd there
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
if [ -n "$DIR" ] ; then
  cd $DIR
else
  echo "error determining script dir"
  exit 1
fi

# Start the soft IOC
softIoc -s st.cmd
