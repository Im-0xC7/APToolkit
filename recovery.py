#!/usr/bin/env python3

from pathlib import Path

import time
import usb

import transport
import argparse
import mxsldr

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help=".sb file to upload", nargs="?", type=Path)
    args = ap.parse_args()

    t = transport.Transport()
    t.open()
    print("opened cobb gadget, attempting handshake")
    try:
        t.handshake()
        print("got handshake, sending command")
        t.send(transport.runtask("about=;/sbin/enable_recovery"))
    finally:
        t.close()

    print("waiting for recovery mode")

    i = 0
    while True:
        device = usb.core.find(idVendor=0x15A2, idProduct=0x004F)

        if device is not None:
            print("ap in recovery mode found")
            break

        if i == 15:
            print("no device found after 15s")

        time.sleep(1)
        i += 1

    if args.file:
        mxsldr.load_bootstream(args.file, log=print)

if __name__ == "__main__":
    main()