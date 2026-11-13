#!/usr/bin/env python3

import time
import usb

import transport

cmd = (
    'about=;(set -f; '
    'S=$(readeeprom -s); '
    'case "$S" in '
    '*AP_CM1_STATE=1*|*AP_CM1_STATE=2*) CM1="--cm1_state=0 --cm1_vehicle=?";; '
    '*) CM1="";; '
    'esac; '
    'ap_mount --remount=rw; '
    'writeeeprom --quiet --state=0 --vehicle=? $CM1; RC=$?; '
    'if [ $RC -eq 0 ]; then '
    'rm -f '
    '/user/ap-data/user.rom /user/ap-data/user.md5sum '
    '/user/ap-data/user_tcm.rom /user/ap-data/user_tcm.md5sum '
    '/user/ap-data/user_df0.rom /user/ap-data/user_df1.rom '
    '/user/ap-data/vidblock /user/ap-data/flashed_rom '
    '/user/ap-data/user_vin /user/ap-data/ecu_og_id /user/ap-data/tcm_og_id '
    '/user/ap-data/original_ecu.txt /user/ap-data/did_dump.txt '
    '/user/ap-data/preinstall_stored_data /user/ap-data/recovery_settings '
    '/user/ap-data/prev_install /user/ap-user/backupcksum; '
    'cur_map --working=/user/ap-data --clear --realtime --reflash --quiet; '
    'if [ -n "$CM1" ]; then cur_map --working=/user/ap-data --clear --tcm_reflash --quiet; fi; '
    'fi; '
    'ap_mount --remount=ro; '
    'echo UNMARRY_RC=$RC; '
    'readeeprom -s -i -k -f)'
)

def main():
    t = transport.Transport()
    t.open()
    print("opened cobb gadget, attempting handshake")
    try:
        t.handshake()
        print("got handshake, sending command")
        # t.send(t.text_command("about=;cd /ap-app/bin;. /ap-app/cmdline_setup.sh;./ap_mount --remount=rw;rm -f /user/ap-data/user_vin /user/ap-data/user.rom /user/ap-data/user.md5sum /user/ap-data/user_tcm.rom /user/ap-data/user_tcm.md5sum /user/ap-data/original_ecu.txt /user/ap-data/prev_install /user/ap-data/preinstall_stored_data /user/ap-user/backupcksum;./writeeeprom --quiet --state=0 --cm1_state=0 --vehicle=? --cm1_vehicle=?;./cur_map --working=/user/ap-data/ --clear --reflash --quiet;./ap_mount --remount=ro;echo DONE=$?"))
        # t.send(t.text_command("about=;cd /ap-app/bin;./ap_mount --remount=rw;./writeeeprom --quiet --state=0 --cm1_state=0 --vehicle='?' --cm1_vehicle='?';echo RC=$?;./readeeprom -s -i"))
        # t.send(transport.runtask("about=;( cd /ap-app/bin;./ap_mount --remount=rw;./writeeeprom --quiet --state=0 --vehicle='?';c=$(./readeeprom -s | sed -n 's/.*CM1_STATE=//p');[ \"${c:-0}\" -gt 0 ] 2>/dev/null && ./writeeeprom --quiet --cm1_state=0 --cm1_vehicle='?';echo \"RC=$?\";./ap_mount --remount=ro;./readeeprom ) 2>&1"))
        t.send(transport.runtask(cmd))
        print("device stdout:")
        print(transport.get_stdout(t.recv_until_finish()))
        print("sending reboot command")
        t.send(transport.frame(0x18))
        t.drain()
    finally:
        t.close()

    print("done")

if __name__ == "__main__":
    main()