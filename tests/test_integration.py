"""Smoke tests against a real Home Assistant core (needs pytest-homeassistant-custom-component)."""
import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lego_tracker.const import DOMAIN
from custom_components.lego_tracker.parsers import Parsed

CSV = "Number;Name;Theme;Qty;Paid;Value\n10281-1;Bonsai;Botanicals;1;40;50\n42143;Ferrari;Technic;1;350;400\n"


@pytest.fixture
def entry(hass):
    e = MockConfigEntry(domain=DOMAIN, data={}, options={"discount_threshold": 25, "retailers": ["bol", "amazon_nl"],
                                                          "digest_time": "08:00:00", "min_history_days": 0})
    e.add_to_hass(hass)
    return e


@pytest.fixture(autouse=True)
def no_network():
    fetch = AsyncMock(return_value=(Parsed(price=30.0, title="LEGO Bonsai"), None))
    with patch("custom_components.lego_tracker.client.Fetcher.fetch_offer", fetch), \
         patch("custom_components.lego_tracker.client.Fetcher.discover", AsyncMock(return_value=None)), \
         patch("custom_components.lego_tracker.client.Fetcher._get", AsyncMock(return_value=(404, ""))), \
         patch("custom_components.lego_tracker.coordinator.lookup_metadata", AsyncMock(return_value=({}, None))):
        yield fetch


async def test_setup_services_and_sensors(hass: HomeAssistant, entry, hass_ws_client, no_network):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.lego_price_tracker_tracked_sets").state == "0"

    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "name": "Bonsai", "theme": "Botanicals", "rrp": 49.99}, blocking=True)
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/x/1/"}, blocking=True)
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)

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
        "discount_threshold": 40, "retailers": ["bol"], "digest_time": "09:00:00", "min_history_days": 2,
        "auto_refresh": True, "refresh_times": "8:00, 20.15"})
    assert out["type"] == "create_entry"
    await hass.async_block_till_done()          # reload must not raise on re-registering panel/services
    assert entry.state.value == "loaded"


async def test_config_flow(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "form"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "discount_threshold": 30, "retailers": ["bol", "kruidvat_be"], "digest_time": "08:00:00", "min_history_days": 3,
        "auto_refresh": True, "refresh_times": "nooit"})
    assert r["type"] == "form" and r["errors"] == {"refresh_times": "invalid_times"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "discount_threshold": 30, "retailers": ["bol", "kruidvat_be"], "digest_time": "08:00:00", "min_history_days": 3,
        "auto_refresh": True, "refresh_times": "19:30, 7:30"})
    assert r["type"] == "create_entry" and r["options"]["discount_threshold"] == 30
    assert r["options"]["refresh_times"] == "07:30, 19:30"



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
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.lego_price_tracker_offers_with_errors").state == "1"
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["per_retailer"]["bol"]["errors"] == 1 and "blocked (HTTP 403)" in diag["errors"]
    assert diag["options"]["notify_service"] == "**REDACTED**" if "notify_service" in entry.options else True


async def test_import_preview_ws_and_validated_import(hass: HomeAssistant, entry, hass_ws_client):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    csv = "Number;Name;Qty;Paid\n10281;Bonsai;1;40\nxx;bad;1;1\n42143;Ferrari;0;300\n"
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/import_preview", "csv_text": csv})
    prev = (await ws.receive_json())["result"]
    assert prev["summary"]["ok"] == 1 and prev["summary"]["error"] == 2
    assert hass.states.get("sensor.lego_price_tracker_tracked_sets").state == "0"      # preview wrote nothing
    res = await hass.services.async_call(DOMAIN, "import_collection", {"csv_text": csv, "track_prices": False},
                                         blocking=True, return_response=True)
    assert res["added"] == 1 and res["skipped"] == 2


