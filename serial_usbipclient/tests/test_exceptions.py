"""test exceptions are properly raised"""
import socket

from common_test_base import CommonTestBase, MockSocketWrapper

from serial_usbipclient import (EndPointDescriptor, HardwareID, USBIPClient,
                                USBIPResponseTimeoutError)
from serial_usbipclient.protocol.packets import (CMD_SUBMIT, CMD_UNLINK,
                                                 OP_REP_IMPORT)
from serial_usbipclient.protocol.urb_packets import ConfigurationDescriptor
from serial_usbipclient.usbip_client import (USBConnectionLostError,
                                             USBIP_Connection, USBIPValueError)


class ConnectionErrorSocketWrapper(MockSocketWrapper):
    """raise a gai error on connection"""
    def sendall(self, data: bytes) -> None:
        """raise a socket error"""
        raise ConnectionError('mock socket wrapper')


class TestExceptions(CommonTestBase):
    """tests exception throwing"""
    def test_endpoint_exceptions(self):
        """test exceptions from endpoints"""
        conn: USBIP_Connection = USBIP_Connection()
        _ = conn.control  # no exception

        # exceptions should be thrown from uninitialized endpoints
        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no input endpoint!'):
            _ = conn.input

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no output endpoint!'):
            _ = conn.output

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no endpoint!'):
            _ = conn.pending_reads

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no configuration'):
            _ = conn.configuration

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='cannot set configuration'):
            conn.configuration = ConfigurationDescriptor()

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='socket not available'):
            conn.send_command(command=CMD_SUBMIT())

        with self.assertRaisesRegex(expected_exception=USBConnectionLostError, expected_regex='connection lost'):
            conn.socket = ConnectionErrorSocketWrapper(family=socket.AF_INET, kind=socket.SOCK_STREAM)
            conn.send_unlink(command=CMD_UNLINK(seqnum=2, unlink_seqnum=1))

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex=r'missing endpoint\(s\)'):
            conn.wait_for_response(header_data=None)

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex=r'missing input endpoint'):
            conn.response_data()

    def test_valid_endpoints(self):
        """valid endpoints, bad socket"""
        conn: USBIP_Connection = USBIP_Connection()
        conn.endpoint.input = EndPointDescriptor()
        conn.endpoint.output = EndPointDescriptor()
        conn.socket = MockSocketWrapper(family=socket.AF_INET, kind=socket.SOCK_STREAM)
        self.assertFalse(conn.wait_for_response(header_data=None))

        self.assertIsNone(conn.wait_for_unlink())

        with self.assertRaisesRegex(expected_exception=USBIPResponseTimeoutError, expected_regex=r'Timeout error'):
            conn.response_data()

    def test_oserror_shutdown(self):
        """test we handle an OSError thrown during shutdown"""
        class ShutdownErrorSocketWrapper(MockSocketWrapper):
            """raise a timeout error on connection"""
            def shutdown(self, how: int) -> None:
                """raise an OSError"""
                raise OSError('mock shutdown')

        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no socket to read'):
            USBIPClient.readall(size=0, usb=USBIP_Connection())

        self.client = USBIPClient(remote=(self.host, self.port), socket_class=ShutdownErrorSocketWrapper)
        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no socket to set options'):
            self.client.set_tcp_nodelay()
        self.client.connect_server()
        self.client.disconnect_server()  # OSError raised but swallowed as expected

    def test_no_connection(self):
        """test we handle no connection when we try to use it"""
        self.client = USBIPClient(remote=(self.host, self.port), socket_class=MockSocketWrapper)
        with self.assertRaisesRegex(expected_exception=USBIPValueError, expected_regex='no socket to remove'):
            self.client.create_connection(device=HardwareID(0, 0), attached=OP_REP_IMPORT(devnum=1, busnum=1))
