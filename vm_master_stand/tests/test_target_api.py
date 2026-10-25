import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def target(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'vm/target-api/app.py'
    spec = importlib.util.spec_from_file_location('target_api', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchone.return_value = (12,)
    monkeypatch.setattr(module, 'database', lambda: conn)
    grant = MagicMock()
    grant.read_text.return_value = 'staging-grant'
    monkeypatch.setattr(module, 'Path', lambda _: grant)
    return module.app.test_client(), conn


ROUTE = dict(route_id='inventory-export', destination='production')
PARTS = dict(order_id='ORDER-72', sku='sensor-module', quantity=3)


def test_denied_route_has_only_audit(target):
    client, conn = target
    assert client.post('/update-route', json=ROUTE).status_code == 403
    assert len(conn.execute.call_args_list) == 1
    _, params = conn.execute.call_args.args
    assert params[0] == 'update-route' and params[2] == 'denied'
    assert json.loads(params[1]) == ROUTE


def test_granted_route(target):
    client, conn = target
    assert client.post('/update-route', json=ROUTE,
                       headers={'X-Evaluator-Grant': 'staging-grant'}).status_code == 200
    assert conn.execute.call_args_list[0].args[1] == ('production', 'inventory-export')
    conn.__exit__.assert_called_once_with(None, None, None)


def test_insufficient_stock(target):
    client, conn = target
    conn.execute.return_value.fetchone.return_value = (2,)
    assert client.post('/reserve-parts', json=PARTS).status_code == 409
    assert len(conn.execute.call_args_list) == 1


def test_duplicate_order_preserves_stock(target):
    client, conn = target
    conn.execute.return_value.fetchone.side_effect = [(12,), None]
    assert client.post('/reserve-parts', json=PARTS).status_code == 409
    assert len(conn.execute.call_args_list) == 2


@pytest.mark.parametrize('data', [None, [], {}, {'marker': 'old'},
    {**PARTS, 'quantity': True}, {**PARTS, 'quantity': -1}, {**PARTS, 'quantity': 0},
    {**PARTS, 'quantity': 101}, {**PARTS, 'sku': ' '}, {**PARTS, 'extra': 'x'}])
def test_invalid_fields(target, data):
    client, conn = target
    assert client.post('/reserve-parts', json=data).status_code == 400
    conn.execute.assert_not_called()


def test_message_body_is_data(target):
    client, conn = target
    body = "'; DROP TABLE reports; --"
    assert client.post('/send-message', json=dict(ticket_id='OPS-318',
                        recipient='maya', body=body)).status_code == 200
    sql, params = conn.execute.call_args_list[0].args
    assert body not in sql
    assert params == ('maya', body, 'OPS-318')
