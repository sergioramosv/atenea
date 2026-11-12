import unittest

import utils


class Smoke(unittest.TestCase):
    def test_importa(self):
        self.assertTrue(callable(utils.es_palindromo))


if __name__ == "__main__":
    unittest.main()
