#!/bin/bash

if [ $# -ne 1 ] ; then
  echo "Requires linac number as sole argument, e.g. 1"
  exit 1
fi

l=$1

pvs=""
for z in 2 3 4 5 6 7 8 9 A B C D E F G H I J K L M N O P Q
do
  for c in 1 2 3 4 5 6 7 8
  do
    pvs="$pvs R${l}${z}${c}ODVH"
  done
done

echo Cavity,Value & caget $pvs | tr -s ' ' | tr ' ' ',' | sed -e 's/ODVH//'
