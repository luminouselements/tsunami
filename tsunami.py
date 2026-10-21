#!/usr/bin/env python3
"""Poll tsunami.gov Atom feeds and broadcast tsunami alerts over Meshtastic."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from meshtastic.mesh_interface import MeshInterface
from meshtastic.tcp_interface import TCPInterface


FEEDS = {
    "ntwc": ("NTWC", "https://www.tsunami.gov/events/xml/PAAQAtom.xml"),
    "ptwc": ("PTWC", "https://www.tsunami.gov/events/xml/PHEBAtom.xml"),
}
ATOM_NS = "http://www.w3.org/2005/Atom"
CATEGORY_ALIASES = {
    "cancel": "cancellation",
    "cancelled": "cancellation",
    "canceled": "cancellation",
}
ALERT_CATEGORIES = {"warning", "watch", "advisory", "cancellation"}
MAX_MESSAGE_BYTES = 199
FETCH_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
LOG = logging.getLogger("tsunami-notifier")


@dataclass(frozen=True)
class TsunamiProduct:
    product_id: str
    center: str
    title: str
    updated: str
    category: str
    summary: str
    bulletin_url: str
    cap_url: str


def parse_csv(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def category_from_summary(summary: str) -> str:
    match = re.search(
        r"\bCategory:\s*(.*?)\s+(?:Bulletin Issue Time:|Preliminary Magnitude:|"
        r"Lat/Lon:|Affected Region:|Note:|Definition:|$)",
        summary,
        flags=re.IGNORECASE,
    )
    if not match:
        return "unknown"
    value = match.group(1).strip().lower()
    if value in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[value]
    for category in ALERT_CATEGORIES:
        if re.search(rf"\b{category}\b", value):
            return category
    return value or "unknown"


def parse_feed(content: bytes, center: str, feed_url: str) -> list[TsunamiProduct]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid Atom XML from {center}") from exc

    if root.tag != f"{{{ATOM_NS}}}feed":
        raise ValueError(f"Expected an Atom feed from {center}")

    products: list[TsunamiProduct] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry"):
        product_id = (entry.findtext(f"{{{ATOM_NS}}}id") or "").strip()
        title = (entry.findtext(f"{{{ATOM_NS}}}title") or "").strip()
        updated = (entry.findtext(f"{{{ATOM_NS}}}updated") or "").strip()
        summary_element = entry.find(f"{{{ATOM_NS}}}summary")
        summary = (
            " ".join(" ".join(summary_element.itertext()).split())
            if summary_element is not None
            else ""
        )
        if not product_id or not title:
            LOG.warning("Skipping a %s feed entry without an ID or title", center)
            continue

        bulletin_url = ""
        cap_url = ""
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            resolved_url = urljoin(feed_url, href)
            link_type = (link.get("type") or "").lower()
            link_title = (link.get("title") or "").lower()
            relation = (link.get("rel") or "").lower()
            if "cap+xml" in link_type or "cap" in link_title:
                cap_url = resolved_url
            elif relation == "alternate" or "bulletin" in link_title:
                bulletin_url = resolved_url

        products.append(
            TsunamiProduct(
                product_id=product_id,
                center=center,
                title=title,
                updated=updated,
                category=category_from_summary(summary),
                summary=summary,
                bulletin_url=bulletin_url,
                cap_url=cap_url,
            )
        )
    return products


def truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    if max_bytes <= 3:
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    return encoded[: max_bytes - 3].decode("utf-8", errors="ignore") + "..."


def format_product(product: TsunamiProduct) -> str:
    source_url = product.bulletin_url or product.cap_url or "https://www.tsunami.gov/"
    prefix = f"TSUNAMI {product.category.upper()}: {product.title}\n{product.center}\n"
    suffix = f"\n{source_url}"
    context = truncate_utf8(
        product.summary,
        max(0, MAX_MESSAGE_BYTES - len((prefix + suffix).encode("utf-8"))),
    )
    return truncate_utf8(prefix + context + suffix, MAX_MESSAGE_BYTES)


class TsunamiNotifier:
    def __init__(
        self,
        node_ip: str,
        channel: int,
        interval: int,
        state_file: Path,
        send_initial: bool,
        centers: set[str],
    ) -> None:
        invalid_centers = centers - FEEDS.keys()
        if invalid_centers:
            raise ValueError(f"Unknown tsunami warning center(s): {', '.join(sorted(invalid_centers))}")
        if not centers:
            raise ValueError("At least one tsunami warning center must be selected")

        self.node_ip = node_ip
        self.channel = channel
        self.interval = interval
        self.state_file = state_file
        self.send_initial = send_initial
        self.centers = centers
        self.interface: TCPInterface | None = None
        self.seen_ids, self.state_initialized = self.load_state()
        self.running = True

    def load_state(self) -> tuple[set[str], bool]:
        if not self.state_file.exists():
            return set(), False
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("seen_product_ids"), list):
            raise ValueError(f"Invalid state file format: {self.state_file}")
        seen_ids = data["seen_product_ids"]
        if any(not isinstance(product_id, str) for product_id in seen_ids):
            raise ValueError(f"Invalid product IDs in state file: {self.state_file}")
        return set(seen_ids), True

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.state_file.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps({"seen_product_ids": sorted(self.seen_ids)[-200:]}, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(self.state_file)
        self.state_initialized = True

    def fetch_center(self, center_key: str) -> Iterable[TsunamiProduct]:
        center_name, feed_url = FEEDS[center_key]
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                response = requests.get(
                    feed_url,
                    headers={"Accept": "application/atom+xml, application/xml"},
                    timeout=30,
                )
                response.raise_for_status()
                return parse_feed(response.content, center_name, feed_url)
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code not in RETRYABLE_STATUS_CODES or attempt == FETCH_ATTEMPTS:
                    raise
                LOG.warning(
                    "%s returned HTTP %s; retrying in %d seconds (attempt %d/%d)",
                    center_name,
                    status_code,
                    attempt * 5,
                    attempt,
                    FETCH_ATTEMPTS,
                )
            except requests.RequestException:
                if attempt == FETCH_ATTEMPTS:
                    raise
                LOG.warning(
                    "Request to %s failed; retrying in %d seconds (attempt %d/%d)",
                    center_name,
                    attempt * 5,
                    attempt,
                    FETCH_ATTEMPTS,
                )
            time.sleep(attempt * 5)
        raise RuntimeError(f"Could not fetch the {center_name} Atom feed")

    def fetch_products(self) -> list[TsunamiProduct]:
        products: list[TsunamiProduct] = []
        for center_key in sorted(self.centers):
            products.extend(self.fetch_center(center_key))
        return products

    @staticmethod
    def is_alert(product: TsunamiProduct) -> bool:
        return product.category in ALERT_CATEGORIES

    def process_once(self) -> None:
        products = self.fetch_products()
        alerts = [product for product in products if self.is_alert(product)]
        for product in products:
            if product.category == "unknown":
                LOG.warning(
                    "Could not classify %s product %s; not sending it",
                    product.center,
                    product.product_id,
                )

        if not self.state_initialized and not self.send_initial:
            self.seen_ids.update(product.product_id for product in alerts)
            self.save_state()
            LOG.info("Initialized state with %d current tsunami alerts", len(alerts))
            return

        new_alerts = [product for product in alerts if product.product_id not in self.seen_ids]
        if not new_alerts:
            LOG.info("No new tsunami alerts or cancellations")
            if not self.state_initialized:
                self.save_state()
            return

        if self.interface is None:
            self.interface = TCPInterface(self.node_ip)
        for product in new_alerts:
            message = format_product(product)
            try:
                self.interface.sendText(text=message, channelIndex=self.channel)
            except MeshInterface.MeshInterfaceError as exc:
                LOG.error(
                    "Could not send tsunami product %s (%d UTF-8 bytes): %s",
                    product.product_id,
                    len(message.encode("utf-8")),
                    exc,
                )
                continue
            self.seen_ids.add(product.product_id)
            LOG.info(
                "Broadcast %s product %s from %s on channel %d",
                product.category,
                product.product_id,
                product.center,
                self.channel,
            )
            time.sleep(2)
        self.save_state()

    def stop(self, *_: object) -> None:
        self.running = False
        if self.interface is not None:
            self.interface.close()
            self.interface = None

    def run(self) -> None:
        while self.running:
            try:
                self.process_once()
            except (requests.RequestException, ValueError, OSError) as exc:
                LOG.error("Tsunami notification cycle failed: %s", exc)
            if self.running:
                time.sleep(self.interval)


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one polling cycle and exit")
    parser.add_argument("--node", default=os.getenv("MESH_NODE_IP", "192.168.50.46"))
    parser.add_argument(
        "--channel", type=int, default=int(os.getenv("MESH_CHANNEL_INDEX", "5"))
    )
    parser.add_argument(
        "--interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not 0 <= args.channel <= 7:
        parser.error("--channel must be between 0 and 7")
    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")

    try:
        notifier = TsunamiNotifier(
            node_ip=args.node,
            channel=args.channel,
            interval=args.interval,
            state_file=Path(os.getenv("STATE_FILE", "state.json")),
            send_initial=os.getenv("SEND_INITIAL_ALERTS", "false").lower() == "true",
            centers=parse_csv(os.getenv("TSUNAMI_CENTERS", "ntwc,ptwc")),
        )
        signal.signal(signal.SIGTERM, notifier.stop)
        signal.signal(signal.SIGINT, notifier.stop)
        if args.once:
            try:
                notifier.process_once()
                return 0
            except (requests.RequestException, ValueError, OSError) as exc:
                LOG.error("Tsunami notification cycle failed: %s", exc)
                return 1
            finally:
                notifier.stop()
        notifier.run()
        return 0
    except (ValueError, OSError) as exc:
        LOG.error("Could not start tsunami notifier: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
