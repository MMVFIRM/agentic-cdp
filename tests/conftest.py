import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acdp.agents import DeterministicAgent  # noqa: E402
from acdp.config import ResolutionPolicy, Settings  # noqa: E402
from acdp.connectors import SourceRow  # noqa: E402
from acdp.engine import Engine  # noqa: E402
from acdp.models import make_session_factory  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{tmp_path / 't.db'}", pii_hmac_key="test-key",
                    policy=ResolutionPolicy())


@pytest.fixture
def engine(settings):
    return Engine(make_session_factory(settings.database_url), settings, agent=DeterministicAgent())


def sf_row(key, first, last, email=None, phone=None, dob=None, street=None, zip_=None, optout=None,
           ts="2026-01-01T00:00:00.000+0000", suffix=None):
    return SourceRow(key, {"Id": key, "FirstName": first, "LastName": last, "Email": email, "Phone": phone,
                           "Birthdate": dob, "MailingStreet": street, "MailingPostalCode": zip_,
                           "HasOptedOutOfEmail": optout, "SystemModstamp": ts, "Suffix": suffix}, ts)


def shop_row(key, first, last, email=None, phone=None, zip_=None, street=None, consent=None,
             ts="2026-02-01T00:00:00Z"):
    return SourceRow(key, {"id": key, "email": email, "first_name": first, "last_name": last, "phone": phone,
                           "updated_at": ts,
                           "email_marketing_consent": {"state": consent} if consent else None,
                           "default_address": {"address1": street, "zip": zip_}}, ts)
