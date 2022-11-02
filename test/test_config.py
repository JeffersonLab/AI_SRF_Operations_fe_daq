import os
from unittest import TestCase
import logging
from src.fe_daq.app_config import Config
from threading import Thread

logger = logging.getLogger()
test_dir = os.path.realpath(os.path.dirname(__file__))


class TestCavity(TestCase):
    def test_clear_config(self):
        Config.config['test_entry'] = 'test'
        Config.config['test-dict'] = {'test': 123}
        Config.clear_config()
        self.assertDictEqual(Config.config, {})

    def test_parse_config_file(self):
        # Test that we can correctly parse a config file
        exp = {
            "test-entry": 'test',
            "test-dict": {'test': 123, 'test2': [1, 2, 3], 'test3': {'test4': 'test'}},
            "test-array": [1, 2, 3]
        }
        Config.parse_config_file(test_dir + "/config-test.json")
        self.assertDictEqual(Config.config, exp)

    def test_config_multiple_threads(self):
        # Test that a change in one thread is seen back in another
        def _setter():
            with Config.config_lock:
                Config.config['thread-test'] = 'thread-test-val'

        Config.clear_config()
        t = Thread(target=_setter)
        t.start()
        t.join()

        self.assertDictEqual(Config.config, {'thread-test': 'thread-test-val'})

