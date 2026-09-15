import json
import unittest

from agenthub import fastjson


class FastJsonTests(unittest.TestCase):
    SAMPLES = [
        '{"a": 1, "b": [1, 2.5, "x", null, true], "c": {"d": "é 中文 \\u00e9"}}',
        '[]', '"str"', '9223372036854775807', '-0.0', '1e400',
        '{"nested": {"deep": [[[]]]}, "unicode": "\\ud83d\\ude00"}',
    ]

    def test_matches_stdlib_on_text_and_bytes(self):
        for sample in self.SAMPLES:
            self.assertEqual(fastjson.loads(sample), json.loads(sample), sample)
            self.assertEqual(fastjson.loads(sample.encode()), json.loads(sample), sample)

    def test_stdlib_extensions_fall_back_instead_of_failing(self):
        # orjson rejects NaN/Infinity; the standard library's answer wins.
        for sample in ('{"x": NaN}', '[Infinity, -Infinity]'):
            self.assertEqual(repr(fastjson.loads(sample)), repr(json.loads(sample)))

    def test_errors_are_the_stdlib_error(self):
        for sample in ('{"a": }', '', 'nul', '[1,]'):
            with self.assertRaises(json.JSONDecodeError):
                fastjson.loads(sample)

    def test_integers_beyond_64_bits_are_the_documented_exception(self):
        # orjson hands these back as floats; documented, not guarded (the guard cost
        # more than orjson saved on real session files).
        value = fastjson.loads('123456789012345678901234567890')
        self.assertIn(type(value), (int, float))
        if fastjson.AVAILABLE:
            self.assertAlmostEqual(value, 1.2345678901234568e+29)

    def test_keyword_arguments_route_to_stdlib(self):
        hook = lambda d: {"hooked": True, **d}
        self.assertEqual(fastjson.loads('{"a": 1}', object_hook=hook), {"hooked": True, "a": 1})
        self.assertEqual(fastjson.loads('1.5', parse_float=str), "1.5")


if __name__ == "__main__":
    unittest.main()
