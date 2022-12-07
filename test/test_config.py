import os
from unittest import TestCase
import logging
from fe_daq import app_config as config
from threading import Thread

logger = logging.getLogger()
test_dir = os.path.realpath(os.path.dirname(__file__))


class TestCavity(TestCase):
    def test_clear_config(self):
        config.set_parameter('test_entry', 'test')
        config.set_parameter('test-dict', {'test': 123})
        config.clear_config()
        self.assertDictEqual(config.get_parameter(None), {})

    def test_parse_config_file(self):
        # Test that we can correctly parse a config file
        exp = {
            "test-entry": 'test',
            "test-dict": {'test': 123, 'test2': [1, 2, 3], 'test3': {'test4': 'test'}},
            "test-array": [1, 2, 3]
        }
        config.parse_config_file(test_dir + "/config-test.json")
        try:
            self.assertDictEqual(config.get_parameter(None), exp)
        finally:
            config.clear_config()

    def test_config_multiple_threads(self):
        # Test that a change in one thread is seen back in another
        def _setter():
            config.set_parameter('thread-test',  'thread-test-val')

        config.clear_config()
        t = Thread(target=_setter)
        t.start()
        t.join()

        try:
            self.assertDictEqual(config.get_parameter(None), {'thread-test': 'thread-test-val'})
        finally:
            config.clear_config()

    def test_config_set_get_parameter_single(self):
        config.clear_config()
        config.set_parameter('testing', 'test-value')
        self.assertEqual(config.get_parameter('testing'), 'test-value')

    def test_config_set_get_parameter_multi(self):
        config.clear_config()
        config.set_parameter('testing', {'13': 'test-value'})
        self.assertEqual(config.get_parameter(['testing', '13']), 'test-value')

    def test_config_set_multi_get_parameter_multi(self):
        config.clear_config()
        config.set_parameter('testing', {})
        config.set_parameter(['testing', '13'], 'test-value')
        self.assertEqual(config.get_parameter(['testing', '13']), 'test-value')

