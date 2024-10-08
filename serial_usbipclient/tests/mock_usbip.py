"""mock USBIP server"""

import errno
import glob
import json
import logging
import os
import platform
import re
import select
import socket
import struct
import sys
import traceback
from enum import EnumType, StrEnum
from pathlib import Path
from threading import Event, Thread
from time import sleep, time
from typing import Any, Optional, cast

from serial_usbipclient.protocol.packets import (CMD_SUBMIT, CMD_SUBMIT_PREFIX,
                                                 CMD_UNLINK, HEADER_BASIC,
                                                 OP_REP_DEV_INTERFACE,
                                                 OP_REP_DEV_PATH,
                                                 OP_REP_DEVLIST_HEADER,
                                                 OP_REP_IMPORT, OP_REQ_IMPORT,
                                                 RET_UNLINK, USBIP_RET_SUBMIT,
                                                 CommonHeader)
from serial_usbipclient.protocol.urb_packets import (
    ACMFunctionalDescriptor, CallManagementFunctionalDescriptor,
    ConfigurationDescriptor, DeviceDescriptor, EndPointDescriptor,
    HeaderFunctionalDescriptor, InterfaceAssociation, InterfaceDescriptor,
    StringDescriptor, UnionFunctionalDescriptor, URBBase, URBCDCRequestType,
    UrbSetupPacket, URBStandardDeviceRequest)
from serial_usbipclient.protocol.usb_descriptors import DescriptorType
from serial_usbipclient.protocol.usbip_defs import BasicCommands, Direction
from serial_usbipclient.socket_wrapper import SocketWrapper

LOGGER: logging.Logger = logging.getLogger(__name__)


class OrderlyExit(SystemExit):
    """perform an orderly exit"""


class MockDevice:
    """URB information for a device"""
    def __init__(self, vendor: int, product: int, device: DeviceDescriptor, busnum: int, devnum: int) -> None:
        """setup our instance"""
        self.vendor: int = vendor
        self.product: int = product
        self.busid: bytes = f"{busnum}-{devnum}".encode('utf-8')
        self.busid += b'\0' * (32 - len(self.busid))
        self.device: DeviceDescriptor = device
        self.queued_reads: dict[int, CMD_SUBMIT] = {}  # keyed by the sequence # for easy access
        self._attached: bool = False

    def enqueue_read(self, command: CMD_SUBMIT) -> None:
        """enqueue the read command"""
        self.queued_reads[command.seqnum] = command

    def dequeue_read(self, seq: int) -> CMD_SUBMIT:
        """remove the queued read from the queue"""
        if not seq:
            return self.queued_reads.popitem()[1]

        if seq in self.queued_reads:
            cmd_submit: CMD_SUBMIT = self.queued_reads.pop(seq)
            return cmd_submit
        else:
            raise ValueError(f"{seq=} not in queue! {self.queued_reads=}")

    @property
    def is_attached(self) -> bool:
        """=True, then device is attached"""
        return self._attached

    @property
    def attach(self) -> bool:
        """attach device =True"""
        previous_state: bool = self._attached
        self._attached = True
        return previous_state

    @property
    def detach(self) -> bool:
        """detach device"""
        previous_state: bool = self._attached
        self._attached = False
        return previous_state

    @property
    def busnum(self) -> int:
        """extract the bus number from the busid"""
        busid: str = "".join([chr(item) for item in self.busid if item])
        return int(busid.split('-', maxsplit=1)[0])

    @property
    def devnum(self) -> int:
        """extract the bus number from the busid"""
        busid: str = "".join([chr(item) for item in self.busid if item])
        return int(busid.split('-')[1])

    def __hash__(self):
        """return the hash of this device"""
        return hash((self.vendor, self.product))

    def __str__(self):
        """display readable version"""
        return f"{self.busnum}-{self.devnum} VID/PID={self.vendor:04x}:{self.product:04x}"


