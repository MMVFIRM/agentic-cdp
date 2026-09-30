from .base import Connector, ConnectorError, SourceRow
from .files import CSVConnector, JSONLConnector, SQLConnector
from .saas import HubSpotConnector, SalesforceConnector, SegmentConnector, ShopifyConnector, StripeConnector

REGISTRY: dict[str, type[Connector]] = {
    c.kind: c for c in (CSVConnector, JSONLConnector, SQLConnector, SalesforceConnector, HubSpotConnector,
                        ShopifyConnector, StripeConnector, SegmentConnector)
}


class MemoryConnector(Connector):
    """Push-only source for programmatic ingestion (Engine.ingest(source_id, rows=[...]))."""
    kind = "memory"
    push_only = True

    def read(self, cursor):
        return iter(())


REGISTRY["memory"] = MemoryConnector

__all__ = ["Connector", "ConnectorError", "SourceRow", "REGISTRY", "MemoryConnector"]
