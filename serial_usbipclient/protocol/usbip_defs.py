"""full implementation of a python-only client to connect to usbipd servers"""

# https://docs.kernel.org/usb/usbip_protocol.html

import errno
from enum import IntEnum
from os import strerror

# To keep with names used by the URB specification
# pylint: disable=invalid-name, too-many-arguments, too-many-instance-attributes, too-many-locals

DEFAULT_PORT: int = 3240


class BasicCommands(IntEnum):
    """command codes for USBIP packets"""

    CMD_SUBMIT = 0x01
    RET_SUBMIT = 0x03
    CMD_UNLINK = 0x02
    RET_UNLINK = 0x04
    UNDEFINED = 0x00

    REQ_DEVLIST = 0x8005
    REP_DEVLIST = 0x0005
    REQ_IMPORT = 0x8003


class Direction(IntEnum):
    """direction of usb transfer, from client's perspective"""

    USBIP_DIR_OUT = 0  # host -> device
    USBIP_DIR_IN = 1  # device -> host


class DeviceSpeed(IntEnum):
    """definitions for reported USB speed"""

    USB_SPEED_UNKNOWN = 0
    USB_SPEED_LOW = 1
    USB_SPEED_FULL = 2  # usb 1.1
    USB_SPEED_HIGH = 3  # usb 2.0
    USB_SPEED_VARIABLE = 4  # wireless(usb2.5)


class Status(IntEnum):
    """status codes for USBIP"""

    SUCCESS = 0x0
    ERROR = 0x1
    BUSY = 0x2  # attempting to attach to a device that is ready attached


class CDCControl(IntEnum):
    """control types specific to CDC devices for SetControlLineState()"""

    USB_CDC_CTRL_DTR = 1 << 0
    USB_CDC_CTRL_RTS = 1 << 1


class ErrorCodes:  # pylint: disable=line-too-long
    """mappings for errno -> description"""

    ERRNO: dict[int, str] = {
        errno.ENOMEM: "no memory for allocation of internal structures",
        errno.EBUSY: "The URB is already active.",
        errno.ENODEV: "specified USB-device or bus doesn't exist",
        errno.ENOENT: "specified interface or endpoint does not exist or is not enabled",
        errno.ENXIO: "host controller driver does not support queuing of this type of urb. (treat as a host controller bug.)",
        errno.EINVAL: "a. Invalid transfer type specified (or not supported)\n"
        "b. Invalid or unsupported periodic transfer interval\n"
        "c. ISO: attempted to change transfer interval\n"
        "d. ISO: number_of_packets is < 0\n"
        "e. various other cases",
        errno.EXDEV: "ISO: URB_ISO_ASAP wasn't specified and all the frames the URB would be scheduled in have already expired.",
        errno.EFBIG: "Host controller driver can’t schedule that many ISO frames.",
        errno.EPIPE: "The pipe type specified in the URB doesn't match the endpoint’s actual type.",
        errno.EMSGSIZE: "a. endpoint maxpacket size is zero; it is not usable in the current interface altsetting.\n"
        "b. ISO packet is larger than the endpoint maxpacket.\n"
        "c. requested data transfer length is invalid: negative or too large for the host controller.",
        errno.ENOSPC: "This request would over commit the usb bandwidth reserved for periodic transfers (interrupt, isochronous).",
        errno.ESHUTDOWN: "The device or host controller has been disabled due to some problem that could not be worked around.",
        errno.EPERM: "Submission failed because urb->reject was set.",
        errno.EHOSTUNREACH: "URB was rejected because the device is suspended.",
        errno.ENOEXEC: "A control URB doesn't contain a Setup packet.",
    }

    @staticmethod
    def readable_errno(err_no: int) -> str:
        """return a user readable version of the error code"""
        return ErrorCodes.ERRNO.get(err_no, strerror(err_no))