class ParseLSUSB:
    """parse the output of a lsusb command to get a USB device configuration"""
    def __init__(self):
        """parse the data"""
        self.logger = LOGGER
        self.root: str = os.path.join(os.path.dirname(__file__), '*.lsusb')
        self.devices: list[MockDevice] = []
        for file_path in glob.glob(self.root):
            self.file_path: str = file_path
            basename: str = Path(file_path).stem
            parts: list[str] = basename.split('-')
            busnum: int = int(parts[0])
            devnum: int = int(parts[1])
            device_descriptor: DeviceDescriptor = DeviceDescriptor()
            usb_configuration: list[str] = self.read_file(self.file_path)
            for offset in range(len(usb_configuration)):
                if usb_configuration[offset].startswith('Device Descriptor:'):
                    self.parse_descriptor(usb_configuration, offset, urb=device_descriptor)
                    self.devices.append(MockDevice(device_descriptor.idVendor, device_descriptor.idProduct,
                                                   device=device_descriptor, busnum=busnum, devnum=devnum))
                    break

    @staticmethod
    def read_file(file_path: str) -> list[str]:
        """read in the entire file"""
        usb_config_lines: list[str] = []
        with open(file_path, "r", encoding='utf-8') as usb:
            while line := usb.readline():
                usb_config_lines.append(line.strip())  # remove leading & trailing whitespace
            return usb_config_lines

    @staticmethod
    def from_hex(hex_value: str) -> int:
        """convert hex to integer"""
        return int(hex_value[2:], 16)  # return as an integer

    @staticmethod
    def to_bcd(bcd_value: str) -> int:
        """convert a string to BCD int"""
        # 2.00 -> 0x0200
        hex_value: str = bcd_value.replace('.', '')  # now it's a hex string
        return int(hex_value, 16)  # return as an integer

    def set_attribute(self, urb: URBBase, name: str, value: Any):
        """set the attribute to the structure"""
        if not isinstance(value, str):
            raise ValueError(f"{value=} should be str not {type(value)}")
        if name == 'bMaxPacketSize0':  # indicates max packet size for default endpoint
            name = 'bMaxPacketSize'

        if name == 'MaxPower':
            name = 'bMaxPower'
            value = value.replace('mA', '')

        for field in urb.fields():
            if field[0].name == name:
                if '.' in value:  # encoded, BCD
                    value = self.to_bcd(bcd_value=value)
                elif value.startswith('0x'):
                    value = self.from_hex(value)

                if type(field[0].type) == EnumType:  # pylint: disable=unidiomatic-typecheck
                    value = int(value)
                typed_value = field[0].type(value)
                setattr(urb, name, typed_value)
                return
        raise NotImplementedError(f"{name=} was not found on {urb.__class__.__name__}")

    def parse_descriptor(self, usb: list[str], offset: int, urb: URBBase) -> int:
        """parse the device descriptor data"""
        end_offset: int = len(usb) - 1
        while offset < end_offset:
            offset += 1
            section: str = usb[offset]
            if not section.endswith(':'):
                parts: list[str] = re.split(r'\s+', section)
                attribute_name: str = parts[0]
                attribute_value: str = parts[1]
                if (attribute_name not in ['Self', 'line', 'Transfer', 'Synch', 'Usage', 'Remote'] and
                        attribute_value not in ['Powered', 'coding', 'Type', 'Wakeup']):
                    self.set_attribute(urb, attribute_name, attribute_value)
            else:
                if section == 'Configuration Descriptor:':
                    device_desc: DeviceDescriptor = cast(DeviceDescriptor, urb)
                    device_desc.configurations.append(ConfigurationDescriptor())
                    offset = self.parse_descriptor(usb, offset, device_desc.configurations[-1])
                    configuration_response: bytes = device_desc.configurations[-1].pack()
                    for association in device_desc.configurations[-1].associations:
                        configuration_response += association.pack()
                    for interface in device_desc.configurations[-1].interfaces:
                        configuration_response += interface.pack()
                        for descriptor in interface.descriptors:
                            configuration_response += descriptor.pack()
                    if len(configuration_response) != device_desc.configurations[-1].wTotalLength:
                        self.logger.error(f"Parsing lsusb output, mismatch for configuration size "
                                          f"{device_desc.configurations[-1].wTotalLength=} != {len(configuration_response)}")

                elif section == 'Interface Association:':
                    if not isinstance(urb, ConfigurationDescriptor):
                        return offset - 1
                    config_descriptor: ConfigurationDescriptor = cast(ConfigurationDescriptor, urb)
                    config_descriptor.associations.append(InterfaceAssociation())
                    offset = self.parse_descriptor(usb, offset, config_descriptor.associations[-1])
                elif section == 'Interface Descriptor:':
                    if not isinstance(urb, ConfigurationDescriptor):
                        return offset - 1
                    config_descriptor = cast(ConfigurationDescriptor, urb)
                    config_descriptor.interfaces.append(InterfaceDescriptor())
                    offset = self.parse_descriptor(usb, offset, config_descriptor.interfaces[-1])
                elif section == "Endpoint Descriptor:":
                    if not isinstance(urb, InterfaceDescriptor):
                        return offset - 1
                    if_descriptor: InterfaceDescriptor = cast(InterfaceDescriptor, urb)
                    if_descriptor.descriptors.append(EndPointDescriptor())
                    offset = self.parse_descriptor(usb, offset, if_descriptor.descriptors[-1])
                elif section == 'CDC Header:':
                    if not isinstance(urb, InterfaceDescriptor):
                        return offset - 1
                    if_descriptor = cast(InterfaceDescriptor, urb)
                    if_descriptor.descriptors.append(HeaderFunctionalDescriptor())
                    offset = self.parse_descriptor(usb, offset, if_descriptor.descriptors[-1])
                elif section == 'CDC Call Management:':
                    if not isinstance(urb, InterfaceDescriptor):
                        return offset - 1
                    if_descriptor = cast(InterfaceDescriptor, urb)
                    if_descriptor.descriptors.append(CallManagementFunctionalDescriptor())
                    offset = self.parse_descriptor(usb, offset, if_descriptor.descriptors[-1])
                elif section == 'CDC ACM:':
                    if not isinstance(urb, InterfaceDescriptor):
                        return offset - 1
                    if_descriptor = cast(InterfaceDescriptor, urb)
                    if_descriptor.descriptors.append(ACMFunctionalDescriptor())
                    offset = self.parse_descriptor(usb, offset, if_descriptor.descriptors[-1])
                elif section == 'CDC Union:':
                    if not isinstance(urb, InterfaceDescriptor):
                        return offset - 1
                    if_descriptor = cast(InterfaceDescriptor, urb)
                    if_descriptor.descriptors.append(UnionFunctionalDescriptor())
                    offset = self.parse_descriptor(usb, offset, if_descriptor.descriptors[-1])

        return offset


