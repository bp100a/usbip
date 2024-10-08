"""test connection to usbip server"""
import os
from time import sleep
from typing import Optional

from common_test_base import CommonTestBase
from mock_usbip import MockUSBIP

from serial_usbipclient.protocol.packets import OP_REP_DEVLIST_HEADER
from serial_usbipclient.usbip_client import (HardwareID, USBAttachError,
                                             USBIP_Connection, USBIPClient)


class TestUSBIPConnection(CommonTestBase):
    """test connections to a USBIP service"""
    def __init__(self, methodName):
        """set up local variables"""
        super().__init__(methodName)
        self.mock_usbip: Optional[MockUSBIP] = None
        self.client: Optional[USBIPClient] = None

        # this is the 8.8 inch Turing smart screen (CPU monitor)
        self.vid: int = 0x525  # Netchip Technology, Inc.
        self.pid: int = 0xA4A7  # Linux-USB Serial Gadget (CDC ACM mode)
        self.hardware_id: HardwareID = HardwareID(vid=self.vid, pid=self.pid)

    def setUp(self):
        """set up our connection test"""
        super().setUp()
        self.port += self.get_test_index(name=os.path.join(__file__, str(__class__.__name__), self._testMethodName))
        self.mock_usbip = MockUSBIP(host=self.host, port=self.port)

    def test_connection(self):
        """test simple connection"""
        # if not self.CI:
        #     # run against a "real" USBIPD service when not in the CI environment
        #     self.port = self.DEFAULT_USBIP_SERVER_PORT

        published: OP_REP_DEVLIST_HEADER = self.connect_server()
        self.assertTrue(published.paths)

        try:
            self.client.attach(devices=[self.hardware_id], published=published)
            connections: list[USBIP_Connection] = self.client.get_connection(device=self.hardware_id)
            self.assertEqual(len(connections), 2)  # 2 devices with same PID/VID is possible!
        except USBAttachError as a_error:
            self.logger.error(a_error.detail)
            raise
        except ValueError as v_error:
            self.logger.error(v_error)
            raise
        except Exception as a_error:
            self.logger.error(str(a_error))
            raise
        finally:
            pass

    def connect_server(self) -> OP_REP_DEVLIST_HEADER:
        """connect to the USBIP server"""
        self.client: USBIPClient = USBIPClient(remote=(self.host, self.port))
        self.client.connect_server()
        return self.client.list_published()

    def test_connection_shutdown(self):
        """test shutting down the connection"""
        published: OP_REP_DEVLIST_HEADER = self.connect_server()
        self.assertTrue(published.paths)
        try:
            self.client.attach(devices=[self.hardware_id], published=published)
            connections: list[USBIP_Connection] = self.client.get_connection(device=self.hardware_id)
            self.assertEqual(len(connections), 2)  # should be 2 connections
            self.client.shutdown()  # shut it all down
        except USBAttachError as a_error:
            self.logger.error(a_error.detail)
            raise

    def test_queue_urbs(self):
        """test queue urbs to the server"""
        published: OP_REP_DEVLIST_HEADER = self.connect_server()
        self.assertTrue(published.paths)
        try:
            self.client.attach(devices=[self.hardware_id], published=published)
            connections: list[USBIP_Connection] = self.client.get_connection(device=self.hardware_id)
            self.assertEqual(len(connections), 2)  # should be a single connection
            self.client.queue_urbs(usb=connections[0])
            sleep(1.0)
            self.client.shutdown()  # shut it all down
        except USBAttachError as a_error:
            self.logger.error(a_error.detail)
            raise
