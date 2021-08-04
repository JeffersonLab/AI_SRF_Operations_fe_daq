#!/bin/sh

# Figure out script dir and cd there
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
if [ -n "$DIR" ] ; then
  cd $DIR
else
  echo "error determining script dir"
  exit 1
fi

if [ -f ./pid.txt ] ; then
	kill -9 `cat  pid.txt`
	rm -f pid.txt
else
	echo "No pid file found.  Cannot kill soft IOC"
	exit 1
fi