class MockUSBDevice:
    """wrapper for devices we'll be mocking"""
    def __init__(self, devices: list[MockDevice]):
        """set up the devices we'll be mocking"""
        self.devices: list[MockDevice] = devices
        self.usbip: Optional[OP_REP_DEVLIST_HEADER] = None

    def device(self, busnum: int, devnum: int) -> Optional[MockDevice]:
        """retrieve the USB device associated with this devid"""
        for device in self.devices:
            if device.busnum == busnum and device.devnum == devnum:
                return device
        return None

    def setup(self):
        """create the USBIP device list"""
        usbip_dev_header: OP_REP_DEVLIST_HEADER = OP_REP_DEVLIST_HEADER()
        usbip_dev_header.num_exported_devices = len(self.devices)

        for usb in self.devices:
            root_dev_path: bytes = f"/sys/devices/pci0000.0/0000:00.1d1/usb2/{usb.busnum}-{usb.devnum}".encode('utf-8')
            dev_path: bytes = root_dev_path + (b'\0' * (256 - len(root_dev_path)))
            path: OP_REP_DEV_PATH = OP_REP_DEV_PATH(busid=usb.busid, path=dev_path, busnum=usb.busnum, devnum=usb.devnum,
                                                    idVendor=usb.vendor, idProduct=usb.product,
                                                    bcdDevice=usb.device.bcdDevice,
                                                    bDeviceClass=usb.device.bDeviceClass,
                                                    bDeviceSubClass=usb.device.bDeviceSubClass,
                                                    bDeviceProtocol=usb.device.bDeviceProtocol,
                                                    bConfigurationValue=0x0,
                                                    bNumConfigurations=len(usb.device.configurations),
                                                    bNumInterfaces=sum([len(item.interfaces)
                                                                        for item in usb.device.configurations]))
            usbip_dev_header.paths.append(path)
            for configuration in usb.device.configurations:
                for interface in configuration.interfaces:
                    usbip_interface: OP_REP_DEV_INTERFACE = OP_REP_DEV_INTERFACE(bInterfaceClass=interface.bInterfaceClass,
                                                                                 bInterfaceSubClass=interface.bInterfaceSubClass,
                                                                                 bInterfaceProtocol=interface.bInterfaceProtocol)
                    path.interfaces.append(usbip_interface)

        self.usbip = usbip_dev_header

    def pack(self) -> bytes:  # create the byte representation of the data
        """walk the USBIP representation and create the byte stream"""
        if self.usbip is None:
            raise ValueError("usbip not initialized")

        response: bytes = self.usbip.pack()
        for path in self.usbip.paths:
            response += path.pack()
            for interface in path.interfaces:
                response += interface.pack()
        return response


