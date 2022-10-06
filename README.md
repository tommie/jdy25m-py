# JDY-25M Python API

The [JDY-25M](https://w.electrodragon.com/w/Category:JDY-25M) is a
Bluetooth 5.0 LE module capable of mesh networking, BT-to-serial,
keyfob and beacon duties.

This is a simple Python API for the "APP" interface over Bluetooth,
allowing device configuration.

## Prerequisites

* Linux and Bluez.
* Uses [`python-bluezero`](https://github.com/ukBaz/python-bluezero/).

## Caveats

* There seems to be some bugs, e.g. `write_tx_power` not taking effect
  and `read_ibeacon_sing` timing out.
