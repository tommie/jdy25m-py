#!/usr/bin/python3

from bluezero import adapter
import jdy25mbt


def main():
    adp = adapter.Adapter()

    if not adp.powered:
        print('Powering adapter on...')
        adp.powered = True

    devs = jdy25mbt.available_devices()
    if not devs:
        print('Waiting for device...')
        devs = jdy25mbt.discover(adp)

    if not devs:
        print('No devices found')
        return

    dev = devs[0]
    print('Device found:', dev.address, dev.name or 'unnamed')

    print('Connecting...')
    dev.connect()
    try:
        with jdy25mbt.Device(dev, adp, default_timeout=1) as jdydev:
            jdy25mbt.print_device_identity(jdydev)
            print()
            jdy25mbt.print_device_configuration(jdydev)
    finally:
        dev.disconnect()

main()
