import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import tsunami


ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Example coast</title>
    <updated>2026-10-20T10:45:01Z</updated>
    <id>urn:uuid:warning-1</id>
    <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <strong>Category:</strong> Warning<br/>
      <strong>Bulletin Issue Time:</strong> 2026-10-20 10:45 UTC<br/>
      <strong>Affected Region:</strong> Example coast
    </div></summary>
    <link rel="related" title="CapXML document" href="/event/cap.xml" type="application/cap+xml"/>
    <link rel="alternate" title="Bulletin" href="/event/bulletin.txt" type="application/xml"/>
  </entry>
  <entry>
    <title>Information-only event</title>
    <updated>2026-10-20T10:45:01Z</updated>
    <id>urn:uuid:info-1</id>
    <summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
      <strong>Category:</strong> Information<br/>
      <strong>Bulletin Issue Time:</strong> 2026-10-20 10:45 UTC
    </div></summary>
  </entry>
</feed>"""


class ParseFeedTests(unittest.TestCase):
    def test_parses_atom_fields_and_product_links(self) -> None:
        products = tsunami.parse_feed(
            ATOM_FEED, "NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"
        )

        self.assertEqual(len(products), 2)
        self.assertEqual(products[0].category, "warning")
        self.assertEqual(
            products[0].bulletin_url,
            "https://www.tsunami.gov/event/bulletin.txt",
        )
        self.assertEqual(
            products[0].cap_url,
            "https://www.tsunami.gov/event/cap.xml",
        )
        self.assertEqual(products[1].category, "information")

    def test_rejects_non_atom_or_invalid_xml(self) -> None:
        with self.assertRaises(ValueError):
            tsunami.parse_feed(b"<html/>", "NTWC", "https://www.tsunami.gov/feed")
        with self.assertRaises(ValueError):
            tsunami.parse_feed(b"<feed", "NTWC", "https://www.tsunami.gov/feed")

    def test_cancellation_category_is_actionable(self) -> None:
        self.assertEqual(
            tsunami.category_from_summary("Category: Cancellation Bulletin Issue Time: now"),
            "cancellation",
        )

    def test_notification_fits_meshtastic_byte_limit(self) -> None:
        product = tsunami.parse_feed(
            ATOM_FEED, "NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"
        )[0]

        message = tsunami.format_product(product)

        self.assertLessEqual(len(message.encode("utf-8")), tsunami.MAX_MESSAGE_BYTES)
        self.assertIn(product.bulletin_url, message)


class NotifierTests(unittest.TestCase):
    def make_notifier(self, state_file: Path) -> tsunami.TsunamiNotifier:
        return tsunami.TsunamiNotifier(
            node_ip="127.0.0.1",
            channel=5,
            interval=300,
            state_file=state_file,
            send_initial=False,
            centers={"ntwc", "ptwc"},
        )

    @patch("tsunami.time.sleep")
    @patch("tsunami.TCPInterface")
    def test_baselines_first_run_then_sends_new_alert(
        self, interface_factory: Mock, _sleep: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            notifier = self.make_notifier(state_file)
            warning = tsunami.parse_feed(
                ATOM_FEED, "NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"
            )[0]
            notifier.fetch_products = Mock(return_value=[warning])

            notifier.process_once()
            interface_factory.assert_not_called()
            self.assertTrue(notifier.state_initialized)

            new_warning = tsunami.TsunamiProduct(
                product_id="urn:uuid:warning-2",
                center=warning.center,
                title=warning.title,
                updated=warning.updated,
                category=warning.category,
                summary=warning.summary,
                bulletin_url=warning.bulletin_url,
                cap_url=warning.cap_url,
            )
            notifier.fetch_products = Mock(return_value=[new_warning])
            notifier.process_once()

            interface_factory.assert_called_once_with("127.0.0.1")
            interface_factory.return_value.sendText.assert_called_once()
            self.assertIn(new_warning.product_id, notifier.seen_ids)
            persisted = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn(new_warning.product_id, persisted["seen_product_ids"])

    @patch("tsunami.TCPInterface")
    def test_information_products_are_not_sent(self, interface_factory: Mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            notifier = self.make_notifier(Path(temp_dir) / "state.json")
            products = tsunami.parse_feed(
                ATOM_FEED, "NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"
            )
            notifier.fetch_products = Mock(return_value=[products[1]])

            notifier.process_once()

            interface_factory.assert_not_called()
            self.assertTrue(notifier.state_file.exists())

    @patch("tsunami.time.sleep")
    @patch("tsunami.TCPInterface")
    def test_empty_initial_feed_does_not_suppress_later_alert(
        self, interface_factory: Mock, _sleep: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            notifier = self.make_notifier(Path(temp_dir) / "state.json")
            notifier.fetch_products = Mock(return_value=[])

            notifier.process_once()
            self.assertTrue(notifier.state_initialized)

            warning = tsunami.parse_feed(
                ATOM_FEED, "NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"
            )[0]
            notifier.fetch_products = Mock(return_value=[warning])
            notifier.process_once()

            interface_factory.assert_called_once_with("127.0.0.1")
            interface_factory.return_value.sendText.assert_called_once()


if __name__ == "__main__":
    unittest.main()