class SocketPair:
    """create a socket pair"""
    def __init__(self):
        """create our socket pair"""
        self._near: Optional[socket.socket] = None
        self._far: Optional[socket.socket] = None
        family: socket.AddressFamily = socket.AF_INET if sys.platform == 'win32' else socket.AF_UNIX  # type: ignore[attr-defined]
        self._near, self._far = socket.socketpair(family=family, type=socket.SOCK_STREAM)

    def wakeup(self):
        """send a simple data byte to the far socket"""
        if self._near:
            self._near.sendall(b'\0')  # wakeup the far listener

    @property
    def listener(self) -> socket.socket:
        """return the socket that will be listening"""
        if self._far:
            return self._far
        raise ValueError("socket pair not specified!")

    def shutdown(self):
        """done with our socket pair"""
        if self._near:
            self._near.shutdown(socket.SHUT_RDWR)
            self._near.close()
            self._near = None
        if self._far:
            self._far.shutdown(socket.SHUT_RDWR)
            self._far.close()
            self._far = None


class USBIPServerClient:
    """connections to the usbip clients, wrap the socket to provide more context"""
    def __init__(self, connection: socket.socket, address: tuple[str, int], size: int = 0) -> None:
        """local variables"""
        self.socket: socket.socket = connection
        self.address: tuple[str, int] = address
        self.socket.settimeout(None)  # default to blocking connections
        self._size: int = size if size else CommonHeader.size  # type: ignore[assignment]
        self._busid: Optional[bytes] = None
        self._name: str = f"@{self.address[0]}"

    def fileno(self) -> int:
        """return the file id of the underlying socket"""
        return self.socket.fileno()

    @property
    def name(self) -> str:
        """return the fabricated name"""
        return self._name

    @property
    def is_connected(self) -> bool:
        """the underlying socket is up & running"""
        return self.socket is not None and self.fileno() != -1

    def shutdown(self) -> None:
        """clean up the wrapped socket"""
        if self.is_connected:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()

    def recv(self, size: int) -> bytes:
        """read from the wrapped socket"""
        return self.socket.recv(size)

    def sendall(self, message: bytes) -> None:
        """send all the data to the underlying socket"""
        return self.socket.sendall(message)

    @property
    def size(self) -> int:
        """return the size we are expecting to receive"""
        return self._size

    @size.setter
    def size(self, size: int) -> None:
        """set the size we are expecting to receive"""
        self._size = size

    @property
    def is_attached(self) -> bool:
        """indicates that client has been imported by remote host"""
        return bool(self._busid)

    @property
    def busid(self) -> bytes:
        """return the busid (if assigned)"""
        if self._busid is None:
            raise ValueError("busid not assigned!")
        return self._busid

    @busid.setter
    def busid(self, busid: bytes) -> None:
        """set the busid for this client"""
        self._size = CMD_SUBMIT_PREFIX.size if busid else CommonHeader.size  # type: ignore[assignment]
        self._busid = busid

    def __repr__(self) -> str:
        """return a developer readable view"""
        busid: str = self.busid.strip(b'\0').decode('utf-8') if self.is_attached else 'None'
        return f"{self.name=}, {self.address=}, {self.fileno()=}, {busid=}"