async def test_update_set_validation_and_overview_extras(hass: HomeAssistant, entry, hass_ws_client, no_network):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "10281", "name": "Bonsai", "rrp": 49.99}, blocking=True)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/update_set", "set_number": "10281", "fields": {"paid": -3}})
    assert (await ws.receive_json())["error"]["code"] == "invalid_format"
    await ws.send_json({"id": 2, "type": "lego_tracker/update_set", "set_number": "10281",
                        "fields": {"added": "2999-01-01", "owned": True}})
    assert "toekomst" in (await ws.receive_json())["error"]["message"]
    await ws.send_json({"id": 3, "type": "lego_tracker/update_set", "set_number": "10281",
                        "fields": {"owned": True, "qty": "2", "condition": "Sealed", "priority": 3, "retiring": True}})
    card = (await ws.receive_json())["result"]
    assert card["collection"]["qty"] == 2 and card["priority"] == 3 and card["retiring_soon"]
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "10281", "retailer": "bol", "url": "https://www.bol.com/nl/nl/p/x/1/"}, blocking=True)
    no_network.return_value = (Parsed(price=3.0), None)                             # parse error: accessory price
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    await ws.send_json({"id": 4, "type": "lego_tracker/overview"})
    ov = (await ws.receive_json())["result"]
    assert "verdachte prijs" in ov["sets"][0]["offers"]["bol"]["error"] and ov["sets"][0]["best_price"] is None
    assert ov["retailer_stats"]["bol"]["errors"] == 1 and "analytics" in ov and isinstance(ov["events"], list)
    assert hass.states.get("sensor.lego_price_tracker_sets_retiring_soon").state == "1"


# ---------------------------------------------------------------- 0.5.0: jobs, links, enrichment
async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def test_refresh_job_progress_pause_and_cancel(hass: HomeAssistant, entry, hass_ws_client, no_network):
    c = await _setup(hass, entry)
    for n in ("10281", "10311", "42143"):
        await hass.services.async_call(DOMAIN, "add_set", {"set_number": n}, blocking=True)
        for rid, url in (("bol", f"https://www.bol.com/nl/nl/p/lego-{n}/1/"), ("amazon_nl", "B08XYZ1234")):
            await hass.services.async_call(DOMAIN, "set_offer", {"set_number": n, "retailer": rid, "url": url}, blocking=True)
    c.fetcher.blocked_until["amazon_nl"] = time.time() + 3600          # amazon paused
    res = await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True, return_response=True)
    assert res["started"] and res["total"] == 3 and "Amazon.nl" in res["note"]
    with pytest.raises(ServiceValidationError, match="Er loopt al een taak"):
        await hass.services.async_call(DOMAIN, "discover_offers", {}, blocking=True, return_response=True)
    await hass.async_block_till_done(wait_background_tasks=True)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/job"})
    info = (await ws.receive_json())["result"]
    assert info["job"]["running"] is False and info["last"]["done"] == 3 and info["last"]["updated"] == 3
    assert info["paused"] == {"Amazon.nl": 1.0} and info["schedule"]["auto"] is True
    assert no_network.await_count == 3                                  # amazon was skipped, not fetched
    assert "amazon_nl" not in {r for r in c.store["offers"]["10281"] if c.store["offers"]["10281"][r].get("error")}
    # force ignores the pause
    res = await hass.services.async_call(DOMAIN, "refresh", {"force": True}, blocking=True, return_response=True)
    assert res["note"] is None
    c.cancel_job()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert c.last_job["cancelled"] is True


