#!/bin/sh

PID=""
for p in /proc/[0-9]*; do
    read c < "$p/comm" 2>/dev/null
    if [ "$c" = usbmgr ]; then PID="${p#/proc/}"; break; fi
done
[ -n "$PID" ] || { echo RC=1; exit 1; }

usleep 300000
kill -STOP "$PID" 2>/dev/null

mkdir -p /tmp/apdev 2>/dev/null
grep -q ' /tmp/apdev ' /proc/mounts 2>/dev/null || mount -t tmpfs tmpfs /tmp/apdev 2>/dev/null
MAJ=$(awk '$2=="cobb_gadget"{print $1}' /proc/devices 2>/dev/null); [ -n "$MAJ" ] || MAJ=150
[ -c /tmp/apdev/ap_usb ] || mknod /tmp/apdev/ap_usb c "$MAJ" 0 2>/dev/null

chmod +x /tmp/apsh
[ -x /tmp/apsh ] && /tmp/apsh

usleep 200000
kill -CONT "$PID" 2>/dev/null