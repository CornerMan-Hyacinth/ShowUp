import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["GATEWAY_FAILURE_RATE"] = "0"
os.environ["GATEWAY_DELAY"] = "0.05"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.gateway import gateway  # noqa: E402
from app.main import app  # noqa: E402
from seed import seed  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed(db)
    db.close()
    gateway.failure_rate = 0
    gateway.delay = 0.05
    return TestClient(app)


@pytest.fixture()
def db():
    s = SessionLocal()
    yield s
    s.close()
