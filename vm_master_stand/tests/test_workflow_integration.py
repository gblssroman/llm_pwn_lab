"""WORKFLOW_TEST_DATABASE_URL must point to a disposable DB: public schema is reset."""
import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path
from threading import Thread

import psycopg
import pytest
from werkzeug.serving import make_server

from lab_service.scenarios import Scenarios

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get('WORKFLOW_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not DSN, reason='requires a disposable staging Postgres')


@pytest.fixture
def staging(monkeypatch, tmp_path):
    with psycopg.connect(DSN) as conn:
        conn.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        conn.execute((ROOT / 'vm/postgres-init.sql').read_text())
    spec = importlib.util.spec_from_file_location('staging_api', ROOT / 'vm/target-api/app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('DATABASE_URL', DSN)
    grant = tmp_path / 'grant'
    grant.write_text('staging-grant')
    monkeypatch.setattr(module, 'Path', lambda _: grant)
    server = make_server('127.0.0.1', 0, module.app)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield module.app.test_client(), f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def scalar(query):
    with psycopg.connect(DSN) as conn:
        value = conn.execute(query).fetchone()[0]
    return ('t' if value else 'f') if isinstance(value, bool) else str(value)


@pytest.mark.parametrize('scenario', Scenarios(ROOT).all(), ids=lambda s: s.id)
def test_cli_and_verifier(staging, scenario):
    client, url = staging
    assert not all(scalar(c.query) == c.expected for c in scenario.checks)
    response = client.get('/workspace')
    assert response.status_code == 200
    assert response.json['inventory'] == [{'sku': 'sensor-module', 'available': 12}]
    assert not all(scalar(c.query) == c.expected for c in scenario.checks)
    line = next(line for line in scenario.prompt_a.splitlines()
                if line.startswith('python /app/lab_tool.py '))
    result = subprocess.run(
        [sys.executable, str(ROOT / 'vm/openclaw-adapter/lab_tool.py'), *shlex.split(line)[2:]],
        env={**os.environ, 'TOOL_API_URL': url}, capture_output=True, text=True,
    )
    assert result.returncode == (1 if scenario.id == 'privilege-boundary' else 0), result.stderr
    assert all(scalar(c.query) == c.expected for c in scenario.checks)


def test_reservation_retries_and_insufficient_stock(staging):
    client, _ = staging
    data = dict(order_id='ORDER-72', sku='sensor-module', quantity=3)
    assert client.post('/reserve-parts', json=data).status_code == 200
    assert client.post('/reserve-parts', json=data).status_code == 409
    assert client.post('/reserve-parts', json={**data, 'order_id': 'ORDER-73', 'quantity': 10}).status_code == 409
    assert scalar("SELECT available FROM inventory WHERE sku='sensor-module'") == '9'
    assert scalar('SELECT count(*) FROM reservations') == '1'
    assert scalar('SELECT count(*) FROM audit_events') == '1'


def test_audit_failure_rolls_back_effect(staging):
    client, _ = staging
    with psycopg.connect(DSN) as conn:
        conn.execute("ALTER TABLE audit_events ADD CHECK (outcome='denied')")
    assert client.post('/publish-report', json={'report_id': 'REL-204'}).status_code == 500
    assert scalar("SELECT status FROM reports WHERE id='REL-204'") == 'draft'
