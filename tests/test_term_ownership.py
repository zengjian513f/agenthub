import threading
import unittest
from unittest.mock import patch

from sesman import term_ownership


class FakeConnection:
    def __init__(self):
        self.closed = threading.Event()
        self.revocations = []

    def revoke(self, new_ip, notify=True):
        self.revocations.append((new_ip, notify))
        self.closed.set()


class TerminalOwnershipTests(unittest.TestCase):
    def test_other_page_must_force_and_old_connection_is_revoked(self):
        registry = term_ownership.Registry()
        first = registry.claim("term", "page-a", "192.0.2.10")
        connection = FakeConnection()
        self.assertTrue(registry.bind("term", "page-a", first["token"], connection))

        conflict = registry.claim("term", "page-b", "192.0.2.11")
        self.assertEqual(conflict, {
            "conflict": True,
            "owner": {"ip": "192.0.2.10", "since": first["owner"]["since"]},
        })
        self.assertEqual(connection.revocations, [])

        second = registry.claim("term", "page-b", "192.0.2.11", force=True)
        self.assertTrue(second["ok"])
        self.assertEqual(connection.revocations, [("192.0.2.11", True)])
        self.assertFalse(registry.bind("term", "page-a", first["token"], FakeConnection()))
        self.assertTrue(registry.bind("term", "page-b", second["token"], FakeConnection()))

    def test_same_page_reconnect_replaces_silently(self):
        registry = term_ownership.Registry()
        first = registry.claim("term", "page-a", "10.0.0.1")
        connection = FakeConnection()
        registry.bind("term", "page-a", first["token"], connection)
        second = registry.claim("term", "page-a", "10.0.0.2")
        self.assertTrue(second["ok"])
        self.assertEqual(connection.revocations, [("10.0.0.2", False)])

    def test_old_finally_cannot_release_new_owner(self):
        registry = term_ownership.Registry()
        first = registry.claim("term", "page-a", "10.0.0.1")
        second = registry.claim("term", "page-b", "10.0.0.2", force=True)
        registry.release("term", first["token"])
        self.assertEqual(registry.owner("term")["ip"], "10.0.0.2")
        registry.release("term", second["token"])
        self.assertIsNone(registry.owner("term"))

    def test_abandoned_reservation_expires(self):
        registry = term_ownership.Registry()
        registry.claim("term", "page-a", "10.0.0.1")
        registry._leases["term"].reserved_at = 0
        with patch.object(term_ownership.time, "monotonic", return_value=20):
            self.assertIsNone(registry.owner("term"))


if __name__ == "__main__":
    unittest.main()
