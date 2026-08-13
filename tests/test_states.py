import unittest

from forecast_collector.states import congressional_district_code, plain_house_seat, resolve_state


class StateTests(unittest.TestCase):
    def test_four_digit_district_code(self):
        self.assertEqual(congressional_district_code("Alabama", 1), "0101")
        self.assertEqual(congressional_district_code("NY", 7), "3607")
        self.assertEqual(congressional_district_code("Texas", 38), "4838")

    def test_plain_language_labels(self):
        self.assertEqual(plain_house_seat("WY", 1), "Wyoming At-Large Congressional District")
        self.assertEqual(plain_house_seat("NY", 2), "New York 2nd Congressional District")

    def test_state_resolution(self):
        self.assertEqual(resolve_state("Pennsylvania"), ("PA", "Pennsylvania", "42"))
