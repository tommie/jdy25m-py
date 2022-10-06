"""
A Python interface for the Bluetooth "APP" API of JDY-25M devices.

See https://w.electrodragon.com/w/JDY-25M

:Example:

>>> from bluezero import adapter
>>> import jdy25mbt
>>>
>>> adp = adapter.Adapter()
>>> devs = jdy25mbt.discover(adp)
>>> dev = devs[0]
>>> dev.connect()
>>> try:
>>>     with jdy25mbt.Device(dev, adp, default_timeout=1) as dev:
>>>         jdy25mbt.print_device_identity(dev)
>>>         jdy25mbt.print_device_configuration(dev)
>>> finally:
>>>     dev.disconnect()
"""

import collections
from dataclasses import dataclass
from enum import Enum
import struct
import sys
import uuid

# https://github.com/ukBaz/python-bluezero
from bluezero import adapter, constants, dbus_tools, device, GATT
import dbus
from gi.repository import GLib


class _Device(device.Device):
    """An improved device.Device"""

    def advertising_data(self):
        return dbus_tools.get(dev.remote_device_props,
                              constants.DEVICE_INTERFACE,
                              'AdvertisingData', None)


class _Descriptor(GATT.Descriptor):
    """A fixed GATT.Descriptor."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def resolve_gatt(self):
        """
        Get the methods and properties for the discovered characteristics
        :return: Boolean of if characteristics have been resolved
        """
        if not self.rmt_device.services_resolved or self.descriptor_methods:
            return bool(self.descriptor_methods)

        self.descriptor_methods = dbus_tools.get_methods(
            self.adapter_addr,
            self.device_addr,
            self.srv_uuid,
            self.chrc_uuid,
            self.dscr_uuid)
        self.descriptor_props = dbus_tools.get_props(
            self.adapter_addr,
            self.device_addr,
            self.srv_uuid,
            self.chrc_uuid,
            self.dscr_uuid)
        return True

    @property
    def flags(self):
        """
        Return a list of how this descriptor value can be used.
        :return: list example ['read', 'write']
        """
        return self.descriptor_props.Get(
            constants.GATT_DESC_IFACE, 'Flags')

    @property
    def value(self):
        """
        The cached value of the descriptor.
        This property gets updated only after a successful read request, upon
        which a PropertiesChanged signal will be emitted.
        :return: DBus byte array
        """
        return self.descriptor_props.Get(
            constants.GATT_DESC_IFACE, 'Value')


def _wait_for_resolved_services(dev: device.Device, adp: adapter.Adapter):
    """Waits for the ServicesResolved property to become true."""

    def on_props_changed(iname, changed_props, inv_props):
        if iname != constants.DEVICE_INTERFACE:
            return

        if 'ServicesResolved' in changed_props and bool(changed_props['ServicesResolved']):
            adp.quit()
        elif 'ServicesResolved' in inv_props and dev.services_resolved:
            adp.quit()

    sig = dev.remote_device_props.connect_to_signal('PropertiesChanged', on_props_changed)
    try:
        try:
            if not dev.services_resolved:
                adp.run()
        except KeyboardInterrupt:
            adp.quit()
            raise
    finally:
        sig.remove()


def _gatt_services(dev: device.Device, adp: adapter.Adapter):
    """Returns all services, characteristics and descriptors of a device."""

    srvs = {}
    srvchars = collections.defaultdict(dict)
    descs = collections.defaultdict(dict)

    om = dbus.Interface(dev.bus.get_object(constants.BLUEZ_SERVICE_NAME, '/'), constants.DBUS_OM_IFACE)
    for k, v in om.GetManagedObjects().items():
        if not k.startswith(dev.remote_device_path):
            continue

        if constants.GATT_SERVICE_IFACE in v:
            srvs[k] = str(v[constants.GATT_SERVICE_IFACE]['UUID'])
        elif constants.GATT_CHRC_IFACE in v:
            srv = str(v[constants.GATT_CHRC_IFACE]['Service'])
            srvchars[srv][k] = str(v[constants.GATT_CHRC_IFACE]['UUID'])
        elif constants.GATT_DESC_IFACE in v:
            char = str(v[constants.GATT_DESC_IFACE]['Characteristic'])
            descs[char][k] = str(v[constants.GATT_DESC_IFACE]['UUID'])

    services = {}
    for srv, chars in srvchars.items():
        services[srvs[srv]] = GATT.Service(dev.adapter, dev.address, srv)
        chars = {uuid: (k, GATT.Characteristic(dev.adapter, dev.address, srvs[srv], uuid)) for k, uuid in chars.items()}
        services[srvs[srv]].characteristics = {uuid: char for uuid, (_, char) in chars.items()}
        for char_uuid, (chark, char) in chars.items():
            while True:
                if not char.characteristic_methods and not char.resolve_gatt():
                    _wait_for_resolved_services(dev, adp)
                    continue
                break

            char.descriptors = {uuid: _Descriptor(dev.adapter, dev.address, srvs[srv], char_uuid, uuid) for uuid in descs.get(chark, {}).values()}
            for desc in char.descriptors.values():
                while True:
                    if not desc.resolve_gatt():
                        _wait_for_resolved_services(dev, adp)
                        continue
                    break

    return services


JDY_SERVICE_UUID = '0000ffe0-0000-1000-8000-00805f9b34fb'
JDY_MESH_CHARACTERISTIC_UUID = '0000ffe3-0000-1000-8000-00805f9b34fb'
JDY_MANUFACTURER_ID = 0x8911


class Role(Enum):
    """The configured role of a device."""

    TRANSPARENT_SLAVE = 0
    TRANSPARENT_MASTER = 1
    BLE_PROBE = 2
    IBEACON = 3
    IBEACON_PROBE = 4
    MESH_NETWORK = 5
    MULTI_MASTER = 6
    MULTI_SLAVE = 7
    KEY_LABEL_DETECTION = 8

class PasswordType(Enum):
    """The configured authorization requirement."""

    NONE = 0
    CONNECTION = 1
    CONNECTION_AND_BINDING = 2

class BaudRate(Enum):
    """The configured serial baud rate."""

    B2400 = 2
    B4800 = 3
    B9600 = 4
    B19200 = 5
    B38400 = 6
    B57600 = 7
    B115200 = 8

@dataclass
class KeyParam:
    """A description of an output when acting as a keyfob receiver."""

    target_address: int
    output_pin: int
    full_duplex: bool

@dataclass
class LearnerParam:
    """A description of an input when acting as a keyfob sender."""

    input_pin: int
    sender_address: int


class Device(object):
    """A JDY-25M device."""

    def __init__(self, *args, **kwargs):
        self._vc = JDYCharacteristicValueCache(*args, **kwargs)

    def __enter__(self):
        self._vc.__enter__()
        return self

    def __exit__(self, *args):
        self._vc.__exit__(*args)

    def read_module_software_version(self):
        return str(self._vc.read(0xC101).rstrip().split(b'=', 1)[-1], 'ascii')

    def read_broadcast_name(self):
        return str(self._vc.read(0xC102), 'utf-8')

    def read_password_value(self):
        return str(self._vc.read(0xC103), 'utf-8')

    def read_password_type(self):
        return PasswordType(self._vc.read(0xC104)[0])

    def read_baud_rate(self):
        return BaudRate(self._vc.read(0xC105)[0])

    def read_power_on_sleep(self):
        return int(self._vc.read(0xC106)[0])

    def read_advertising_interval(self):
        return int(self._vc.read(0xC107)[0])

    def read_tx_power(self):
        return int(self._vc.read(0xC108)[0])

    def read_role(self):
        return Role(self._vc.read(0xC109)[0])

    def read_ibeacon_uuid(self):
        return uuid.UUID(bytes=self._vc.read(0xC201))

    def read_ibeacon_major(self):
        return struct.unpack('>H', self._vc.read(0xC202))[0]

    def read_ibeacon_minor(self):
        return struct.unpack('>H', self._vc.read(0xC203))[0]

    def read_ibeacon_sing(self):
        # JDY-25M-V1.7313: no response
        return int(self._vc.read(0xC204)[0])

    def read_mesh_netid(self):
        return struct.unpack('>H', self._vc.read(0xC301))[0]

    def read_mesh_maddr(self):
        return struct.unpack('>H', self._vc.read(0xC302))[0]

    def read_mesh_mclss(self):
        return int(self._vc.read(0xC303)[0])

    def read_key_param(self, index: int):
        assert 1 <= index <= 5
        return KeyParam(*struct.unpack('>HBB', self._vc.read(0xC303 + index)))

    def read_subtype(self):
        return int(self._vc.read(0xC309)[0])

    def read_learn(self):
        return int(self._vc.read(0xC30A)[0])

    def read_learner_param(self, index: int):
        assert 1 <= index <= 5
        return LearnerParam(*struct.unpack('>BH', self._vc.read(0xC30A + index)))

    def read_devclss(self):
        return int(self._vc.read(0xC310)[0])

    def read_klabel(self):
        # JDY-25M-V1.7313: no response
        return self._vc.read(0xC311)[0]

    def read_kltime(self):
        # JDY-25M-V1.7313: no response
        return self._vc.read(0xC312)[0]

    def read_klrssi(self):
        # JDY-25M-V1.7313: no response
        return self._vc.read(0xC313)[0]

    def reset_device(self):
        self._vc.write(0xA1E1, b'\xF1\x01')

    def write_broadcast_name(self, name: str):
        self._vc.write(0xA2E2, name.encode('utf-8'))

    def write_password_type(self, typ: PasswordType):
        self._vc.write(0xA4E4, bytes([typ.value]))

    def write_baud_rate(self, rate: BaudRate):
        self._vc.write(0xA5E5, bytes([rate.value]))

    def disconnect_device(self):
        self._vc.write(0xA6E6, b'\x01')

    def write_tx_power(self, v: int):
        # JDY-25M-V1.7313: no effect
        self._vc.write(0xA9E9, bytes([v]))

    def restore_device_config(self):
        self._vc.write(0xAAEA)

    def write_role(self, role: Role):
        self._vc.write(0xF505, bytes([role.value]))


class JDYCharacteristicValueCache(object):
    """A value cache.

    The device uses a single GATT characteristic in a request/response
    way: writing two bytes, then reading two bytes (and possibly some
    value). The device then generally resets the value to empty.

    Writes are not acknowledged at all.

    That is an annoying interface, so this class receives GATT
    notifications about value changes. It keeps track of new values,
    keyed by the first two bytes.
    """

    def __init__(self, dev: device.Device, adp: adapter.Adapter, default_timeout: float = None):
        self._dev = dev
        self._adp = adp
        self._default_timeout = default_timeout
        self._mesh = None
        self._values = {}
        self._resolve_gatt()

    def __enter__(self):
        if not self._mesh:
            self._resolve_gatt()
        self._mesh.start_notify()
        self._mesh.add_characteristic_cb(self._on_props_changed)
        return self

    def __exit__(self, *args):
        self._mesh.add_characteristic_cb(None)
        self._mesh.stop_notify()

    def _resolve_gatt(self):
        """Resolves the needed GATT characteristics object."""

        while True:
            srv = _gatt_services(self._dev, self._adp).get(JDY_SERVICE_UUID, None)
            if not srv:
                _wait_for_resolved_services(self._dev, self._adp)
                continue

            mesh = srv.characteristics.get(JDY_MESH_CHARACTERISTIC_UUID, None)
            if not mesh:
                _wait_for_resolved_services(self._dev, self._adp)
                continue

            self._mesh = mesh
            return

    def _on_props_changed(self, iname: str, changed_props, inv_props):
        """Handles a PropertyChanged signal."""

        if iname != constants.GATT_CHRC_IFACE:
            return

        if 'Value' in changed_props:
            v = bytes(changed_props['Value'])
        elif 'Value' in inv_props:
            v = self._mesh.read_raw_value()
        else:
            return
        if v:
            self._values[struct.unpack('>H', v[:2])[0]] = v[2:]
            self._adp.quit()

    def wait_for(self, cmd: int, timeout: float = None):
        """Waits for the given command to pop into the value cache."""

        _wait_with_timeout((lambda: cmd in self._values), self._adp, timeout)

    def read(self, cmd: int, read_timeout: float = None):
        """Sends a read command and waits for the response."""

        cmdb = struct.pack('>H', cmd)
        self._values.pop(cmd, None)
        self._mesh.write_value(cmdb)
        self.wait_for(cmd, timeout=read_timeout)
        return self._values[cmd]

    def write(self, cmd: int, data: bytes = b''):
        """Writes a command (and value)."""

        cmdb = struct.pack('>H', cmd)
        self._values.pop(cmd, None)
        self._mesh.write_value(cmdb + data)


def print_device_identity(dev: Device, file=sys.stdout):
    """Prints various identifying information about the device."""

    print('version:', dev.read_module_software_version(), file=file)
    print('name:', dev.read_broadcast_name(), file=file)
    print('ibeacon UUID:', dev.read_ibeacon_uuid(), file=file)
    print('mesh netid:', hex(dev.read_mesh_netid()), file=file)
    print('mesh maddr:', hex(dev.read_mesh_maddr()), file=file)

def print_device_configuration(dev: Device, file=sys.stdout):
    """Prints various configuration parameters from the device."""

    print('password:', dev.read_password_value(), file=file)
    print('password type:', dev.read_password_type(), file=file)
    print('baud rate:', dev.read_baud_rate(), file=file)
    print('power on sleep:', dev.read_power_on_sleep(), file=file)
    print('TX power:', dev.read_tx_power(), file=file)
    print('role:', dev.read_role(), file=file)
    print('ibeacon ver:', dev.read_ibeacon_major(), dev.read_ibeacon_minor(), file=file)
    print('mesh mclss:', dev.read_mesh_mclss(), file=file)
    print('key params:', [dev.read_key_param(i) for i in range(1, 6)], file=file)
    print('subtype:', dev.read_subtype(), file=file)
    print('learn:', dev.read_learn(), file=file)
    print('learner params:', [dev.read_learner_param(i) for i in range(1, 6)], file=file)
    print('dev class:', dev.read_devclss(), file=file)
    #print('ibeacon sing:', dev.read_ibeacon_sing(), file=file)
    #print('klabel:', dev.read_klabel(), file=file)
    #print('kltime:', dev.read_kltime(), file=file)
    #print('klrssi:', dev.read_klrssi(), file=file)

def available_devices(service_uuid: str = JDY_SERVICE_UUID):
    """Returns the available devices reported by Bluez, filtered by the given service UUID."""

    return [dev
            for dev in device.Device.available()
            if service_uuid in dev.uuids]


def discover(adp: adapter.Adapter, timeout: float = None, service_uuid: str = JDY_SERVICE_UUID):
    """Finds nearby devices by service UUID."""

    devs = []
    def on_device_found(dev: device.Device):
        if service_uuid not in dev.uuids:
            return

        devs.append(_Device(dev.adapter, dev.address))
        adp.quit()
    adp.on_device_found = on_device_found

    adp.show_duplicates()
    adp.start_discovery()
    try:
        _wait_with_timeout((lambda: devs), adp, timeout)
    finally:
        adp.stop_discovery()

    return devs


def _wait_with_timeout(cond, adp: adapter.Adapter, timeout: float):
    """Waits for a condition function to return true."""

    tosrc = None
    timedout = []
    def on_timeout():
        GLib.source_remove(tosrc)
        tosrc = None
        timedout.append(True)
        adp.quit()

    if timeout:
        tosrc = GLib.timeout_add_seconds(timeout, on_timeout)

    try:
        while not cond():
            adp.run()
            if timedout:
                raise TimeoutError('wait timed out')
    except KeyboardInterrupt:
        adp.quit()
        raise
    finally:
        if tosrc:
            GLib.source_remove(tosrc)
