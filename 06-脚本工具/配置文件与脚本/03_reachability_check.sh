#!/bin/bash

ip=$1
if [ -z $1 ];then
  echo "first param is blank"
  exit 2
fi
if [ -z $2 ];then
  echo "second param is blank"
  exit 2
fi

filepath=$2
flag=`echo $ip | cut -d '.'  -f 2`
expr $flag + 0 >&/dev/null
if [  $? != 0 ]; then
    echo "ip parsefailed"
    exit 2
fi


if [ "$flag" -eq 80 ] || [ "$flag" -eq 81 ];
then
   iport='gre12'
elif [ $flag -eq 82 ] || [ $flag -eq 83 ]
then
   iport='gre13'
elif [ $flag -eq 70 ] || [ $flag -eq 71 ] || [ $flag -eq 72 ]
then
   iport='gre8'
elif [ $flag -eq 75 ] || [ $flag -eq 74 ] || [ $flag -eq 76 ]
then
   iport='gre9'
elif [ $flag -eq 242 ] || [ $flag -eq 241 ] || [ $flag -eq 240 ] || [ $flag -eq 243 ]
then
   iport='gre14'
elif [ $flag -eq 244 ] || [ $flag -eq 245 ] || [ $flag -eq 246 ] || [ $flag -eq 247 ]
then
   iport='gre15'
fi

echo "sudo tcpdump -nn -i $iport host $ip >> $filepath"
sudo tcpdump -nn -i $iport host $ip >> $filepath

#这条命令解析不出原始数据，如果需要解析原始数据需要加参数-s0 -X -c 20


#筛选出unreachable的ip
grep "unreachable" 10.80.1.142 | awk -F " " '{print $7}'| sort | uniq