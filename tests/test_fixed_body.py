import base64
import json
import unittest
from fixed_body import normalize_body, decode_body, body_prompt, fixed_prompt


class FixedBodyTests(unittest.TestCase):
    def test_saved_body_round_trip(self):
        body = {"heightCm": None, "build": "reference", "bust": "D"}
        self.assertEqual(decode_body(base64.b64encode(json.dumps(body).encode()).decode()), body)
        self.assertIn("D-cup-like", body_prompt(body))
        self.assertIn("without inventing", body_prompt(body))

    def test_default_is_reference_not_invented_size(self):
        self.assertIn("Preserve the reference bust", body_prompt(None))
        self.assertNotIn("D-cup", body_prompt(None))

    def test_invalid_data(self):
        for body in ({}, {"heightCm": True, "build": "reference", "bust": "D"},
                     {"heightCm": None, "build": "reference", "bust": "bad"}):
            with self.assertRaises(ValueError):
                normalize_body(body)

    def test_replaces_stale_instruction_once(self):
        prompt = fixed_prompt("Walk outside. " + body_prompt(None), {"heightCm": 170, "build": "slim", "bust": "D"})
        self.assertEqual(prompt.count("FIXED ADULT BODY IDENTITY"), 1)
        self.assertIn("170 cm", prompt)


if __name__ == "__main__":
    unittest.main()
