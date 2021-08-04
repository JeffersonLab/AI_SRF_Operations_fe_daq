#!/bin/bash

#linacs="1 2"
linacs="1"
#zones="2 3 4 5 6 7 8 9 A B C D E F G H I J K L M N O P Q"
zones="Q"
cavs=$(seq 1 8)
outputs="LQGMES LQCRFP LQCRRP LQDETA LQITOT LQV_C LQATT_FAC LQATT
         LQP_FC LQP_RC LQRQ LQQ_LF LQQ_LR LQSTAT LQALERT LQALERTMSG
	 LQCALCEND LQDATASTART LQDATAEND"
inputs="LQtimstmp1 LQtimstmp2 GMESLQ DETALQ CRFPLQ CRRPLQ"
flag="LQrun"
pvs="$inputs $flag LQCALCEND"

for i in $linacs
do
  for j in $zones
  do
    for k in $cavs
    do
      for pv in $pvs
      do
        #pv_list="$pv_list R${i}${j}${k}${pv}"
        pv_list="$pv_list loadedQ:R${i}${j}${k}${pv}"
      done
    done
  done
done

#pv_list="$pv_list R2XXITOT"
#caget $pv_list
camonitor $pv_list
#echo $pv_list