async def test_link_check_confirm_remove_and_block(hass: HomeAssistant, entry, hass_ws_client, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "21028", "rrp": 49.99}, blocking=True)
    c.store["offers"]["21028"]["amazon_nl"] = {"url": "https://www.amazon.nl/dp/B0LEDLEDLE", "history": []}
    c.store["sets"]["21028"]["name"] = "Led-verlichting voor Lego 21028 Architecture New York"   # legacy shop name
    res = await hass.services.async_call(DOMAIN, "verify_links", {}, blocking=True, return_response=True)
    assert res["suspect"] == 1
    offer = c.store["offers"]["21028"]["amazon_nl"]
    assert offer["link_status"] == "suspect" and "verkeerd product" in offer["link_reason"]
    no_network.return_value = (Parsed(price=19.99, title="Led-verlichting voor LEGO 21028"), None)
    await hass.services.async_call(DOMAIN, "refresh", {"set_number": "21028"}, blocking=True, return_response=True)
    assert c.compute()["statuses"]["21028"]["best_price"] is None        # suspect link never counts
    # confirm overrides, remove blocks rediscovery of the same page
    await hass.services.async_call(DOMAIN, "confirm_offer", {"set_number": "21028", "retailer": "amazon_nl"}, blocking=True)
    assert c.store["offers"]["21028"]["amazon_nl"]["link_status"] == "confirmed"
    await hass.services.async_call(DOMAIN, "remove_offer", {"set_number": "21028", "retailer": "amazon_nl"}, blocking=True)
    assert "amazon_nl" not in c.store["offers"]["21028"] and "name" not in c.store["sets"]["21028"]
    with patch("custom_components.lego_tracker.client.Fetcher.discover",
               AsyncMock(side_effect=lambda r, n: "https://www.amazon.nl/dp/B0LEDLEDLE" if r == "amazon_nl" else None)):
        found = await hass.services.async_call(DOMAIN, "discover_offers", {"set_number": "21028"}, blocking=True, return_response=True)
    assert found["found"] == 0                                             # rejected page is not re-added
    # manual link is trusted immediately
    await hass.services.async_call(DOMAIN, "set_offer", {"set_number": "21028", "retailer": "amazon_nl", "url": "B0GOODGOOD"}, blocking=True)
    assert c.store["offers"]["21028"]["amazon_nl"]["link_status"] == "confirmed"
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "lego_tracker/set", "set_number": "21028"})
    assert (await ws.receive_json())["result"]["offers"]["amazon_nl"]["link_status"] == "confirmed"


async def test_enrich_replaces_shop_names_only(hass: HomeAssistant, entry, no_network):
    c = await _setup(hass, entry)
    await hass.services.async_call(DOMAIN, "import_collection", {"csv_text": "Number;Name\n10311;Orchid\n", "track_prices": False},
                                   blocking=True, return_response=True)
    await hass.services.async_call(DOMAIN, "add_set", {"set_number": "358"}, blocking=True)
    c.store["sets"]["358"]["name"] = "LEGO Speed Champions BMW M3 (E30) 77263"       # legacy wrong shop title
    meta = {"name": "Official name", "theme": "Icons", "year": 2022, "pieces": 608, "image": "https://img/x.jpg"}
    with patch("custom_components.lego_tracker.coordinator.lookup_metadata", AsyncMock(return_value=(meta, "Rebrickable"))), \
         patch("custom_components.lego_tracker.coordinator.asyncio.sleep", AsyncMock()):
        res = await hass.services.async_call(DOMAIN, "enrich_sets", {}, blocking=True, return_response=True)
        assert res["started"] and res["total"] == 2
        await hass.async_block_till_done(wait_background_tasks=True)
    assert c.store["sets"]["358"]["name"] == "Official name" and c.store["sets"]["358"]["name_source"] == "Rebrickable"
    assert c.store["sets"]["10311"]["name"] == "Orchid"                              # imported name kept
    assert c.store["sets"]["10311"]["year"] == 2022 and c.last_job["updated"] == 2


async def test_schedule_times(hass: HomeAssistant, entry):
    hass.config_entries.async_update_entry(entry, options={**entry.options, "refresh_times": "19:30, 07:05"})
    c = await _setup(hass, entry)
    assert c.refresh_times == [(7, 5), (19, 30)] and c.schedule_info()["times"] == ["07:05", "19:30"]
    assert c.next_refresh() > time.time()
    hass.config_entries.async_update_entry(entry, options={**entry.options, "auto_refresh": False})
    await hass.async_block_till_done()
    c = hass.data[DOMAIN][entry.entry_id]
    assert c.schedule_info()["next"] is None
