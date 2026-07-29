import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from fastapi.testclient import TestClient

from core.models import Base
from core.main import app


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def test_client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides["get_db"] = override_get_db
    return TestClient(app)