# noinspection PyTypeChecker
class MockUSBIP:
    """mock USBIP server"""
    STARTUP_TIMEOUT: float = 5.0

    class DebugCommands(StrEnum):
        """commands that are tunneled and their expected behavior"""
        NO_WRITE_RESPONSE = 'no-write-response'  # write does not return acknowledgement
        NO_READ_RESPONSE = 'no-read-response'  # suppress an expected read

    def __init__(self, host: str, port: int, socket_class: type = SocketWrapper):
        """set up our instance"""
        self.host: str = host
        self.port: int = port
        self.logger: logging.Logger = LOGGER
        self.server_socket: Optional[SocketWrapper] = None
        self._socket_class: type = socket_class
        self.thread: Optional[Thread] = Thread(name=f'mock-usbip@{self.host}:{self.port}', target=self.run_server, daemon=True)
        self.event: Event = Event()
        self._is_windows: bool = platform.system() == 'Windows'
        self._protocol_responses: dict[str, list[str]] = {}
        self.urb_queue: dict[int, Any] = {}  # pending read URBs, queued by seq #
        self.usb_devices: MockUSBDevice = MockUSBDevice(devices=[])  # to keep mypy & others happy
        self._wakeup: Optional[SocketPair] = SocketPair()
        self._clients: list[USBIPServerClient] = []
        self.setup()
        if self.host and self.port:
            self.event.clear()
            self.thread.start()
            start_time: float = time()
            while time() - start_time < MockUSBIP.STARTUP_TIMEOUT:
                if self.event.is_set():
                    return
                sleep(MockUSBIP.STARTUP_TIMEOUT / 100.0)  # allow thread time to start

            raise TimeoutError(f"Timed out waiting for USBIP server @{self.host}:{self.port} to start, "
                               f"waited {round(time() - start_time, 2)} seconds")
        else:
            LOGGER.info("MockUSBIP server not started")

    @property
    def has_clients(self) -> bool:
        """return the clients we have (for testing)"""
        return bool(self._clients)

    def setup(self):
        """setup our instance"""
        data_path: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'usbip_packets.json')
        with open(file=data_path, mode='r', encoding='utf-8') as recording:
            self._protocol_responses = json.loads(recording.read())

        parser: ParseLSUSB = ParseLSUSB()
        self.usb_devices = MockUSBDevice(parser.devices)
        self.usb_devices.setup()  # now we have binary for the USB devices we can emulate

    def wakeup(self):
        """send a wakeup packet"""
        if self._wakeup:
            self._wakeup.wakeup()

    def shutdown(self):
        """shutdown the USBIP server thread"""
        if self.thread and self.event.is_set():
            self.logger.info("clear event, wait for thread to recognize exit condition")
            self.event.clear()  # -> 0, thread will exit loop if we aren't blocking on accept()
            if self.server_socket:
                try:
                    self.server_socket.shutdown(socket.SHUT_RDWR)
                except OSError:  # if socket isn't bound yet, an exception is generated
                    pass
                self.server_socket.close()  # if we are waiting for accept(), should unblock

            self.logger.info("waiting for event to signal (0->1)")
            if self._wakeup:
                self._wakeup.wakeup()  # if blocking for i/o, this will shake things up

            if self.event.wait(timeout=10.0):
                self.thread.join(timeout=1.0)
                self.thread = None
                if self._wakeup:
                    self._wakeup.shutdown()  # cleanup the pair
                    self._wakeup = None
                return

            raise TimeoutError(f"Timed out waiting for USBIP server @{self.host}:{self.port} "
                               f"to acknowledge shutdown {self.event.is_set()=}")

    def unlink(self, message: bytes) -> bytes:
        """unlink the specified read"""
        cmd_unlink: CMD_UNLINK = CMD_UNLINK.unpack(message)
        busnum, devnum = cmd_unlink.devid >> 16, cmd_unlink.devid & 0xFFFF
        usb: Optional[MockDevice] = self.usb_devices.device(busnum, devnum)
        if usb is None:
            raise ValueError(f"{busnum=}/{devnum=} not found for unlink")
        unlinked_command: CMD_SUBMIT = usb.dequeue_read(seq=cmd_unlink.unlink_seqnum)
        status: int = ~errno.ECONNRESET if not unlinked_command else 0
        ret_unlink: RET_UNLINK = RET_UNLINK(status=status, seqnum=cmd_unlink.seqnum, devid=cmd_unlink.devid)
        self.logger.info(f"unlink #{cmd_unlink.unlink_seqnum} for {busnum}-{devnum}")
        return ret_unlink.pack()

    def generate_mock_response(self, usb: MockDevice, request: CMD_SUBMIT) -> bytes:
        """create a mocked response"""
        ret_submit = USBIP_RET_SUBMIT(status=0, seqnum=request.seqnum, transfer_buffer=bytes())
        response: bytes = ret_submit.pack()
        queued_read: CMD_SUBMIT = usb.dequeue_read(seq=0)  # get the first one available

        # we can send behavioral messages over as JSON
        if request.transfer_buffer.startswith(b'{'):
            cmd: dict = json.loads(request.transfer_buffer.decode('utf-8'))
            debug_command: str = cmd.get('cmd', '')
            self.logger.info(f"tunneled command: {debug_command}")
            if debug_command == MockUSBIP.DebugCommands.NO_WRITE_RESPONSE:
                self.logger.info("no-write-response, expect TimeoutError")
                return bytes()  # no RET_SUBMIT response, write will fail
            elif debug_command == MockUSBIP.DebugCommands.NO_READ_RESPONSE:
                self.logger.info("no-read-response, expect TimeoutError")
                return response  # no expected echo of write data, so subsequent read fails

        ret_submit = USBIP_RET_SUBMIT(status=0, seqnum=queued_read.seqnum, transfer_buffer=request.transfer_buffer)
        self.logger.info("generate_mock_response()")
        return response + ret_submit.pack()  # send the read values back immediately

    def mock_urb_responses(self, client: USBIPServerClient, message: bytes) -> bytes:
        """return URB packets"""
        urb_header: HEADER_BASIC = HEADER_BASIC.unpack(message)
        if urb_header.command == BasicCommands.CMD_UNLINK:  # de-queue a read request
            return self.unlink(message)

        if urb_header.command == BasicCommands.CMD_SUBMIT:
            cmd_submit: CMD_SUBMIT = CMD_SUBMIT.unpack(message)
            if cmd_submit.ep and cmd_submit.direction == Direction.USBIP_DIR_IN:  # a read is being issued
                # mock the read. Reads are "pending", a URB is queued and returned when the device has
                # data to return to the host.
                busnum, devnum = cmd_submit.devid >> 16, cmd_submit.devid & 0xFFFF
                usb: Optional[MockDevice] = self.usb_devices.device(busnum, devnum)
                if usb is None:
                    raise ValueError(f"{busnum=}/{devnum=} not found for queueing read")
                usb.enqueue_read(cmd_submit)  # associate this read with the device it's intended
                self.logger.info(f"queued read #{cmd_submit.seqnum} "
                                 f"for {busnum}-{devnum} ({len(usb.queued_reads)} in queue)")
                return bytes()  # there is no response (yet!)
            if cmd_submit.ep and cmd_submit.direction == Direction.USBIP_DIR_OUT:  # write to the device
                # mock the writing of the device. For now returns an "echo" of what was written as the
                # URB response.
                busnum, devnum = cmd_submit.devid >> 16, cmd_submit.devid & 0xFFFF
                usb = self.usb_devices.device(busnum, devnum)
                if usb is None:
                    raise ValueError(f"{busnum=}/{devnum=} not found for write")
                self.logger.info(f"device write #{cmd_submit.seqnum} "
                                 f"for {busnum}-{devnum} ({len(usb.queued_reads)} in queue)")
                return self.generate_mock_response(usb, cmd_submit)

            urb_setup: UrbSetupPacket = UrbSetupPacket.unpack(cmd_submit.setup)
            self.logger.info(f"Setup flags: {str(urb_setup)}\n{client.busid.hex()=}")
            for device in self.usb_devices.devices:
                if device.busid == client.busid:
                    transfer_buffer: bytes = bytes()
                    if urb_setup.descriptor_type == DescriptorType.DEVICE_DESCRIPTOR:
                        transfer_buffer = device.device.pack()
                    elif urb_setup.descriptor_type == DescriptorType.CONFIGURATION_DESCRIPTOR:
                        if urb_setup.request == URBStandardDeviceRequest.GET_DESCRIPTOR:
                            configuration: ConfigurationDescriptor = device.device.configurations[urb_setup.index]
                            transfer_buffer = configuration.pack()
                            interface_idx: int = 0
                            for association in configuration.associations:
                                transfer_buffer += association.pack()
                                for i in range(0, association.bInterfaceCount):
                                    interface: InterfaceDescriptor = configuration.interfaces[i+interface_idx]
                                    transfer_buffer += interface.pack()
                                    for descriptor in interface.descriptors:
                                        transfer_buffer += descriptor.pack()
                                interface_idx += association.bInterfaceCount

                            self.logger.info(f"{urb_setup.length=}, {len(transfer_buffer)=}")
                            transfer_buffer = transfer_buffer[:urb_setup.length]  # restrict response to length requested
                    elif urb_setup.descriptor_type == DescriptorType.STRING_DESCRIPTOR:
                        transfer_buffer = StringDescriptor(wLanguage=0x409).pack()
                    elif urb_setup.request == URBStandardDeviceRequest.SET_CONFIGURATION:
                        to_enable: DescriptorType = DescriptorType(urb_setup.value & 0xFF)
                        self.logger.info(f"Setting configuration: {to_enable.name}")
                        transfer_buffer = bytes()
                    elif urb_setup.request == URBCDCRequestType.SET_LINE_CODING:
                        self.logger.info("Setting line coding")
                        transfer_buffer = bytes()
                    elif urb_setup.request == URBCDCRequestType.SET_CONTROL_LINE_STATE:
                        self.logger.info("Setting control line state")
                        transfer_buffer = bytes()

                    if transfer_buffer is not None:
                        ret_submit = USBIP_RET_SUBMIT(status=0, transfer_buffer=transfer_buffer, seqnum=cmd_submit.seqnum)
                        response: bytes = ret_submit.pack()
                        self.logger.info(f"#{ret_submit.seqnum},{ret_submit.actual_length=}, "
                                         f"{len(response)=} {response.hex()=}")
                        return response

            # device not found, return error
            busid: str = client.busid.strip(b'\0').decode('utf-8') if client.is_attached else 'None'
            devices: str = ",".join([device.busid.strip(b'\0').decode('utf-8') for device in self.usb_devices.devices])
            self.logger.warning(f"operation not recognized: {urb_setup.descriptor_type.name=}, {busid=}, {devices=}")

        failure: CommonHeader = CommonHeader(command=BasicCommands.RET_SUBMIT, status=errno.ENODEV)
        client.busid = bytes()
        return failure.pack()

    def mock_response(self, client: USBIPServerClient, message: bytes) -> None:
        """use the lsusb devices to mock a response"""
        busid: str = client.busid.strip(b'\0').decode('utf-8') if client.is_attached else 'None'
        self.logger.info(f"{busid=}, {message.hex()=}")
        if client.is_attached:  # we have imported a device
            response: bytes = self.mock_urb_responses(client, message)
            if response:
                client.sendall(response)
                self.logger.info(f"client.sendall {len(response)=}, {response.hex()=}")

        header: CommonHeader = CommonHeader.unpack(message)
        if header.command == BasicCommands.REQ_DEVLIST:
            response = self.usb_devices.pack()
            self.logger.info(f"REP_DEVLIST: {response.hex()=}")
            if response:
                client.sendall(response)
            return
        elif header.command == BasicCommands.REQ_IMPORT:
            req_import: OP_REQ_IMPORT = OP_REQ_IMPORT.unpack(message)
            self.logger.info(f"REQ_IMPORT {req_import.busid.hex()}")
            if self.usb_devices.usbip is None or self.usb_devices.usbip.paths is None:
                self.logger.error("REQ_IMPORT from unrecognized device, no connection to USBIP server available")
                return

            for path in self.usb_devices.usbip.paths:
                if path.busid == req_import.busid:
                    device: Optional[MockDevice] = self.usb_devices.device(busnum=path.busnum, devnum=path.devnum)
                    if device is None:
                        raise ValueError(f"{path.busnum=}/{path.devnum=} path not found!")
                    was_already_attached: bool = device.attach  # attach the device (returns previous state
                    status: int = errno.ENODEV if device.devnum == 99 and device.busnum == 99 else 0
                    if was_already_attached:
                        self.logger.warning(f"[usbip-server]Device is already attached! {str(device)}")
                    rep_import: OP_REP_IMPORT = OP_REP_IMPORT(status=status, path=path.path,
                                                              busid=req_import.busid, busnum=path.busnum,
                                                              devnum=path.devnum, speed=path.speed,
                                                              idVendor=path.idVendor,
                                                              idProduct=path.idProduct,
                                                              bcdDevice=path.bcdDevice,
                                                              bDeviceClass=path.bDeviceClass,
                                                              bDeviceSubClass=path.bDeviceSubClass,
                                                              bDeviceProtocol=path.bDeviceProtocol,
                                                              bConfigurationValue=path.bConfigurationValue,
                                                              bNumConfigurations=path.bNumConfigurations,
                                                              bNumInterfaces=path.bNumInterfaces)
                    data: bytes = rep_import.pack()
                    client.sendall(data)
                    client.busid = path.busid
                    self.logger.info(f"OP_REP_IMPORT: {data.hex()}")
                    return

            self.logger.warning("no response")
            return

    def wait_for_message(self, conn: Optional[USBIPServerClient] = None) -> tuple[USBIPServerClient, bytes]:
        """wait for a response (or a shutdown)"""
        self.logger.info(f"wait_for_message({conn=}), {self._clients=}")
        if self._wakeup is None or self.server_socket is None:
            raise ValueError("neither the wakeup or server socket can be empty!")

        rlist: list[socket.socket | USBIPServerClient] = [self._wakeup.listener, self.server_socket.raw_socket]
        if conn:
            if conn not in self._clients:
                self._clients.append(conn)
                self._clients = [item for item in self._clients if item.fileno() != -1]

        rlist.extend(self._clients)

        while self.event.is_set():
            try:
                rlist = [item for item in rlist if item.fileno() != -1]  # no need to listen to closed sockets
                self.logger.debug(f"{rlist=}")
                read_sockets, _, _ = select.select(rlist, [], [])
                for socket_read in read_sockets:
                    if socket_read == self._wakeup.listener:  # time to bail
                        self.logger.info("Wakeup!")
                        raise OrderlyExit("wakeup!")
                    elif socket_read == self.server_socket.raw_socket:  # someone is knocking
                        self.logger.info(f"wait_for_message(): accept() {self.server_socket=}")
                        new_conn, address = self.server_socket.accept()  # accept new connection
                        client: USBIPServerClient = USBIPServerClient(connection=new_conn, address=address)
                        self._clients.append(client)
                        rlist.append(client)
                        self.logger.info(f"client @{address} connected {self._clients=}")
                        continue
                    elif isinstance(socket_read, USBIPServerClient):
                        # should be a USBClient instance
                        message: bytes = socket_read.recv(socket_read.size)
                        self.logger.info(f"wait_for_message: {message.hex()=}")
                        return socket_read, message
            except ValueError:
                if self.server_socket.fileno() < 0:
                    self.logger.warning("server socket closed prior to exit")
                else:
                    raise  # something else, pass it on up
            except OSError as os_error:
                self.logger.error(f"wait_for_message: OSError: {str(os_error)}")

        raise OrderlyExit("event set!")

    def read_message(self, client: Optional[USBIPServerClient] = None) -> tuple[USBIPServerClient, bytes]:
        """read a single message from the socket"""
        client, message = self.wait_for_message(client)
        if client.is_attached and message:  # reading URBs
            try:
                urb_cmd: CMD_SUBMIT_PREFIX = CMD_SUBMIT_PREFIX.unpack(message)
            except struct.error:
                raise ValueError(f"{message.hex()=}") from struct.error
            try:
                transfer_buffer: bytes = client.recv(urb_cmd.transfer_buffer_length) \
                    if urb_cmd.transfer_buffer_length and urb_cmd.direction == Direction.USBIP_DIR_OUT else b''
                return client, message + transfer_buffer
            except OSError:
                self.logger.error(f"Timeout, {BasicCommands(urb_cmd.command).name}, {len(message)=}, "
                                  f"{urb_cmd.transfer_buffer_length=}, {message.hex()=}")
                raise
        elif message:  # USBIP command traffic
            usbip_cmd: CommonHeader = CommonHeader.unpack(message)
            if usbip_cmd.command == BasicCommands.REQ_DEVLIST:
                return client, message
            elif usbip_cmd.command == BasicCommands.REQ_IMPORT:
                remaining_size: int = OP_REQ_IMPORT.size - len(message)  # type: ignore[operator]
                remainder = client.recv(remaining_size)
                if not remainder:
                    raise ValueError(f"Unexpected lack of response {usbip_cmd.command.name}, expected {remaining_size} bytes")
                message += remainder
                return client, message
            else:
                raise ValueError(f"Unrecognized command {usbip_cmd.command.name}")

        return client, b''

    def run_server(self):
        """standup the server, start listening"""
        self.server_socket = self._socket_class(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # type: ignore[union-attr]
        self.server_socket.bind((self.host, self.port))  # type: ignore[union-attr]
        self.server_socket.settimeout(None)  # type: ignore[union-attr]  # accept should block

        self.server_socket.listen(1)  # type: ignore[union-attr] # only allow one connection (testing)
        self.event.set()
        self.logger.info("\nmock USBIP server started @%s:%s", self.host, self.port)
        try:
            while self.event.is_set():
                client, message = self.read_message()
                if not message:
                    client.shutdown()
                elif client.is_connected:
                    self.mock_response(client, message)

        except OSError as os_error:
            failure: str = traceback.format_exc()
            self.logger.error(f"Exception {str(os_error)}\n{failure=}")
        except Exception as bad_error:  # pylint: disable=broad-exception-caught
            failure = traceback.format_exc()
            self.logger.error(f"Exception = {str(bad_error)}\n{failure=}")
        except OrderlyExit as exit_error:
            self.logger.info(f"Orderly System Shutdown ({str(exit_error)})")  # we are exiting as part of a shutdown
        finally:
            self.event.set()  # indicate we are exiting
            self.logger.info("server stopped @%s:%s", self.host, self.port)

    def read_paths(self) -> list[OP_REP_DEV_PATH]:
        """read the paths from the JSON file"""
        devlist: bytes = bytes.fromhex("".join([item for item in self._protocol_responses['OP_REP_DEVLIST']]))
        devlist_header: OP_REP_DEVLIST_HEADER = (
            OP_REP_DEVLIST_HEADER.unpack(devlist[:OP_REP_DEVLIST_HEADER.size]))  # type: ignore[misc]
        devices: bytes = devlist[OP_REP_DEVLIST_HEADER.size:]  # type: ignore[misc]
        paths: list[OP_REP_DEV_PATH] = []
        for _ in range(0, devlist_header.num_exported_devices):
            path: OP_REP_DEV_PATH = OP_REP_DEV_PATH.unpack(devices)
            paths.append(path)
            devices = devices[OP_REP_DEV_PATH.size:]  # type: ignore[misc]
            for _ in range(0, path.bNumInterfaces):
                interface: OP_REP_DEV_INTERFACE = OP_REP_DEV_INTERFACE.unpack(devices)
                path.interfaces.append(interface)
                devices = devices[OP_REP_DEV_INTERFACE.size:]  # type: ignore[misc]

        return paths
