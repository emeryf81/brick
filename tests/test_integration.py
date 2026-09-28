"""Smoke tests against a real Home Assistant core (needs pytest-homeassistant-custom-component)."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lego_tracker.const import DOMAIN
from custom_components.lego_tracker.parsers import Parsed

CSV = "Number;Name;Theme;Qty;Paid;Value\n10281-1;Bonsai;Botanicals;1;40;50\n42143;Ferrari;Technic;1;350;400\n"


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(domain=DOMAIN, data={}, options={"discount_threshold": 25, "retailers": ["bol", "amazon_nl"],
                                                          "update_hours": 6, "digest_time": "08:00:00", "min_history_days": 0})
    e.add_to_hass(hass)
    return e


@pytest.fixture(autouse=True)
def no_network():
    fetch = AsyncMock(return_value=(Parsed(price=30.0, title="LEGO Bonsai"), None))
    with patch("custom_components.lego_tracker.client.Fetcher.fetch_offer", fetch), \
         patch("custom_components.lego_tracker.client.Fetcher.discover", AsyncMock(return_value=None)), \
         patch("custom_components.lego_tracker.client.Fetcher._get", AsyncMock(return_value=(404, ""))):
        yield fetch


async def test_setup_services_and_sensors(hass: HomeAssistant, entry, hass_ws_client, no_network):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.lego_price_tracker_tracked_sets").state == "0"

    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "name": "Bonsai", "theme": "Botanicals", "rrp": 49.99}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/x/1/"}, blocking=True)
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.lego_price_tracker_tracked_sets").state == "1"
    st = hass.states.get("sensor.lego_price_tracker_10281_bonsai")
    assert st is not None and float(st.state) == 30.0 and st.attributes["best_retailer"] == "bol.com"
    assert hass.states.get("sensor.lego_price_tracker_sets_with_high_discount").state == "1"  # 30 vs rrp 49.99 = 40%

    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/overview"})
    res = (await ws.receive_json())["result"]
    assert res["sets"][0]["set_number"] == "10281" and res["sets"][0]["high_discount"] and "Botanicals" in res["themes"]
    await ws.send_json({"id": 2, "type": "lego_tracker/set", "set_number": "10281"})
    detail = (await ws.receive_json())["result"]
    assert detail["history"]["bol"][0][1] == 30.0


async def test_import_and_collection(hass: HomeAssistant, entry, hass_ws_client):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    res = await hass.services.async_call(DOMAIN, "import_collection", {"csv_text": CSV, "track_prices": False},
                                         blocking=True, return_response=True)
    assert res["added"] == 2
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.lego_price_tracker_collection_cost").state) == 390.0
    assert float(hass.states.get("sensor.lego_price_tracker_collection_value").state) == 450.0  # imported values
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/collection"})
    msg = (await ws.receive_json())["result"]
    assert msg["summary"]["sets"] == 2


async def test_options_flow_and_reload(hass: HomeAssistant, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] == "form"
    out = await hass.config_entries.options.async_configure(flow["flow_id"], {
        "discount_threshold": 40, "retailers": ["bol"], "update_hours": 12, "digest_time": "09:00:00", "min_history_days": 2})
    assert out["type"] == "create_entry"
    await hass.async_block_till_done()          # reload must not raise on re-registering panel/services
    assert entry.state.value == "loaded"


async def test_config_flow(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "form"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "discount_threshold": 30, "retailers": ["bol", "kruidvat_be"], "update_hours": 6, "digest_time": "08:00:00", "min_history_days": 3})
    assert r["type"] == "create_entry" and r["options"]["discount_threshold"] == 30



async def test_report_price_by_url_and_manual(hass: HomeAssistant, entry):
    from homeassistant.exceptions import ServiceValidationError

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "name": "Bonsai", "rrp": 49.99}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "amazon_nl", "url": "B08XYZ1234"}, blocking=True)
    # userscript style: only a URL (with extras) and a price
    await hass.services.async_call(DOMAIN, "report_price", {"url": "https://www.amazon.nl/LEGO-Bonsai/dp/B08XYZ1234/ref=x?th=1", "price": 35.5}, blocking=True)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.lego_price_tracker_10281_bonsai").state) == 35.5
    # manual entry from the panel: set + retailer, offer created on the fly
    await hass.services.async_call(DOMAIN, "report_price", {"set_number": "10281", "retailer": "bol", "price": 33.0}, blocking=True)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.lego_price_tracker_10281_bonsai").state) == 33.0
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "report_price", {"url": "https://www.bol.com/nl/nl/p/unknown/1/", "price": 10}, blocking=True)


async def test_bulk_target_notify_export_and_backup(hass: HomeAssistant, entry, no_network):
    from homeassistant.exceptions import ServiceValidationError
    from pytest_homeassistant_custom_component.common import async_mock_service

    hass.config_entries.async_update_entry(entry, options={**entry.options, "notify_service": "notify.phone"})
    notes = async_mock_service(hass, "notify", "phone")
    events = []
    hass.bus.async_listen("lego_tracker_target_price_reached", lambda e: events.append(e))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    res = await hass.services.async_call(DOMAIN, "add_sets", {"set_numbers": "10281, 10311\n42143 10281", "owned": True},
                                         blocking=True, return_response=True)
    assert res == {"added": 3}
    assert hass.states.get("sensor.lego_price_tracker_tracked_sets").state == "3"

    await hass.services.async_call(DOMAIN, "update_set" if False else "add_set",
                                   {"set_number": "10311", "target_price": 35.0, "name": "Orchid"}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10311", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/o/1/"}, blocking=True)
    await hass.services.async_call(DOMAIN, "refresh", {"set_number": "10311"}, blocking=True)   # price 30 <= target 35
    await hass.async_block_till_done()
    assert len(events) == 1 and events[0].data["set_number"] == "10311"
    assert notes and "streefprijs" in notes[0].data["message"]
    assert hass.states.get("sensor.lego_price_tracker_sets_at_target_price").state == "1"

    csv_res = await hass.services.async_call(DOMAIN, "export_collection", {}, blocking=True, return_response=True)
    assert "10281" in csv_res["csv"] and csv_res["csv"].startswith("Number,")

    backup = await hass.services.async_call(DOMAIN, "export_data", {}, blocking=True, return_response=True)
    await hass.services.async_call(DOMAIN, "remove_set", {"set_number": "10281"}, blocking=True)
    assert hass.states.get("sensor.lego_price_tracker_tracked_sets").state == "2"
    out = await hass.services.async_call(DOMAIN, "import_data", {"data": backup, "merge": False}, blocking=True, return_response=True)
    assert out["sets"] == 3
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "import_data", {"data": {"bogus": 1}}, blocking=True, return_response=True)


async def test_diagnostics_and_health_sensor(hass: HomeAssistant, entry, no_network):
    from custom_components.lego_tracker.diagnostics import async_get_config_entry_diagnostics

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    no_network.return_value = (None, "blocked (HTTP 403)")
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281"}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/x/1/"}, blocking=True)
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.lego_price_tracker_offers_with_errors").state == "1"
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["per_retailer"]["bol"]["errors"] == 1 and "blocked (HTTP 403)" in diag["errors"]
    assert diag["options"]["notify_service"] == "**REDACTED**" if "notify_service" in entry.options else True
