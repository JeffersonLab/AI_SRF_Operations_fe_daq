#!/bin/bash

for l in 1 2
do
  pvs=""
  for z in 2 3 4 5 6 7 8 9 A B C D E F G H I J K L M N O P Q
  do
    pvs="$pvs R${l}${z}XSystemUser"
  done
  echo
  echo "=========== ${l}L =============="
  caget $pvs
done

