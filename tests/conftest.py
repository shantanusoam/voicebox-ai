from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from callbox.config import Config
from callbox.db import Database
from callbox.main import create_app

@pytest.fixture
def config(tmp_path):
    return Config(tmp_path, 'test-administrator-token-00000000000000000000', demo=True)

@pytest.fixture
def db():
    db=Database(':memory:', seed=False)
    yield db
    db.close()

@pytest.fixture
def app(config,db):
    return create_app(config,db)

@pytest.fixture
def client(app):
    with TestClient(app) as client:
        assert client.post('/api/auth/demo').status_code==200
        yield client

@pytest.fixture
def make_call(client):
    def create(**kwargs):
        response=client.post('/api/calls',json={'label':'Fictional caller',**kwargs})
        assert response.status_code==201,response.text
        return response.json()
    return create
