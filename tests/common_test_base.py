"""base for all unit tests"""

import sys
from os import getenv
import logging

from unittest import TestCase


class CommonTestBase(TestCase):
    """base class for common behavior to all unit tests"""
    @staticmethod
    def is_truthy(key: str, default: bool) -> bool:
        """read environment variable, return boolean response"""
        value: str = getenv(key, default=str(default)).upper()
        return value in ['TRUE', '1', '1.0']

    def skip_on_ci(self):
        """skip the test if running on a CI/CD system"""
        if self.CI:
            self.skipTest(reason='incompatible with CI')

    def __init__(self, methodName):
        """need some special variables"""
        self.CI: bool = CommonTestBase.is_truthy('CI', False)
        self.logger: logging.Logger = logging.getLogger(__name__)
        if not self.logger.hasHandlers():
            formatter: logging.Formatter = logging.Formatter('%(asctime)s \t%(levelname)s \t%(name)s \t%(message)s')
            handler: logging.Handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(formatter)
            handler.setLevel(logging.DEBUG)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.DEBUG)
            self.logger.info("stdout handler setup")
        super().__init__(methodName)

        if methodName != 'runTest':
            self.logger.debug(f"running {methodName}")
