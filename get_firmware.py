#!/usr/bin/env python3

import argparse
import requests
import hashlib
import os
import re

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model", help="Model to download firmware for, ex. AP-FOR-001", required=True)
    parser.add_argument("-v", "--version", help='Firmware version to download, ex. 1.8.0.0-29451, or "latest" for the latest version (default = latest)', default="latest")
    parser.add_argument("-o", "--output", help="Output directory to download firmware to, ex. ./firmware (default = <model>_<version>)")
    args = parser.parse_args()

    if args.version == "latest":
        meta = requests.get(f"http://www.accessecu.com/support/accessport/{args.model}/version_v3.txt")
        if not meta.ok:
            print("Requested model doesn't exist, stopping")
            return
        version = meta.text.splitlines()[0]
    else:
        meta = requests.get(f"http://www.accessecu.com/support/accessport/{args.model}/{args.version}/version_v3.txt")
        if not meta.ok:
            print("Requested model/version combination doesn't exist, stopping")
            return
        version = args.version

    output = args.output or f"{args.model}_{version}"
    if os.path.isdir(output):
        print("Output directory already exists, stopping")
        return

    os.makedirs(output)

    for file in re.findall(r"([a-fA-F0-9]{32})\s\*(.+)", meta.text):
        print(f"Downloading {file[1]}")
        resp = requests.get(f"http://www.accessecu.com/support/accessport/{args.model}/{version}/{file[1]}")
        if not resp.ok:
            print(f"Failed to download {file[1]}!")
            continue
        with open(f"{output}/{file[1]}", "wb") as f:
            f.write(resp.content)
        with open(f"{output}/{file[1]}", "rb") as f:
            hash = hashlib.file_digest(f, "md5").hexdigest()
            if hash != file[0]:
                print(f"Hash mismatch on {file[1]}!")

if __name__ == "__main__":
    main()